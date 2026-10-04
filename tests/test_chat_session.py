"""Questions run in one Codex session: text is sent once, follow-ups carry only
what is new, and "모든 탭" sends matching passages rather than whole papers."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from paper_saver.core.chat_context import ContextBuilder, PaperSource, SessionState, rank, tokens
from paper_saver.core.geometry import Rect
from paper_saver.core.models import Block, BlockKind, PageLayout, PaperLayout, Sentence


def _block(page: int, index: int, kind: str, sentences: list[str]) -> Block:
    text = " ".join(sentences)
    words = text.split()
    parts, start = [], 0
    for number, sentence in enumerate(sentences):
        count = len(sentence.split())
        parts.append(Sentence(id=f"p{page}b{index}s{number}", first=start, last=start + count, text=sentence))
        start += count
    from paper_saver.core.models import Word

    return Block(
        id=f"p{page}b{index}", page=page, kind=kind, rect=Rect(0, 0, 100, 10),
        words=[Word(text=w, rects=[Rect(0, 0, 1, 1)]) for w in words], sentences=parts, text=text,
        font_size=10, leading=12,
    )


def _paper() -> PaperLayout:
    first = PageLayout(number=0, width=600, height=800, blocks=[
        _block(0, 0, BlockKind.BODY, ["We study retrieval of thinking traces.", "It helps reasoning."]),
        _block(0, 1, BlockKind.HEADING, ["Method"]),
        _block(0, 2, BlockKind.BODY, ["The encoder maps each query to a vector.", "Vectors are compared by cosine."]),
    ])
    second = PageLayout(number=1, width=600, height=800, blocks=[
        _block(1, 0, BlockKind.HEADING, ["Results"]),
        _block(1, 1, BlockKind.BODY, ["Accuracy rises by eight points on AIME."]),
    ])
    return PaperLayout(pages=[first, second])


def test_korean_is_matched_by_character_pairs_and_translations() -> None:
    assert "어텐" in tokens("어텐션은 뭐야?") and "attention" in tokens("Attention heads")
    korean = {"p0b2s0": "인코더는 각 질의를 벡터로 바꿉니다."}
    paper = PaperSource("", "Paper", _paper(), korean.get)
    builder = ContextBuilder(SessionState())
    parts = builder.related([paper], "인코더가 질의를 어떻게 바꿔?", ["[논문 1] "])
    assert len(parts) == 1 and "encoder maps each query" in parts[0]
    assert "Accuracy rises" not in parts[0]
    scores = rank("cosine", ["Vectors are compared by cosine.", "Nothing here."])
    assert scores[0] > 0 == scores[1]


def test_nothing_is_sent_twice_and_tags_stay_put() -> None:
    state = SessionState()
    paper = PaperSource("", "Paper", _paper())
    builder = ContextBuilder(state)
    page = builder.page(paper, 0)
    assert page and "[S1] We study retrieval" in page
    assert builder.page(paper, 0) is None  # already in the session
    whole = builder.paper(paper)
    assert whole and "[S1] We study retrieval" in whole  # same number for the same sentence
    assert builder.paper(paper) is None and builder.page(paper, 1) is None
    # A new builder over the saved state remembers all of it.
    again = ContextBuilder(SessionState.from_dict(state.to_dict()))
    assert again.paper(paper) is None
    assert again.document("f1", "[자료 1] notes", "text", 100) is not None
    assert again.document("f1", "[자료 1] notes", "text", 100) is None


def test_an_unmatched_paper_sends_its_outline_once() -> None:
    paper = PaperSource("other", "Other", _paper())
    builder = ContextBuilder(SessionState())
    parts = builder.related([paper], "전혀 관계없는 질문", ["[논문 2] "])
    assert "개요" in parts[0] and "## Method" in parts[0] and "(p.2)" in parts[0]
    assert "[S" in parts[0] and "other|" in "".join(builder.state.numbering.values())
    assert builder.related([paper], "전혀 관계없는 질문", ["[논문 2] "]) == []


def test_codex_session_commands_and_cleanup(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paper_saver.translate.codex_cli import CodexEngine, _thread_id, forget_codex_session

    engine = CodexEngine()
    keep = engine._command(Path("codex"), Path("a.md"), keep=True)
    assert "--json" in keep and "--ephemeral" not in keep and "read-only" in keep
    resume = engine._command(Path("codex"), Path("a.md"), [Path("c.png")], resume="0199-abcd-ef01-2345")
    assert resume[1:3] == ["exec", "resume"] and resume[-2:] == ["0199-abcd-ef01-2345", "-"]
    assert "--sandbox" not in resume and "--image" in resume
    assert "--ephemeral" in engine._command(Path("codex"), Path("a.md"))  # translation keeps nothing
    assert _thread_id('noise\n{"type":"thread.started","thread_id":"0199-ab"}\n') == "0199-ab"

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    day = tmp_path / "sessions" / "2026" / "10" / "02"
    day.mkdir(parents=True)
    mine = day / "rollout-2026-10-02T07-22-03-01a0f98f-740f-7881-9b76-13dcb0dda79b.jsonl"
    other = day / "rollout-2026-10-02T07-23-00-01a0f98f-0000-7881-9b76-13dcb0dda79b.jsonl"
    mine.write_text("{}")
    other.write_text("{}")
    assert forget_codex_session("01a0f98f-740f-7881-9b76-13dcb0dda79b") == 1
    assert not mine.exists() and other.exists()
    assert forget_codex_session("../../x") == 0


class _SessionEngine:
    """Remembers sessions like Codex does, and can be told to lose one."""

    key = "fake"
    supports_images = True
    supports_sessions = True

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str]] = []
        self.forgotten: list[str] = []
        self.lose_sessions = False
        self.answer = "답입니다 [S2]."

    def converse(self, prompt, *, session=None, images=(), cancel):  # type: ignore[no-untyped-def]
        from paper_saver.translate.engine import TranslationError

        self.calls.append((session, prompt))
        if session and self.lose_sessions:
            raise TranslationError("session not found")
        return self.answer, session or f"s{len(self.calls)}"

    def forget_session(self, session: str) -> None:
        self.forgotten.append(session)


def _wait(qapp, condition, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while not condition() and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    assert condition()


def test_follow_ups_send_only_what_is_new(qapp, tmp_path: Path, two_column_pdf: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from paper_saver.config import Settings
    from paper_saver.ui.main_window import MainWindow, _prepare

    settings = Settings(tmp_path / "settings.json")
    settings.set("projects_dir", str(tmp_path / "projects"))
    settings.set("auto_translate", False)
    window = MainWindow(settings)
    window.show()
    project, layout = _prepare(two_column_pdf, None, settings.projects_dir)(threading.Event(), lambda _m: None)
    window._opened((project, layout))
    qapp.processEvents()
    reader = window.reader
    engine = _SessionEngine()
    reader._engine_provider = lambda: engine
    panel, log = reader.chat_panel, reader.chat_log

    def ask(text: str, count: int) -> str:
        panel.input.setPlainText(text)
        panel.send()
        _wait(qapp, lambda: len(log.messages) == count)
        return engine.calls[-1][1]

    first = ask("요점은?", 2)
    assert engine.calls[-1][0] is None and "규칙" in first and "독자가 보고 있는 쪽(p.1)" in first
    assert log.session is not None and log.session.id == "s1"
    cited = log.messages[1].refs
    assert cited and reader.session.sentence(cited[0]) is not None

    # Same page: the follow-up is the question alone, in the same session,
    # and [S2] still names the same sentence.
    second = ask("더 자세히?", 4)
    assert engine.calls[-1][0] == "s1" and "규칙" not in second and "본문" not in second
    assert second.rstrip().endswith("더 자세히?")
    assert log.messages[3].refs == cited

    # The whole paper goes once; asked again, nothing more is sent.
    panel.set_scope("paper")
    third = ask("전체 요약은?", 6)
    assert "논문 전체 본문" in third
    fourth = ask("결론만?", 8)
    assert "논문 전체 본문" not in fourth and engine.calls[-1][0] == "s1"

    # A session that cannot be resumed: the question goes again in full, in
    # a new session, and the old one's leftovers are deleted.
    engine.lose_sessions = True
    fifth = ask("다시?", 10)
    assert engine.calls[-1][0] is None and "논문 전체 본문" in fifth and "지금까지의 대화" in fifth
    assert engine.forgotten == ["s1"] and log.session.id == f"s{len(engine.calls)}"
    engine.lose_sessions = False

    # 새 대화 ends the session.
    ended: list[str] = []
    log.on_session_end = lambda state: ended.append(state.id)
    monkeypatch.setattr("paper_saver.ui.chat_panel.QMessageBox.question", lambda *a, **k: __import__(
        "PySide6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox.StandardButton.Yes)
    panel._clear()
    assert log.session is None and ended
    window.close()


def test_taking_off_a_sent_file_ends_the_session(tmp_path: Path) -> None:
    from paper_saver.core.chat import ChatLog

    source = tmp_path / "a.txt"
    source.write_text("x", encoding="utf-8")
    log = ChatLog(tmp_path / "p" / "chat.json")
    sent, unsent = log.import_file(source, "x"), log.import_file(source, "y")
    log.attach_file(sent)
    log.attach_file(unsent)
    log.set_session(SessionState(id="s1", engine="codex", sent=[f"file:{sent.id}"]))
    ended: list[str] = []
    log.on_session_end = lambda state: ended.append(state.id)
    log.detach_file(unsent.id)  # never sent: the session goes on
    assert log.session is not None and not ended
    log.detach_file(sent.id)  # the session holds it: a new one starts
    assert log.session is None and ended == ["s1"]
    assert ChatLog(tmp_path / "p" / "chat.json").session is None


def test_a_refused_prompt_is_named_and_its_session_cleaned(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paper_saver.translate.codex_cli import CodexEngine, _forget_sessions_in
    from paper_saver.translate.engine import EnginePromptRejected

    stdout = (
        '{"type":"thread.started","thread_id":"01a0fa5c-ffe5-7201-b80b-642a9eca4fb4"}\n'
        '{"type":"error","message":"Invalid prompt: your prompt was flagged as potentially violating our '
        'usage policy. Please try again with a different prompt"}\n'
    )
    error = CodexEngine._failure(1, stdout, "")
    assert isinstance(error, EnginePromptRejected) and "거절" in str(error)

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    day = tmp_path / "sessions" / "2026" / "10" / "02"
    day.mkdir(parents=True)
    workspace = Path(r"C:\Temp\paper-saver-translate-abc123")
    ours = day / "rollout-a.jsonl"
    theirs = day / "rollout-b.jsonl"
    import json

    # Written the way Codex writes its first line: the folder JSON-escaped.
    ours.write_text(json.dumps({"payload": {"cwd": str(workspace)}}) + "\n", encoding="utf-8")
    theirs.write_text(json.dumps({"payload": {"cwd": r"C:\Temp\elsewhere"}}) + "\n", encoding="utf-8")
    assert _forget_sessions_in(workspace) == 1 and not ours.exists() and theirs.exists()


class _PickyEngine(_SessionEngine):
    """Refuses any prompt carrying a whole paper - like the DeepSeek-R1 appendix."""

    def __init__(self) -> None:
        super().__init__()
        self.refuse_all = False

    def converse(self, prompt, *, session=None, images=(), cancel):  # type: ignore[no-untyped-def]
        from paper_saver.translate.engine import EnginePromptRejected

        self.calls.append((session, prompt))
        if self.refuse_all or "논문 전체 본문" in prompt:
            raise EnginePromptRejected("거절")
        return self.answer, session or f"s{len(self.calls)}"


def test_a_refused_paper_is_asked_again_with_less(qapp, tmp_path: Path, two_column_pdf: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from paper_saver.config import Settings
    from paper_saver.ui.main_window import MainWindow, _prepare

    settings = Settings(tmp_path / "settings.json")
    settings.set("projects_dir", str(tmp_path / "projects"))
    settings.set("auto_translate", False)
    window = MainWindow(settings)
    window.show()
    project, layout = _prepare(two_column_pdf, None, settings.projects_dir)(threading.Event(), lambda _m: None)
    window._opened((project, layout))
    qapp.processEvents()
    reader = window.reader
    engine = _PickyEngine()
    reader._engine_provider = lambda: engine
    panel, log = reader.chat_panel, reader.chat_log
    ended: list[str] = []
    log.on_session_end = lambda state: ended.append(state.id)

    # "이 논문" refused: one more try, with the page and matching passages.
    panel.set_scope("paper")
    panel.input.setPlainText("Graph neural networks 요약해줘")
    panel.send()
    _wait(qapp, lambda: len(log.messages) == 2)
    assert len(engine.calls) == 2 and "논문 전체 본문" not in engine.calls[1][1]
    assert "독자가 보고 있는 쪽" in engine.calls[1][1] and "거절" in panel.status.text()
    assert log.session is not None and not any(s.startswith("paper:") for s in log.session.sent)

    # A refusal inside the session ends it: the session holds the refused text.
    engine.refuse_all = True
    panel.set_scope("page")
    panel.input.setPlainText("다음은?")
    panel.send()
    _wait(qapp, lambda: not panel.busy)
    assert log.session is None and ended and "거절" in panel.view.toPlainText()
    window.close()


class _RefusingTranslator:
    """Translates everything, except a prompt that carries the grading rubric."""

    key = "fake"
    model = ""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def translate(self, prompt: str, *, cancel: threading.Event) -> str:
        import re

        from paper_saver.translate.engine import EnginePromptRejected

        self.prompts.append(prompt)
        if "rubric" in prompt:
            raise EnginePromptRejected("거절")
        numbers = re.findall(r"^\[(\d+)\]", prompt.split("번역할 문장:")[-1], re.M)
        return "\n".join(f"[{n}] 번역 {n}" for n in numbers)


def test_a_refused_sentence_is_left_in_english_and_not_asked_again(tmp_path: Path) -> None:
    from paper_saver.core.project import TranslationStore
    from paper_saver.translate.runner import pending_batches, translate_paper

    page = PageLayout(number=0, width=600, height=800, blocks=[
        _block(0, 0, BlockKind.BODY, ["One fine sentence.", "Grade it with this rubric.", "Another fine one.",
                                      "And a last one."]),
    ])
    layout = PaperLayout(pages=[page])
    store = TranslationStore(tmp_path / "translation.json")
    store.bind(layout)
    engine = _RefusingTranslator()
    progress = translate_paper(layout, store, engine, cancel=threading.Event())
    # The rest of the page is translated; the refused sentence is not, and says so.
    assert store.get("p0b0s0") and store.get("p0b0s2") and store.get("p0b0s3")
    assert store.get("p0b0s1") is None and store.refused("p0b0s1") and "거절" in progress.message

    # Remembered on disk: the next run (the next time the paper opens) asks nothing.
    again = TranslationStore(tmp_path / "translation.json")
    again.bind(layout)
    assert pending_batches(layout, again) == []
    calls = len(engine.prompts)
    assert translate_paper(layout, again, engine, cancel=threading.Event()).total == 0
    assert len(engine.prompts) == calls


def test_translation_and_questions_use_their_own_models(qapp, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paper_saver.config import Settings
    from paper_saver.translate import engine_from_settings
    from paper_saver.translate.codex_cli import CodexEngine

    offered = [("gpt-6-luna", "GPT-6-Luna", "Fast and affordable"), ("gpt-6.1-sol", "Sol", "Latest"),
               ("gpt-6-sol", "Sol 6", "Previous")]
    monkeypatch.setattr("paper_saver.translate.codex_cli.available_models", lambda: offered)
    monkeypatch.setattr("paper_saver.ui.main_window.available_models", lambda: offered)

    # Nothing chosen: the recommended pair, each with its effort.
    settings = Settings(tmp_path / "settings.json")
    engine = engine_from_settings(settings)
    assert (engine.model, engine.chat_model) == ("gpt-6-luna", "gpt-6.1-sol")
    assert (engine.effort, engine.chat_effort, engine.quiz_effort) == ("low", "low", "medium")
    used: list[tuple[str, str]] = []
    monkeypatch.setattr(
        CodexEngine, "_run", lambda self, prompt, images, cancel, **kw: used.append((kw["model"], kw["effort"])) or "ok"
    )
    cancel = threading.Event()
    engine.translate("x", cancel=cancel)  # translation and the dictionary
    engine.ask("x", cancel=cancel)
    engine.converse("x", cancel=cancel)
    engine.generate_json("x", {}, cancel=cancel)  # quizzes
    assert used == [("gpt-6-luna", "low"), ("gpt-6.1-sol", "low"), ("gpt-6.1-sol", "low"), ("gpt-6.1-sol", "medium")]
    command = engine._command(Path("codex"), Path("a.md"), model="gpt-6-luna", effort="low")
    assert command[-5:] == ["-m", "gpt-6-luna", "-c", 'model_reasoning_effort="low"', "-"]
    assert "-m" not in engine._command(Path("codex"), Path("a.md"))
    resumed = engine._command(Path("codex"), Path("a.md"), resume="0199-abcd-ef01", model="gpt-6.1-sol", effort="low")
    assert resumed[-4:] == ["-c", 'model_reasoning_effort="low"', "0199-abcd-ef01", "-"]

    # The reader's own pick wins; "" is Codex's default; a retired one falls back.
    settings.set("chat_model", "")
    assert engine_from_settings(settings).chat_model == ""
    settings.set("translate_model", "gpt-5.5")
    assert engine_from_settings(settings).model == ""

    # The settings dialog shows what is in effect, and keeps a typed name.
    settings.set("translate_model", "gpt-6-luna")
    settings._data.pop("chat_model")
    from paper_saver.ui.main_window import SettingsDialog

    dialog = SettingsDialog(settings)
    assert dialog.model.currentData() == "gpt-6-luna" and dialog.chat_model.currentData() == "gpt-6.1-sol"
    dialog.chat_model.setEditText("gpt-6-sol")
    dialog.accept()
    assert settings.get("translate_model") == "gpt-6-luna" and settings.get("chat_model") == "gpt-6-sol"
