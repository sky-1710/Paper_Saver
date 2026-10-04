"""Translation and word lookup through an AI assistant (§5).

The one part of Paper Saver that sends anything off the computer, and only the
paper's text. See :mod:`paper_saver.translate.engine` for the contract.
"""

from __future__ import annotations

from .engine import (
    EngineAuthError,
    EngineAvailability,
    EngineNotFoundError,
    EngineRateLimited,
    TranslationCancelled,
    TranslationEngine,
    TranslationError,
)

__all__ = [
    "EngineAuthError",
    "EngineAvailability",
    "EngineNotFoundError",
    "EngineRateLimited",
    "TranslationCancelled",
    "TranslationEngine",
    "TranslationError",
    "RECOMMENDED_EFFORT",
    "RECOMMENDED_MODELS",
    "engine_from_settings",
]


#: What each kind of run uses until the reader picks otherwise in Settings,
#: chosen for quality that holds up and usage that lasts (measured 2026-10-02):
#: translation and look-ups are many small sentence-level runs, where the
#: light model at low effort translated as well as at medium; questions get
#: the current workhorse at its own default; a quiz's answer key must be right
#: and quizzes are few, so they think a little longer.
RECOMMENDED_MODELS = {"translate_model": "gpt-6-luna", "chat_model": "gpt-6.1-sol"}
RECOMMENDED_EFFORT = {"translate": "low", "chat": "low", "quiz": "medium"}


def engine_from_settings(settings: object) -> TranslationEngine:
    """The engine the user chose, with the options they set.

    A model the reader never chose is the recommended one. A model Codex no
    longer offers this account (retired, like gpt-5.5 on 2026-10-14) falls
    back to Codex's own default rather than failing every run.
    """
    from .codex_cli import CodexEngine, available_models

    get = getattr(settings, "get", lambda key, default=None: default)
    offered = {slug for slug, _, _ in available_models()}

    def model(key: str) -> str:
        chosen = str(get(key, RECOMMENDED_MODELS[key]) or "")
        return chosen if not chosen or not offered or chosen in offered else ""

    return CodexEngine(
        binary=get("codex_path", "") or None,
        model=model("translate_model"),
        chat_model=model("chat_model"),
        effort=RECOMMENDED_EFFORT["translate"],
        chat_effort=RECOMMENDED_EFFORT["chat"],
        quiz_effort=RECOMMENDED_EFFORT["quiz"],
    )
