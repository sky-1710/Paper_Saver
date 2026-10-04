"""The note editor that formats as it is typed: what it reads, and that the
text it holds stays the Markdown, untouched."""

from __future__ import annotations

from pathlib import Path

from paper_saver.render.live_markdown import CODE, MATH, NORMAL, parse_line


def _roles(text: str) -> dict[str, list[str]]:
    line = parse_line(text)
    found: dict[str, list[str]] = {}
    for span in line.spans:
        found.setdefault(span.role, []).append(text[span.start : span.end])
    return found


def test_block_kinds() -> None:
    heading = parse_line("## 핵심 아이디어")
    assert heading.kind == "heading" and heading.level == 2 and heading.marker == (0, 3)
    assert parse_line("- 항목").kind == "bullet"
    assert parse_line("  * 하위").level == 2
    assert parse_line("3. 셋째").kind == "ordered"
    task = parse_line("- [x] 끝")
    assert task.kind == "task" and task.checked and task.box == 2
    assert parse_line("> 인용").kind == "quote"
    assert parse_line("---").kind == "rule"
    assert parse_line("| a | b |").kind == "table"
    assert parse_line("**굵게** 시작").kind == "text"  # not a bullet
    image = parse_line("![p.1 원문](<assets/cap 1.png>)")
    assert image.kind == "image" and image.target == "assets/cap 1.png" and image.alt == "p.1 원문"
    assert parse_line("앞 ![a](b.png) 뒤").kind == "text"


def test_fences_and_math_blocks_carry_state() -> None:
    assert parse_line("```python").state == CODE
    inside = parse_line("x = **1**", CODE)
    assert inside.kind == "code" and not inside.spans
    assert parse_line("```", CODE).state == NORMAL
    assert parse_line("$$").state == MATH
    assert parse_line(r"\frac{a}{b}", MATH).kind == "math"
    assert parse_line("$$", MATH).state == NORMAL
    assert parse_line("$$ x^2 $$").state == NORMAL


def test_inline_emphasis_works_beside_korean() -> None:
    roles = _roles("**타임 슬라이스(time slice)**는 *기울임*과 ~~취소~~")
    assert roles["bold"] == ["타임 슬라이스(time slice)"]
    assert roles["italic"] == ["기울임"]
    assert roles["strike"] == ["취소"]
    assert roles["marker"].count("**") == 2


def test_code_math_and_links_are_not_emphasis() -> None:
    roles = _roles("`a*b*c` 와 $a_1 * b_2$ 와 snake_case_name 와 [글](https://x.org/a_b_c)")
    assert roles["code"] == ["a*b*c"]
    assert roles["math"] == ["a_1 * b_2"]
    assert roles["link"] == ["글"]
    assert "italic" not in roles
    line = parse_line("[글](https://x.org)")
    assert list(line.links.values()) == ["https://x.org"]
    assert "math" not in _roles("$5 그리고 $10")


def test_editor_keeps_the_markdown_and_signals_only_real_edits(qapp, tmp_path: Path) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage, QTextCursor
    from PySide6.QtTest import QTest

    from paper_saver.ui.live_markdown import LiveMarkdownEdit

    (tmp_path / "assets").mkdir()
    picture = QImage(1200, 300, QImage.Format.Format_RGB32)
    picture.fill(QColor("teal"))
    picture.save(str(tmp_path / "assets" / "cap.png"))
    text = "# 제목\n\n**굵게**는\n\n- 하나\n- [ ] 할 일\n\n![캡처](assets/cap.png)\n끝"

    edit = LiveMarkdownEdit()
    edit.base_dir = tmp_path
    edit.resize(400, 600)
    edit.show()
    edits: list[int] = []
    edit.edited.connect(lambda: edits.append(1))
    edit.setPlainText(text)
    edits.clear()
    # Moving between lines redraws them; that is not an edit.
    for number in range(edit.document().blockCount()):
        cursor = edit.textCursor()
        cursor.setPosition(edit.document().findBlockByNumber(number).position())
        edit.setTextCursor(cursor)
        qapp.processEvents()
    assert not edits and edit.toPlainText() == text

    # The picture is fitted to the width and given its height on the page.
    size = edit.image_size(edit.line_of(edit.document().findBlockByNumber(7)))
    assert size is not None and size[0] < 400
    image_block = edit.document().findBlockByNumber(7)
    assert image_block.layout().boundingRect().height() >= size[1]

    # Enter after a list item starts the next; on the empty one it ends the list.
    cursor = edit.textCursor()
    cursor.setPosition(edit.document().findBlockByNumber(4).position())
    cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
    edit.setTextCursor(cursor)
    QTest.keyClick(edit, Qt.Key.Key_Return)
    assert edit.textCursor().block().text() == "- "
    QTest.keyClick(edit, Qt.Key.Key_Return)
    assert edit.textCursor().block().text() == ""
    assert edits

    # Ticking a box is an ordinary edit of the source.
    cursor.setPosition(0)
    edit.setTextCursor(cursor)
    qapp.processEvents()
    block = edit.document().findBlockByNumber(6)
    assert block.text() == "- [ ] 할 일"
    box = edit._box_rect(block, edit.line_of(block))
    QTest.mouseClick(edit.viewport(), Qt.MouseButton.LeftButton, pos=box.center().toPoint())
    assert edit.document().findBlockByNumber(6).text() == "- [x] 할 일"


def test_table_and_formula_helpers() -> None:
    from paper_saver.render.live_markdown import (
        inline_html,
        is_separator,
        table_alignments,
        table_cells,
    )

    assert table_cells(r"| a | **b** | c\|d |") == ["a", "**b**", "c|d"]
    assert is_separator("|:---|:---:|---:|") and not is_separator("| a | b |")
    assert table_alignments("|:---|:---:|---:|") == ["left", "center", "right"]
    cell = inline_html("**굵게** $a_1$ `x<y`")
    assert "<b>굵게</b>" in cell and "<sub>1</sub>" in cell and "x&lt;y" in cell and "$" not in cell
    # $$...$$ inside a sentence is a formula too; a price is not.
    line = parse_line("한 줄: $$ E = mc^2 $$ 끝")
    assert [m[2] for m in line.maths] == ["E = mc^2"]
    assert not parse_line("$5 그리고 $10").maths


def test_tables_and_formulas_are_drawn_until_the_cursor_enters(qapp) -> None:
    from PySide6.QtGui import QTextCursor

    from paper_saver.ui.live_markdown import LiveMarkdownEdit

    text = "위\n\n| 모델 | 정확도 |\n|---|---:|\n| Ours | 79.4 |\n| GCN | 72.1 |\n\n$$\n\frac{a}{b}\n$$\n\n값 $x^2$ 끝"
    edit = LiveMarkdownEdit()
    edit.resize(500, 600)
    edit.show()
    edit.setPlainText(text)
    qapp.processEvents()
    doc = edit.document()

    group = edit.group_of(doc.findBlockByNumber(4))
    assert group is not None and group[2] == "table"
    assert (group[0].blockNumber(), group[1].blockNumber()) == (2, 5)
    math = edit.group_of(doc.findBlockByNumber(8))
    assert math is not None and (math[0].blockNumber(), math[1].blockNumber(), math[2]) == (7, 9, "math")

    # Drawn: the separator row and the closing $$ shrink to almost nothing.
    assert doc.findBlockByNumber(3).layout().boundingRect().height() < 4
    assert doc.findBlockByNumber(9).layout().boundingRect().height() < 4

    # The cursor in any row shows the whole table's source again.
    cursor = edit.textCursor()
    cursor.setPosition(doc.findBlockByNumber(4).position())
    edit.setTextCursor(cursor)
    qapp.processEvents()
    assert edit.active == (2, 5)
    assert doc.findBlockByNumber(3).layout().boundingRect().height() > 8

    cursor.movePosition(QTextCursor.MoveOperation.End)
    edit.setTextCursor(cursor)
    qapp.processEvents()
    assert edit.toPlainText() == text
