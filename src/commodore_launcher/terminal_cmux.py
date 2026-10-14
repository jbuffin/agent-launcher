"""The cmux terminal adapter. The only module that knows cmux commands and cmux IDs.

Verified against cmux 0.65.0 `--help` and its CLI contract; see docs/terminal-adapters.md. cmux only
accepts socket commands from processes it started, so everything here needs a cmux terminal.

A prompt goes on the harness's command line: the command typed into the workspace's shell reads it from a private
temp file that deletes itself, so the prompt is never echoed in the typed line or kept in the shell's history. The
harness submits it itself once it is up. Nothing is pasted, no key is sent and no screen is read for a new session.
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

from commodore_launcher.logs import trace
from commodore_launcher.terminals import (
    CLOSE_SESSION,
    CREATE_SESSION,
    DISCOVER_SESSIONS,
    FOCUS_SESSION,
    PROMPT_MARKER,
    RUN_INTERACTIVE,
    CreateSessionRequest,
    CreateSessionResult,
    ExternalSession,
    ResumeCheck,
    StaleTerminalSession,
    TerminalAdapter,
    TerminalError,
    TerminalSessionRef,
    UnsupportedCapability,
)

BUNDLED_CLI = "/Applications/cmux.app/Contents/Resources/bin/cmux"
_UUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
TIMEOUT = 20.0
_NOT_FOUND = re.compile(r"\b(Workspace|Surface|Panel) ref not found|\bnot_found: (Workspace|Surface|Panel) not found")
"""What cmux 0.65.0 prints for a missing workspace, exit 1, observed live: `Error: Workspace ref not found:
workspace:999999999` for a ref and `Error: not_found: Workspace not found` for a UUID. Anything else, including `invalid_params: Surface is not a terminal`, is not 'gone'."""
ID_FORMAT = ("--id-format", "uuids")
"""A global flag, so it goes before the subcommand. Without it cmux prints refs (`surface:1000000000`) from
some commands and UUIDs from others; everything recorded or compared uses UUIDs."""


@dataclass(frozen=True)
class CmuxResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], float, str | None], CmuxResult]
"""(argv, timeout, stdin text) -> result. Raises TerminalError if the process cannot run."""


def run_process(argv: Sequence[str], timeout: float, stdin: str | None = None) -> CmuxResult:
    try:
        # CMUX_QUIET keeps cmux's alias notices ("'list-workspaces' is now an alias for …") off stderr, which is parsed.
        done = subprocess.run(
            list(argv),
            input=stdin or "",
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "CMUX_QUIET": "1"},
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
        base_env: Mapping[str, str] | None = None,
    ) -> None:
        self._run: Runner = runner or (lambda argv, timeout, stdin: run_process(argv, timeout, stdin))
        self._cli = cli if cli is not None else find_cli()
        self._sleep = sleep
        self._clock = clock
        self._poll = poll_interval
        self._base_env = os.environ if base_env is None else base_env

    # --- capabilities -------------------------------------------------------------------

    def capabilities(self) -> set[str]:
        # No restore: cmux IDs are not stable enough across restarts to rely on. Discovery only lists what
        # exists now, for explicit adoption.
        return {CREATE_SESSION, FOCUS_SESSION, DISCOVER_SESSIONS, CLOSE_SESSION, RUN_INTERACTIVE}

    def unavailable_reason(self) -> str | None:
        if self._cli is None:
            return "cmux was not found. Install cmux (https://cmux.com) and run commodore from a cmux terminal."
        try:
            result = self._run([self._cli, "ping"], TIMEOUT, None)
        except TerminalError as exc:
            return exc.message
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
            return (
                f"cmux did not answer ({detail}). cmux accepts commands only from terminals it started: "
                "run commodore from a cmux terminal."
            )
        return None

    def available(self) -> bool:
        return self.unavailable_reason() is None

    # --- helpers ------------------------------------------------------------------------

    def _cmux(self, *args: str, stdin: str | None = None, check: bool = True) -> CmuxResult:
        if self._cli is None:
            raise TerminalError("terminal_unavailable", "cmux was not found.")
        result = self._run([self._cli, *ID_FORMAT, *args], TIMEOUT, stdin)
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
        fd, name = tempfile.mkstemp(prefix="commodore-launcher-env-")
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
        at_launch = bool(request.prompt_args) and request.prompt is not None
        if request.prompt is not None and not at_launch:  # nothing is pasted: a prompt travels only on the command line
            raise TerminalError("prompt_args_invalid", "A prompt needs launch arguments to carry it; none were given.")
        if at_launch and sum(PROMPT_MARKER in a for a in request.prompt_args) != 1:  # the file is read once
            raise TerminalError("prompt_args_invalid", "Exactly one launch argument must hold the prompt.")
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
        typed = shlex.join(command)
        prompt_file: Path | None = None
        if at_launch:
            prompt_file = _prompt_file(request.prompt)
            typed += " " + " ".join(_from_file(arg, prompt_file) for arg in request.prompt_args)
        args = [
            "new-workspace",
            "--name",
            request.title,
            "--cwd",
            request.working_directory,
            "--command",
            " " + typed + ("; exit" if request.exit_when_done else ""),  # leading space: kept out of history where the shell ignores it
            "--focus",
            "true",
        ]
        if env_file:
            args += ["--env-file", str(env_file)]
        try:
            created = self._cmux(*args)
        except BaseException:
            if prompt_file:  # no workspace, so no shell will read (and delete) it
                prompt_file.unlink(missing_ok=True)
            raise
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
        if request.resume_check is not None:
            return CreateSessionResult(ref, resume_state=self._watch_resume(ref, request.resume_check))
        # The harness submits a prompt given at launch when it is up; nothing is seen from here.
        return CreateSessionResult(ref, prompt_submitted=at_launch)

    def run_interactive(self, title: str, working_directory: str, command: Sequence[str]) -> TerminalSessionRef:
        """A new workspace that runs `command` and nothing else: a TTY for the picker. It is the ordinary
        workspace creation without a prompt, so the same quoting and `--cwd` rules apply. It is not closed
        here: the command is followed by `; exit`, so the workspace's shell ends with it and cmux should drop the
        surface (not verified live). Not recorded as a task session."""
        return self.create_session(CreateSessionRequest(title, working_directory, command, exit_when_done=True)).session

    def _watch_resume(self, ref: TerminalSessionRef, check: ResumeCheck) -> str:
        """`confirmed` only when the screen shows the resumed conversation, `failed` when the harness says it
        could not resume, `unconfirmed` when neither is seen in time (or no confirming pattern is known)."""
        deadline = self._clock() + check.timeout
        while self._clock() < deadline:
            try:
                screen = self._screen(ref)
            except TerminalError:
                return "unconfirmed"
            if _any(check.failed, screen):
                return "failed"
            if _any(check.confirmed, screen):
                return "confirmed"
            self._sleep(self._poll)
        return "unconfirmed"

    def _panels(self, session: TerminalSessionRef) -> str | None:
        """The workspace's panel listing, or None when cmux says the workspace does not exist. Any other
        failure (socket, permission, version, timeout) raises: it says nothing about the workspace, and a
        session must never be taken for gone, and resumed over, on that basis."""
        panels = self._cmux("list-panels", "--workspace", session.workspace_id, check=False)
        if panels.returncode == 0:
            return panels.stdout
        self._raise_unless_missing(panels, "list-panels")
        return None

    @staticmethod
    def _raise_unless_missing(result: CmuxResult, command: str) -> None:
        text = (result.stderr or result.stdout).strip()
        if not _NOT_FOUND.search(text):
            raise TerminalError(
                "cmux_failed",
                f"cmux {command} failed (exit {result.returncode}) and did not say the workspace is missing, "
                f"so it is not treated as gone: {text[:300] or 'no output'}",
                command=command,
            )

    def _exists(self, session: TerminalSessionRef) -> bool:
        """Is the recorded workspace (and surface, when one was recorded) still there?"""
        panels = self._panels(session)
        return panels is not None and (session.surface_id is None or session.surface_id in panels)

    def read_screen(self, session: TerminalSessionRef) -> str | None:
        if session.surface_id is None:
            # Only the surface UUID tells this workspace from another that cmux later gave the same ref.
            # Without it nothing read from the screen can be attributed to this session.
            raise UnsupportedCapability(self.name, "read_screen (no surface was recorded, so the session cannot be verified)")
        if not self._exists(session):
            return None
        shown = self._cmux("read-screen", *self._target(session), check=False)
        if shown.returncode == 0:
            return shown.stdout
        self._raise_unless_missing(shown, "read-screen")
        return None

    def discover_sessions(self) -> list[ExternalSession]:
        """Every workspace with exactly one panel, its title and the working directory cmux reports for it.
        Read-only: `list-workspaces`, `list-panels` and `sidebar-state`, never `read-screen`. A workspace that
        vanishes between the calls is skipped; any other cmux failure raises. Nothing here says a workspace
        is external: the caller drops those it recorded itself."""
        listing = self._cmux("list-workspaces").stdout
        found: list[ExternalSession] = []
        for line in listing.splitlines():
            handles = _handles(line, "workspace")
            if not handles:
                continue
            workspace = handles[0]
            panels = self._cmux("list-panels", "--workspace", workspace, check=False)
            if panels.returncode != 0:
                self._raise_unless_missing(panels, "list-panels")
                continue
            surfaces = [h for h in _handles(panels.stdout, "surface") if h != workspace]
            if len(surfaces) != 1:
                continue  # nothing to tell it apart from a reused ref, so it is never offered
            state = self._cmux("sidebar-state", "--workspace", workspace, check=False)
            cwd = _sidebar_cwd(state.stdout, surfaces[0]) if state.returncode == 0 else None
            ref = TerminalSessionRef(self.name, workspace, surfaces[0], created_by_launcher=False)
            found.append(ExternalSession(ref, _workspace_title(line), cwd))
        return found

    def focus_session(self, session: TerminalSessionRef) -> None:
        """Select the workspace, but only if it still exists: a stale ID raises `StaleTerminalSession`."""
        if not self._exists(session):
            raise StaleTerminalSession(self.name, session)
        self._cmux("workspace", "select", session.workspace_id)

    def close_session(self, session: TerminalSessionRef) -> None:
        """A running harness makes cmux ask for confirmation (`--force`). Force only a workspace this launcher
        created (recorded) whose recorded surface is still listed in it: cmux refs may be reused after a
        restart, and a different workspace must never be force-closed. Otherwise cmux is left to refuse."""
        args = ["workspace", "close", session.workspace_id]
        if session.created_by_launcher and session.surface_id and self._exists(session):
            args.append("--force")
        self._cmux(*args)


def _sidebar_cwd(text: str, surface: str) -> str | None:
    """The working directory in `cmux sidebar-state` (`key=value` lines, observed on 0.65.0). `focused_cwd` is the
    focused panel's, so it is used only when `focused_panel` is this surface; otherwise the workspace's `cwd`."""
    values = {k: v.strip() for k, _, v in (line.partition("=") for line in text.splitlines()) if _}
    if values.get("focused_cwd") and values.get("focused_panel", "").lower() == surface.lower():
        return values["focused_cwd"]
    return values.get("cwd") or None


def _workspace_title(line: str) -> str:
    """The title in a `list-workspaces` line (observed: two-space indent, ID, two spaces, title; the selected one
    may start with `* `): what is left after the ID and the `*`/`[selected]` markers."""
    text = re.sub(rf"{_UUID}|\bworkspace:\d+", " ", line, count=1)
    text = re.sub(r"\[[^\]]*\]", " ", text).replace("*", " ")
    text = text.strip()
    return text[1:-1] if len(text) > 1 and text[0] == text[-1] == '"' else text


def _prompt_file(prompt: str) -> Path:
    """The prompt in a private temp file (0600) for the typed command to read; `_from_file` removes it."""
    fd, name = tempfile.mkstemp(prefix="commodore-launcher-prompt-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(prompt)
    return Path(name)


def _from_file(arg: str, path: Path) -> str:
    """`arg` for a POSIX shell (zsh, bash), with `PROMPT_MARKER` replaced by the file's contents. The file is
    read and removed by the shell, as one argument; a command substitution drops trailing newlines only."""
    if PROMPT_MARKER not in arg:
        return shlex.quote(arg)
    before, after = arg.split(PROMPT_MARKER, 1)
    file = shlex.quote(str(path))
    read = f'"$(cat -- {file} && rm -f -- {file})"'
    return "".join(part for part in (shlex.quote(before) if before else "", read, shlex.quote(after) if after else ""))


def _any(patterns: Sequence[str], text: str) -> bool:
    return any(re.search(p, text) for p in patterns)
