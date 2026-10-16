"""Per-task locks: the launch lock (ADR 0007) and the session lock (ADR 0022).

Lock files live under `<home>/locks/`, taken with `fcntl.flock`. The operating system drops a lock when the last
process holding it dies, however it dies, so nothing here needs a PID check or an expiry. Different tasks use
different files and never wait for each other. There is no daemon.

- The launch lock (`<task>.lock`) is held by `open` from resolving the task until its session is recorded.
- The session lock (`<task>.session.lock`) is held for as long as the task's harness runs. `open` takes it before it
  starts the harness and the harness inherits it, so it stays held if `commodore` itself dies first. A task's
  harness is running exactly when its session lock is held.
"""

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from commodore_launcher.errors import LauncherError
from commodore_launcher.paths import launcher_home

LOCK_WAIT_SECONDS = 60.0
"""How long a second launch of the same task waits for the first before giving up."""
_POLL_SECONDS = 0.05


def lock_path(task_id: str) -> Path:
    return launcher_home() / "locks" / f"{task_id}.lock"


@contextmanager
def task_lock(task_id: str, *, wait: float | None = None) -> Iterator[None]:
    """Hold the task's launch lock. Raises `task_busy` if another process keeps it past `wait` seconds."""
    path = lock_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + (LOCK_WAIT_SECONDS if wait is None else wait)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LauncherError(
                        "task_busy",
                        f"Another commodore process is launching task {task_id} and did not finish in time. "
                        "Nothing was changed; run the command again in a moment.",
                        task=task_id,
                    ) from None
                time.sleep(_POLL_SECONDS)
        # Informational only; the lock itself is the flock.
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        os.close(fd)  # closing releases the flock


def session_lock_path(task_id: str) -> Path:
    return launcher_home() / "locks" / f"{task_id}.session.lock"


class SessionClaim:
    """A task's session lock, held. `fd` is what a harness inherits to keep holding it."""

    def __init__(self, task_id: str, fd: int) -> None:
        self.task_id = task_id
        self.fd = fd

    def note_pid(self, pid: int) -> None:
        """Informational: the process the lock is held for, shown when another `open` finds it held."""
        os.ftruncate(self.fd, 0)
        os.pwrite(self.fd, f"{pid}\n".encode(), 0)

    def release(self) -> None:
        """Close this process's hold. A harness that inherited the lock keeps holding it until it exits."""
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def claim_session(task_id: str) -> SessionClaim | None:
    """Take the task's session lock without waiting, or None when something holds it (its harness is running)."""
    path = session_lock_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    claim = SessionClaim(task_id, fd)
    claim.note_pid(os.getpid())
    return claim


@dataclass(frozen=True)
class SessionHolder:
    """Who holds a task's session lock, as far as the lock file says."""

    pid: int | None


def session_holder(task_id: str) -> SessionHolder | None:
    """None when the task's session lock is free (no harness running), else who holds it. Takes nothing."""
    path = session_lock_path(task_id)
    if not path.exists():
        return None
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            text = os.pread(fd, 32, 0).decode(errors="replace").strip()
            return SessionHolder(int(text) if text.isdigit() else None)
        return None
    finally:
        os.close(fd)  # closing releases the probe's shared lock, if it got one
