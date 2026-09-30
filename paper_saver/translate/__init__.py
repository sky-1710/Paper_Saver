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
    "engine_from_settings",
]


def engine_from_settings(settings: object) -> TranslationEngine:
    """The engine the user chose, with the options they set."""
    from .codex_cli import CodexEngine

    get = getattr(settings, "get", lambda *_: None)
    return CodexEngine(
        binary=get("codex_path", "") or None,
        model=get("translate_model", "") or "",
    )
