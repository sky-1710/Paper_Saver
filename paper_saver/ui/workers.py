"""Running slow work off the UI thread (§7.4).

A task is a plain function run on a Python thread, given a cancel flag and a
``report`` callable. Its progress, result and failure come back as Qt signals,
which Qt delivers on the UI thread because the task object lives there - so
handlers can touch widgets freely and the function itself never has to.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)

__all__ = ["BackgroundTask"]

_live: set[BackgroundTask] = set()


class BackgroundTask(QObject):
    progress = Signal(object)
    succeeded = Signal(object)
    failed = Signal(object)
    #: Always emitted last, whatever happened.
    done = Signal()

    def __init__(
        self,
        function: Callable[[threading.Event, Callable[[Any], None]], Any],
        *,
        name: str = "task",
    ) -> None:
        super().__init__()
        self._function = function
        self.name = name
        self.cancel_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> BackgroundTask:
        _live.add(self)  # a task nobody holds must not be collected mid-run
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()
        return self

    def cancel(self) -> None:
        self.cancel_event.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        try:
            result = self._function(self.cancel_event, self.progress.emit)
        except BaseException as exc:  # noqa: BLE001 - every failure goes to the UI
            if not isinstance(exc, Exception):
                raise
            log.info("Task %s failed: %s", self.name, exc, exc_info=not _expected(exc))
            self.failed.emit(exc)
        else:
            self.succeeded.emit(result)
        finally:
            self.done.emit()
            _live.discard(self)


def _expected(exc: BaseException) -> bool:
    from ..translate.engine import TranslationError

    return isinstance(exc, TranslationError)
