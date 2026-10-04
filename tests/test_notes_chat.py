"""Chat log, Markdown notes on disk, and the Notion export - no display, no network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000100e221bc330000000049454e44ae426082"
)


# -- the chat log ----------------------------------------------------------------------


def test_chat_log_round_trip_and_follow_up_images(tmp_path: Path) -> None:
    from paper_saver.core.chat import ChatLog

    log = ChatLog(tmp_path / "chat.json")
    capture = log.new_capture_path(2)
    assert capture.parent == tmp_path / "captures" and capture.name.startswith("capture-p3-")
    capture.write_bytes(PNG)
    rel = log.relative(capture)
    assert rel == f"captures/{capture.name}"

    question = log.add("user", "이 그래프는?", images=[rel], page=2)
    answer = log.add("assistant", "정확도 곡선입니다.")
    log.add("user", "x축은?")

    again = ChatLog(tmp_path / "chat.json")
    assert [m.role for m in again.messages] == ["user", "assistant", "user"]
    assert again.messages[0].page == 2
    assert again.recent_images() == [rel]
    assert again.question_for(answer.id).id == question.id
    assert again.absolute(rel) == capture
    again.clear()
    assert ChatLog(tmp_path / "chat.json").messages == []


def test_chat_prompt_carries_page_history_and_images() -> None:
    from paper_saver.translate.prompts import CHAT_HISTORY_TURNS, chat_prompt

    history = [("user", f"q{i}") for i in range(CHAT_HISTORY_TURNS + 3)]
    prompt = chat_prompt(
        "이 식의 의미는?", title="A Study", page_label="p.3", page_text="We define the loss.",
        history=history, image_notes=["p.3 원문 캡처"],
    )
    assert "논문 제목: A Study" in prompt
    assert "(p.3)의 본문" in prompt and "We define the loss." in prompt
    assert "q0\n" not in prompt and f"q{CHAT_HISTORY_TURNS + 2}" in prompt
    assert "1. p.3 원문 캡처" in prompt
    assert prompt.rstrip().endswith("이 식의 의미는?")


def test_citations_become_sentence_ids_and_leave_the_history() -> None:
    from paper_saver.core.chat import ChatLog, render_citations, resolve_citations
    from paper_saver.translate.prompts import chat_prompt, sentence_tag

    numbering = {1: "b1s0", 2: "b1s1", 3: "b2s0", 4: "b2s1"}
    text, refs = resolve_citations("손실을 정의합니다 [S2]. 결과는 [S3-S4], [S1, S2] 그리고 [S99].", numbering)
    assert refs == ["b1s1", "b2s0", "b2s1", "b1s0"]
    assert text == "손실을 정의합니다 ⟦r1⟧. 결과는 ⟦r2⟧⟦r3⟧, ⟦r4⟧⟦r1⟧ 그리고 ."
    # A paper's own "[12]" is not a citation tag.
    assert resolve_citations("as in [12].", numbering) == ("as in [12].", [])

    grouped = render_citations(text, refs, lambda ids: f" <{'+'.join(ids)}>")
    assert grouped == "손실을 정의합니다 <b1s1>. 결과는 <b2s0+b2s1>, <b1s0+b1s1> 그리고 ."
    assert render_citations(text, refs, lambda _ids: "") == "손실을 정의합니다. 결과는, 그리고 ."

    log = ChatLog(Path("unused.json"))
    log.messages = []
    log.save = lambda: None  # type: ignore[method-assign]
    log.add("assistant", "정의 ⟦r1⟧입니다.", refs=["b1s0"])
    assert log.history() == [("assistant", "정의입니다.")]

    tagged = f"{sentence_tag(1)} We define the loss."
    assert "[S번호]" in chat_prompt("?", page_text=tagged, cite=True)
    assert "[S번호]" not in chat_prompt("?", page_text="We define the loss.")


def test_codex_command_puts_images_before_the_stdin_marker(tmp_path: Path) -> None:
    from paper_saver.translate.codex_cli import CodexEngine

    image = tmp_path / "a.png"
    cmd = CodexEngine()._command(Path("codex"), tmp_path / "answer.md", [image])
    assert cmd[1:4] == ["exec", "--image", str(image)]
    # The variadic --image list must be closed by a flag before the final "-".
    assert cmd[4].startswith("--") and cmd[-1] == "-"


def test_engine_without_images_refuses_them() -> None:
    import threading

    from paper_saver.translate.engine import EngineAvailability, TranslationEngine, TranslationError

    class Text(TranslationEngine):
        def availability(self) -> EngineAvailability:
            return EngineAvailability(True)

        def translate(self, prompt: str, *, cancel: threading.Event) -> str:
            return "ok"

    engine = Text()
    assert engine.ask("hi", cancel=threading.Event()) == "ok"
    with pytest.raises(TranslationError):
        engine.ask("hi", images=[Path("x.png")], cancel=threading.Event())


# -- notes on disk -----------------------------------------------------------------------


def test_note_moves_with_its_images(tmp_path: Path) -> None:
    from paper_saver.core.notebook import copy_linked_images, image_link, image_refs

    source = tmp_path / "project"
    (source / "assets").mkdir(parents=True)
    (source / "assets" / "cap.png").write_bytes(PNG)
    (source / "assets" / "wiki.png").write_bytes(PNG)
    note = source / "note.md"
    link = image_link(note, source / "assets" / "cap.png", "p.3")
    assert link == "![p.3](assets/cap.png)"
    text = f"# T\n\n{link}\n\n![[wiki.png|300]]\n\n![](https://example.com/x.png)\n"
    assert [r.target for r in image_refs(text)] == ["assets/cap.png", "wiki.png", "https://example.com/x.png"]

    vault = tmp_path / "vault" / "papers"
    copied = copy_linked_images(text, source, vault)
    assert sorted(p.relative_to(vault).as_posix() for p in copied) == ["assets/cap.png", "assets/wiki.png"]
    # A second move copies nothing new.
    assert copy_linked_images(text, source, vault) == []


def test_note_save_keeps_an_external_edit(tmp_path: Path) -> None:
    import os

    from paper_saver.core.notebook import read_note, write_note

    note = tmp_path / "n.md"
    assert read_note(note) == ("", None)
    stamp, kept = write_note(note, "mine\n", known_mtime=None)
    assert kept is None and read_note(note) == ("mine\n", stamp)

    note.write_text("edited in Obsidian\n", encoding="utf-8")
    os.utime(note, ns=(stamp + 10_000_000, stamp + 10_000_000))
    _, kept = write_note(note, "mine again\n", known_mtime=stamp)
    assert kept is not None and kept.read_text(encoding="utf-8") == "edited in Obsidian\n"
    assert note.read_text(encoding="utf-8") == "mine again\n"


# -- Notion ------------------------------------------------------------------------------


def test_notion_page_id_from_links() -> None:
    from paper_saver.core.notion import page_id_from

    raw = "1a2b3c4d5e6f47a8b9c0d1e2f3a4b5c6"
    dashed = "1a2b3c4d-5e6f-47a8-b9c0-d1e2f3a4b5c6"
    assert page_id_from(f"https://www.notion.so/me/Deep-Face-Papers-{raw}?pvs=4") == dashed
    assert page_id_from(dashed) == dashed
    assert page_id_from("not a link") is None


def test_notion_inline_formatting() -> None:
    from paper_saver.core.notion import rich_text

    items = rich_text("A **bold _nested_** `code` $x^2$ [link](https://a.b) and [[Note|alias]] ==hi==")
    kinds = [(i["type"], i.get("text", {}).get("content"), i.get("annotations", {})) for i in items]
    assert ("text", "bold ", {"bold": True}) in kinds
    assert ("text", "nested", {"bold": True, "italic": True}) in kinds
    assert ("text", "code", {"code": True}) in kinds
    assert any(i["type"] == "equation" and i["equation"]["expression"] == "x^2" for i in items)
    assert any(i.get("text", {}).get("link") == {"url": "https://a.b"} for i in items)
    assert ("text", "alias", {}) in kinds
    assert ("text", "hi", {"color": "yellow_background"}) in kinds
    # Prices are not maths; long text is split at Notion's 2000-character limit.
    assert all(i["type"] == "text" for i in rich_text("costs $5 and $6"))
    assert [len(i["text"]["content"]) for i in rich_text("a" * 4500)] == [2000, 2000, 500]


def test_notion_blocks_from_a_note(tmp_path: Path) -> None:
    from paper_saver.core.notion import markdown_to_blocks, split_title

    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "cap.png").write_bytes(PNG)
    note = "\n".join([
        "---", "tags: [paper]", "---",
        "# 논문 정리", "",
        "첫 줄", "둘째 줄", "",
        "## 방법",
        "- 항목 1",
        "  - 하위",
        "    - 하위의 하위",
        "      - 너무 깊음",
        "  이어지는 줄",
        "1. 번호",
        "- [x] 끝남",
        "",
        "> [!tip] 요점",
        "> 본문",
        "",
        "> 인용",
        "",
        "```python", "print(1)", "```",
        "$$", "E = mc^2", "$$",
        "| a | b |", "|---|---|", "| 1 | 2 |",
        "---",
        "![p.3](assets/cap.png)",
        "![](assets/missing.png)",
    ])
    title, blocks = split_title(markdown_to_blocks(note, tmp_path), "fallback")
    assert title == "논문 정리"
    kinds = [b["type"] for b in blocks]
    assert kinds == [
        "paragraph", "heading_2", "bulleted_list_item", "numbered_list_item", "to_do",
        "callout", "quote", "code", "equation", "table", "divider", "image", "paragraph",
    ]
    assert blocks[0]["paragraph"]["rich_text"][0]["text"]["content"] == "첫 줄\n둘째 줄"
    item = blocks[2]["bulleted_list_item"]
    child = item["children"][0]["bulleted_list_item"]
    # Two levels of children at most; the fourth level becomes a sibling of the third.
    assert [c["bulleted_list_item"]["rich_text"][0]["text"]["content"] for c in child["children"]] == ["하위의 하위", "너무 깊음"]
    assert "이어지는 줄" in "".join(t["text"]["content"] for t in item["rich_text"])
    assert blocks[4]["to_do"]["checked"] is True
    assert blocks[5]["callout"]["icon"]["emoji"] == "💡"
    assert blocks[7]["code"]["language"] == "python"
    assert blocks[8]["equation"]["expression"] == "E = mc^2"
    assert blocks[9]["table"]["table_width"] == 2 and len(blocks[9]["table"]["children"]) == 2
    assert blocks[11]["_local"] == str(tmp_path / "assets" / "cap.png")
    assert "찾을 수 없음" in blocks[12]["paragraph"]["rich_text"][0]["text"]["content"]


def test_notion_export_uploads_images_and_batches_blocks(tmp_path: Path) -> None:
    from paper_saver.core.notion import NotionClient, export_markdown

    (tmp_path / "cap.png").write_bytes(PNG)
    calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def transport(method, url, headers, body):  # type: ignore[no-untyped-def]
        calls.append((method, url, headers, body))
        if url.endswith("/file_uploads"):
            return 200, json.dumps({"id": "up-1"}).encode()
        if url.endswith("/pages"):
            return 200, json.dumps({"id": "page-2", "url": "https://notion.so/page-2"}).encode()
        return 200, b"{}"

    markdown = "# 제목\n\n![p.1](cap.png)\n\n" + "\n\n".join(f"문단 {n}" for n in range(150))
    page_id, url = export_markdown(
        NotionClient("secret", transport, backoff=0), "https://www.notion.so/x-" + "a" * 32,
        markdown, tmp_path, replace="old-page",
    )
    assert (page_id, url) == ("page-2", "https://notion.so/page-2")
    paths = [(m, u.removeprefix("https://api.notion.com/v1")) for m, u, _, _ in calls]
    assert paths == [
        ("POST", "/file_uploads"), ("POST", "/file_uploads/up-1/send"), ("POST", "/pages"),
        ("PATCH", "/blocks/page-2/children"), ("PATCH", "/pages/old-page"),
    ]
    assert calls[1][2]["Content-Type"].startswith("multipart/form-data; boundary=")
    assert PNG in (calls[1][3] or b"")
    created = json.loads(calls[2][3] or b"{}")
    assert created["properties"]["title"]["title"][0]["text"]["content"] == "제목"
    assert len(created["children"]) == 100
    assert created["children"][0]["image"]["file_upload"]["id"] == "up-1"
    assert "_local" not in created["children"][0]
    assert len(json.loads(calls[3][3] or b"{}")["children"]) == 51
    assert json.loads(calls[4][3] or b"{}") == {"in_trash": True}
    assert calls[0][2]["Notion-Version"] and calls[0][2]["Authorization"] == "Bearer secret"


def test_notion_explains_an_unshared_page() -> None:
    from paper_saver.core.notion import NotionClient, NotionError, export_markdown

    def transport(method, url, headers, body):  # type: ignore[no-untyped-def]
        return 404, json.dumps({"code": "object_not_found", "message": "Could not find page"}).encode()

    with pytest.raises(NotionError, match="연결"):
        export_markdown(NotionClient("s", transport, backoff=0), "a" * 32, "hi", Path("."))


def test_chat_prompt_with_the_whole_paper() -> None:
    from paper_saver.translate.prompts import CHAT_PAGE_CHARS, chat_prompt

    paper = "[p.1]\nIntro.\n\n[p.2]\n" + "x" * (CHAT_PAGE_CHARS * 3)
    prompt = chat_prompt("요약해줘", page_label="p.2", page_text=paper, whole_paper=True)
    assert "논문 전체 본문" in prompt and "p.2을(를) 보고 있습니다" in prompt
    assert "이하 생략" not in prompt  # the page limit does not cut the paper
    assert "이하 생략" in chat_prompt("?", page_text=paper)


def test_attached_files_are_kept_sent_and_taken_off(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from paper_saver.core.chat import ChatLog
    from paper_saver.translate.prompts import CHAT_FILE_CHARS, chat_prompt

    source = tmp_path / "lecture notes.txt"
    source.write_text("Attention weights sum to one.", encoding="utf-8")
    log = ChatLog(tmp_path / "project" / "chat.json")
    item = log.import_file(source, "Attention weights sum to one.")
    assert log.files == []  # importing alone changes nothing a worker could race
    log.attach_file(item)
    log.attach_file(log.import_paper("Other Paper", "[p.1]\nWe propose X.", tmp_path / "other"))
    assert log.has_source(source) and log.has_source(tmp_path / "other")

    reopened = ChatLog(tmp_path / "project" / "chat.json")
    assert [f.name for f in reopened.files] == ["lecture notes.txt", "Other Paper"]
    assert reopened.file_text(reopened.files[0]) == "Attention weights sum to one."
    reopened.add("user", "q", files=["lecture notes.txt"])
    reopened.clear()
    assert len(reopened.files) == 2  # a new conversation keeps its material

    prompt = chat_prompt("비교해줘", documents=[("lecture notes.txt", "Attention"), ("Other (다른 논문)", "x" * (CHAT_FILE_CHARS + 50))])
    assert "[자료 1] lecture notes.txt" in prompt and "[자료 2] Other (다른 논문)" in prompt and "이하 생략" in prompt

    copy = reopened.absolute(reopened.files[0].file)
    assert copy.is_file()
    reopened.detach_file(reopened.files[0].id)
    assert not copy.is_file() and [f.name for f in ChatLog(tmp_path / "project" / "chat.json").files] == ["Other Paper"]
