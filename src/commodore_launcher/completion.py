"""Archiving and unarchiving a task (ADR 0018, as ADR 0021 keeps it).

Archiving is metadata only (`tasks.state`): the checkout and the session record stay. `unarchive` restores the state
kept in `tasks.archived_state`; for a task archived before that was kept (a #18 reassignment) the state is derived:
`active` when it has a session, else `created`. Each change is a row in `task_events`.

A task the first-run copy archived (`is_copy_archived`) is one whose conversation started in an old worktree. Unarchiving
it, by `tasks unarchive` or by `open`, detaches that session's copied terminal and conversation ID, so the next launch
starts a fresh conversation on the same session (`launch.open_task`).
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from commodore_launcher import copied_state
from commodore_launcher.locks import task_lock
from commodore_launcher.sessions import primary_session
from commodore_launcher.state import transaction
from commodore_launcher.tasks import (
    TASK_ACTIVE,
    TASK_ARCHIVED,
    TASK_CREATED,
    Task,
    TaskError,
    archive_tasks,
    get_user_task,
    record_event,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ArchiveResult:
    task: Task
    changed: bool


def archive_task(conn: sqlite3.Connection, task_ref: str) -> ArchiveResult:
    task = get_user_task(conn, task_ref)
    with task_lock(task.id):
        task = get_user_task(conn, task.id)
        if task.state == TASK_ARCHIVED:
            return ArchiveResult(task, False)
        with transaction(conn):
            archive_tasks(conn, [task.id], "tasks archive")
    return ArchiveResult(get_user_task(conn, task.id), True)


def is_copy_archived(conn: sqlite3.Connection, task: Task) -> bool:
    """Archived by the first-run copy: the task's latest `archived` event is the copy's. A task the user archived
    (before or after the copy) or that the copy found already archived is not."""
    if task.state != TASK_ARCHIVED:
        return False
    row = conn.execute(
        "SELECT detail FROM task_events WHERE task_id = ? AND event = 'archived' ORDER BY id DESC LIMIT 1", (task.id,)
    ).fetchone()
    return row is not None and row[0] == copied_state.ARCHIVE_DETAIL


def unarchive_locked(conn: sqlite3.Connection, task: Task) -> None:
    """Restore the state the task had. The caller holds the task's lock and has checked it is archived. For a task the
    copy archived, also forget the copied session's terminal and conversation ID (kept as one session row, ADR 0005)
    and the copied launch record: they point into an old worktree."""
    copied = is_copy_archived(conn, task)
    restored = task.archived_state
    if restored is None or restored == TASK_ARCHIVED:
        restored = TASK_ACTIVE if primary_session(conn, task.id) else TASK_CREATED
    with transaction(conn):
        conn.execute(
            "UPDATE tasks SET state = ?, archived_state = NULL, updated_at = ? WHERE id = ?",
            (restored, _now(), task.id),
        )
        record_event(conn, task.id, "unarchived", restored)
        if copied:
            conn.execute("DELETE FROM launches WHERE task_id = ?", (task.id,))
            session = primary_session(conn, task.id)
            if session is not None:
                conn.execute("DELETE FROM terminal_sessions WHERE session_id = ?", (session.id,))
                conn.execute("UPDATE agent_conversations SET conversation_id = NULL WHERE session_id = ?", (session.id,))


def unarchive_task(conn: sqlite3.Connection, task_ref: str) -> ArchiveResult:
    task = get_user_task(conn, task_ref)
    with task_lock(task.id):
        task = get_user_task(conn, task.id)
        if task.state != TASK_ARCHIVED:
            raise TaskError("task_not_archived", f"Task {task.id} is not archived (state {task.state}).", task=task.id)
        unarchive_locked(conn, task)
    return ArchiveResult(get_user_task(conn, task.id), True)
