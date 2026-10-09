"""Per-task launch locks (ADR 0007).

One lock file per task under `<home>/locks/`, taken with `fcntl.flock` and held for the whole launch. The
operating system drops the lock when the holder dies, however it dies, so a crashed launch can never leave a
stale lock behind and nothing here needs a PID check or an expiry. Different tasks use different files and
never wait for each other. There is no daemon: the lock lives exactly as long as the `open` process.
"""

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from agent_launcher.errors import LauncherError
from agent_launcher.paths import launcher_home

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
                        f"Another agent-launcher process is launching task {task_id} and did not finish in time. "
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
