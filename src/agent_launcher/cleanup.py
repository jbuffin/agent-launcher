"""Conservative worktree cleanup (ticket #22, SPEC §25, ADR 0018).

Only launcher-created worktrees are ever offered, and only when every check passes. A check that cannot be answered
counts against removal. Removal is `git worktree remove` without `--force`, then `git branch -d` (merged branches
only). The task, session and worktree rows stay; the worktree row gets `removed_at`.
"""

import shutil
import sqlite3
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_launcher import git
from agent_launcher.errors import LauncherError
from agent_launcher.locks import task_lock
from agent_launcher.logs import trace
from agent_launcher.sessions import primary_session
from agent_launcher.state import transaction
from agent_launcher.tasks import TASK_ARCHIVED, Task, get_task, list_tasks, record_event
from agent_launcher.terminals import TerminalAdapter
from agent_launcher.worktrees import CREATED, WorktreeRecord, get_worktree

LSOF_TIMEOUT_SECONDS = 10.0

SessionLive = Callable[[Any], bool | None]
"""Whether a session record's terminal is alive: True, False, or None when it cannot be told."""
ProcessCheck = Callable[[str], bool | None]
"""Whether any process uses the directory: True, False, or None when it cannot be told."""


@dataclass(frozen=True)
class Blocker:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass
class Candidate:
    task: Task
    worktree: WorktreeRecord
    blockers: list[Blocker] = field(default_factory=list)

    @property
    def removable(self) -> bool:
        return not self.blockers

    @property
    def ignored(self) -> list[str]:
        """Ignored entries removal would delete too; only looked up for a removable worktree."""
        return git.ignored_entries(self.worktree.path) if self.removable else []

    def to_dict(self) -> dict[str, Any]:
        return {
            "ignored_entries": self.ignored,
            "task": self.task.id,
            "title": self.task.title,
            "state": self.task.state,
            "eligible_for_cleanup": self.task.cleanup_eligible_at is not None,
            "worktree": self.worktree.path,
            "branch": self.worktree.branch,
            "removable": self.removable,
            "blockers": [b.to_dict() for b in self.blockers],
        }


@dataclass(frozen=True)
class Removal:
    task_id: str
    path: str
    branch: str | None
    branch_deleted: bool
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task_id, "worktree": self.path, "branch": self.branch,
            "branch_deleted": self.branch_deleted, "note": self.note,
        }


def lsof_check(path: str) -> bool | None:
    """Best effort: is any process using files under `path`? None (unknown) when lsof is missing, times out or fails."""
    exe = shutil.which("lsof")
    if exe is None:
        return None
    try:
        done = subprocess.run(
            [exe, "-w", "-Fp", "+D", path], capture_output=True, text=True, timeout=LSOF_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if done.stdout.strip():
        return True
    return False if done.returncode == 1 else None


def session_liveness(adapter: TerminalAdapter) -> SessionLive:
    """A session's terminal is live while the configured adapter can still read its screen. A terminal of another
    adapter, an adapter that cannot read, or an error is unknown."""

    def probe(session) -> bool | None:
        ref = session.terminal
        if ref is None:
            return False
        if ref.adapter != adapter.name:
            return None
        try:
            return adapter.read_screen(ref) is not None
        except Exception:
            return None

    return probe


def task_is_active(task: Task) -> bool:
    """Every task that is neither archived nor completed is still being worked on (created, launching, launch_failed
    and active included): `open` would resume it."""
    return task.state != TASK_ARCHIVED and task.cleanup_eligible_at is None


def check_worktree(
    conn: sqlite3.Connection, task: Task, record: WorktreeRecord, *, live: SessionLive, in_use: ProcessCheck
) -> list[Blocker]:
    """Every reason this worktree must not be removed. Empty means all checks passed."""
    blockers: list[Blocker] = []
    add = lambda code, message: blockers.append(Blocker(code, message))  # noqa: E731
    if record.ownership != CREATED:
        add("adopted", "The launcher did not create this worktree (it was adopted); it is never removed by the launcher.")
        return blockers  # nothing else matters
    if record.removed_at:
        add("already_removed", f"Already removed on {record.removed_at}.")
        return blockers
    if task_is_active(task):
        add("task_active", f"The task is still active (state {task.state}); only completed or archived tasks are cleaned up.")
    path = Path(record.path)
    if not path.is_dir():
        add("worktree_missing", "The directory does not exist on disk; nothing to remove (see `git worktree prune`).")
        return blockers
    try:
        registered = {git.canonical(e.path) for e in git.list_worktrees(task.repo_path)}
    except LauncherError as exc:
        add("git_unknown", f"Could not list the repository's worktrees: {exc.message}")
        registered = None
    if registered is not None and git.canonical(path) not in registered:
        add("not_a_worktree", "Git does not list this directory as a worktree of the task's repository.")
        return blockers
    try:
        if git.has_changes(path):
            add("uncommitted_changes", "It has uncommitted changes or untracked files.")
    except LauncherError as exc:
        add("git_unknown", f"Could not read its status: {exc.message}")
    try:
        count = git.unpushed_commits(path, record.branch, record.base_ref)
        if count:
            add("unpushed_commits", f"{count} commit(s) are not on any remote-tracking branch or the base branch.")
    except LauncherError as exc:
        add("git_unknown", f"Could not check for unpushed commits: {exc.message}")
    session = primary_session(conn, task.id)
    if session is not None and session.terminal is not None:
        alive = live(session)
        if alive is None:
            add("session_unknown", "Its recorded terminal session cannot be checked, so it is treated as in use.")
        elif alive:
            add("session_live", "Its recorded terminal session is still live; close it first.")
    used = in_use(record.path)
    if used is None:
        add("process_unknown", "Whether a process uses the directory could not be checked, so it is treated as in use.")
    elif used:
        add("process_running", "A running process is using the directory.")
    return blockers


def candidates(
    conn: sqlite3.Connection,
    tasks: Sequence[str] | None = None,
    *,
    live: SessionLive,
    in_use: ProcessCheck = lsof_check,
) -> list[Candidate]:
    """With no `tasks`: launcher-created worktrees of completed or archived tasks. With them: those tasks' worktrees,
    whatever their state (the checks decide). A named task without a worktree is a `no_worktree` error."""
    found: list[Candidate] = []
    if tasks:
        chosen = []
        for ref in tasks:
            task = get_task(conn, ref)
            if get_worktree(conn, task.id) is None:
                raise LauncherError("no_worktree", f"Task {task.id} has no worktree.", task=task.id)
            if task not in chosen:
                chosen.append(task)
    else:
        chosen = [
            t for t in list_tasks(conn)
            if (t.cleanup_eligible_at is not None or t.state == TASK_ARCHIVED)
            and (w := get_worktree(conn, t.id)) is not None and w.ownership == CREATED and not w.removed_at
        ]
    for task in chosen:
        record = get_worktree(conn, task.id)
        assert record is not None
        found.append(Candidate(task, record, check_worktree(conn, task, record, live=live, in_use=in_use)))
    return found


def remove_candidate(
    conn: sqlite3.Connection, task_id: str, *, live: SessionLive, in_use: ProcessCheck = lsof_check
) -> Removal:
    """Re-run every check under the task lock, then remove. Raises `cleanup_refused` listing the blockers."""
    with task_lock(task_id):
        task = get_task(conn, task_id)
        record = get_worktree(conn, task.id)
        if record is None:
            raise LauncherError("no_worktree", f"Task {task.id} has no worktree.", task=task.id)
        blockers = check_worktree(conn, task, record, live=live, in_use=in_use)
        if blockers:
            raise LauncherError(
                "cleanup_refused",
                f"Not removing {record.path}: " + " ".join(b.message for b in blockers),
                task=task.id, blockers=[b.to_dict() for b in blockers],
            )
        git.remove_worktree(task.repo_path, record.path)  # no --force: git refuses what the checks missed
        deleted, note = False, None
        if record.branch:
            try:
                deleted = git.delete_branch_if_merged(task.repo_path, record.branch)
            except LauncherError as exc:
                note = f"Branch {record.branch} kept: {exc.message}"
            else:
                if not deleted:
                    note = f"Branch {record.branch} kept: it is not merged."
        with transaction(conn):
            conn.execute(
                "UPDATE worktrees SET removed_at = ? WHERE task_id = ?",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), task.id),
            )
            record_event(conn, task.id, "worktree_removed", record.path)
        trace("worktree removed", task=task.id, branch_deleted=deleted)
        return Removal(task.id, record.path, record.branch, deleted, note)
