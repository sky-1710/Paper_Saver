# Project: Paper Saver — side-by-side paper reader with a layout-faithful Korean translation

A desktop reader for academic papers. The left half is the paper exactly as its
PDF renders. The right half is the *same page* with the body text replaced by a
literal Korean translation, at the same page size, with every figure, table,
equation and column boundary in the same place at the same size.

    Paper PDF → layout extraction → sentence split → GPT literal translation
             → translated page composed at original coordinates
             → hover-linked sentences · click-to-dictionary · notes
             → capture a region → ask GPT about it · paste it into a Markdown note → Notion

Paper Saver is the sibling of Lecture Saver
and follows its conventions: PySide6 desktop app, a pure-Python core with no Qt
in it, one project folder per document, atomic JSON writes, translation through
the Codex CLI so no API key is needed.

---

## 1. THE TWO PANES

§1.1 The left pane shows the original PDF, rendered by PyMuPDF. Nothing about
it is reinterpreted — it is the paper.

§1.2 The right pane is a *composite*: the original page image with only the
translated text blocks painted over, and Korean drawn inside the rectangle the
English occupied. Everything else — figures, plots, tables, equations, author
names, page furniture, rules, watermarks — survives as the original pixels at
the original coordinates. This is what makes "identical template, identical
figure and table positions, identical dimensions" true by construction rather
than by effort: those parts are never redrawn, so they cannot move.

§1.3 The two panes therefore have identical geometry. Scrolling is shared, and
a sentence's translation sits at the same y as its original. Alignment is free.

§1.4 What gets translated: body paragraphs, section headings, the title, and
figure/table captions. What is left as original pixels: anything inside a
figure or plot, table cells, display equations, author and affiliation lines,
the reference list, headers and footers.

---

## 2. LINKED SENTENCES

§2.1 A block's text is split into sentences with an abbreviation-aware
splitter: "et al.", "e.g.", "Fig. 3", "0.05", "[12]." must not end a sentence.

§2.2 Every source sentence is one word range on the page, so it has exact
rectangles (one per line it spans). Every translated sentence gets the
rectangles it was laid out into. The two share an id.

§2.3 Pointing *near* a sentence in either pane highlights both it and its
counterpart. Near, not on: the nearest sentence within a small distance wins,
so the reader does not have to aim.

---

## 3. WORD LOOKUP

§3.1 Clicking an English word opens a dictionary panel: its meaning *in this
sentence* first, then its general senses. Context is what a paper needs —
"significant", "novel", "attention" all mean something particular here.

§3.2 The lookup is one Codex call, cached on disk by word and context. A word
looked up once is instant forever after, and a cached word costs nothing.

§3.3 Any looked-up word can be starred into the paper's wordbook, which keeps
the sentence it came from as the example and exports as TSV for flashcards.

---

## 4. NOTES

§4.1 A free-form Markdown memo per paper, always one keystroke away.

§4.2 Notes anchored to a sentence: a marker appears beside that line in both
panes, and the note list doubles as a table of contents of the reader's own
thinking.

§4.3 Both autosave. Nothing the reader types may be lost to a crash, so writes
are atomic and debounced, never on a "save" the reader has to remember.

§4.4 The note: a Markdown file per paper that the reader may put anywhere
(project folder by default, or an Obsidian vault). Pictures live in `assets/`
beside it and are linked relatively, so the file reads the same in any
Markdown tool; moving the note copies the pictures it links. A save never
silently overwrites an edit made in another program: that version is kept
beside the note first.

§4.5 Notion export: on the reader's press only, the note becomes a child page
of a Notion page they chose, images uploaded. Re-exporting replaces the old
page by trashing it (restorable), never by editing it block by block.

---

## 4b. CAPTURE AND QUESTIONS

§4b.1 Capture mode turns the next drag on either pane into a rectangle on one
page. The region is re-rendered from the PDF at up to 3x, not grabbed from
the screen, so small print stays legible whatever the zoom.

§4b.2 A capture can be asked about (GPT tab), pasted into the note, or copied.
The GPT tab is a per-paper conversation (`chat.json`); each question carries
the page in view, the recent turns and the attached captures. A follow-up
without a picture re-sends the latest captures, since every Codex run starts
fresh.

---

## 5. TRANSLATION ENGINE

§5.1 Translation goes through the Codex CLI, signed in with the user's ChatGPT
account: no API key, no per-token bill. `codex exec` is run non-interactively
with the prompt on stdin and the answer written to a file.

§5.2 The engine is one class behind an interface, exactly as in Lecture Saver,
so an OpenAI-API engine or a local model is an added file rather than a change.

§5.3 Translation sends the paper's *text* to OpenAI. This is stated plainly
before the first run. The only other thing that goes is a capture the reader
explicitly attached to a question (`codex exec --image`). The PDF itself and
the reader's notes never go to OpenAI; a note goes to Notion only on export.

§5.4 Results are written to disk per page as they arrive. Reopening a paper
that was translated last week is instant and costs nothing, and a translation
interrupted halfway resumes where it stopped.

---

## 6. PROJECT FOLDER

    <Projects>/<paper title>/
        paper.json          source, page count, page sizes
        paper.pdf           the PDF, copied in (papers are small; be self-contained)
        layout.json         blocks, sentences, word boxes per page
        translation.json    sentence id → Korean, with engine and model
        notes.json          free memo + anchored notes
        wordbook.json       starred words
        chat.json           the GPT conversation
        captures/           captures attached to questions
        note.md, assets/    the Markdown note, until the reader moves it
        exports/

---

## 7. NON-NEGOTIABLES

§7.1 No layout may shift. If Korean does not fit a rectangle, the font shrinks;
the rectangle never grows.
§7.2 No telemetry, no analytics, no cloud sync. Notion export is an explicit
export, like saving a PDF, not a sync.
§7.3 The core (`paper_saver/core`, `pdf`, `render`) imports no Qt widgets and is
testable without a display; `QtGui` is used only where a font metric is needed.
§7.4 Nothing blocks the UI thread: extraction, rendering and translation all run
in workers.
