"""Pure logic: geometry, sentence splitting, prompts, stores, word cleaning."""

from __future__ import annotations

from pathlib import Path

from paper_saver.core.geometry import Rect, merge_into_lines
from paper_saver.core.project import Notes, Project, TranslationStore, Wordbook, WordEntry
from paper_saver.core.sentences import split_sentences
from paper_saver.translate.dictionary import clean_word
from paper_saver.translate.prompts import SentenceRequest, batch_prompt, parse_batch, parse_lookup


def _pieces(text: str) -> list[str]:
    return [text[a:b] for a, b in split_sentences(text)]


# -- sentences (§2.1) ----------------------------------------------------------------


def test_abbreviations_do_not_end_sentences() -> None:
    text = "As shown by Zhong et al. in Fig. 3, the model works. It also generalises, e.g. to SEED-IV."
    assert _pieces(text) == [
        "As shown by Zhong et al. in Fig. 3, the model works.",
        "It also generalises, e.g. to SEED-IV.",
    ]


def test_decimals_initials_and_formulas_are_kept_whole() -> None:
    text = "Accuracy was 79.37 % by J. R. Smith. The loss ⟦m4⟧. Then the next term follows here."
    assert _pieces(text) == [
        "Accuracy was 79.37 % by J. R. Smith.",
        "The loss ⟦m4⟧.",
        "Then the next term follows here.",
    ]


def test_every_character_belongs_to_a_sentence() -> None:
    text = "First sentence is here. Second one (13) follows. Short. Last sentence of the paragraph"
    spans = split_sentences(text)
    covered = "".join(text[a:b] for a, b in spans).replace(" ", "")
    assert covered == text.replace(" ", "")


def test_equation_tag_joins_previous_sentence() -> None:
    assert len(split_sentences("The update rule is given below as stated. (4)")) == 1


# -- geometry -----------------------------------------------------------------------------


def test_distance_and_line_merge() -> None:
    rect = Rect(10, 10, 20, 20)
    assert rect.distance_to(15, 15) == 0
    assert rect.distance_to(23, 14) == 3
    words = [Rect(0, 0, 5, 10), Rect(6, 0.5, 9, 10), Rect(0, 12, 4, 22)]
    assert merge_into_lines(words) == [Rect(0, 0, 9, 10), Rect(0, 12, 4, 22)]


# -- prompts ------------------------------------------------------------------------------


def test_prompt_carries_ids_and_formula_rule() -> None:
    prompt = batch_prompt([SentenceRequest("p1b2s1", "The loss ⟦m1⟧ is minimised.")])
    assert '"id": "p1b2s1"' in prompt
    assert "⟦m1⟧" in prompt


def test_parse_batch_tolerates_fences_and_drops_unknown_ids() -> None:
    answer = 'Here you go:\n```json\n{"translations": [{"id": "a", "ko": "가 나"}, {"id": "zzz", "ko": "x"},]}\n```'
    assert parse_batch(answer, ["a", "b"]) == {"a": "가 나"}


def test_parse_batch_accepts_flat_mapping() -> None:
    assert parse_batch('{"a": "번역"}', ["a"]) == {"a": "번역"}


def test_parse_lookup() -> None:
    entry = parse_lookup('{"lemma": "attention", "in_context": "주의 메커니즘", "senses": ["주의", "관심"]}')
    assert entry is not None and entry["in_context"] == "주의 메커니즘" and entry["senses"] == ["주의", "관심"]
    assert parse_lookup("not json") is None


def test_clean_word() -> None:
    assert clean_word("networks,") == "networks"
    assert clean_word("(RGNN)") == "RGNN"
    assert clean_word("model's") == "model"
    assert clean_word("[12]") == ""
    assert clean_word("⟦m1⟧") == ""


# -- stores (§4.3, §5.4) -----------------------------------------------------------------------


def test_translation_store_round_trip(tmp_path: Path) -> None:
    store = TranslationStore(tmp_path / "translation.json")
    store.update({"a": "하나"}, engine="codex")
    store.set("b", "둘")
    again = TranslationStore(tmp_path / "translation.json")
    assert again.snapshot() == {"a": "하나", "b": "둘"} and again.engine == "codex"


def test_notes_and_wordbook(tmp_path: Path) -> None:
    notes = Notes(tmp_path / "notes.json")
    notes.set_memo("# 요약")
    note = notes.add("p0b1s1", 0, "중요", "Some sentence.")
    notes.edit(note.id, "매우 중요")
    loaded = Notes(tmp_path / "notes.json")
    assert loaded.memo == "# 요약" and loaded.anchored[0].text == "매우 중요"

    book = Wordbook(tmp_path / "wordbook.json")
    book.add(WordEntry(word="graphs", lemma="graph", in_context="그래프", sentence="Graphs are nice."))
    book.add(WordEntry(word="graph", lemma="graph", in_context="그래프(갱신)"))
    assert len(Wordbook(tmp_path / "wordbook.json").entries) == 1
    assert book.to_tsv().splitlines()[1].startswith("graph\t그래프(갱신)")


def test_damaged_file_is_set_aside(tmp_path: Path) -> None:
    path = tmp_path / "notes.json"
    path.write_text("{broken", encoding="utf-8")
    assert Notes(path).memo == ""
    assert list(tmp_path.glob("notes.json.damaged-*"))


def test_same_pdf_reuses_project(tmp_path: Path, two_column_pdf: Path) -> None:
    first = Project.find_or_create(two_column_pdf, tmp_path / "projects", "A Study")
    first.translations.set("x", "번역")
    second = Project.find_or_create(two_column_pdf, tmp_path / "projects", "A Study")
    assert second.root == first.root and second.translations.get("x") == "번역"


def test_translation_follows_text_not_position(tmp_path: Path, two_column_pdf: Path) -> None:
    """Re-extraction can renumber sentences; a translation must stay with its text."""
    from paper_saver.core.models import Block, PageLayout, PaperLayout, Sentence, Word

    def layout_with(order: list[str]) -> PaperLayout:
        blocks = []
        for index, text in enumerate(order):
            word = Word(text, (Rect(0, index * 10, 50, index * 10 + 9),))
            blocks.append(
                Block(id=f"p0b{index}", page=0, kind="body", rect=word.rect, words=[word],
                      sentences=[Sentence(f"p0b{index}s1", 0, 1, text)], text=text,
                      font_size=10, leading=12)
            )
        return PaperLayout(pages=[PageLayout(0, 612, 792, blocks)])

    store = TranslationStore(tmp_path / "translation.json")
    store.bind(layout_with(["Alpha sentence.", "Beta sentence."]))
    store.update({"p0b0s1": "알파"})
    # Same paper, re-extracted with the paragraphs numbered the other way round.
    reopened = TranslationStore(tmp_path / "translation.json")
    reopened.bind(layout_with(["Beta sentence.", "Alpha sentence."]))
    assert reopened.get("p0b1s1") == "알파"
    assert reopened.get("p0b0s1") is None


def test_highlights_round_trip_and_recolor(tmp_path: Path) -> None:
    from paper_saver.core.project import Highlights

    marks = Highlights(tmp_path / "highlights.json")
    marks.set("p0b1s1", 0, "#ffd400", "A sentence.")
    marks.set("p0b1s1", 0, "#4aa3ff", "A sentence.")  # re-highlighting changes the colour
    marks.set("p0b2s1", 0, "#ff6fb0", "Another.")
    marks.remove("p0b2s1")
    assert Highlights(tmp_path / "highlights.json").colors() == {"p0b1s1": "#4aa3ff"}
