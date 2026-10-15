"""Archiving and unarchiving a task (ADR 0018, as ADR 0021 keeps it).

Archiving is metadata only (`tasks.state`): the checkout and the session record stay. `unarchive` restores the state
kept in `tasks.archived_state`; for a task archived before that was kept (a #18 reassignment) the state is derived:
`active` when it has a session, else `created`. Each change is a row in `task_events`.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

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
    get_task,
    record_event,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ArchiveResult:
    task: Task
    changed: bool


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
    return ArchiveResult(get_task(conn, task.id), True)
