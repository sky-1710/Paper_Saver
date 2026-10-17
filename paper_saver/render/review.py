"""A review document drawn for reading, every cited part a link (§4d).

The document is Markdown (``core.review``); this draws it as the HTML a
QTextBrowser shows, and makes each part that cites the paper clickable:

* a stretch of text up to a citation ``[p.3](ref:…)`` links to the sentences
  cited, so each sentence of a summary paragraph has its own;
* a section heading links to everything cited under it;
* in Cornell notes each ``### cue`` and the lines under it become a row of the
  cue | notes grid, the cue linking to all its notes cite, and a ``## `` section
  after the grid (the 요약) a box beneath it.

Links are numbered ``seg:N`` in the order drawn; :func:`review_html` returns
the sentence ids of each, so following a link needs no ids in the URL. Any
Markdown the reader writes is drawn too - headings, lists, quotes, rules,
paragraphs - linked where it cites something.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from ..core.review import refs_in, split_citations
from .live_markdown import inline_html

__all__ = ["ACTIVE_BACKGROUND", "LINK_STYLE", "review_html"]

#: Links read as text; the page chip after them says they lead somewhere.
LINK_STYLE = "a { color: #202124; text-decoration: none; }"
#: The part last clicked, in the green the PDF lights its sentences with.
ACTIVE_BACKGROUND = "#d6eedc"
_CHIP = "color:#5f6368; background-color:#eef0f2; font-size:8pt"
_HEAD_COLOR = "#1a4fb4"

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_TASK = re.compile(r"^(\s*)[-*+]\s+\[([ xX])\]\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)\d{1,9}[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")


@dataclass
class _Line:
    #: ``heading``, ``bullet``, ``ordered``, ``quote``, ``rule``, ``text``, ``blank``.
    kind: str
    text: str
    level: int = 0


def _parse(markdown: str) -> list[_Line]:
    lines: list[_Line] = []
    for raw in markdown.splitlines():
        if not raw.strip():
            lines.append(_Line("blank", ""))
        elif m := _HEADING.match(raw):
            lines.append(_Line("heading", m.group(2), len(m.group(1))))
        elif _RULE.match(raw):
            lines.append(_Line("rule", ""))
        elif m := _TASK.match(raw):
            box = "☑ " if m.group(2) in "xX" else "☐ "
            lines.append(_Line("bullet", box + m.group(3), len(m.group(1).expandtabs(4)) // 2))
        elif m := _BULLET.match(raw):
            lines.append(_Line("bullet", m.group(2), len(m.group(1).expandtabs(4)) // 2))
        elif m := _ORDERED.match(raw):
            lines.append(_Line("ordered", m.group(2), len(m.group(1).expandtabs(4)) // 2))
        elif m := _QUOTE.match(raw):
            lines.append(_Line("quote", m.group(1)))
        else:
            lines.append(_Line("text", raw.strip()))
    return lines


def _next_heading(lines: list[_Line], start: int, *, level: int = 6) -> int:
    """The index of the next heading at ``level`` or above, after ``start``."""
    for index in range(start + 1, len(lines)):
        if lines[index].kind == "heading" and lines[index].level <= level:
            return index
    return len(lines)


class _Drawer:
    def __init__(self, active: int) -> None:
        self.active = active
        #: The sentence ids of each link, by its number.
        self.segments: list[list[str]] = []

    def link(self, refs: list[str], inner: str, color: str = "") -> str:
        number = len(self.segments)
        self.segments.append(refs)
        style = [f"color:{color}"] if color else []
        if number == self.active:
            style.append(f"background-color:{ACTIVE_BACKGROUND}")
        styled = f' style="{"; ".join(style)}"' if style else ""
        return f'<a href="seg:{number}"{styled}>{inner}</a>'

    def inline(self, text: str) -> str:
        """A line's text, each stretch linked to what it cites."""
        out = []
        for segment in split_citations(text):
            inner = inline_html(segment.text.strip())
            if not segment.refs:
                out.append(inner)
                continue
            labels = html.escape(", ".join(label for label in segment.labels if label.strip()) or "원문")
            chip = f'<span style="{_CHIP}">&nbsp;{labels}&nbsp;</span>'
            out.append(self.link(segment.refs, f"{inner}{'&nbsp;' if inner else ''}{chip}"))
        return " ".join(out)

    def heading(self, line: _Line, refs: list[str]) -> str:
        if line.level == 1:
            return f'<p style="font-size:15pt; font-weight:700; margin-bottom:6px">{self.inline(line.text)}</p>'
        size = {2: "11.5pt", 3: "10.5pt"}.get(line.level, "10pt")
        text = self.inline(line.text)
        if refs and not refs_in(line.text):
            text = self.link(refs, text, _HEAD_COLOR)
        return (
            f'<p style="font-size:{size}; font-weight:700; color:{_HEAD_COLOR}; '
            f'margin-top:12px; margin-bottom:3px">{text}</p>'
        )

    def blocks(self, lines: list[_Line]) -> str:
        """Lists, quotes, rules and paragraphs."""
        out: list[str] = []
        lists: list[tuple[str, int]] = []
        paragraph: list[str] = []
        quote: list[str] = []

        def flush() -> None:
            if paragraph:
                out.append(f'<p style="margin-top:4px; margin-bottom:4px">{self.inline(" ".join(paragraph))}</p>')
                paragraph.clear()
            if quote:
                inner = "<br>".join(self.inline(q) for q in quote)
                out.append(
                    '<table width="100%" cellspacing="0" cellpadding="8" bgcolor="#eef3fd" '
                    f'style="margin-top:4px; margin-bottom:6px"><tr><td>{inner}</td></tr></table>'
                )
                quote.clear()

        def close(level: int = -1) -> None:
            while lists and lists[-1][1] > level:
                out.append(f"</{lists.pop()[0]}>")

        for line in lines:
            if line.kind in ("bullet", "ordered"):
                flush()
                tag = "ul" if line.kind == "bullet" else "ol"
                close(line.level)
                if lists and lists[-1] == (tag, line.level):
                    pass
                else:
                    if lists and lists[-1][1] == line.level:
                        close(line.level - 1)
                    out.append(f'<{tag} style="margin-top:2px; margin-bottom:4px">')
                    lists.append((tag, line.level))
                out.append(f'<li style="margin-bottom:3px">{self.inline(line.text)}</li>')
                continue
            close()
            if line.kind == "quote":
                if paragraph:
                    flush()
                quote.append(line.text)
            elif line.kind == "text":
                if quote:
                    flush()
                paragraph.append(line.text)
            else:  # blank or rule ends a paragraph
                flush()
                if line.kind == "rule":
                    out.append("<hr>")
        flush()
        close()
        return "".join(out)

    def cornell(self, rows: list[tuple[_Line, list[_Line]]]) -> str:
        """The cue | notes grid: the table's colour shows through as rules."""
        out = [
            '<table width="100%" cellspacing="1" cellpadding="8" bgcolor="#dadce0" style="margin-top:6px">',
            '<tr><td width="34%" bgcolor="#f1f3f4"><b>단서</b></td><td bgcolor="#f1f3f4"><b>노트</b></td></tr>',
        ]
        for cue, body in rows:
            refs = refs_in("\n".join(line.text for line in [cue, *body]))
            text = self.inline(cue.text)
            if refs and not refs_in(cue.text):
                text = self.link(refs, text)
            out.append(
                f'<tr><td width="34%" bgcolor="#f8f9fb" valign="top"><b>{text}</b></td>'
                f'<td bgcolor="#ffffff" valign="top">{self.blocks(body)}</td></tr>'
            )
        out.append("</table>")
        return "".join(out)

    def box(self, heading: _Line, body: list[_Line]) -> str:
        """A section under the grid - the 요약 - set apart."""
        return (
            '<table width="100%" cellspacing="0" cellpadding="10" bgcolor="#fef7e0" style="margin-top:10px">'
            f'<tr><td><b style="color:#6b4e00">{self.inline(heading.text)}</b>{self.blocks(body)}</td></tr></table>'
        )


def review_html(markdown: str, kind: str, *, active: int = -1) -> tuple[str, list[list[str]]]:
    """The document as HTML, and the sentence ids of each ``seg:N`` link.

    ``kind`` is "one_page" or "cornell"; ``active`` is the link drawn as the
    one last clicked.
    """
    lines = _parse(markdown)
    drawer = _Drawer(active)
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.kind != "heading":
            end = _next_heading(lines, index - 1)
            out.append(drawer.blocks(lines[index:end]))
            index = end
            continue
        end = _next_heading(lines, index)
        if kind == "cornell" and line.level >= 3:
            rows = []
            while index < len(lines) and lines[index].kind == "heading" and lines[index].level >= 3:
                end = _next_heading(lines, index)
                rows.append((lines[index], lines[index + 1 : end]))
                index = end
            out.append(drawer.cornell(rows))
            continue
        if kind == "cornell" and line.level == 2:
            out.append(drawer.box(line, lines[index + 1 : end]))
            index = end
            continue
        section = lines[index + 1 : _next_heading(lines, index, level=line.level)]
        out.append(drawer.heading(line, refs_in("\n".join(item.text for item in section))))
        out.append(drawer.blocks(lines[index + 1 : end]))
        index = end
    return "".join(out), drawer.segments
