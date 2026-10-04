"""What is asked of GPT, and how its answer is read back.

One request per batch of sentences, each sentence numbered, and the answer
asked for as one line per sentence under the same number: ``[3] 번역``. The
numbers are what make the highlight pairs possible (§2.2): a translation that
merely came back "in order" would drift by one the first time GPT merged two
short sentences, and every highlight after that point would be wrong. Number
``n`` is the ``n``-th sentence of the batch, so the caller hands the parser
the same ids, in the same order, that it built the prompt from.

Numbered lines rather than JSON keyed by sentence id: the answer is most of
what a translation costs, and repeating ``{"id": "p3b12s4", "ko": ...}`` for
every sentence made it 10-14% longer (measured 2026-10-04, same translations,
nothing dropped).

The parser is deliberately forgiving about the *envelope* - code fences, a
sentence of chatter before the lines, a translation wrapped onto a second
line, or an answer in the old JSON shape - and strict about the *content*: a
number that was not asked for is dropped, and one that is missing is simply
not translated yet, so the next run picks it up.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "SentenceRequest",
    "batch_prompt",
    "chat_followup_prompt",
    "chat_prompt",
    "lookup_prompt",
    "parse_batch",
    "parse_lookup",
    "sentence_tag",
]


@dataclass(frozen=True, slots=True)
class SentenceRequest:
    id: str
    text: str
    #: "body", "heading", "caption" or "title" - headings are translated as
    #: headings (no sentence-final 다), captions keep their "Figure 3:" label.
    kind: str = "body"


_RULES = """\
당신은 학술 논문 전문 번역가입니다. 아래 영어 문장들을 한국어로 **직역**하세요.

규칙:
1. 번호마다 정확히 하나의 번역을 같은 번호로 돌려주세요. 문장을 합치거나 나누지 마세요.
2. 직역: 원문의 구조와 어순을 최대한 살리고, 내용을 더하거나 빼거나 요약하지 마세요.
3. ⟦m1⟧ 같은 표식은 수식입니다. 글자 하나 바꾸지 말고 문장 속 알맞은 자리에 그대로 두세요.
4. 인용 번호([12], [3, 4]), 식 번호((5)), 그림·표 번호(Fig. 3, Table 2)는 원문 표기 그대로 두세요.
5. 전문 용어는 학계에서 통용되는 한국어 용어를 쓰고, 처음 나올 때만 괄호 안에 영어를 병기하세요.
   예: 그래프 신경망(graph neural network). 약어(EEG, GCN, SEED 등)는 그대로 두세요.
6. 본문 문장은 "~다." 체로 끝내세요. (heading)/(title) 표시가 붙은 것은 명사형 제목으로 옮기세요.
7. (caption) 표시가 붙은 것은 앞의 "Fig. 1:" / "TABLE 2:" 같은 머리표를 그대로 두고 뒤만 번역하세요.
   (heading)/(title)/(caption) 표시 자체는 번역에 넣지 마세요.
8. 출력은 번역 줄뿐입니다. 설명, 코드 펜스, 인사말을 붙이지 마세요.

출력 형식 (한 줄에 한 문장, 번호 뒤에 번역):
[1] <번역>
[2] <번역>
"""


def batch_prompt(sentences: Sequence[SentenceRequest], *, context: str = "") -> str:
    """The prompt for one batch. ``context`` is the paper's title, if known.

    Sentence ``n`` (from 1) is numbered ``[n]``; :func:`parse_batch` must be
    given the ids of ``sentences`` in this order.
    """
    parts = [_RULES]
    if context:
        parts.append(f"논문 제목(참고용, 번역하지 마세요): {context}\n")
    parts.append("번역할 문장:")
    for number, sentence in enumerate(sentences, 1):
        tag = "" if sentence.kind == "body" else f"({sentence.kind}) "
        parts.append(f"[{number}] {tag}{' '.join(sentence.text.split())}")
    return "\n".join(parts)


def _json_payload(answer: str) -> object | None:
    """The first JSON object or array in ``answer``, however it is wrapped."""
    text = answer.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start == -1 or end <= start:
            continue
        chunk = text[start : end + 1]
        for candidate in (chunk, re.sub(r",\s*([}\]])", r"\1", chunk)):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    return None


#: "[3] 번역" at the start of a line (or "**[3]**", which GPT slips into now
#: and then). Brackets only: "3." would also match "3.5%의 정확도".
_NUMBERED = re.compile(r"^\s*(?:\*\*)?\[(\d{1,4})\](?:\*\*)?(\s*)(.*)$")
_LABEL = re.compile(r"^\((?:heading|title|caption|body)\)\s*", re.IGNORECASE)


def _parse_lines(answer: str, expected: Sequence[str]) -> dict[str, str]:
    """``[n] 번역`` lines, the format :func:`batch_prompt` asks for."""
    found: dict[int, list[str]] = {}
    #: The sentence a wrapped line continues, and the last number seen.
    current: int | None = None
    last = 0
    for line in answer.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("```"):
            current = None
            continue
        match = _NUMBERED.match(stripped)
        number = int(match.group(1)) if match else 0
        # Answers come in order, so a number that goes backwards or repeats
        # is text - a "[4]" citation opening a wrapped line - not a sentence.
        if match and last < number <= len(expected):
            current = last = number
            found[number] = [_LABEL.sub("", match.group(3))]
        elif match and match.group(2):
            # "[2] ..." again, or a number not asked for: an answer line that
            # is not wanted, never the end of the one before. (A citation
            # opening a wrapped line, "[4]에서", has no space after it.)
            current = None
        elif current is not None:
            # A translation wrapped onto the next line.
            found[current].append(stripped)
    result: dict[str, str] = {}
    for number, pieces in found.items():
        text = " ".join(" ".join(pieces).split())
        if text:
            result[expected[number - 1]] = text
    return result


def parse_batch(answer: str, expected: Sequence[str]) -> dict[str, str]:
    """``{sentence id: Korean}`` for every expected sentence the answer covered.

    ``expected`` is the batch's ids in the order :func:`batch_prompt` numbered
    them. An answer in the JSON shape of earlier versions is still read.
    """
    if not answer.lstrip().startswith(("{", "[{", "```json")):
        lines = _parse_lines(answer, expected)
        if lines:
            return lines
    wanted = set(expected)
    payload = _json_payload(answer)
    items: list[object] = []
    if isinstance(payload, dict):
        found = payload.get("translations")
        if isinstance(found, list):
            items = found
        else:
            # {"p3b2s1": "...", ...} - a shape GPT sometimes prefers.
            items = [{"id": k, "ko": v} for k, v in payload.items()]
    elif isinstance(payload, list):
        items = payload

    result: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        sentence_id = str(item.get("id", "")).strip()
        text = item.get("ko") or item.get("text") or item.get("translation")
        if sentence_id in wanted and isinstance(text, str) and text.strip():
            result[sentence_id] = " ".join(text.split())
    return result


# -- word lookup (§3.1) ---------------------------------------------------------

_LOOKUP_RULES = """\
당신은 영한 학술 사전입니다. 아래 문장 속 단어의 뜻을 한국어로 알려주세요.
단어: {word}
문장: {sentence}

출력은 JSON 하나뿐입니다(코드 펜스·설명 금지):
{{"lemma": "<사전 표제어(원형)>",
  "pos": "<품사, 한국어로 예: 명사/동사/형용사>",
  "ipa": "<발음기호, 모르면 빈 문자열>",
  "in_context": "<이 문장에서의 뜻, 한국어 한 줄>",
  "senses": ["<일반적인 뜻 1>", "<뜻 2>", "<뜻 3까지>"],
  "note": "<이 분야에서의 쓰임이나 헷갈리기 쉬운 점, 없으면 빈 문자열>"}}
"""


def lookup_prompt(word: str, sentence: str) -> str:
    return _LOOKUP_RULES.format(word=word, sentence=sentence.strip()[:600])


def parse_lookup(answer: str) -> dict[str, object] | None:
    payload = _json_payload(answer)
    if not isinstance(payload, dict) or not payload.get("in_context"):
        return None
    senses = payload.get("senses")
    return {
        "lemma": str(payload.get("lemma") or ""),
        "pos": str(payload.get("pos") or ""),
        "ipa": str(payload.get("ipa") or ""),
        "in_context": str(payload.get("in_context") or ""),
        "senses": [str(s) for s in senses][:4] if isinstance(senses, list) else [],
        "note": str(payload.get("note") or ""),
    }


# -- questions about the paper -----------------------------------------------------

_CHAT_RULES = """당신은 학술 논문을 읽고 있는 사람을 돕는 연구 조교입니다. 독자의 질문에 한국어로 답하세요.

규칙:
1. 아래 논문 본문과 첨부 이미지(독자가 논문 화면에서 잘라낸 캡처)를 근거로 답하세요.
   논문에 없는 내용을 보충할 때는 그렇다고 밝히세요. 모르면 모른다고 하세요.
2. 캡처가 있으면 먼저 그 안의 그림·표·수식을 정확히 읽고, 그것을 짚어 가며 설명하세요.
3. 수식은 LaTeX로 쓰세요: 문장 속은 $...$, 따로 쓰는 식은 $$...$$.
4. 답은 Markdown으로, 핵심부터 간결하게. 필요할 때만 목록과 소제목을 쓰세요.
5. 전문 용어는 한국어로 쓰되 처음 나올 때 괄호 안에 영어를 병기하세요.
6. 파일을 읽거나 명령을 실행하지 마세요. 필요한 것은 모두 이 메시지 안에 있습니다.
"""

_CHAT_CITE = """7. 본문의 문장마다 [S번호] 표시가 붙어 있습니다. 답의 근거가 된 본문 문장이 있으면
   그 내용을 말한 곳 바로 뒤에 그 표시를 그대로 붙이세요 (예: 손실을 이렇게 정의합니다 [S12]).
   여러 문장이면 [S12][S13]처럼 따로 쓰세요. 근거가 되는 문장만, 본문에 있는 번호만 쓰세요.
"""


#: Said once, at the start of a session, before its first material.
_SESSION_NOTE = (
    "이 대화에서는 본문과 자료를 한 번씩만 보냅니다. 앞에서 보낸 본문·자료와 [S번호]는 "
    "대화가 끝날 때까지 그대로 유효하니, 다음 질문에서도 근거로 쓰세요."
)


def chat_followup_prompt(
    question: str, *, context: Sequence[str] = (), image_notes: Sequence[str] = ()
) -> str:
    """A later question in the same session: only what is new, then the question.

    The rules, the material sent before and the conversation so far are all
    in the session already.
    """
    parts: list[str] = []
    if context:
        parts.append("새로 보내는 본문·자료 (앞에서 보낸 것에 더해집니다):\n\n" + "\n\n".join(context) + "\n")
    if image_notes:
        parts.append("첨부 이미지 (첨부된 순서대로):")
        parts += [f"{index}. {note}" for index, note in enumerate(image_notes, start=1)]
        parts.append("")
    parts.append("[독자의 새 질문]\n" + (question.strip() or "첨부한 캡처를 설명해 주세요."))
    return "\n".join(parts)


def sentence_tag(number: int) -> str:
    """How sentence ``number`` of the page is marked for GPT to cite."""
    return f"[S{number}]"


#: Caps that keep a long conversation from growing the prompt without bound.
CHAT_PAGE_CHARS = 7000
#: The whole paper, when the reader asks with every page: a long paper with
#: its references is 100-200k characters, well inside what Codex takes.
CHAT_PAPER_CHARS = 400_000
#: Every open paper together; each gets an equal share (see ReaderView).
CHAT_PAPERS_CHARS = 800_000
#: One attached file, and all of them together.
CHAT_FILE_CHARS = 80_000
CHAT_FILES_CHARS = 240_000
CHAT_HISTORY_TURNS = 10
CHAT_TURN_CHARS = 2500


def _documents(documents: Sequence[tuple[str, str]]) -> str:
    """The files the reader attached, each cut to its share of the room."""
    parts = [
        "독자가 함께 보낸 자료 (논문 본문과 구별해서 쓰고, 자료를 근거로 할 때는 "
        "[자료 1]처럼 어느 자료인지 밝히세요):"
    ]
    room = CHAT_FILES_CHARS
    for index, (name, text) in enumerate(documents, start=1):
        text = text.strip()
        limit = min(CHAT_FILE_CHARS, max(0, room))
        if len(text) > limit:
            text = text[:limit] + " …(이하 생략)"
        room -= len(text)
        parts.append(f"[자료 {index}] {name}\n<<<\n{text or '(읽을 수 있는 글자가 없습니다)'}\n>>>\n")
    return "\n".join(parts)


def chat_prompt(
    question: str,
    *,
    title: str = "",
    page_label: str = "",
    page_text: str = "",
    history: Sequence[tuple[str, str]] = (),
    image_notes: Sequence[str] = (),
    cite: bool = False,
    whole_paper: bool = False,
    several_papers: bool = False,
    documents: Sequence[tuple[str, str]] = (),
    context: Sequence[str] = (),
) -> str:
    """One question, with the page in view and the conversation so far.

    ``history`` is ``(role, text)`` pairs, role "user" or "assistant", oldest
    first; only the last few turns are kept. ``image_notes`` describes the
    attached images in the order they are attached. With ``cite``, the page
    text carries :func:`sentence_tag` marks and GPT is asked to cite them.
    With ``whole_paper``, ``page_text`` is every page (each headed ``[p.N]``)
    and ``page_label`` is the page the reader is looking at; with
    ``several_papers`` it is every open paper, each headed ``[논문 N]``,
    the reader's own first. ``documents``
    is ``(name, text)`` for each file the reader attached, in order.
    """
    parts = [_CHAT_RULES + (_CHAT_CITE if cite and (page_text.strip() or context) else "")]
    if title:
        parts.append(f"논문 제목: {title}\n")
    if page_text.strip():
        text = page_text.strip()
        limit = CHAT_PAPERS_CHARS if several_papers else CHAT_PAPER_CHARS if whole_paper else CHAT_PAGE_CHARS
        if len(text) > limit:
            text = text[:limit] + " …(이하 생략)"
        if several_papers:
            looking = f" 독자는 지금 논문 1의 {page_label}을(를) 보고 있습니다." if page_label else ""
            parts.append(
                "독자가 열어 둔 논문 전체의 본문 (논문마다 [논문 번호] 제목으로, 쪽마다 [p.번호]로 시작합니다)."
                f"{looking} 논문을 함께 묻는 질문이면 어느 논문의 내용인지 밝히며 답하세요:"
                f"\n<<<\n{text}\n>>>\n"
            )
        elif whole_paper:
            looking = f" 독자는 지금 {page_label}을(를) 보고 있습니다." if page_label else ""
            parts.append(
                f"논문 전체 본문 (쪽마다 [p.번호]로 시작합니다).{looking}"
                " 질문이 특정 쪽을 가리키지 않으면 논문 전체를 근거로 답하세요:"
                f"\n<<<\n{text}\n>>>\n"
            )
        else:
            where = f"({page_label})" if page_label else ""
            parts.append(f"독자가 보고 있는 쪽{where}의 본문:\n<<<\n{text}\n>>>\n")
    if context:
        parts.append(_SESSION_NOTE + "\n\n" + "\n\n".join(context) + "\n")
    if documents:
        parts.append(_documents(documents))
    turns = list(history)[-CHAT_HISTORY_TURNS:]
    if turns:
        parts.append("지금까지의 대화:")
        for role, text in turns:
            text = text.strip()
            if len(text) > CHAT_TURN_CHARS:
                text = text[:CHAT_TURN_CHARS] + " …"
            speaker = "독자" if role == "user" else "조교"
            parts.append(f"[{speaker}]\n{text}\n")
    if image_notes:
        parts.append("첨부 이미지 (첨부된 순서대로):")
        parts += [f"{index}. {note}" for index, note in enumerate(image_notes, start=1)]
        parts.append("")
    parts.append("[독자의 새 질문]\n" + (question.strip() or "첨부한 캡처를 설명해 주세요."))
    return "\n".join(parts)
