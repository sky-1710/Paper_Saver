"""What a translation engine is, and the errors one may raise.

An engine takes a prompt and gives back the assistant's answer as text. That
is the whole contract: everything above it - how the paper is cut into
batches, how the answer is parsed, how it reaches the page - is the same
whichever engine runs (§5.2). Adding another assistant means adding one class
here, not touching the runner or the UI.

THE ONE EXCEPTION (§5.3)
--------------------------
Every other part of Paper Saver runs on this machine. An engine does not:
it hands the paper's *text* to an assistant, which for a CLI-backed engine
means the text leaves the computer. That is why translation is off until
the user turns it on, and why the confirmation before the first run says so
plainly.

What never leaves, on any path: the PDF itself and the notes. An engine is
given a string and returns a string; it is not handed the project, its files
or their paths. The one exception is :meth:`TranslationEngine.ask`, which may
also be given the capture images the reader explicitly attached to a question
about the paper - those pictures, and nothing else of the project, go too.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "EngineAvailability",
    "EngineAuthError",
    "EngineNotFoundError",
    "EngineRateLimited",
    "TranslationCancelled",
    "TranslationEngine",
    "TranslationError",
]


class TranslationError(RuntimeError):
    """Base class. ``str(err)`` is always safe to show a user."""


class EngineNotFoundError(TranslationError):
    """The assistant's program is not installed, or could not be located."""


class EngineAuthError(TranslationError):
    """The program is there but is not logged in."""


class EngineRateLimited(TranslationError):
    """The account's usage limit was reached.

    Worth its own class because it is the one failure that is neither the
    user's mistake nor a bug: the work done so far is kept and the rest can be
    picked up later, so the message says that rather than apologising.
    """


class TranslationCancelled(TranslationError):
    """The user stopped the translation. Not a failure."""


@dataclass(frozen=True)
class EngineAvailability:
    """Whether an engine can run right now, and what to say if it cannot."""

    ready: bool
    #: Shown under the engine's name: the version, or why it is unusable.
    detail: str = ""
    #: What the user would have to do about it, when there is something.
    remedy: str = ""


class TranslationEngine(ABC):
    """One assistant, reachable as ``prompt in -> answer out``."""

    #: Stable identifier stored in settings and in ``translations.json``.
    key: str = ""
    #: Shown in the engine picker.
    label: str = ""
    #: Whether using this engine sends the text off this computer.
    #: An engine that runs a local model would set this False and the warning
    #: before the first run would not appear.
    sends_text_externally: bool = True
    #: Whether :meth:`ask` can be given images along with the prompt.
    supports_images: bool = False

    @abstractmethod
    def availability(self) -> EngineAvailability:
        """Check without running anything that costs the user quota."""

    @abstractmethod
    def translate(self, prompt: str, *, cancel: threading.Event) -> str:
        """Send ``prompt``, return the answer as text.

        Implementations poll ``cancel`` while waiting and raise
        :class:`TranslationCancelled` once it is set, so stopping a long
        paper takes effect in seconds rather than at the end of the batch.
        """

    def ask(self, prompt: str, *, images: Sequence[Path] = (), cancel: threading.Event) -> str:
        """A question about the paper, optionally with captured images.

        Same contract as :meth:`translate`; an engine that can look at
        pictures overrides this and sets ``supports_images``.
        """
        if images and not self.supports_images:
            raise TranslationError("이 엔진은 이미지를 볼 수 없습니다. 캡처 없이 질문해 주세요.")
        return self.translate(prompt, cancel=cancel)
