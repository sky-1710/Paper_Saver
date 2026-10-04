"""Translation through the Codex CLI, signed in with the user's ChatGPT account.

Codex is run once per batch, non-interactively::

    codex exec --skip-git-repo-check --ephemeral --ignore-user-config \\
               --sandbox read-only --color never \\
               --output-last-message ANSWER.md -

Every flag there is load-bearing:

``exec``
    Non-interactive mode. Plain ``codex`` would open the TUI and hang.
``-`` with the prompt on **stdin**
    A batch of paper is far longer than Windows allows on a
    command line (~32k characters), so the prompt cannot be an argument.
``--output-last-message FILE``
    Writes the final answer, and nothing else, to a file. stdout carries
    progress chatter that would otherwise have to be separated from the
    translation by guesswork.
``--ephemeral``
    No session file is written to ``~/.codex/sessions``. The text is being
    sent to OpenAI either way, but it does not also accumulate in a log on
    disk that the user does not know about.
``--sandbox read-only``
    Translating needs no file access. If the model decides to run a command
    anyway it cannot write anything.
``--skip-git-repo-check``
    Codex otherwise refuses to start outside a Git repository.
``--image FILE`` (questions only)
    Capture images the reader attached to a question. Codex reads them
    itself before the run, so the read-only sandbox does not get in the way.
    They go first, straight after ``exec``: the option takes any number of
    values, and the next ``--flag`` is what ends the list - put last, it
    would swallow the ``-`` that means "prompt on stdin".
``--output-schema FILE`` (quizzes only)
    A JSON Schema the final answer is held to. The file is written by Paper
    Saver into the scratch folder; nothing the reader typed or uploaded is
    ever part of a command line, only the fixed paths Paper Saver chose.
``--ignore-user-config``
    ``~/.codex/config.toml`` is set up for whatever the user does with Codex
    otherwise, and none of it belongs in a translation. Two parts actively
    break one: a default model their installed Codex is too old to accept
    ("requires a newer version of Codex"), and MCP servers that are started
    for every run and fail if they need authorising. Translating needs no
    tools, so the whole file is skipped. Their sign-in is not in it - auth
    comes from ``CODEX_HOME`` and still applies.

Questions to GPT are the one exception to ``--ephemeral`` (§4b.2): a
conversation runs in one Codex session, continued with::

    codex exec resume --skip-git-repo-check --ignore-user-config --json \\
               --output-last-message ANSWER.md SESSION_ID -

so a follow-up carries only what is new, and OpenAI's prompt cache serves
what the session already holds (measured: a third question hit the cache for
99% of its input; separate runs never hit it beyond Codex's own preamble).
The session keeps the read-only sandbox it was started with. Its file in
``CODEX_HOME/sessions`` is deleted by :meth:`forget_session` when the reader
starts a new conversation or takes an attached file off.

Both stdin and the answer file are UTF-8 explicitly: the Windows default is
cp949 here, which mangles Korean.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .engine import (
    EngineAuthError,
    EngineAvailability,
    EngineNotFoundError,
    EnginePromptRejected,
    EngineRateLimited,
    TranslationCancelled,
    TranslationEngine,
    TranslationError,
)

log = logging.getLogger(__name__)

__all__ = ["CODEX_PATH_ENV", "CodexEngine", "find_codex"]

#: Lets the user point at a Codex the search below does not find.
CODEX_PATH_ENV = "PAPER_SAVER_CODEX"

#: How long one batch may take before it is given up on. Codex reasons for a
#: while on a long batch; the ceiling exists so a wedged process cannot leave
#: the translation hanging forever.
BATCH_TIMEOUT_SECONDS = 900

#: How often the wait loop looks at the cancel flag.
POLL_SECONDS = 0.2


def _no_window() -> dict[str, Any]:
    """Keep a console window from flashing on Windows for every batch."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": flags} if flags else {}


def _bundled_candidates() -> list[Path]:
    """Codex installations that put no ``codex`` on the PATH.

    The VS Code ChatGPT extension is the common case on Windows: it unpacks
    Codex into a folder named after the build, so the path changes with every
    update and cannot be hard-coded::

        %LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<build>\\codex.exe

    The newest build wins, which is the one the extension is using.
    """
    home = Path.home()
    roots: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "OpenAI" / "Codex" / "bin")
    roots.append(home / ".codex" / "bin")

    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for name in ("codex.exe", "codex"):
            found += [p for p in root.glob(f"*/{name}") if p.is_file()]
            direct = root / name
            if direct.is_file():
                found.append(direct)

    # The extension also ships Codex inside itself, one build per platform:
    #     ~/.vscode/extensions/openai.chatgpt-<ver>-<platform>/bin/<os-arch>/codex
    # This is where it lives on a Mac, where there is no LOCALAPPDATA copy.
    platform_dir = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    for editor in (".vscode", ".vscode-insiders", ".cursor", ".windsurf"):
        extensions = home / editor / "extensions"
        if not extensions.is_dir():
            continue
        for candidate in extensions.glob("openai.chatgpt-*/bin/*/codex*"):
            if (
                candidate.is_file()
                and candidate.stem == "codex"
                and candidate.parent.name.startswith(platform_dir)
            ):
                found.append(candidate)

    # A Mac app launched from Finder or the Dock does not see the shell's PATH,
    # so the places npm and Homebrew install to are looked in directly.
    if sys.platform != "win32":
        for folder in (
            Path("/opt/homebrew/bin"),
            Path("/usr/local/bin"),
            home / ".local" / "bin",
            home / ".npm-global" / "bin",
            home / ".volta" / "bin",
            home / ".bun" / "bin",
        ):
            if (folder / "codex").is_file():
                found.append(folder / "codex")
        found += [p for p in (home / ".nvm" / "versions" / "node").glob("*/bin/codex") if p.is_file()]

    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def find_codex(override: str | Path | None = None) -> Path | None:
    """Locate the Codex program: an explicit setting, the PATH, then bundles."""
    for candidate in (override, os.environ.get(CODEX_PATH_ENV)):
        if candidate:
            path = Path(candidate).expanduser()
            return path if path.is_file() else None

    from shutil import which

    on_path = which("codex")
    if on_path:
        return Path(on_path)

    bundled = _bundled_candidates()
    return bundled[0] if bundled else None


@dataclass
class CodexEngine(TranslationEngine):
    """Runs one batch per call. Holds no state between batches.

    Each batch is an independent Codex run with the rules repeated, rather
    than one conversation continued: a fresh run cannot drift part-way through
    a paper, and a batch that fails can be retried on its own.
    """

    key: str = "codex"
    label: str = "Codex CLI (ChatGPT sign-in)"
    sends_text_externally: bool = True
    supports_images: bool = True

    #: Explicit path to the program, when the user set one.
    binary: str | Path | None = None
    #: Passed to ``-m`` for translation and dictionary look-ups: many small
    #: runs, where a light model ("gpt-6-luna") saves most of the usage.
    #: Empty means Codex's own default.
    model: str = ""
    #: The same for questions to GPT and quizzes, which need more thought.
    chat_model: str = ""
    #: Reasoning effort ("low", "medium", ...) for each kind of run, passed as
    #: ``-c model_reasoning_effort=...``; empty leaves the model's default.
    #: Measured on a real batch: luna at "low" translated as well as at
    #: "medium", 30% faster.
    effort: str = ""
    chat_effort: str = ""
    quiz_effort: str = ""
    timeout: int = BATCH_TIMEOUT_SECONDS

    def resolve(self) -> Path:
        path = find_codex(self.binary)
        if path is None:
            raise EngineNotFoundError(
                "이 컴퓨터에서 Codex를 찾지 못했습니다.\n\n"
                "번역과 사전은 OpenAI Codex CLI를 통해 ChatGPT에 연결합니다.\n"
                "1) 터미널에서 설치:  npm install -g @openai/codex\n"
                "   (또는 VS Code의 'ChatGPT' 확장 프로그램 설치)\n"
                "2) 로그인:  codex login  (본인 ChatGPT 계정)\n\n"
                "다른 위치에 설치했다면 설정 > Codex 경로에 전체 경로를 넣거나 "
                f"{CODEX_PATH_ENV} 환경 변수를 지정하세요."
            )
        return path

    def availability(self) -> EngineAvailability:
        """Report the version. Deliberately does not check the sign-in.

        There is no way to ask Codex whether it is signed in without starting
        a run, and a run spends the user's quota. A sign-in problem surfaces
        on the first batch instead, with a message that says what to do.
        """
        try:
            path = self.resolve()
        except EngineNotFoundError as exc:
            return EngineAvailability(ready=False, detail="Not installed", remedy=str(exc))
        try:
            result = subprocess.run(
                [str(path), "--version"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, **_no_window(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return EngineAvailability(
                ready=False,
                detail="Could not be started",
                remedy=f"Codex is at {path} but would not run:\n{exc}",
            )
        version = (result.stdout or result.stderr).strip().splitlines()
        return EngineAvailability(ready=True, detail=version[0] if version else str(path))

    def _command(
        self,
        path: Path,
        answer_file: Path,
        images: Sequence[Path] = (),
        schema_file: Path | None = None,
        *,
        keep: bool = False,
        resume: str | None = None,
        model: str = "",
        effort: str = "",
    ) -> list[str]:
        tuning = ["-c", f'model_reasoning_effort="{effort}"'] if effort.isalpha() else []
        if resume:
            # ``resume`` takes no --sandbox or --color: the session keeps the
            # read-only sandbox it was started with.
            cmd = [str(path), "exec", "resume"]
            for image in images:
                cmd += ["--image", str(image)]
            cmd += ["--skip-git-repo-check", "--ignore-user-config", "--json",
                    "--output-last-message", str(answer_file)]
            if model:
                cmd += ["-m", model]
            return [*cmd, *tuning, resume, "-"]
        cmd = [str(path), "exec"]
        for image in images:
            cmd += ["--image", str(image)]
        cmd += ["--skip-git-repo-check"]
        # A kept session is the start of a conversation; --json reports its id.
        cmd += ["--json"] if keep else ["--ephemeral"]
        cmd += [
            "--ignore-user-config",
            "--sandbox", "read-only",
            "--color", "never",
            "--output-last-message", str(answer_file),
        ]
        if schema_file is not None:
            cmd += ["--output-schema", str(schema_file)]
        if model:
            cmd += ["-m", model]
        # A trailing "-" tells Codex the prompt arrives on stdin.
        return [*cmd, *tuning, "-"]

    def translate(self, prompt: str, *, cancel: threading.Event) -> str:
        return self._run(prompt, (), cancel, model=self.model, effort=self.effort)

    def ask(self, prompt: str, *, images: Sequence[Path] = (), cancel: threading.Event) -> str:
        missing = [str(p) for p in images if not Path(p).is_file()]
        if missing:
            raise TranslationError("첨부한 캡처 파일을 찾을 수 없습니다:\n" + "\n".join(missing))
        return self._run(
            prompt, [Path(p).resolve() for p in images], cancel, model=self.chat_model, effort=self.chat_effort
        )

    supports_sessions: bool = True

    def converse(
        self, prompt: str, *, session: str | None = None, images: Sequence[Path] = (), cancel: threading.Event
    ) -> tuple[str, str | None]:
        missing = [str(p) for p in images if not Path(p).is_file()]
        if missing:
            raise TranslationError("첨부한 캡처 파일을 찾을 수 없습니다:\n" + "\n".join(missing))
        found: list[str] = []
        answer = self._run(
            prompt, [Path(p).resolve() for p in images], cancel, keep=True, resume=session, session_out=found,
            model=self.chat_model, effort=self.chat_effort,
        )
        return answer, session or (found[0] if found else None)

    def forget_session(self, session: str) -> None:
        forget_codex_session(session)

    def generate_json(
        self,
        prompt: str,
        schema: Mapping[str, Any],
        *,
        images: Sequence[Path] = (),
        cancel: threading.Event,
    ) -> str:
        missing = [str(p) for p in images if not Path(p).is_file()]
        if missing:
            raise TranslationError("첨부할 이미지 파일을 찾을 수 없습니다:\n" + "\n".join(missing))
        return self._run(
            prompt, [Path(p).resolve() for p in images], cancel, schema=schema, model=self.chat_model,
            effort=self.quiz_effort,
        )

    def _run(
        self,
        prompt: str,
        images: Sequence[Path],
        cancel: threading.Event,
        *,
        schema: Mapping[str, Any] | None = None,
        keep: bool = False,
        resume: str | None = None,
        session_out: list[str] | None = None,
        model: str = "",
        effort: str = "",
    ) -> str:
        path = self.resolve()
        # Codex treats its working directory as the workspace root. A scratch
        # folder keeps it away from the paper project and from whatever
        # directory the application happens to have been started in.
        # ignore_cleanup_errors: a cancelled run can leave Codex holding a file in
        # the folder for a moment, and on Windows that made the cleanup raise
        # over the top of the cancellation.
        with tempfile.TemporaryDirectory(prefix="paper-saver-translate-", ignore_cleanup_errors=True) as scratch:
            workspace = Path(scratch)
            answer_file = workspace / "answer.md"
            schema_file = None
            if schema is not None:
                schema_file = workspace / "schema.json"
                schema_file.write_text(json.dumps(schema), encoding="utf-8")
            try:
                process = subprocess.Popen(
                    self._command(path, answer_file, images, schema_file, keep=keep, resume=resume, model=model, effort=effort),
                    cwd=str(workspace),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace",
                    **_no_window(),
                )
            except OSError as exc:
                raise TranslationError(f"Codex could not be started:\n{exc}") from exc

            try:
                stdout, stderr = self._communicate(process, prompt, cancel)
            except TranslationError:
                # Stopped before it said its id: find the new session by the
                # scratch folder it ran in, which no other run shares.
                if keep and not resume:
                    _forget_sessions_in(workspace)
                raise

            if process.returncode != 0:
                # A new session whose first turn failed is no conversation:
                # its file would only sit in CODEX_HOME holding the text.
                if keep and not resume and (thread := _thread_id(stdout)):
                    forget_codex_session(thread)
                raise self._failure(process.returncode, stdout, stderr)
            if session_out is not None and (thread := _thread_id(stdout)):
                session_out.append(thread)
            answer = self._read_answer(answer_file, "" if keep or resume else stdout)
            if not answer.strip():
                raise TranslationError(
                    "Codex finished but returned nothing to translate with.\n\n"
                    "This usually means the run was interrupted. Try again; "
                    "pages already translated will be skipped."
                )
            return answer

    def _communicate(
        self, process: subprocess.Popen[str], prompt: str, cancel: threading.Event
    ) -> tuple[str, str]:
        """Feed the prompt in and wait, watching the cancel flag throughout.

        ``Popen.communicate`` has to be given a timeout to be interruptible at
        all, so it is called in short slices; each expiry is just another turn
        of the loop, not a failure.
        """
        waited = 0.0
        sent = False
        while True:
            if cancel.is_set():
                self._stop(process)
                raise TranslationCancelled("Translation stopped.")
            try:
                # The prompt goes in on the first pass only; afterwards stdin
                # is already closed and communicate() must be called with None.
                stdout, stderr = process.communicate(
                    input=None if sent else prompt, timeout=POLL_SECONDS
                )
                return stdout or "", stderr or ""
            except subprocess.TimeoutExpired:
                sent = True
                waited += POLL_SECONDS
                if waited >= self.timeout:
                    self._stop(process)
                    raise TranslationError(
                        f"Codex did not answer within {self.timeout // 60} minutes "
                        "and was stopped.\n\nTry again, or choose fewer sentences per "
                        "batch in Settings."
                    ) from None

    @staticmethod
    def _stop(process: subprocess.Popen[str]) -> None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    @staticmethod
    def _read_answer(answer_file: Path, stdout: str) -> str:
        """The answer file, or stdout if Codex wrote no file."""
        try:
            if answer_file.is_file():
                return answer_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            log.warning("Could not read the Codex answer file", exc_info=True)
        return stdout

    @staticmethod
    def _failure(returncode: int, stdout: str, stderr: str) -> TranslationError:
        """Turn an exit code into something worth showing a user."""
        combined = f"{stderr}\n{stdout}".lower()
        tail = (stderr.strip() or stdout.strip())[-800:]

        # First: its wording mentions none of the words below, but the JSON
        # around it could ("error", a link), and it needs its own handling.
        if "invalid_prompt" in combined or "flagged as potentially violating" in combined:
            return EnginePromptRejected(
                "OpenAI가 이번에 보낸 글을 사용 정책 위반 가능성으로 거절했습니다.\n\n"
                "논문 본문에 실린 평가용 프롬프트·채점 지시문·위험한 질문 예시 같은 "
                "인용문 때문인 경우가 대부분입니다 (예: DeepSeek-R1 논문의 부록 표)."
            )
        signed_out = ("not logged in", "unauthorized", "please run codex login",
                      "authentication")
        if any(word in combined for word in signed_out):
            return EngineAuthError(
                "Codex is installed but not signed in.\n\n"
                "Open a terminal and run:\n    codex login\n\n"
                "Then try the translation again."
            )
        if "requires a newer version of codex" in combined:
            return TranslationError(
                "The model this translation asked for needs a newer Codex than "
                "the one installed.\n\n"
                "Update Codex (run 'codex update', or update the ChatGPT "
                "extension in VS Code), or set a different model in Settings."
            )
        limited = ("rate limit", "usage limit", "quota", "429", "too many requests")
        if any(word in combined for word in limited):
            return EngineRateLimited(
                "Your ChatGPT usage limit has been reached.\n\n"
                "Pages translated so far have been saved. Run the translation "
                "again later and it will carry on from where it stopped."
            )
        offline = ("network", "connection", "dns", "timed out", "unreachable")
        if any(word in combined for word in offline):
            return TranslationError(
                "Codex could not reach OpenAI. Check the internet connection "
                f"and try again.\n\n{tail}"
            )
        return TranslationError(f"Codex failed (exit code {returncode}).\n\n{tail}")


def _thread_id(stdout: str) -> str | None:
    """The session id in ``--json`` output (its "thread.started" event)."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("thread_id"):
            return str(event["thread_id"])
    return None


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def forget_codex_session(session: str) -> int:
    """Delete a session's files from ``CODEX_HOME``; how many went.

    Codex names them ``rollout-<time>-<session id>.jsonl`` under
    ``sessions/<year>/<month>/<day>/``. Only names ending in this id are
    touched, and an id is checked to look like one first.
    """
    if not re.fullmatch(r"[0-9a-fA-F-]{16,64}", session or ""):
        return 0
    removed = 0
    for folder in ("sessions", "archived_sessions"):
        root = codex_home() / folder
        if not root.is_dir():
            continue
        for path in root.rglob(f"*{session}.jsonl"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                log.warning("Could not delete Codex session file %s", path, exc_info=True)
    return removed


def _forget_sessions_in(workspace: Path) -> int:
    """Delete today's session files whose run had ``workspace`` as its folder.

    Each run gets a scratch folder of its own, and Codex writes it into the
    first line of the session file (``"cwd"``), so the match is exact.
    """
    root = codex_home() / "sessions"
    if not root.is_dir():
        return 0
    needle = json.dumps(str(workspace))[1:-1]  # as it is escaped in the file
    removed = 0
    cutoff = time.time() - 6 * 3600
    for path in root.rglob("rollout-*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            with path.open(encoding="utf-8", errors="replace") as handle:
                first = handle.readline(8192)
            if needle in first:
                path.unlink()
                removed += 1
        except OSError:
            log.warning("Could not check Codex session file %s", path, exc_info=True)
    return removed


def available_models() -> list[tuple[str, str, str]]:
    """The models this Codex offers the signed-in account: (id, name, note).

    Read from the list Codex itself keeps (``models_cache.json``), so it is
    the account's real choice; empty when Codex has not fetched one yet.
    """
    path = codex_home() / "models_cache.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    models = []
    for item in data.get("models") or []:
        if not isinstance(item, dict) or item.get("visibility") != "list" or not item.get("slug"):
            continue
        models.append((str(item["slug"]), str(item.get("display_name") or item["slug"]), str(item.get("description") or "")))
    return models
