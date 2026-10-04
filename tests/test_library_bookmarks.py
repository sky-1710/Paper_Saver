"""Folders on the start screen, and the bookmarks tab."""

from __future__ import annotations

import threading
from pathlib import Path

from paper_saver.core.atomic_io import read_json, write_json_atomic
from paper_saver.core.library import Library
from paper_saver.core.project import Project


def _paper(projects: Path, name: str, **meta) -> Path:  # type: ignore[no-untyped-def]
    root = projects / name
    root.mkdir(parents=True)
    write_json_atomic(root / "paper.json", {"app": "paper-saver", "title": name, **meta})
    return root


def test_folders_are_labels_kept_in_each_paper(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    a = _paper(projects, "Attention")
    b = _paper(projects, "BERT")
    _paper(projects, "Elsewhere", folder="From another machine")

    library = Library(projects)
    library.add_folder("  NLP   basics ")
    library.add_folder("Empty")
    library.set_folder(a, "NLP basics")
    library.set_folder(b, "NLP basics")
    assert read_json(a / "paper.json")["folder"] == "NLP basics"

    reopened = Library(projects)
    entries = {e.title: e.folder for e in reopened.entries()}
    assert entries == {"Attention": "NLP basics", "BERT": "NLP basics", "Elsewhere": "From another machine"}
    # An empty folder stays; a folder known only from a paper is picked up.
    assert reopened.folders == ["NLP basics", "Empty", "From another machine"]

    reopened.rename_folder("NLP basics", "NLP")
    assert reopened.folders[0] == "NLP"
    assert {e.title: e.folder for e in reopened.entries()}["BERT"] == "NLP"

    reopened.move_folder("Empty", 0)
    assert reopened.folders[0] == "Empty"

    reopened.remove_folder("NLP")
    assert "NLP" not in Library(projects).folders
    assert "folder" not in read_json(a / "paper.json")
    assert (a / "paper.json").is_file()  # the paper itself stays


def test_an_open_paper_does_not_write_its_folder_away(tmp_path: Path) -> None:
    root = _paper(tmp_path, "Open paper")
    project = Project(root)
    Library(tmp_path).set_folder(root, "Reading")
    project.set_meta(note_file="note.md")
    meta = read_json(root / "paper.json")
    assert meta["folder"] == "Reading" and meta["note_file"] == "note.md"


def test_bookmarks_list_highlights_with_their_notes(qapp, tmp_path: Path, two_column_pdf: Path) -> None:
    from paper_saver.config import Settings
    from paper_saver.ui.main_window import MainWindow, _prepare

    settings = Settings(tmp_path / "settings.json")
    settings.set("projects_dir", str(tmp_path / "projects"))
    settings.set("auto_translate", False)
    window = MainWindow(settings)
    project, layout = _prepare(two_column_pdf, None, settings.projects_dir, "Reading")(
        threading.Event(), lambda _m: None
    )
    assert project.meta["folder"] == "Reading"
    ids = [s.id for _, _, s in layout.sentences()]
    project.translations.update({ids[1]: "두 번째 문장"})
    window._opened((project, layout))
    qapp.processEvents()
    reader = window.reader
    assert reader is not None

    reader.highlight(ids[3], "#5fd35f")
    reader.highlight(ids[1], "#ffd400")
    project.notes.add(ids[1], 0, "핵심 주장", "")
    reader._apply_notes()
    panel = reader.bookmarks_panel
    marks = panel.bookmarks()
    assert [b.sentence_id for b in marks] == [ids[1], ids[3]]  # reading order
    assert marks[0].translation == "두 번째 문장"

    card = panel.cards.itemAt(0).widget()
    texts = [label.text() for label in card.findChildren(type(panel.count))]
    assert "핵심 주장" in texts and "두 번째 문장" in texts

    # A dragged stroke is a bookmark too, tied to the sentence it starts in.
    ref = reader.session.sentence(ids[3])
    rects = ref.sentence.rects(ref.block)[:1]
    reader.left.pen_color = "#4aa3ff"
    reader._add_stroke("source", ref.page, rects, "a stroke")
    stroke = project.highlights.strokes[0]
    assert stroke.sentence_id == ids[3]
    assert len(panel.bookmarks()) == 3

    panel.set_filter("#5fd35f")
    assert panel.cards.count() == 1
    panel.set_filter("")

    reader._remove_bookmark("sentence", ids[3])
    reader._remove_bookmark("stroke", stroke.id)
    assert [b.sentence_id for b in panel.bookmarks()] == [ids[1]]

    # The start screen lists the paper under its folder.
    window.show_start()
    tree = window.start.tree
    folders = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
    assert any("Reading" in f for f in folders)
    window.start.move_papers([project.root], "")
    folders = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
    assert any("폴더 없음" in f for f in folders)
    window.close()
