"""Completion detection and archiving (ticket #22, SPEC §25, ADR 0018).

GitHub closing an issue, or closing or merging a pull request, sets the task's `cleanup_eligible_at`. It is a flag
beside the lifecycle state, and the only effect: no session is closed, nothing is deleted, the task still opens.
If GitHub later shows the item open again the flag is cleared. Each change is a row in `task_events`.

Archiving is metadata only (`tasks.state`): files, worktree and session record stay. `unarchive` restores the state
kept in `tasks.archived_state`; for a task archived before that was kept (a #18 reassignment) the state is derived:
`active` when it has a session, else `created`.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from agent_launcher.locks import task_lock
from agent_launcher.logs import trace
from agent_launcher.sessions import primary_session
from agent_launcher.state import transaction
from agent_launcher.worktrees import get_worktree
from agent_launcher.tasks import (
    TASK_ACTIVE,
    TASK_ARCHIVED,
    TASK_CREATED,
    Task,
    TaskError,
    archive_tasks,
    get_task,
    list_tasks,
    record_event,
)

COMPLETED_STATES = ("closed", "merged")
"""GitHub states that finish a task: a closed issue, a closed or merged pull request."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def apply_github_state(conn: sqlite3.Connection, task_id: str, github_state: str) -> str | None:
    """Set or clear the task's cleanup flag from the item's GitHub state. Returns `completed` or `reopened` when it
    changed. Any other state (including `unknown`) changes nothing. Call inside a transaction."""
    row = conn.execute("SELECT cleanup_eligible_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    flagged = row[0] is not None
    if github_state in COMPLETED_STATES and not flagged:
        conn.execute("UPDATE tasks SET cleanup_eligible_at = ? WHERE id = ?", (_now(), task_id))
        record_event(conn, task_id, "completed", github_state)
        trace("task completed", task=task_id, state=github_state)
        return "completed"
    if github_state == "open" and flagged:
        conn.execute("UPDATE tasks SET cleanup_eligible_at = NULL WHERE id = ?", (task_id,))
        record_event(conn, task_id, "reopened", github_state)
        return "reopened"
    return None


def completed_tasks(conn: sqlite3.Connection) -> list[Task]:
    """Tasks flagged eligible for cleanup, archived or not."""
    return [t for t in list_tasks(conn) if t.cleanup_eligible_at is not None]


@dataclass(frozen=True)
class ArchiveResult:
    task: Task
    changed: bool
    warning: str | None = None


def archive_task(conn: sqlite3.Connection, task_ref: str) -> ArchiveResult:
    task = get_task(conn, task_ref)
    with task_lock(task.id):
        task = get_task(conn, task.id)
        if task.state == TASK_ARCHIVED:
            return ArchiveResult(task, False)
        with transaction(conn):
            archive_tasks(conn, [task.id], "tasks archive")
    return ArchiveResult(get_task(conn, task.id), True)


def unarchive_task(conn: sqlite3.Connection, task_ref: str) -> ArchiveResult:
    task = get_task(conn, task_ref)
    with task_lock(task.id):
        task = get_task(conn, task.id)
        if task.state != TASK_ARCHIVED:
            raise TaskError("task_not_archived", f"Task {task.id} is not archived (state {task.state}).", task=task.id)
        restored = task.archived_state
        if restored is None or restored == TASK_ARCHIVED:
            restored = TASK_ACTIVE if primary_session(conn, task.id) else TASK_CREATED
        with transaction(conn):
            conn.execute(
                "UPDATE tasks SET state = ?, archived_state = NULL, updated_at = ? WHERE id = ?",
                (restored, _now(), task.id),
            )
            record_event(conn, task.id, "unarchived", restored)
    tree = get_worktree(conn, task.id)
    warning = (
        f"The worktree {tree.path} was removed by cleanup, so `open` stops with worktree_removed until you adopt a "
        f"new one: `agent-launcher worktrees associate {task.id} <path> --force`."
        if tree is not None and tree.removed_at else None
    )
    return ArchiveResult(get_task(conn, task.id), True, warning)
