"""Sending a Markdown note to Notion as a page, pictures included.

The reader creates an *internal integration* at notion.so/my-integrations,
connects it to one page of theirs, and gives Paper Saver the integration's
secret and that page's link. An export then makes a child page under it:

    1. the note is converted to Notion blocks (below);
    2. every local image it links is uploaded (``POST /file_uploads`` then
       ``/send``) and its block points at the upload;
    3. the page is created with the first 100 blocks - the most one request
       may carry - and the rest are appended 100 at a time.

Exporting the same note again makes a fresh page and moves the previous one
to Notion's trash, where it can be restored for 30 days. Updating a page in
place would mean deleting its blocks one request at a time; replacing it is
one request and cannot leave a half-updated page behind.

Nothing is sent until the reader presses the button (§7.2): this is an
export, like saving a PDF, not a sync.

The Markdown understood is what a note here holds: headings, paragraphs,
bullet / numbered / to-do lists (nested), quotes and Obsidian callouts, code
fences, ``$$`` display math, tables, dividers, images, and inline bold,
italic, strike, ``==highlight==``, code, links and ``$math$``.
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .notebook import image_refs, resolve_image

__all__ = [
    "NOTION_VERSION",
    "NotionClient",
    "NotionError",
    "export_markdown",
    "markdown_to_blocks",
    "page_id_from",
    "rich_text",
]

API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
#: Notion's limits: characters per text object, blocks per request.
TEXT_LIMIT = 2000
BLOCKS_PER_REQUEST = 100
RICH_TEXT_ITEMS = 100
#: Nested children one create request may carry.
MAX_DEPTH = 2
UPLOAD_LIMIT = 20 * 1024 * 1024

#: ``(method, url, headers, body) -> (status, body)``; swapped out in tests.
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class NotionError(Exception):
    """``str(err)`` is always fit to show the reader."""


def page_id_from(text: str) -> str | None:
    """The page id in a Notion link or a bare id, dashed; ``None`` if there is none."""
    text = text.strip().split("?")[0].split("#")[0]
    plain = re.findall(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])", text)
    dashed = re.findall(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", text)
    raw = (plain or [d.replace("-", "") for d in dashed] or [None])[-1]
    if raw is None:
        return None
    raw = raw.lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


# -- inline text -------------------------------------------------------------------

_INLINE = re.compile(
    r"(?P<code>`(?P<code_t>[^`\n]+)`)"
    r"|(?P<eq>(?<![\\$\w])\$(?P<eq_t>[^$\s](?:[^$\n]*[^$\s])?)\$(?!\$|\d))"
    r"|(?P<wiki>\[\[(?P<wiki_t>[^\]|]+)(?:\|(?P<wiki_a>[^\]]+))?\]\])"
    r"|(?P<link>\[(?P<link_t>[^\]]+)\]\((?P<link_u>[^)\s]+)\))"
    r"|(?P<bold>\*\*(?P<bold_t>.+?)\*\*|__(?P<bold_u>.+?)__)"
    r"|(?P<strike>~~(?P<strike_t>.+?)~~)"
    r"|(?P<mark>==(?P<mark_t>.+?)==)"
    r"|(?P<em>(?<![*\w])\*(?![\s*])(?P<em_t>.+?)(?<![\s*])\*(?![*\w])|(?<![_\w])_(?![\s_])(?P<em_u>.+?)(?<![\s_])_(?![_\w]))"
)


def _text_items(content: str, annotations: dict[str, Any], link: str | None = None) -> list[dict[str, Any]]:
    items = []
    for start in range(0, len(content), TEXT_LIMIT):
        text: dict[str, Any] = {"content": content[start : start + TEXT_LIMIT]}
        if link:
            text["link"] = {"url": link}
        item: dict[str, Any] = {"type": "text", "text": text}
        if annotations:
            item["annotations"] = dict(annotations)
        items.append(item)
    return items


def rich_text(text: str, annotations: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Notion rich text for one run of Markdown inline text."""
    items = _rich(text, dict(annotations or {}))
    if len(items) > RICH_TEXT_ITEMS:
        # Past Notion's cap the tail is kept as plain text rather than lost.
        rest = "".join(_plain_of(i) for i in items[RICH_TEXT_ITEMS - 1 :])
        items = items[: RICH_TEXT_ITEMS - 1] + _text_items(rest, {})[:1]
    return items


def _plain_of(item: dict[str, Any]) -> str:
    if item["type"] == "equation":
        return f"${item['equation']['expression']}$"
    return str(item["text"]["content"])


def _rich(text: str, ann: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out += _text_items(_unescape(text[pos : m.start()]), ann)
        kind = m.lastgroup
        if kind == "code":
            out += _text_items(m.group("code_t"), {**ann, "code": True})
        elif kind == "eq":
            out.append({"type": "equation", "equation": {"expression": m.group("eq_t")}})
        elif kind == "wiki":
            out += _text_items(m.group("wiki_a") or m.group("wiki_t"), ann)
        elif kind == "link":
            url = m.group("link_u")
            inner = _rich(m.group("link_t"), ann)
            if re.match(r"^(https?|mailto):", url, re.I):
                for item in inner:
                    if item["type"] == "text":
                        item["text"]["link"] = {"url": url}
            out += inner
        elif kind == "bold":
            out += _rich(m.group("bold_t") or m.group("bold_u"), {**ann, "bold": True})
        elif kind == "strike":
            out += _rich(m.group("strike_t"), {**ann, "strikethrough": True})
        elif kind == "mark":
            out += _rich(m.group("mark_t"), {**ann, "color": "yellow_background"})
        elif kind == "em":
            out += _rich(m.group("em_t") or m.group("em_u"), {**ann, "italic": True})
        pos = m.end()
    if pos < len(text):
        out += _text_items(_unescape(text[pos:]), ann)
    return out


def _unescape(text: str) -> str:
    return re.sub(r"\\([\\`*_{}\[\]()#+\-.!|$=~])", r"\1", text)


# -- blocks ------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_TODO = re.compile(r"^(\s*)[-*+]\s+\[([ xX])\]\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBER = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_DIVIDER = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+#.-]*)")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_CALLOUT = re.compile(r"^\[!(\w+)\][+-]?\s*(.*)$")

_LANGUAGES = {
    "py": "python", "python": "python", "js": "javascript", "javascript": "javascript",
    "ts": "typescript", "typescript": "typescript", "sh": "shell", "bash": "bash", "shell": "shell",
    "c": "c", "cpp": "c++", "c++": "c++", "cs": "c#", "java": "java", "go": "go", "rust": "rust",
    "r": "r", "matlab": "matlab", "julia": "julia", "json": "json", "yaml": "yaml", "yml": "yaml",
    "sql": "sql", "html": "html", "css": "css", "latex": "latex", "tex": "latex", "markdown": "markdown",
    "md": "markdown", "kotlin": "kotlin", "swift": "swift", "ruby": "ruby", "php": "php",
    "powershell": "powershell", "ps1": "powershell", "scala": "scala", "haskell": "haskell",
    "lua": "lua", "perl": "perl", "xml": "xml", "toml": "toml", "dockerfile": "docker", "mermaid": "mermaid",
}
_CALLOUT_ICONS = {
    "note": "📝", "info": "ℹ️", "tip": "💡", "hint": "💡", "important": "❗", "warning": "⚠️",
    "caution": "⚠️", "danger": "⛔", "error": "⛔", "bug": "🐞", "example": "📋", "quote": "💬",
    "question": "❓", "faq": "❓", "success": "✅", "check": "✅", "done": "✅", "abstract": "📄",
    "summary": "📄", "todo": "☑️", "failure": "❌",
}


def _block(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"object": "block", "type": kind, kind: body}


def _paragraph(text: str) -> dict[str, Any]:
    return _block("paragraph", {"rich_text": rich_text(text)})


def _image(target: str, base_dir: Path, alt: str = "", *, remote: bool = False) -> dict[str, Any]:
    if remote:
        return _block("image", {"type": "external", "external": {"url": target}})
    path = resolve_image(target, base_dir)
    if path is None:
        return _paragraph(f"(이미지를 찾을 수 없음: {target})")
    block = _block("image", {"type": "file_upload", "file_upload": {"id": ""}})
    if alt.strip():
        block["image"]["caption"] = rich_text(alt.strip())
    block["_local"] = str(path)
    return block


def _image_line(line: str, base_dir: Path) -> list[dict[str, Any]] | None:
    """Blocks for a line holding images, with any text around them as paragraphs."""
    refs = image_refs(line)
    if not refs:
        return None
    blocks: list[dict[str, Any]] = []
    pos = 0
    for ref in refs:
        before = line[pos : ref.start].strip()
        if before:
            blocks.append(_paragraph(before))
        if ref.wiki:
            path = resolve_image(ref, base_dir)
            blocks.append(_image(str(path) if path else ref.target, base_dir))
        else:
            blocks.append(_image(ref.target, base_dir, ref.alt, remote=ref.remote))
        pos = ref.end
    after = line[pos:].strip()
    if after:
        blocks.append(_paragraph(after))
    return blocks


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def _width(indent: str) -> int:
    return len(indent.expandtabs(4))


def _strip_images(text: str) -> str:
    """Images inside a heading or list item: kept as their alt text."""
    for ref in reversed(image_refs(text)):
        text = text[: ref.start] + (ref.alt or "") + text[ref.end :]
    return text


def markdown_to_blocks(markdown: str, base_dir: Path) -> list[dict[str, Any]]:
    """Notion blocks for a note. Local images carry a ``_local`` path to upload."""
    lines = markdown.replace("\r\n", "\n").split("\n")
    # YAML front matter is metadata, not content.
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None)
        if end is not None:
            lines = lines[end + 1 :]

    blocks: list[dict[str, Any]] = []
    #: Open list items: (indent width, block, depth).
    stack: list[tuple[int, dict[str, Any], int]] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append(_paragraph("\n".join(paragraph)))
            paragraph.clear()

    def add_item(indent: int, block: dict[str, Any]) -> None:
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not stack:
            blocks.append(block)
            stack.append((indent, block, 0))
            return
        parent_indent, parent, depth = stack[-1]
        if depth >= MAX_DEPTH:
            # Notion takes two levels of children per request; deeper items
            # become siblings of the deepest level allowed.
            while stack and stack[-1][2] >= MAX_DEPTH:
                stack.pop()
            parent_indent, parent, depth = stack[-1]
        parent[parent["type"]].setdefault("children", []).append(block)
        stack.append((indent, block, depth + 1))

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush()
            i += 1
            continue

        fence = _FENCE.match(line)
        if fence:
            flush()
            stack.clear()
            marker, language = fence.group(1), fence.group(2).lower()
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker):
                body.append(lines[i])
                i += 1
            i += 1
            code = "\n".join(body)
            blocks.append(_block("code", {
                "rich_text": _text_items(code, {}) or _text_items(" ", {}),
                "language": _LANGUAGES.get(language, "plain text"),
            }))
            continue

        if stripped.startswith("$$"):
            flush()
            stack.clear()
            inner = stripped[2:]
            if inner.rstrip().endswith("$$") and len(inner.strip()) > 2:
                expression = inner.rstrip()[:-2]
                i += 1
            else:
                parts = [inner] if inner.strip() else []
                i += 1
                while i < len(lines) and not lines[i].strip().endswith("$$"):
                    parts.append(lines[i])
                    i += 1
                if i < len(lines):
                    last = lines[i].strip()[:-2]
                    if last.strip():
                        parts.append(last)
                    i += 1
                expression = "\n".join(parts)
            blocks.append(_block("equation", {"expression": expression.strip()[:1000]}))
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush()
            stack.clear()
            rows = [_cells(line)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            width = max(len(r) for r in rows)
            blocks.append(_block("table", {
                "table_width": width,
                "has_column_header": True,
                "has_row_header": False,
                "children": [
                    _block("table_row", {"cells": [rich_text(c) for c in row + [""] * (width - len(row))]})
                    for row in rows
                ],
            }))
            continue

        if stripped.startswith(">"):
            flush()
            stack.clear()
            quoted: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quoted.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            callout = _CALLOUT.match(quoted[0].strip())
            if callout:
                kind, title = callout.group(1).lower(), callout.group(2).strip()
                text = "\n".join(([f"**{title}**"] if title else []) + quoted[1:]).strip()
                blocks.append(_block("callout", {
                    "rich_text": rich_text(_strip_images(text)) or _text_items(kind.title(), {}),
                    "icon": {"type": "emoji", "emoji": _CALLOUT_ICONS.get(kind, "💡")},
                }))
            else:
                blocks.append(_block("quote", {"rich_text": rich_text(_strip_images("\n".join(quoted).strip()))}))
            continue

        if _DIVIDER.match(line):
            flush()
            stack.clear()
            blocks.append(_block("divider", {}))
            i += 1
            continue

        heading = _HEADING.match(stripped)
        if heading:
            flush()
            stack.clear()
            level = min(3, len(heading.group(1)))
            blocks.append(_block(f"heading_{level}", {"rich_text": rich_text(_strip_images(heading.group(2)))}))
            i += 1
            continue

        todo, bullet, number = _TODO.match(line), _BULLET.match(line), _NUMBER.match(line)
        if todo or bullet or number:
            flush()
            if todo:
                block = _block("to_do", {
                    "rich_text": rich_text(_strip_images(todo.group(3))),
                    "checked": todo.group(2).lower() == "x",
                })
                indent = todo.group(1)
            elif bullet:
                block = _block("bulleted_list_item", {"rich_text": rich_text(_strip_images(bullet.group(2)))})
                indent = bullet.group(1)
            else:
                assert number is not None
                block = _block("numbered_list_item", {"rich_text": rich_text(_strip_images(number.group(2)))})
                indent = number.group(1)
            add_item(_width(indent), block)
            i += 1
            continue

        # A line indented under an open list item continues the deepest item
        # it is indented past.
        leading = _width(line[: len(line) - len(line.lstrip())])
        while stack and leading <= stack[-1][0]:
            stack.pop()
        if stack and not paragraph:
            item = stack[-1][1]
            body_text = item[item["type"]]
            body_text["rich_text"] = body_text["rich_text"] + rich_text("\n" + _strip_images(stripped))
            i += 1
            continue

        stack.clear()
        images = _image_line(stripped, base_dir)
        if images is not None:
            flush()
            blocks += images
            i += 1
            continue
        paragraph.append(stripped)
        i += 1

    flush()
    return blocks


def _walk(blocks: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    for block in blocks:
        yield block
        body = block.get(block.get("type", ""), {})
        if isinstance(body, dict):
            yield from _walk(body.get("children", []))


def split_title(blocks: list[dict[str, Any]], fallback: str) -> tuple[str, list[dict[str, Any]]]:
    """The note's leading ``# heading`` becomes the page title, if it has one."""
    if blocks and blocks[0]["type"] == "heading_1":
        title = "".join(_plain_of(i) for i in blocks[0]["heading_1"]["rich_text"]).strip()
        if title:
            return title, blocks[1:]
    return fallback, blocks


# -- the API -----------------------------------------------------------------------


def _urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""


@dataclass
class NotionClient:
    token: str
    transport: Transport | None = None
    #: Pauses between retries; zero in tests.
    backoff: float = 1.0

    def _send(self, method: str, path: str, body: bytes | None, content_type: str | None) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.token.strip()}",
            "Notion-Version": NOTION_VERSION,
        }
        if content_type:
            headers["Content-Type"] = content_type
        transport = self.transport or _urllib_transport
        for attempt in range(4):
            try:
                status, raw = transport(method, API + path, headers, body)
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                raise NotionError(f"Notion에 연결하지 못했습니다. 인터넷 연결을 확인하세요.\n\n{exc}") from exc
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                payload = {}
            if 200 <= status < 300:
                return payload if isinstance(payload, dict) else {}
            if (status == 429 or status >= 500) and attempt < 3:
                time.sleep(self.backoff * (2**attempt))
                continue
            raise NotionError(_explain(status, payload))
        raise NotionError("Notion이 계속 응답하지 않습니다. 잠시 뒤 다시 시도하세요.")

    def request(self, method: str, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
        return self._send(method, path, body, "application/json" if body is not None else None)

    def upload(self, path: Path) -> str:
        """Upload one file; returns the id an image block can point at."""
        data = path.read_bytes()
        if len(data) > UPLOAD_LIMIT:
            raise NotionError(f"이미지가 20MB를 넘어 Notion에 올릴 수 없습니다: {path.name}")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        name = path.name if path.name.isascii() else f"image{path.suffix.lower()}"
        created = self.request("POST", "/file_uploads", {"filename": name, "content_type": content_type})
        upload_id = str(created.get("id") or "")
        if not upload_id:
            raise NotionError("Notion이 파일 업로드를 받아들이지 않았습니다.")
        boundary = uuid.uuid4().hex
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
        self._send("POST", f"/file_uploads/{upload_id}/send", body, f"multipart/form-data; boundary={boundary}")
        return upload_id


def _explain(status: int, payload: dict[str, Any]) -> str:
    code = str(payload.get("code") or "")
    message = str(payload.get("message") or "")
    if status == 401 or code == "unauthorized":
        return "Notion 통합(integration) 시크릿이 올바르지 않습니다. 설정에서 다시 넣어 주세요."
    if status == 404 or code == "object_not_found":
        return (
            "대상 Notion 페이지를 찾을 수 없습니다.\n\n"
            "그 페이지에서 ••• → 연결(Connections) → 만든 통합을 추가해야 Paper Saver가 쓸 수 있습니다. "
            "링크가 페이지(데이터베이스가 아닌)인지도 확인하세요."
        )
    if status == 403 or code == "restricted_resource":
        return "통합에 이 페이지에 내용을 넣을 권한이 없습니다. 통합의 기능(Capabilities)에서 '콘텐츠 삽입'을 켜세요."
    return f"Notion이 요청을 거절했습니다 ({status} {code}).\n\n{message}"


def export_markdown(
    client: NotionClient,
    parent_page: str,
    markdown: str,
    base_dir: Path,
    *,
    fallback_title: str = "Paper Saver 노트",
    replace: str | None = None,
    report: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
) -> tuple[str, str]:
    """Create the page; returns its id and URL. ``replace`` is trashed afterwards."""
    parent = page_id_from(parent_page)
    if parent is None:
        raise NotionError("Notion 페이지 링크에서 페이지 id를 찾지 못했습니다. 페이지 링크를 그대로 붙여 넣어 주세요.")
    say = report or (lambda _m: None)

    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise NotionError("Notion 보내기를 멈췄습니다.")

    title, blocks = split_title(markdown_to_blocks(markdown, base_dir), fallback_title)
    images = [b for b in _walk(blocks) if "_local" in b]
    uploaded: dict[str, str] = {}
    for number, block in enumerate(images, start=1):
        check()
        local = block.pop("_local")
        say(f"Notion에 이미지 올리는 중… {number}/{len(images)}")
        if local not in uploaded:
            uploaded[local] = client.upload(Path(local))
        block["image"]["file_upload"]["id"] = uploaded[local]

    check()
    say("Notion 페이지 만드는 중…")
    page = client.request("POST", "/pages", {
        "parent": {"type": "page_id", "page_id": parent},
        "properties": {"title": {"title": _text_items(title[:TEXT_LIMIT], {})}},
        "children": blocks[:BLOCKS_PER_REQUEST],
    })
    page_id = str(page.get("id") or "")
    for start in range(BLOCKS_PER_REQUEST, len(blocks), BLOCKS_PER_REQUEST):
        check()
        say(f"Notion에 내용 넣는 중… {start}/{len(blocks)}")
        client.request("PATCH", f"/blocks/{page_id}/children", {"children": blocks[start : start + BLOCKS_PER_REQUEST]})

    if replace and replace != page_id:
        try:
            client.request("PATCH", f"/pages/{replace}", {"in_trash": True})
        except NotionError:
            pass  # Already gone, or no longer shared: nothing to replace.
    return page_id, str(page.get("url") or "")
