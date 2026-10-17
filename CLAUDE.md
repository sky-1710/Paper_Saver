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

§1.5 Several papers can be open at once, each in a tab of its own with its own
panes, notes, conversation and translation; opening one already open shows
its tab.

§1.6 The start screen lists the papers by folder, not by recency (recent
papers come first within a folder). A folder is a label written into the
paper's `paper.json`, never a directory the project is moved into, so
nothing that refers to a project folder breaks; the folder list and its order
live in `<Projects>/library.json`, so an empty folder stays.

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
The note has no preview: Markdown is drawn formatted as it is typed (markers
show only on the line being edited, an image link is drawn as the picture),
while the text stays the exact Markdown the file holds - never rewritten.

§4.6 Every highlight is a bookmark. The 북마크 tab lists them in reading
order - a whole sentence with its translation, a dragged stroke with what it
covers - each with the notes on its sentence shown right beneath it. A stroke
remembers the sentence it starts in, which is where it jumps and whose notes
it shows.

§4.5 Notion export: on the reader's press only, the note becomes a child page
of a Notion page they chose, images uploaded. Re-exporting replaces the old
page by trashing it (restorable), never by editing it block by block.

---

## 4b. CAPTURE AND QUESTIONS

§4b.1 Capture mode turns the next drag on either pane into a rectangle on one
page. The region is re-rendered from the PDF at up to 3x, not grabbed from
the screen, so small print stays legible whatever the zoom.

§4b.2 A capture can be asked about (GPT tab), pasted into the note, or copied.
The GPT tab is a per-paper conversation (`chat.json`) run in one Codex
session (`codex exec resume`): text goes once per session and a follow-up
carries only what is new, while OpenAI's prompt cache serves what the session
holds. What goes is the reader's pick: the page in view; "이 논문", the whole
paper; or "모든 탭", the page in view plus each open paper's opening and the
paragraphs matching the question (English and Korean; an outline when
nothing matches) - never several whole papers. Sentence tags are numbered
across the session, and `chat.json` keeps what was sent. A session that cannot
be resumed is replaced by a fresh one carrying everything, with the recent
turns and the latest captures. An answer citing another open paper lights
that sentence in its own tab, the tab is marked ◆, and the citation link
brings it forward. A sentence can be quoted into the question from its menu
or with Ctrl+G on hover (original and translation, gathered above what the
reader typed); a quoted sentence's page is the page the question is about.

§4b.3 Files can be attached to the conversation (📎, or dropped on the
input): PDF, txt, md, srt, vtt - or another paper the reader has opened,
taken from its layout. They are copied into `chat_files/` with their text
beside it and stay attached, going with every question as [자료 N], until the
reader takes them off (which also ends the session, since it holds them);
clearing the conversation keeps them.

---

## 4c. SLIDE QUIZZES

§4c.1 A lecture PDF opened in Paper Saver is a slide deck: each page is a
slide. The 퀴즈 tab follows the page in view and, on the reader's press,
writes three True/False questions for that slide — one Easy (a definition or
property stated on the slide), one Medium (one key term, condition or cause
swapped into a plausible misconception), one Hard (concepts combined, a
condition judged, a small calculation or scenario).

§4c.2 Materials are kept apart and named as what they are: the slides (the
open PDF, never uploaded separately), the lecture's STT (optional; PDF, txt,
md, srt, vtt), and the professor's earlier quizzes, used for *style only*.
Registered files are copied into `quiz/materials/` with their text beside them.
The STT may be several files - a lecture recorded in parts - kept in lecture
order and joined into one lecture, each file's "Slide N" numbers moved past
the files before it; the one-page review and Cornell notes (§4d) use the same
list.

§4c.3 Only one slide's context is sent: its text, the STT sections matched to
it, and the extracted style examples. STT sections are matched to pages by
shared words in lecture order, since Lecture Saver's "Slide N" counts screen
changes, not PDF pages. Questions must stay inside the slide and its STT.

§4c.4 Codex answers JSON held to a schema (`--output-schema`); the answer is
validated (one question per difficulty, complete fields, a hint that does not
give the answer away) and retried once with the reason before failing. A
slide's quiz and the reader's answers are kept in `quiz.json` until the
reader presses Regenerate.

---

## 4d. ONE-PAGE REVIEW AND CORNELL NOTES

§4d.1 The right pane's header switches it between the translation and two
documents GPT writes about the whole paper, on the reader's press: a one-page
review (a one-line summary, then 문제와 동기 · 핵심 아이디어 · 방법 · 실험과
결과 · 기여 · 한계와 의문 · 기억할 점) and Cornell notes (cue | notes rows in
reading order, a summary beneath). The PDF stays on the left.

§4d.2 Every point rests on the paper. GPT sees all of its text - a lecture's
tables, diagrams and code are its content - with each sentence tagged `[S12]`,
and answers JSON held to a schema, each point naming its sentences; numbers
the paper did not have are dropped, and an answer in which most points cite
nothing is sent back once with the reason. Clicking a point, a cue, a summary
sentence or a heading lights its sentences in the PDF and scrolls there.
With the lecture's STT registered (§4c.2), all of it goes, each part under the
page it was matched to; a point taken from the lecture alone starts "(강의)"
and cites that page.

§4d.3 A document is Markdown the reader may edit, in the note's editor, saved
as it is typed into `review.json`. Its citations are `[p.3](ref:<sentence
ids>)` links: Ctrl+click follows one while editing, and a sentence's menu puts
a new one at the cursor. The text of each cited sentence is kept beside the
documents, so a link follows its sentence when the layout is re-extracted. A
document is kept until the reader presses 다시 만들기, and can go into the note
with its citations as page numbers.

---

## 5. TRANSLATION ENGINE

§5.1 Translation goes through the Codex CLI, signed in with the user's ChatGPT
account: no API key, no per-token bill. `codex exec` is run non-interactively
with the prompt on stdin and the answer written to a file. Two models can be
set: one for translation and dictionary look-ups, one for GPT questions,
quizzes and reviews. Unless the reader picks otherwise they are gpt-6-luna
(low effort - many small sentence-level runs, measured to translate as well
as at medium) and gpt-6.1-sol (low for questions; medium for quizzes, whose
answer key must be right, and for reviews of the whole paper). A model Codex no longer offers falls back to Codex's default.

§5.2 The engine is one class behind an interface, exactly as in Lecture Saver,
so an OpenAI-API engine or a local model is an added file rather than a change.

§5.3 Translation sends the paper's *text* to OpenAI. This is stated plainly
before the first run. The only other thing that goes is a capture the reader
explicitly attached to a question (`codex exec --image`), the text of a file
the reader explicitly attached to the conversation (§4b.3), and, for a quiz, the
slide's text, its matched STT excerpt and the style examples — plus the
slide's picture only if the reader turned that on. A review or Cornell notes
(§4d) sends all of the paper's text again - figure, table and code text
included, and lines the layout could not place read from the page - without
its reference list and page numbers, and the registered lecture STT with it. The PDF itself and the reader's notes never go to OpenAI;
a note goes to Notion only on export. Translation, dictionary, quiz and
review runs leave nothing on disk (`--ephemeral`); the
session file Codex keeps for a GPT conversation is deleted when the reader
starts a new conversation or takes off a file it was sent.

§5.4 Results are written to disk per page as they arrive. Reopening a paper
that was translated last week is instant and costs nothing, and a translation
interrupted halfway resumes where it stopped.

§5.5 Usage is spent on the answer more than the question, so a batch is
asked for and answered as numbered lines (`[3] 번역`), not JSON keyed by
sentence id. Codex's own flags stay as they are: trimming its instructions or
tools breaks the prompt cache it shares, and costs more (measured).
An opt-in read-ahead translates only the page in view and a few after it,
following the reader; "번역 시작" and a translated-PDF export still translate
the whole paper.

---

## 6. PROJECT FOLDER

    <Projects>/library.json     the start screen's folders, in order
    <Projects>/<paper title>/
        paper.json          source, page count, page sizes, folder
        highlights.json     highlighted sentences and strokes (the bookmarks)
        paper.pdf           the PDF, copied in (papers are small; be self-contained)
        layout.json         blocks, sentences, word boxes per page
        translation.json    sentence id → Korean, with engine and model
        notes.json          free memo + anchored notes
        wordbook.json       starred words
        chat.json           the GPT conversation
        captures/           captures attached to questions
        chat_files/         files attached to the conversation, with their text
        quiz.json           slide quizzes, answers, the list of quiz materials
        quiz/materials/     the STT and style references, copied in, with their text
        review.json         the one-page review and Cornell notes, as Markdown,
                            and the text of each sentence they cite
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
