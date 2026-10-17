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

The prompt is the paper first and what to write last, so the two documents
and a retry share one long prefix that OpenAI's prompt cache can serve.

An answer that fails the check is sent back once with the reason, as quizzes
are. A second failure is reported; nothing half-valid reaches the reader.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..core.review import KINDS, ReviewFormatError, review_markdown
from .engine import TranslationEngine, TranslationError
from .prompts import _json_payload

log = logging.getLogger(__name__)

__all__ = ["SCHEMAS", "CodexReviewGenerator", "ReviewRequest", "parse_review_answer", "review_prompt"]


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

_ONE_PAGE = """[쓸 문서: 한 장 요약 (one-page review)]
문서 전체를 A4 한 장(한국어 1,500자 안팎)에 담는 리뷰입니다.
- one_liner: 이 문서가 무엇을 했고 무엇을 보였는지 한 문장.
- sections: 아래 소제목을 이 순서로 쓰고, 소제목마다 points 2~4개.
  1. 문제와 동기  2. 핵심 아이디어  3. 방법  4. 실험과 결과  5. 기여  6. 한계와 의문  7. 기억할 점
  '실험과 결과'에는 가장 중요한 수치를 넣고, '기억할 점'은 읽은 사람이 남겨야 할 메시지입니다.
  강의 자료처럼 이 틀이 맞지 않는 문서라면 내용에 맞는 소제목 5~7개
  (예: 핵심 개념, 정의와 원리, 예시, 주의할 점)를 쓰세요.

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

_ASK = {"one_page": _ONE_PAGE, "cornell": _CORNELL}


def review_prompt(request: ReviewRequest, *, retry_reason: str = "") -> str:
    parts = [_RULES]
    if request.transcripts:
        names = ", ".join(f"영상 {n} = {name}" for n, name in enumerate(request.transcripts, start=1))
        parts.append(_SPOKEN.format(count=len(request.transcripts), names=names))
    if request.title:
        parts.append(f"문서 제목: {request.title}\n")
    parts.append("[본문] (쪽마다 [p.번호]로 시작합니다)")
    parts.append(f"<<<\n{request.text.strip()}\n>>>\n")
    parts.append(_ASK[request.kind])
    if retry_reason:
        parts.append(f"직전 답은 형식 검사를 통과하지 못했습니다: {retry_reason}\n규칙을 모두 지켜 처음부터 다시 쓰세요.\n")
    parts.append(f"{KINDS[request.kind]} JSON:")
    return "\n".join(parts)


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
