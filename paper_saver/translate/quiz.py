"""O/X quizzes for one slide, written by GPT through Codex (§4c).

    QuizPanel ─(slide)─> ReaderView worker ─(QuizGenerationInput)─> QuizGenerator
        ─> TranslationEngine.generate_json (codex exec --output-schema) ─> JSON
        ─> slide_quiz_from_dict (validated) ─> SlideQuiz ─> QuizStore / QuizPanel

The generator sees only what one slide needs: that slide's text, the part of
the lecture's STT that went with it, and the professor's earlier questions as
examples of *style*. It never sees the files, their names or their paths.

An answer that fails validation is sent back once with the reason, which
fixes most slips (a hint that gives the answer away, two "medium" questions).
A second failure is reported, and nothing half-valid reaches the screen.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.quiz import DIFFICULTIES, QuizFormatError, SlideQuiz, slide_quiz_from_dict
from .engine import TranslationEngine, TranslationError
from .prompts import _json_payload

log = logging.getLogger(__name__)

__all__ = [
    "QUIZ_SCHEMA",
    "CodexQuizGenerator",
    "QuizGenerationInput",
    "QuizGenerator",
    "parse_quiz_answer",
    "quiz_prompt",
]


@dataclass(frozen=True)
class QuizGenerationInput:
    slide_number: int
    slide_content: str
    slide_title: str = ""
    #: The STT that went with this slide; empty when there is none.
    transcript_content: str = ""
    #: Which part of the STT that was ("Slide 3 (00:00:20–00:03:41)").
    transcript_section: str = ""
    #: The professor's earlier questions, for style only.
    quiz_style_examples: str = ""
    lecture_title: str = ""
    #: A picture of the slide, sent only when the reader turned that on.
    slide_image: Path | None = None
    #: Prefix for question ids ("os06-memory-").
    id_prefix: str = ""


class QuizGenerator(ABC):
    """Anything that can write one slide's quiz."""

    @abstractmethod
    def generate_slide_quiz(self, request: QuizGenerationInput, *, cancel: threading.Event) -> SlideQuiz:
        """Return a validated quiz, or raise :class:`TranslationError`."""


#: The answer's shape, handed to Codex as ``--output-schema``. Strict-mode
#: rules apply: every property required, no others allowed.
QUIZ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["slide", "title", "questions"],
    "properties": {
        "slide": {"type": "integer"},
        "title": {"type": "string"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "difficulty", "type", "question", "answer", "hint", "explanation"],
                "properties": {
                    "id": {"type": "string"},
                    "difficulty": {"type": "string", "enum": list(DIFFICULTIES)},
                    "type": {"type": "string", "enum": ["true_false"]},
                    "question": {"type": "string"},
                    "answer": {"type": "boolean"},
                    "hint": {"type": "string"},
                    "explanation": {"type": "string"},
                },
            },
        },
    },
}


_RULES = """당신은 운영체제(OS) 과목 교수님의 퀴즈 출제를 돕는 조교입니다.
아래 [슬라이드]와 [강의 STT]만을 근거로, 이 슬라이드 하나에 대한 True/False(O/X) 문제 3개를 만드세요.

범위 규칙 (가장 중요):
1. 문제의 내용과 정답 근거는 [슬라이드]와 [강의 STT]에 실제로 있는 것뿐입니다.
   두 자료에 없는 일반 지식, 다른 장의 내용, 추측을 섞지 마세요.
2. [슬라이드]가 1차 출처(정의·공식·표·그림 속 관계)입니다. [강의 STT]는 교수님이 강조한 설명,
   반복한 내용, 추가 예시, 주의를 준 부분을 알려 줍니다. STT에서 강조된 부분을 우선 출제하세요.
   STT는 음성 인식 결과라 오탈자가 있을 수 있으니 뜻으로 읽으세요.
3. [교수님 이전 퀴즈]는 "문체와 함정 방식"을 배우기 위한 예시일 뿐입니다.
   그 문제들의 주제·용어·숫자·시나리오를 새 문제에 가져오지 마세요.
4. 파일을 읽거나 명령을 실행하지 마세요. 필요한 것은 모두 이 메시지 안에 있습니다.

교수님 출제 스타일 (이전 퀴즈 분석):
- 한 문장(길어야 두 문장)의 영어 평서문. 끝에 (O)/(X)를 붙이지 않습니다.
- 거짓 문장은 그럴듯한 오개념 하나로 만듭니다: 핵심 용어 하나 바꾸기
  (virtual↔physical, private↔shared, stack↔heap, compile time↔runtime, base↔bounds,
  highest↔lowest, static↔dynamic 등), 조건 뒤집기, 원인과 결과 바꾸기,
  A의 특징을 B의 것이라고 말하기, 숫자 바꾸기.
- must / only / always / never / guaranteed / same / eliminates 같은 단정 표현을 함정으로 씁니다.
  반대로 참인 문장은 may / can 처럼 정확한 한정어를 씁니다. 단정 표현이 있다고 항상 거짓은 아닙니다.
- "A only does X; it does not do Y"처럼 절반만 틀린 복합 문장도 씁니다.
- 계산 문제는 구체적인 값을 주고 결과 하나를 주장합니다. 해설에서 계산 과정을 보여 줍니다.

난이도:
- easy: 슬라이드에 직접 적힌 핵심 정의·속성을 이해했는지 확인. 사소한 문구 암기 문제는 금지.
- medium: 슬라이드의 핵심 진술에서 중요한 요소 하나를 바꿔 헷갈리게 만든 문제
  (위 스타일의 오개념). 참 문장이라면 흔히 틀리는 지점을 정확히 짚는 문장.
- hard: 두 개 이상 개념의 결합, 조건에 따른 결과 판단, 간단한 계산, 주소 변환,
  메모리 할당 시나리오, 코드 동작, edge case, 또는 STT에서 강조한 예제의 응용 중 하나 이상.
  문장을 길고 어렵게 쓰는 것으로 난이도를 올리지 마세요. 자료에 계산이나 시나리오의 근거가
  없으면 개념 결합이나 조건 판단으로 만드세요.

형식:
- 정확히 3문제: difficulty가 easy, medium, hard인 것 각각 하나. type은 모두 "true_false".
- question: 영어 평서문 (교수님 퀴즈처럼).
- answer: 참이면 true, 거짓이면 false. 세 문제의 정답이 모두 같은 쪽이 되지 않게 하세요.
- hint: 한국어 한두 문장. 스스로 생각하게 만드는 단서만 주고, 정답이나 참/거짓을 말하지 마세요.
  나쁜 예: "프로세스들이 physical memory를 공유하므로 거짓입니다."
  좋은 예: "'private address space'가 물리적으로 분리된 메모리 하드웨어를 필요로 하는지 생각해 보세요."
- explanation: 한국어 2~4문장 (전문 용어는 영어 그대로). 왜 참/거짓인지, 거짓이면 어느 부분이
  틀렸고 올바른 개념이 무엇인지, 계산이면 계산 과정. 근거가 슬라이드인지 강의 설명인지 밝히세요.
- 출력은 JSON 객체 하나뿐입니다. 설명, 코드 펜스, 인사말을 붙이지 마세요.

출력 형식:
{"slide": <번호>, "title": "<슬라이드 제목>", "questions": [
 {"id": "q1", "difficulty": "easy", "type": "true_false", "question": "...", "answer": true, "hint": "...", "explanation": "..."},
 {"id": "q2", "difficulty": "medium", ...}, {"id": "q3", "difficulty": "hard", ...}]}
"""


def quiz_prompt(request: QuizGenerationInput, *, retry_reason: str = "") -> str:
    parts = [_RULES]
    if request.lecture_title:
        parts.append(f"강의: {request.lecture_title}")
    title = f" — {request.slide_title}" if request.slide_title else ""
    parts.append(f"\n[슬라이드 {request.slide_number}{title}]")
    parts.append(f"<<<\n{request.slide_content.strip() or '(슬라이드에서 텍스트를 찾지 못했습니다)'}\n>>>")
    if request.slide_image is not None:
        parts.append("첨부 이미지는 이 슬라이드의 화면입니다. 그림·표·다이어그램 속 관계도 출처로 쓸 수 있습니다.")
    if request.transcript_content.strip():
        where = f" ({request.transcript_section})" if request.transcript_section else ""
        parts.append(f"\n[강의 STT — 이 슬라이드를 설명한 부분{where}]")
        parts.append(f"<<<\n{request.transcript_content.strip()}\n>>>")
    else:
        parts.append("\n[강의 STT]\n(이 슬라이드에 연결된 STT가 없습니다. 슬라이드만 근거로 하세요.)")
    if request.quiz_style_examples.strip():
        parts.append("\n[교수님 이전 퀴즈 — 스타일 참고용. 내용은 절대 가져오지 마세요]")
        parts.append(f"<<<\n{request.quiz_style_examples.strip()}\n>>>")
    if retry_reason:
        parts.append(
            f"\n직전 답은 형식 검사를 통과하지 못했습니다: {retry_reason}\n규칙을 모두 지켜 처음부터 다시 쓰세요."
        )
    parts.append(f"\n슬라이드 {request.slide_number}의 퀴즈 JSON:")
    return "\n".join(parts)


def parse_quiz_answer(answer: str, request: QuizGenerationInput) -> SlideQuiz:
    """Codex's answer as a validated quiz. Raises :class:`QuizFormatError`."""
    payload = _json_payload(answer)
    if payload is None:
        raise QuizFormatError("JSON을 찾지 못했습니다.")
    source: dict[str, Any] = {"slide": request.slide_number}
    if request.transcript_section:
        source["transcriptSection"] = request.transcript_section
    title = request.slide_title
    if not title and isinstance(payload, dict):
        title = str(payload.get("title") or "")
    return slide_quiz_from_dict(
        payload, slide=request.slide_number, title=title, id_prefix=request.id_prefix, source=source
    )


class CodexQuizGenerator(QuizGenerator):
    """Writes quizzes through a :class:`TranslationEngine` - the Codex CLI.

    Built on the engine rather than on ``subprocess`` directly, so it shares
    Codex's discovery, sign-in errors, timeout and cancellation with
    translation, and another engine drops in without touching this class.
    """

    def __init__(self, engine: TranslationEngine, *, attempts: int = 2) -> None:
        self.engine = engine
        self.attempts = max(1, attempts)

    def generate_slide_quiz(self, request: QuizGenerationInput, *, cancel: threading.Event) -> SlideQuiz:
        images = [request.slide_image] if request.slide_image is not None else []
        reason = ""
        for attempt in range(1, self.attempts + 1):
            prompt = quiz_prompt(request, retry_reason=reason)
            answer = self.engine.generate_json(prompt, QUIZ_SCHEMA, images=images, cancel=cancel)
            try:
                quiz = parse_quiz_answer(answer, request)
            except QuizFormatError as exc:
                reason = str(exc)
                log.info("Quiz for slide %s rejected (try %s): %s", request.slide_number, attempt, reason)
                continue
            quiz.generated = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            quiz.engine = self.engine.key
            return quiz
        raise TranslationError(
            f"GPT가 만든 퀴즈가 형식 검사를 통과하지 못했습니다.\n({reason})\n\n다시 시도해 주세요."
        )

