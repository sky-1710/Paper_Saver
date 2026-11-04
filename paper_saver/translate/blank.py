"""백지 복습, written and marked by GPT through Codex (§4e).

    BlankPanel ─> ReaderView worker ─(ReviewRequest)─> CodexBlankGenerator
        ─> TranslationEngine.generate_json (codex exec --output-schema)
        ─> sheet_from_answer / grade_from_answer (checked) ─> BlankStore

Both runs are shown the PDF as a review is (``review_context``: every
sentence tagged ``[S12]``, the lecture's STT and its signals under the pages,
the professor's quizzes) and ask for something else at the end, so the
three share one long prefix for OpenAI's prompt cache.

The standard is the PDF. The answer key is made from the PDF's own text, its
figures and tables included - cut out of the page and attached as pictures
(``core.blank.figure_regions``), since a table pasted in as an image has no
text - and the STT only says what the professor stressed. Marking needs no
pictures: by then the key holds what they showed. The one-page review and the
Cornell notes are never sent: they are a summary of the PDF, and recall
held against a summary would leave out what the summary did.

An answer that fails the check is sent back once with the reason, as
reviews and quizzes are.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from ..core.blank import MARKS, Sheet, Wrong, answer_text, grade_from_answer, sheet_from_answer
from ..core.review import ReviewFormatError
from .engine import TranslationEngine, TranslationError
from .prompts import _json_payload
from .review import ReviewRequest, _signals, review_context

log = logging.getLogger(__name__)

__all__ = ["GRADE_SCHEMA", "SHEET_SCHEMA", "CodexBlankGenerator", "grade_prompt", "sheet_prompt"]


SHEET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["topics"],
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["heading", "keywords", "points"],
                "properties": {
                    "heading": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "points": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["text", "sources", "important"],
                            "properties": {
                                "text": {"type": "string"},
                                "sources": {"type": "array", "items": {"type": "integer"}},
                                "important": {"type": "boolean"},
                            },
                        },
                    },
                },
            },
        }
    },
}

GRADE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["marks", "wrong", "overall"],
    "properties": {
        "marks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["point", "status", "missing"],
                "properties": {
                    "point": {"type": "integer"},
                    "status": {"type": "string", "enum": list(MARKS)},
                    "missing": {"type": "string"},
                },
            },
        },
        "wrong": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["topic", "wrote", "correct", "sources"],
                "properties": {
                    "topic": {"type": "integer"},
                    "wrote": {"type": "string"},
                    "correct": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "integer"}},
                },
            },
        },
        "overall": {"type": "string"},
    },
}


_SHEET = """[쓸 문서: 백지 복습 문제지와 채점 기준]
백지 복습은 키워드만 보고 기억나는 것을 모두 백지에 쓴 뒤, 원본과 맞춰 보며 기억하지 못한 곳을 찾는
공부법입니다. 학생은 이 문제지를 보고 [본문]의 PDF를 보지 않은 채 통째로 써 봅니다.
그 문제지(주제와 키워드)와 채점 기준을 만드세요.
- 기준은 PDF입니다. 채점 기준(points)은 [본문]의 PDF 글(본문, 슬라이드, 그림·표·코드 속 글)에 있는 내용으로만
  만드세요. 요약이 아니라 PDF 자체를 덮어야 합니다. PDF에 있는데 어느 주제의 points에도 없는 세부가
  없도록 하세요.{spoken}
- topics: PDF 순서대로 주제 {topics}개. 분량이 많은 주제는 나누세요.
  - heading: 주제 이름만 짧게 (예: "Clock 알고리즘"). 답을 담지 마세요.
  - keywords: 쓰기 시작할 실마리 3~6개. 용어·이름만 씁니다. 정의, 설명, 숫자, 결과, 규칙은 넣지 마세요.
    키워드가 답을 알려 주면 백지 복습이 되지 않습니다. 하나에 25자 안팎.
    (좋은 예: "reference bit", "victim page"  나쁜 예: "reference bit가 0이면 교체")
    주제에 중요한 표나 그림이 있으면 "표: 알고리즘별 page fault 수", "그림: 주소 변환 과정"처럼 그 이름만
    키워드에 넣어, 학생이 그 표·그림을 기억으로 다시 그려 보게 하세요.
  - points: 채점 기준. 이 주제를 다 썼다면 들어 있어야 할 진술 {points}개, 하나에 한 문장 60자 안팎.
    {what}
    표와 그림도 PDF의 내용입니다: 표는 무엇을 무엇과 비교하는지와 칸의 값·순서, 그림은 구성 요소와
    그 사이의 흐름·관계를 진술로 쓰세요 (예: "표: FIFO는 page fault 9번, LRU는 8번").
    학생은 채점이 끝난 뒤에야 points를 봅니다.
    - important: {important} 전체 points의 3분의 1 이하로.
    - sources: 그 진술이 나온 [S번호] 1~3개.

출력 형식:
{{"topics": [{{"heading": "...", "keywords": ["...", "..."],
  "points": [{{"text": "...", "sources": [12], "important": true}}, ...]}}, ...]}}
"""

_SHEET_SPOKEN = """
  강의 STT는 무엇이 중요한지(important) 판단하고 슬라이드의 뜻을 이해하는 데만 쓰세요. STT에만 있고 PDF에
  없는 내용은 points에 넣지 마세요."""

_WHAT = {
    True: "정의와 조건, 판단 규칙과 단계, 비슷한 것과의 차이, 예제의 조건과 수치처럼 시험에 나올 세부를 담으세요.",
    False: "풀려는 문제, 핵심 아이디어, 방법의 단계, 주요 수치 결과, 한계처럼 논문을 이해했다면 쓸 수 있어야 할 것을 담으세요.",
}

_GRADE = """[할 일: 백지 복습 채점]
학생이 아래 주제와 키워드만 보고, PDF를 보지 않고 기억나는 대로 쓴 [학생 답안]을 [채점 기준]에 맞춰
채점하세요. 무엇이 맞는지는 [본문]의 PDF로 판단합니다.
- marks: [채점 기준]의 항목마다 정확히 하나, point에 항목 번호를 넣습니다. 채점할 번호: {numbers}.
  - "recalled": 그 진술의 핵심(규칙, 조건, 수치, 차이)을 맞게 썼다. 말이 다르거나, 영어와 한국어가
    섞이거나, 다른 주제 아래 썼어도 됩니다.
  - "partial": 일부만 썼거나 흐릿하게 썼다. missing에 빠진 부분을 짧게 쓰세요.
  - "missed": 쓰지 않았거나 틀리게 썼다. 키워드를 옮겨 적기만 한 것은 쓴 것이 아닙니다.
  - 학생은 표를 Markdown 표(| a | b |)로, 그림을 말이나 화살표(→)로 그려 쓸 수 있습니다. 담은 내용이
    기준과 맞으면 그 항목을 쓴 것으로 봅니다.
  - missing은 partial일 때만 쓰고, 나머지는 ""입니다.
- wrong: 학생이 쓴 진술 가운데 [본문]의 PDF와 어긋나는 것. 시험이라면 틀렸다고 할 것만 넣으세요.
  - topic: 그 진술이 놓인 주제 번호(모르면 0). wrote: 학생이 쓴 말 그대로, 짧게. correct: 바른 내용 한 문장.
  - sources: 바른 내용이 나온 [S번호] 1~3개.
  [본문]에 없다는 이유만으로, 또는 맞춤법이나 표현 때문에 틀렸다고 하지 마세요. 없으면 [].
- overall: 한국어 한두 문장. 무엇을 잘 기억했고, 시험 전에 무엇을 다시 봐야 하는지.
- 시험 채점처럼 공정하게: 너그럽게 봐주지도, 표현을 트집 잡지도 마세요.

출력 형식:
{{"marks": [{{"point": 1, "status": "recalled", "missing": ""}}, ...],
 "wrong": [{{"topic": 2, "wrote": "...", "correct": "...", "sources": [12]}}],
 "overall": "..."}}
"""


def _retry(reason: str) -> str:
    return f"직전 답은 형식 검사를 통과하지 못했습니다: {reason}\n규칙을 모두 지켜 처음부터 다시 쓰세요.\n" if reason else ""


_FIGURES = """[첨부 이미지] PDF의 그림·표 {count}개를 잘라 순서대로 붙였습니다: {where}.
이미지 속 표의 행·열과 값, 그림이 보여 주는 구조·흐름·관계도 PDF의 내용입니다. 시험에 나올 만한 것은
points에 넣고, sources에는 그 쪽의 가장 가까운 [S번호](캡션, 슬라이드·절 제목)를 넣으세요.
"""


def sheet_prompt(request: ReviewRequest, *, figures: Sequence[int] = (), retry_reason: str = "") -> str:
    """The request for the sheet; ``figures`` are the pages (from 0) of the
    pictures attached, in order."""
    lecture = request.lecture
    if lecture and _signals(request):
        important = "교수님이 오래 설명했거나 여러 번 다시 띄웠거나 반복해 말한 내용이면 true."
    elif lecture:
        important = "시험에 나올 가능성이 가장 큰 내용이면 true."
    else:
        important = "논문의 핵심 주장과 주요 결과이면 true."
    ask = _SHEET.format(
        spoken=_SHEET_SPOKEN if request.transcripts else "",
        topics="6~12" if lecture else "5~8",
        points="3~8" if lecture else "3~6",
        what=_WHAT[lecture],
        important=important,
    )
    attached = []
    if figures:
        where = ", ".join(f"이미지 {n} = p.{page + 1}" for n, page in enumerate(figures, start=1))
        attached.append(_FIGURES.format(count=len(figures), where=where))
    return "\n".join([*review_context(request), *attached, ask, _retry(retry_reason), "백지 복습 문제지 JSON:"])


def _key(sheet: Sheet, topics: Sequence[int]) -> str:
    lines = []
    for number in topics:
        topic = sheet.topic(number)
        if topic is None:
            continue
        lines.append(f"[주제 {topic.number}] {topic.heading} — 키워드: {' · '.join(topic.keywords)}")
        lines += [f"{point.number}. {point.text}" for point in topic.points]
        lines.append("")
    return "\n".join(lines).strip()


def grade_prompt(
    request: ReviewRequest, sheet: Sheet, topics: Sequence[int], written: str, *, retry_reason: str = ""
) -> str:
    numbers = ", ".join(str(p.number) for p in sheet.points(topics))
    parts = [
        *review_context(request),
        _GRADE.format(numbers=numbers),
        "[채점 기준] (학생에게는 보이지 않았습니다)",
        f"<<<\n{_key(sheet, topics)}\n>>>\n",
        "[학생 답안] (\"## 번호. 주제\" 아래에 쓴 것이 그 주제의 답입니다)",
        f"<<<\n{answer_text(written)}\n>>>\n",
        _retry(retry_reason),
        "백지 채점 JSON:",
    ]
    return "\n".join(parts)


class CodexBlankGenerator:
    """Writes the keyword sheet and marks attempts through a
    :class:`TranslationEngine` - the Codex CLI."""

    def __init__(self, engine: TranslationEngine, *, attempts: int = 2) -> None:
        self.engine = engine
        self.attempts = max(1, attempts)

    def sheet(
        self,
        request: ReviewRequest,
        *,
        figures: Sequence[tuple[int, Path]] = (),
        cancel: threading.Event,
    ) -> Sheet:
        """The checked sheet, or :class:`TranslationError`. ``figures`` are
        the PDF's figures and tables cut out as pictures: (page, PNG file)."""
        pages = [page for page, _ in figures]
        images = [path for _, path in figures]
        reason = ""
        for attempt in range(1, self.attempts + 1):
            prompt = sheet_prompt(request, figures=pages, retry_reason=reason)
            answer = self.engine.generate_json(prompt, SHEET_SCHEMA, images=images, cancel=cancel)
            try:
                payload = _json_payload(answer)
                if payload is None:
                    raise ReviewFormatError("JSON을 찾지 못했습니다.")
                return sheet_from_answer(payload, numbering=request.numbering, page_of=request.pages.get)
            except ReviewFormatError as exc:
                reason = str(exc)
                log.info("Blank sheet rejected (try %s): %s", attempt, reason)
        raise TranslationError(f"GPT가 만든 키워드 시트가 형식 검사를 통과하지 못했습니다.\n({reason})\n\n다시 시도해 주세요.")

    def grade(
        self, request: ReviewRequest, sheet: Sheet, topics: Iterable[int], written: str, *, cancel: threading.Event
    ) -> tuple[dict[int, str], dict[int, str], list[Wrong], str]:
        """(marks, missing, wrong, overall), checked, or :class:`TranslationError`."""
        scope = list(topics)
        reason = ""
        for attempt in range(1, self.attempts + 1):
            prompt = grade_prompt(request, sheet, scope, written, retry_reason=reason)
            answer = self.engine.generate_json(prompt, GRADE_SCHEMA, cancel=cancel)
            try:
                payload = _json_payload(answer)
                if payload is None:
                    raise ReviewFormatError("JSON을 찾지 못했습니다.")
                return grade_from_answer(payload, sheet, scope, numbering=request.numbering, page_of=request.pages.get)
            except ReviewFormatError as exc:
                reason = str(exc)
                log.info("Blank marking rejected (try %s): %s", attempt, reason)
        raise TranslationError(f"GPT의 채점이 형식 검사를 통과하지 못했습니다.\n({reason})\n\n다시 채점해 주세요.")
