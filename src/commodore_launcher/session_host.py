"""Session hosts: what owns the terminal a session's harness runs in (ADR 0022).

The core asks a host three things: is this task's harness running, claim its session for a new harness, and run a
harness on a claim. Whether a harness is running is the task's session lock (`locks`), so every host answers it
the same way, from any process.

`ForegroundHost` is the only host: the harness runs as a child of `commodore`, in the terminal `commodore` was
started from, and `commodore` waits for it. A host that runs harnesses headless, for a dashboard or a second
terminal to attach to, would be another implementation of `SessionHost`; nothing outside this module would change.
"""

import os
import signal
import subprocess
import threading
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field

from commodore_launcher.errors import LauncherError
from commodore_launcher.locks import SessionClaim, SessionHolder, claim_session, session_holder


@dataclass(frozen=True)
class HarnessCommand:
    """A harness to start: argv (run directly, never through a shell), its working directory and its environment."""

    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    """The full environment. It may hold secrets: never log or record the values."""
    profile_env: tuple[str, ...] = ()
    """The names of the variables the profile sets, for display. Never their values."""
    prompt: str | None = None
    """The prompt `argv` carries (the harness submits it at launch), for display."""


class SessionHost(ABC):
    name: str

    def running(self, task_id: str) -> SessionHolder | None:
        """Who runs the task's harness, or None when nothing does."""
        return session_holder(task_id)

    def claim(self, task_id: str) -> SessionClaim | None:
        """The task's session lock, or None when its harness is running."""
        return claim_session(task_id)

    @abstractmethod
    def run(self, claim: SessionClaim | None, command: HarnessCommand) -> int:
        """Run the harness until it exits and return its exit status. The claim is handed to the harness and released
        here once it exits. None (`resume --force` over a held lock) runs it without one."""


class ForegroundHost(SessionHost):
    """The harness runs in this terminal, as a child that inherits it, and `commodore` waits for it.

    The harness also inherits the session lock, so the task counts as running for as long as the harness does, even
    if `commodore` is killed first. While it waits, `commodore` ignores Ctrl-C and Ctrl-\\, which the terminal sends
    to both processes: they are the harness's keys."""

    name = "foreground"

    def run(self, claim: SessionClaim | None, command: HarnessCommand) -> int:
        pass_fds: tuple[int, ...] = ()
        if claim is not None:
            os.set_inheritable(claim.fd, True)
            pass_fds = (claim.fd,)
        try:
            try:
                process = subprocess.Popen(list(command.argv), cwd=command.cwd, env=dict(command.env), pass_fds=pass_fds)
            except OSError as exc:
                raise LauncherError(
                    "harness_start_failed",
                    f"Could not start {command.argv[0]}: {exc.strerror or exc}.",
                    executable=command.argv[0],
                ) from exc
            if claim is not None:
                claim.note_pid(process.pid)
            # Set after the start: an ignored signal stays ignored in the child, and the harness needs its own Ctrl-C.
            with _ignoring(signal.SIGINT, signal.SIGQUIT):
                status = process.wait()
        finally:
            if claim is not None:
                claim.release()
        return 128 - status if status < 0 else status


class _ignoring:
    def __init__(self, *signals: signal.Signals) -> None:
        self.signals = signals
        self.previous: dict[signal.Signals, object] = {}

    def __enter__(self) -> None:
        if threading.current_thread() is threading.main_thread():
            self.previous = {s: signal.signal(s, signal.SIG_IGN) for s in self.signals}

    def __exit__(self, *exc: object) -> None:
        for s, handler in self.previous.items():
            signal.signal(s, handler)  # type: ignore[arg-type]


def default_host() -> SessionHost:
    return ForegroundHost()
