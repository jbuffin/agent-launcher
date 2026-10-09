"""The task registry: local tasks and their durable identity (SPEC §3).

A task ID is opaque and stable: `t-` plus eight random characters from a lower-case
alphabet without look-alikes (short enough to type). It is never derived from a title,
a path or a GitHub issue number, so linking a task to an issue (ticket #17) leaves it alone.

A task is not a session. The agent conversation and the terminal session live in
`sessions.py`; a task keeps its identity when either is gone.
"""

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from agent_launcher.errors import LauncherError
from agent_launcher.logs import trace
from agent_launcher.state import transaction

ID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
ID_PREFIX = "t-"
ID_LENGTH = 8

TASK_CREATED = "created"
TASK_ACTIVE = "active"
"""Lifecycle values for now. Ticket #11 replaces this with the transactional state machine."""


class TaskError(LauncherError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_task_id() -> str:
    return ID_PREFIX + "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LENGTH))


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    description: str
    repository_id: int
    repo_path: str
    profile: str
    agent: str | None
    state: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "repository_id": self.repository_id,
            "repo_path": self.repo_path,
            "profile": self.profile,
            "agent": self.agent,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


_COLUMNS = "id, title, description, repository_id, repo_path, profile, agent, state, created_at, updated_at"


def create_task(
    conn: sqlite3.Connection,
    title: str,
    description: str,
    repository_id: int,
    repo_path: str,
    profile: str,
) -> Task:
    title = title.strip()
    if not title:
        raise TaskError("invalid_task", "A task needs a title.")
    now = _now()
    with transaction(conn):
        while True:
            task_id = new_task_id()
            if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
                break
        conn.execute(
            f"INSERT INTO tasks ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (task_id, title, description.strip(), repository_id, repo_path, profile, TASK_CREATED, now, now),
        )
    trace("task created", task=task_id, profile=profile)
    return get_task(conn, task_id)


def _row(row: tuple) -> Task:
    return Task(*row)


def get_task(conn: sqlite3.Connection, ref: str) -> Task:
    """A task by full ID, or by a unique prefix of it. Anything else is an error, never a guess."""
    ref = ref.strip().lower()
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM tasks WHERE id = ? OR id LIKE ? ESCAPE '\\'",
        (ref, ref.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"),
    ).fetchall()
    exact = [r for r in rows if r[0] == ref]
    rows = exact or rows
    if not rows or not ref:
        raise TaskError("task_not_found", f"No task matches {ref!r}. List them with `agent-launcher tasks list`.")
    if len(rows) > 1:
        raise TaskError(
            "ambiguous_task",
            f"{ref!r} matches more than one task; type more of the ID.",
            matches=sorted(r[0] for r in rows),
        )
    return _row(rows[0])


def list_tasks(conn: sqlite3.Connection) -> list[Task]:
    rows = conn.execute(f"SELECT {_COLUMNS} FROM tasks ORDER BY created_at, id").fetchall()
    return [_row(r) for r in rows]


def mark_active(conn: sqlite3.Connection, task_id: str, agent: str) -> None:
    """Set the lifecycle state. Call inside a transaction (it does not open one)."""
    conn.execute(
        "UPDATE tasks SET state = ?, agent = ?, updated_at = ? WHERE id = ?",
        (TASK_ACTIVE, agent, _now(), task_id),
    )
