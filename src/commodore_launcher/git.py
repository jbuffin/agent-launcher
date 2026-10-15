"""Git access: argv lists, timeouts, checked exit codes, machine-readable output only.

The launcher does not touch a task's checkout (ADR 0021). Its one user is the maintenance repository `configure`
creates, which the launcher owns.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from commodore_launcher.errors import LauncherError

GIT_TIMEOUT_SECONDS = 15.0


class GitError(LauncherError):
    """A git command failed, timed out, or is not installed."""


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C", GCM_INTERACTIVE="never")
    return env


def run_git(
    repo: str | Path, args: list[str], *, timeout: float = GIT_TIMEOUT_SECONDS, ok: tuple[int, ...] = (0,)
) -> GitResult:
    """`git -C <repo> <args>`. An exit code not in `ok` raises `GitError`; the caller decides what the others mean."""
    argv = ["git", "-C", str(repo), *args]
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, check=False, env=_env()
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError("git_timeout", f"`git {args[0]}` timed out after {timeout:g}s.", command=args[0]) from exc
    except OSError as exc:
        raise GitError("git_unavailable", f"Cannot run git: {exc.strerror or exc}.") from exc
    if done.returncode not in ok:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise GitError(
            "git_failed",
            f"`git {args[0]}` failed (exit {done.returncode}): {detail[-1] if detail else 'no output'}",
            command=args[0],
            exit_code=done.returncode,
        )
    return GitResult(done.returncode, done.stdout, done.stderr)
