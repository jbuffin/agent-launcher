"""The cmux terminal adapter. The only module that knows cmux commands and cmux IDs.

Verified against cmux 0.65.0 `--help` and its CLI contract; see docs/terminal-adapters.md. cmux only
accepts socket commands from processes it started, so everything here needs a cmux terminal.

Preparing a prompt never submits it: it is entered with `cmux paste` (one bracketed paste, newlines
stay inside it) and never with `--submit`, `--force` or a newline keystroke. It is entered only after
the agent's input box is visible, and checked on screen afterwards. Anything less certain falls back
to the clipboard and a notice, and the session is still created.
"""

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_launcher.logs import trace
from agent_launcher.terminals import (
    CLOSE_SESSION,
    CREATE_SESSION,
    FOCUS_SESSION,
    PREPARE_PROMPT,
    SUBMIT_PROMPT,
    CreateSessionRequest,
    CreateSessionResult,
    PromptInput,
    TerminalAdapter,
    TerminalError,
    TerminalSessionRef,
)

BUNDLED_CLI = "/Applications/cmux.app/Contents/Resources/bin/cmux"
_UUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
TIMEOUT = 20.0


@dataclass(frozen=True)
class CmuxResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], float, str | None], CmuxResult]
"""(argv, timeout, stdin text) -> result. Raises TerminalError if the process cannot run."""


def run_process(argv: Sequence[str], timeout: float, stdin: str | None = None) -> CmuxResult:
    try:
        done = subprocess.run(
            list(argv), input=stdin or "", capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise TerminalError("terminal_timeout", f"{argv[0]} timed out after {timeout:g}s.") from exc
    except OSError as exc:
        raise TerminalError("terminal_unavailable", f"Cannot run {argv[0]}: {exc.strerror or exc}") from exc
    return CmuxResult(done.returncode, done.stdout or "", done.stderr or "")


def find_cli() -> str | None:
    return shutil.which("cmux") or (BUNDLED_CLI if os.access(BUNDLED_CLI, os.X_OK) else None)


def _handles(text: str, kind: str) -> list[str]:
    """Distinct UUIDs and `kind:N` refs in cmux output, in order (real ones: `workspace:1000000054`)."""
    return list(dict.fromkeys(re.findall(rf"{_UUID}|\b{kind}:\d+", text)))


class CmuxAdapter(TerminalAdapter):
    name = "cmux"

    def __init__(
        self,
        runner: Runner | None = None,
        *,
        cli: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_interval: float = 0.5,
        settle_time: float = 0.5,
        base_env: Mapping[str, str] | None = None,
    ) -> None:
        self._run: Runner = runner or (lambda argv, timeout, stdin: run_process(argv, timeout, stdin))
        self._cli = cli if cli is not None else find_cli()
        self._sleep = sleep
        self._clock = clock
        self._poll = poll_interval
        self._settle = settle_time
        self._base_env = os.environ if base_env is None else base_env

    # --- capabilities -------------------------------------------------------------------

    def capabilities(self) -> set[str]:
        # No SUBMIT_PROMPT: `cmux paste --submit` exists but is not used or verified here.
        # No discovery or restore: cmux IDs are not stable enough across restarts to rely on.
        return {CREATE_SESSION, FOCUS_SESSION, CLOSE_SESSION, PREPARE_PROMPT}

    def unavailable_reason(self) -> str | None:
        if self._cli is None:
            return "cmux was not found. Install cmux (https://cmux.com) and run agent-launcher from a cmux terminal."
        try:
            result = self._run([self._cli, "ping"], TIMEOUT, None)
        except TerminalError as exc:
            return exc.message
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
            return (
                f"cmux did not answer ({detail}). cmux accepts commands only from terminals it started: "
                "run agent-launcher from a cmux terminal."
            )
        return None

    def available(self) -> bool:
        return self.unavailable_reason() is None

    # --- helpers ------------------------------------------------------------------------

    def _cmux(self, *args: str, stdin: str | None = None, check: bool = True) -> CmuxResult:
        if self._cli is None:
            raise TerminalError("terminal_unavailable", "cmux was not found.")
        result = self._run([self._cli, *args], TIMEOUT, stdin)
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:300]
            raise TerminalError(
                "cmux_failed", f"cmux {args[0]} failed (exit {result.returncode}): {detail}", command=args[0]
            )
        return result

    def _env_file(self, extra: Mapping[str, str]) -> Path | None:
        """The variables for the workspace, in a private temp file (not on argv)."""
        if not extra:
            return None
        bad = [k for k, v in extra.items() if "\n" in v or "\r" in v or "=" in k or not k]
        if bad:
            raise TerminalError(
                "env_unsupported", f"Environment variables cannot be passed to cmux: {', '.join(sorted(bad))}."
            )
        fd, name = tempfile.mkstemp(prefix="agent-launcher-env-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:  # mkstemp creates it 0600
            handle.writelines(f"{k}={v}\n" for k, v in extra.items())
        return Path(name)

    def _screen(self, ref: TerminalSessionRef) -> str:
        return self._cmux("read-screen", *self._target(ref), check=False).stdout

    @staticmethod
    def _target(ref: TerminalSessionRef) -> list[str]:
        args = ["--workspace", ref.workspace_id]
        if ref.surface_id:
            args += ["--surface", ref.surface_id]
        return args

    # --- sessions -----------------------------------------------------------------------

    def create_session(self, request: CreateSessionRequest) -> CreateSessionResult:
        if request.submit_prompt:
            self.require(SUBMIT_PROMPT)
        # Every variable the profile sets goes to the workspace, equal to ours or not, plus whatever
        # else differs from the caller's own environment.
        pinned = {k: request.env[k] for k in request.pinned_env if k in request.env}
        extra = {k: v for k, v in request.env.items() if self._base_env.get(k) != v}
        extra = {k: v for k, v in {**extra, **pinned}.items() if not k.startswith("CMUX_")}
        env_file = self._env_file(extra)
        # `--command` is typed into the workspace's interactive shell, which reads the user's rc files
        # first and could override the variables above. So the profile's variables are also set on the
        # command itself, with `env`. Each argument is shell-quoted.
        command = ["/usr/bin/env", *(f"{k}={v}" for k, v in pinned.items() if not k.startswith("CMUX_")), *request.command]
        args = [
            "new-workspace",
            "--name",
            request.title,
            "--cwd",
            request.working_directory,
            "--command",
            " " + shlex.join(command),  # leading space: kept out of history where the shell ignores it
            "--focus",
            "true",
        ]
        if env_file:
            args += ["--env-file", str(env_file)]
        try:
            created = self._cmux(*args)
        finally:
            if env_file:
                env_file.unlink(missing_ok=True)
        found = _handles(created.stdout, "workspace")
        workspace = found[0] if found else None
        if workspace is None:
            raise TerminalError(
                "cmux_unexpected_output", "cmux created a workspace but did not report its ID.", output=created.stdout[:200]
            )
        # The only panel the new workspace has. With several and no way to tell, record none:
        # targeting then falls to the workspace.
        panels = self._cmux("list-panels", "--workspace", workspace, check=False).stdout
        surfaces = [h for h in _handles(panels, "surface") if h != workspace]
        surface = surfaces[0] if len(surfaces) == 1 else None
        ref = TerminalSessionRef(self.name, workspace, surface, created_by_launcher=True)
        trace("cmux workspace created", workspace=workspace, surface=surface)
        if request.prompt is None:
            return CreateSessionResult(ref)
        return self._prepare(ref, request.prompt, request.prompt_input)

    def _prepare(self, ref: TerminalSessionRef, prompt: str, spec: PromptInput | None) -> CreateSessionResult:
        try:
            problem = self._enter_prompt(ref, prompt, spec)
        except TerminalError as exc:  # a timeout or a failed read: the workspace exists, so say so
            problem = f"{exc.message}"
        if problem is None:
            return CreateSessionResult(ref, prompt_prepared=True)
        return CreateSessionResult(ref, notice=f"The prompt was not entered: {problem} {self._clipboard(prompt)}")

    def _enter_prompt(self, ref: TerminalSessionRef, prompt: str, spec: PromptInput | None) -> str | None:
        """Paste the prompt into the idle input box. Returns why it was not, or None once it is on screen."""
        if spec is None:
            return "this agent has no known way to be told it is ready, so nothing was typed into it."
        blocked = self._wait_ready(ref, spec)
        if blocked is not None:
            return blocked
        # No --submit, no --force, no newline keystroke. cmux itself refuses over a draft or a dialog.
        pasted = self._cmux("paste", *self._target(ref), "-", stdin=prompt, check=False)
        if pasted.returncode != 0:
            return f"cmux refused the paste ({(pasted.stderr or pasted.stdout).strip()[:200]})."
        self._sleep(self._settle)
        after = self._screen(ref)
        if _any(spec.busy, after):
            return "the agent started working, so the prompt may have been submitted. Check the terminal."
        if not _shows(prompt, after, spec):
            return "the prompt could not be seen in the input box afterwards."
        return None

    def _wait_ready(self, ref: TerminalSessionRef, spec: PromptInput) -> str | None:
        """None once the input box is up and stable; otherwise why it is not."""
        deadline = self._clock() + spec.ready_timeout
        previous = None
        while self._clock() < deadline:
            screen = self._screen(ref)
            if _any(spec.blocked, screen):
                return "the agent is waiting on a dialog (trust, login or permission) that needs you."
            if _any(spec.ready, screen) and screen == previous:  # two equal reads: the TUI has settled
                return None
            previous = screen
            self._sleep(self._poll)
        return "the agent did not show its input box in time."

    def _clipboard(self, prompt: str) -> str:
        try:
            copied = self._run(["pbcopy"], TIMEOUT, prompt)
        except TerminalError:
            return "Copy the prompt from the task and paste it yourself."
        if copied.returncode == 0:
            return "It is on your clipboard: paste it into the agent yourself."
        return "Copy the prompt from the task and paste it yourself."

    def focus_session(self, session: TerminalSessionRef) -> None:
        self._cmux("workspace", "select", session.workspace_id)

    def close_session(self, session: TerminalSessionRef) -> None:
        """A running agent makes cmux ask for confirmation (`--force`). Force only a workspace this
        launcher created; anything else is left for the user to confirm."""
        args = ["workspace", "close", session.workspace_id]
        if session.created_by_launcher:
            args.append("--force")
        self._cmux(*args)


def _any(patterns: Sequence[str], text: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def _shows(prompt: str, screen: str, spec: PromptInput) -> bool:
    """Is the prompt in the input box? Long pastes may be collapsed to a placeholder by the TUI."""
    if _any(spec.collapsed, screen):
        return True
    first = next((line.strip() for line in prompt.splitlines() if line.strip()), "")
    return bool(first) and first[:40] in screen
