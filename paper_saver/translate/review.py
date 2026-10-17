"""One-page reviews and Cornell notes, written by GPT through Codex (§4d).

    ReviewPanel ─(kind)─> ReaderView worker ─(ReviewRequest)─> CodexReviewGenerator
        ─> TranslationEngine.generate_json (codex exec --output-schema) ─> JSON
        ─> review_markdown (checked) ─> Markdown with citation links ─> ReviewStore

The paper goes whole - a review of one page of it would not be one - with
every sentence tagged ``[S12]``, and each point of the answer names the
sentences it rests on. That is what lets the reader click a point and see
where in the PDF it came from. When the lecture's STT is registered (one file
or several, for a lecture recorded in parts), what the professor said over
each page goes under that page, and a point taken from it alone says so.

Two modes, the reader's choice: 논문 모드 writes a paper review, 강의 모드
writes for the exam - templates that ask for the details a quiz asks about
(rules, conditions, differences, numbers) and a list of easily confused
points, weighted by what the professor spent time on and repeated: how long
each slide stayed on screen, how many times it was put back, and which of
its terms were said most. Measured 2026-10-17 by how many of the
professor's quiz questions the one-pager answers (stated, or its rule
stated): OS08 8.5/14 with the paper template (nothing from the 5-minute
enhanced-clock slide), 12 with slide time, 13 with repetition too; OS06,
which the prompt was not written against, 6.5/12, 11.5, 11 - one run each,
so a point either way is noise. The one-pager grew from about 2,300 to
3,900-4,100 characters.

The prompt is the paper first and what to write last, so the two documents
and a retry share one long prefix that OpenAI's prompt cache can serve.

An answer that fails the check is sent back once with the reason, as quizzes
are. A second failure is reported; nothing half-valid reaches the reader.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.lecture import (
    align_transcript,
    combine_transcripts,
    format_duration,
    parse_transcript,
    repeated_terms,
    seconds_by_page,
    transcript_by_page,
    visits_by_page,
)
from ..core.models import PaperLayout
from ..core.review import KINDS, ReviewFormatError, looks_like_slides, paper_text, review_markdown
from .engine import TranslationEngine, TranslationError
from .prompts import _json_payload

log = logging.getLogger(__name__)

__all__ = [
    "SCHEMAS", "CodexReviewGenerator", "ReviewRequest", "default_lecture", "page_notes", "parse_review_answer",
    "review_prompt", "review_request",
]


@dataclass(frozen=True)
class ReviewRequest:
    #: "one_page" or "cornell".
    kind: str
    title: str
    #: The paper, tagged (``core.review.paper_text``).
    text: str
    #: Tag number -> sentence id.
    numbering: Mapping[int, str] = field(default_factory=dict)
    #: Sentence id -> its page (from 0), for the citation labels.
    pages: Mapping[str, int] = field(default_factory=dict)
    #: The names of the lecture STT files whose text is in ``text``, in order.
    transcripts: tuple[str, ...] = ()
    #: A lecture (slides, or a document with its STT) rather than a paper:
    #: written for the exam, not as a reader's review.
    lecture: bool = False
    #: Page (from 0) -> seconds the lecture stayed on it, from the STT.
    seconds: Mapping[int, int] = field(default_factory=dict)
    #: Page (from 0) -> times it was put on screen.
    visits: Mapping[int, int] = field(default_factory=dict)
    #: The slides' terms the professor kept saying: (term, times, pages).
    terms: tuple[tuple[str, int, int], ...] = ()
    #: The professor's earlier quizzes (quiz tab, ③): what kind of detail is asked.
    styles: str = ""


#: One point and the sentences it rests on. Strict-mode rules apply: every
#: property required, no others allowed.
_POINT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "sources"],
    "properties": {"text": {"type": "string"}, "sources": {"type": "array", "items": {"type": "integer"}}},
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "one_page": {
        "type": "object",
        "additionalProperties": False,
        "required": ["one_liner", "sections"],
        "properties": {
            "one_liner": _POINT,
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["heading", "points"],
                    "properties": {"heading": {"type": "string"}, "points": {"type": "array", "items": _POINT}},
                },
            },
        },
    },
    "cornell": {
        "type": "object",
        "additionalProperties": False,
        "required": ["rows", "summary"],
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["cue", "notes"],
                    "properties": {"cue": {"type": "string"}, "notes": {"type": "array", "items": _POINT}},
                },
            },
            "summary": {"type": "array", "items": _POINT},
        },
    },
}


_RULES = """당신은 학술 논문(또는 강의 자료)을 공부하는 사람을 돕는 연구 조교입니다.
아래 [본문]만을 근거로, 맨 끝에 적힌 문서를 한국어로 쓰세요.

근거 규칙 (가장 중요):
1. [본문]의 문장마다 [S번호]가 붙어 있습니다. 모든 항목(text)마다 그 내용이 나온 본문 문장의
   번호를 sources에 1~3개 넣으세요(예: [S12]는 12). 가장 직접적인 문장만, 본문에 있는 번호만 씁니다.
   번호를 text 안에 쓰지는 마세요. 독자는 항목을 눌러 그 문장을 원문에서 확인합니다.
   "(그림·표 속 글)"(그림·표·코드 안의 글)과 "(따로 읽은 글)"(쪽에서 따로 읽어 낸 글) 아래에는 번호가
   없습니다. 그 내용도 문서의 일부이니 빠뜨리지 말고, 근거로 쓸 때는 같은 쪽의 가장 가까운 [S번호]
   (슬라이드·절 제목, 그림 캡션, 앞뒤 문장)를 sources에 넣으세요.
2. 본문에 없는 내용, 일반 지식, 추측을 사실처럼 쓰지 마세요. 본문을 넘어선 당신의 평가(한계, 의문 등)는
   따로 한 항목으로 쓰고 "(평가)"로 시작하세요. 본문의 사실과 한 항목에 섞지 마세요.
   그 평가가 기대는 문장이 있으면 sources에 넣으세요.
3. 숫자, 데이터셋·모델 이름, 수치 결과는 본문 그대로 옮기세요.
4. 전문 용어는 한국어로 쓰되 처음 나올 때 괄호 안에 영어를 병기하세요. 약어는 그대로 둡니다.
   수식은 LaTeX로($...$) 씁니다. 본문의 [수식]은 원문 속 수식이 있던 자리입니다.
5. 항목 하나는 한두 문장으로 짧고 정확하게. "~다." 체로 쓰거나 명사형으로 끝냅니다.
   강조가 필요하면 **굵게**를 쓸 수 있습니다.
6. 파일을 읽거나 명령을 실행하지 마세요. 필요한 것은 모두 이 메시지 안에 있습니다.
7. 출력은 JSON 객체 하나뿐입니다. 설명, 코드 펜스, 인사말을 붙이지 마세요.
"""

_SPOKEN = """강의 STT 규칙 (이 문서에는 교수님 강의를 받아 적은 STT가 함께 있습니다):
- 각 쪽 아래 "(강의 STT — 이 쪽을 설명한 부분)"은 그 쪽을 띄워 놓고 교수님이 말한 내용을 음성 인식으로
  받아 적은 글입니다. 영상 {count}개를 강의 순서대로 이었습니다: {names}.
  음성 인식이라 오탈자가 있으니 뜻으로 읽으세요.
- 슬라이드에 없는 교수님의 설명·이유·예시·주의할 점, 특히 강조하거나 반복하거나 시험과 관련해 말한
  내용을 반영하세요. STT에서만 나온 항목은 "(강의)"로 시작하고, sources에는 그 STT가 붙은 쪽의
  가장 가까운 [S번호](보통 슬라이드 제목)를 넣으세요.
- 출석, 과제 공지, 잡담처럼 내용과 무관한 이야기는 빼세요.
"""

_LECTURE = """강의 자료 규칙 (이 문서는 강의이고, 목적은 시험 대비입니다. 가장 중요):
- 시험은 개요가 아니라 세부를 묻습니다: 알고리즘·기법의 판단 기준과 단계, 조건이 바뀌면 결과가 어떻게
  달라지는지, 비슷한 두 개념의 차이, 경계값·순서·주체, 예제의 조건과 수치. 이런 세부를 담으세요.
  개요나 "~가 중요하다" 같은 일반론, 당신의 평가("(평가)" 항목)는 쓰지 마세요.
- 이름이 붙은 알고리즘·기법·메커니즘·그 변형·시스템 구성 요소·함수·옵션은 하나도 빠뜨리지 말고 각각
  이름과 동작 규칙을 쓰세요. 비슷한 것들을 한 항목에 뭉뚱그리지 말고, 무엇이 다른지 드러나게 쓰세요.
- 예제는 조건(입력, 크기, 개수)과 결과 수치를 함께 쓰세요.
- 같은 제목으로 여러 쪽에 걸쳐 설명한 주제일수록 비중이 크니, 그 쪽들에만 있는 규칙과 예시를
  빠짐없이 담으세요.
"""

_IMPORTANCE = """- 무엇이 중요한지는 교수님이 그 내용에 쓴 시간과 반복한 횟수로 판단합니다. 쪽 머리의
  "(강의 N분 M초 · K번 띄움)"은 그 쪽을 설명한 시간과 화면에 띄운 횟수이고, 아래 [강의에서 중요하게
  다룬 곳]은 오래 설명한 쪽, 여러 번 다시 띄운 쪽, 강의에서 여러 번 말한 용어의 순위입니다.
  여기에 오른 쪽과 용어는 하나도 빠뜨리지 말고, 많이 오를수록 항목을 많이 주어 그 규칙과 예시를 담으세요.
  짧게 한 번 지나간 내용은 줄이거나 빼도 됩니다. 용어 횟수는 음성 인식으로 센 것이라 대략적입니다.
"""

_ONE_PAGE = """[쓸 문서: 한 장 요약 (one-page review)]
문서 전체를 A4 한 장(한국어 1,500자 안팎)에 담는 리뷰입니다.
- one_liner: 이 문서가 무엇을 했고 무엇을 보였는지 한 문장.
- sections: 아래 소제목을 이 순서로 쓰고, 소제목마다 points 2~4개.
  1. 문제와 동기  2. 핵심 아이디어  3. 방법  4. 실험과 결과  5. 기여  6. 한계와 의문  7. 기억할 점
  '실험과 결과'에는 가장 중요한 수치를 넣고, '기억할 점'은 읽은 사람이 남겨야 할 메시지입니다.

출력 형식:
{"one_liner": {"text": "...", "sources": [3]},
 "sections": [{"heading": "문제와 동기", "points": [{"text": "...", "sources": [5, 6]}, ...]}, ...]}
"""

_CORNELL = """[쓸 문서: 코넬 노트 (Cornell notes)]
왼쪽 단서(cue) 칸, 오른쪽 노트 칸, 아래 요약 칸으로 된 복습용 노트입니다.
- rows: 문서의 흐름(읽는 순서)대로 8~12개. 문서 전체를 고르게 다루세요.
  - cue: 복습할 때 노트를 가리고 스스로 답해 볼 질문이나 핵심어. 되도록 질문형, 25자 안팎.
    (예: "왜 순환 구조 대신 self-attention을 쓰는가?")
  - notes: 그 단서에 답하는 노트 2~4개. 정의, 이유, 방법의 핵심 단계, 수치 결과, 예시.
- summary: 문서 전체를 자기 말로 정리한 요약 3~5문장. 문장 하나가 항목 하나입니다.

출력 형식:
{"rows": [{"cue": "...?", "notes": [{"text": "...", "sources": [12]}, ...]}, ...],
 "summary": [{"text": "...", "sources": [2]}, ...]}
"""

_LECTURE_ONE_PAGE = """[쓸 문서: 한 장 요약 — 강의, 시험 대비]
강의 전체를 A4 한 장에 담는 시험 대비 요약입니다. 넓게 훑는 개요가 아니라 문제로 나올 세부를
촘촘하게 담되, 한 장을 넘기지 마세요: 모든 text를 합쳐 한국어 2,500~3,000자 안쪽입니다.
- one_liner: 이 강의가 다룬 것을 한 문장으로.
- sections: 강의 순서대로 주제별 소제목 5~7개, 소제목마다 points 3~5개.
  point 하나는 시험에 나올 진술 하나(정의와 조건, 판단 규칙, 단계, 차이, 수치)이며 한 문장, 50자 안팎.
  연도, 사람·회사·제품 이름, 역사, 곁가지 사례처럼 시험에 나오지 않을 것은 빼세요.
  자리가 모자라면 교수님이 짧게 한 번 지나간 내용부터 덜어 내고, 오래 설명하거나 반복한 내용의 규칙은 남기세요.
- 마지막 소제목은 "헷갈리기 쉬운 점"이고 points 5~8개입니다. 새 사실을 늘어놓는 곳이 아니라, 위 내용
  가운데 참과 거짓이 갈리기 쉬운 지점(비슷한 용어, 경계 조건, 바뀌기 쉬운 숫자·순서·주체)을 바른
  진술로 짚는 곳입니다. (예: "A는 X일 때가 아니라 Y일 때 ~한다", "A가 아니라 B를 줄인다")

출력 형식:
{"one_liner": {"text": "...", "sources": [3]},
 "sections": [{"heading": "...", "points": [{"text": "...", "sources": [5, 6]}, ...]}, ...,
              {"heading": "헷갈리기 쉬운 점", "points": [...]}]}
"""

_LECTURE_CORNELL = """[쓸 문서: 코넬 노트 — 강의, 시험 대비]
왼쪽 단서(cue) 칸, 오른쪽 노트 칸, 아래 요약 칸으로 된 복습용 노트입니다.
- rows: 강의 순서대로 주제마다 하나, 강의 분량에 맞춰 10~16개. 오래 설명한 주제는 여러 행으로 나누세요.
  - cue: 노트를 가리고 스스로 답해 볼 질문. 답이 하나로 정해지는 시험 문제처럼, 25자 안팎.
    (예: "X가 Y를 고르는 기준은?", "A와 B는 무엇이 다른가?")
  - notes: 그 단서에 답하는 노트 2~5개. 판단 규칙과 단계, 조건, 차이, 예제의 수치.
- summary: 강의 전체를 정리한 요약 3~5문장. 문장 하나가 항목 하나입니다.

출력 형식:
{"rows": [{"cue": "...?", "notes": [{"text": "...", "sources": [12]}, ...]}, ...],
 "summary": [{"text": "...", "sources": [2]}, ...]}
"""

_ASK = {
    ("one_page", False): _ONE_PAGE,
    ("cornell", False): _CORNELL,
    ("one_page", True): _LECTURE_ONE_PAGE,
    ("cornell", True): _LECTURE_CORNELL,
}
#: The pages ranked by lecture time that the prompt names: the top third.
_RANKED_SHARE = 3
#: The professor's earlier quizzes sent with a lecture, at most.
STYLE_CHARS = 6000


def _signals(request: ReviewRequest) -> bool:
    return bool(request.seconds or request.visits or request.terms)


def _importance(request: ReviewRequest) -> str:
    """Where the professor spent time, what they came back to, what they kept saying."""
    lines = ["[강의에서 중요하게 다룬 곳 — 교수님이 쓴 시간과 반복]"]
    if request.seconds:
        ranked = sorted(request.seconds.items(), key=lambda item: -item[1])
        top = ranked[: max(5, len(ranked) // _RANKED_SHARE)]
        lines.append("- 오래 설명한 쪽: " + ", ".join(f"p.{page + 1} {format_duration(s)}" for page, s in top))
    shown = sorted(((page, n) for page, n in request.visits.items() if n >= 2), key=lambda item: -item[1])
    if shown:
        lines.append("- 여러 번 다시 띄운 쪽: " + ", ".join(f"p.{page + 1} {n}번" for page, n in shown[:15]))
    if request.terms:
        lines.append(
            "- 강의에서 여러 번 말한 용어: " + ", ".join(f"{term} {n}회({p}개 쪽)" for term, n, p in request.terms)
        )
    return "\n".join(lines) + "\n"


def page_notes(seconds: Mapping[int, int], visits: Mapping[int, int]) -> dict[int, str]:
    """What each page's mark says of the lecture: "강의 5분 26초 · 6번 띄움"."""
    notes: dict[int, str] = {}
    for page in sorted(set(seconds) | set(visits)):
        parts = [f"강의 {format_duration(seconds[page])}"] if seconds.get(page) else []
        if visits.get(page, 0) >= 2:
            parts.append(f"{visits[page]}번 띄움")
        if parts:
            notes[page] = " · ".join(parts)
    return notes


def review_prompt(request: ReviewRequest, *, retry_reason: str = "") -> str:
    parts = [_RULES]
    if request.transcripts:
        names = ", ".join(f"영상 {n} = {name}" for n, name in enumerate(request.transcripts, start=1))
        parts.append(_SPOKEN.format(count=len(request.transcripts), names=names))
    if request.lecture:
        parts.append(_LECTURE + (_IMPORTANCE if _signals(request) else ""))
    if request.title:
        parts.append(f"문서 제목: {request.title}\n")
    if request.lecture and _signals(request):
        parts.append(_importance(request))
    parts.append("[본문] (쪽마다 [p.번호]로 시작합니다)")
    parts.append(f"<<<\n{request.text.strip()}\n>>>\n")
    if request.lecture and request.styles.strip():
        styles = request.styles.strip()
        if len(styles) > STYLE_CHARS:
            styles = styles[:STYLE_CHARS] + " …"
        parts.append(
            "[교수님 이전 퀴즈 — 다른 강의일 수 있습니다] 교수님이 어떤 종류의 세부(조건, 경계, 순서, 수치, 차이)를 "
            "묻는지 보고, 이 강의에서 같은 종류의 세부를 놓치지 마세요. 이 퀴즈의 내용을 문서에 옮기지는 마세요."
        )
        parts.append(f"<<<\n{styles}\n>>>\n")
    parts.append(_ASK[(request.kind, request.lecture)])
    if retry_reason:
        parts.append(f"직전 답은 형식 검사를 통과하지 못했습니다: {retry_reason}\n규칙을 모두 지켜 처음부터 다시 쓰세요.\n")
    parts.append(f"{KINDS[request.kind]} JSON:")
    return "\n".join(parts)


def review_request(
    kind: str,
    title: str,
    layout: PaperLayout,
    *,
    strays: Mapping[int, list[str]] | None = None,
    transcripts: Sequence[tuple[str, str]] = (),
    slides: Sequence[str] = (),
    styles: str = "",
    lecture: bool | None = None,
    until_references: bool = False,
) -> ReviewRequest:
    """Everything one document is asked from: the text with each page's STT
    under it, and, for a lecture, where the professor spent time and what
    they repeated.

    ``transcripts`` is the lecture's STT as (file name, text) in lecture
    order, matched to ``slides`` (each page's text); ``styles`` the
    professor's earlier quizzes, sent with a lecture only. ``lecture`` is
    the mode the reader chose; ``None`` asks :func:`default_lecture`.
    """
    if lecture is None:
        lecture = default_lecture(layout, bool(transcripts))
    spoken: dict[int, str] = {}
    seconds: dict[int, int] = {}
    visits: dict[int, int] = {}
    terms: list[tuple[str, int, int]] = []
    if transcripts:
        sections = combine_transcripts([parse_transcript(text) for _, text in transcripts])
        mapping = align_transcript(slides, sections)
        spoken = transcript_by_page(slides, sections, mapping=mapping)
        if lecture:
            seconds = seconds_by_page(slides, sections, mapping)
            visits = visits_by_page(sections, mapping)
            terms = repeated_terms(slides, sections, mapping)
    text, numbering = paper_text(
        layout, strays=strays, spoken=spoken, notes=page_notes(seconds, visits), until_references=until_references
    )
    return ReviewRequest(
        kind, title, text, numbering, {s.id: page.number for page, _, s in layout.sentences()},
        transcripts=tuple(name for name, _ in transcripts), lecture=lecture, seconds=seconds, visits=visits,
        terms=tuple(terms), styles=styles if lecture else "",
    )


def default_lecture(layout: PaperLayout, has_transcript: bool) -> bool:
    """The mode a paper starts in: a lecture if it has its STT or is slides."""
    return has_transcript or looks_like_slides(layout)


def parse_review_answer(answer: str, request: ReviewRequest) -> str:
    """Codex's answer as the document's Markdown. Raises :class:`ReviewFormatError`."""
    payload = _json_payload(answer)
    if payload is None:
        raise ReviewFormatError("JSON을 찾지 못했습니다.")
    return review_markdown(
        request.kind, payload, numbering=request.numbering, page_of=request.pages.get, title=request.title
    )


class CodexReviewGenerator:
    """Writes reviews through a :class:`TranslationEngine` - the Codex CLI -
    sharing its discovery, sign-in errors, timeout and cancellation."""

    def __init__(self, engine: TranslationEngine, *, attempts: int = 2) -> None:
        self.engine = engine
        self.attempts = max(1, attempts)

    def generate(self, request: ReviewRequest, *, cancel: threading.Event) -> str:
        """The document's Markdown, or :class:`TranslationError`."""
        if request.kind not in SCHEMAS:
            raise TranslationError(f"알 수 없는 문서 종류입니다: {request.kind}")
        reason = ""
        for attempt in range(1, self.attempts + 1):
            prompt = review_prompt(request, retry_reason=reason)
            answer = self.engine.generate_json(prompt, SCHEMAS[request.kind], cancel=cancel)
            try:
                return parse_review_answer(answer, request)
            except ReviewFormatError as exc:
                reason = str(exc)
                log.info("%s rejected (try %s): %s", request.kind, attempt, reason)
        raise TranslationError(
            f"GPT가 만든 문서({KINDS[request.kind]})가 형식 검사를 통과하지 못했습니다.\n({reason})\n\n다시 시도해 주세요."
        )
