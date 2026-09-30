"""What is asked of GPT, and how its answer is read back.

One request per batch of sentences, each sentence tagged with its id, and the
answer asked for as JSON keyed by the same ids. The ids are what make the
highlight pairs possible (§2.2): a translation that merely came back "in
order" would drift by one the first time GPT merged two short sentences, and
every highlight after that point would be wrong.

The parser is deliberately forgiving about the *envelope* - code fences, a
sentence of chatter before the JSON, a trailing comma - and strict about the
*content*: an id that was not asked for is dropped, and an id that is missing
is simply not translated yet, so the next run picks it up.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["SentenceRequest", "batch_prompt", "lookup_prompt", "parse_batch", "parse_lookup"]


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
1. 문장마다 정확히 하나의 번역을 id 그대로 돌려주세요. 문장을 합치거나 나누지 마세요.
2. 직역: 원문의 구조와 어순을 최대한 살리고, 내용을 더하거나 빼거나 요약하지 마세요.
3. ⟦m1⟧ 같은 표식은 수식입니다. 글자 하나 바꾸지 말고 문장 속 알맞은 자리에 그대로 두세요.
4. 인용 번호([12], [3, 4]), 식 번호((5)), 그림·표 번호(Fig. 3, Table 2)는 원문 표기 그대로 두세요.
5. 전문 용어는 학계에서 통용되는 한국어 용어를 쓰고, 처음 나올 때만 괄호 안에 영어를 병기하세요.
   예: 그래프 신경망(graph neural network). 약어(EEG, GCN, SEED 등)는 그대로 두세요.
6. 본문 문장은 "~다." 체로 끝내세요. kind가 heading/title인 것은 명사형 제목으로 옮기세요.
7. kind가 caption이면 앞의 "Fig. 1:" / "TABLE 2:" 같은 머리표를 그대로 두고 뒤만 번역하세요.
8. 출력은 JSON 하나뿐입니다. 설명, 코드 펜스, 인사말을 붙이지 마세요.

출력 형식:
{"translations": [{"id": "<id>", "ko": "<번역>"}, ...]}
"""


def batch_prompt(sentences: Sequence[SentenceRequest], *, context: str = "") -> str:
    """The prompt for one batch. ``context`` is the paper's title, if known."""
    items = [{"id": s.id, "kind": s.kind, "en": s.text} for s in sentences]
    parts = [_RULES]
    if context:
        parts.append(f"논문 제목(참고용, 번역하지 마세요): {context}\n")
    parts.append("번역할 문장:")
    parts.append(json.dumps(items, ensure_ascii=False, indent=1))
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


def parse_batch(answer: str, expected: Sequence[str]) -> dict[str, str]:
    """``{sentence id: Korean}`` for every expected id the answer covered."""
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
