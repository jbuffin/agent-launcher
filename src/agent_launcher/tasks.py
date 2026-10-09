"""The task registry: local tasks and their durable identity (SPEC §3).

A task ID is opaque and stable: `t-` plus eight random characters from a lower-case
alphabet without look-alikes (short enough to type). It is never derived from a title,
a path or a GitHub issue number, so linking a task to an issue (ticket #17) leaves it alone.

A task is not a session. The agent conversation and the terminal session live in
`sessions.py`; a task keeps its identity when either is gone.
"""

import secrets
import sqlite3
from collections.abc import Sequence
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
TASK_LAUNCHING = "launching"
TASK_LAUNCH_FAILED = "launch_failed"
TASK_ACTIVE = "active"
"""A task is `active` only once its agent session is recorded. `launching` is a first launch in progress or
interrupted (a killed process cannot update it); `launch_failed` is one that stopped with an error or Ctrl-C
(see `launches.py`). `open` again resumes either."""
TASK_ARCHIVED = "archived"
"""Set by `tasks archive` (ticket #22) and by a repository reassignment with `--archive-tasks` (ticket #18).
`open`, `resume`, `prompt` and `restart` refuse an archived task. Files, worktrees and sessions are untouched.
`tasks unarchive` restores the state recorded in `tasks.archived_state` (see `completion.py`)."""


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
    source: str = "local"
    """`local` or `github`. What is specific to a source lives in its own table (`task_github`)."""
    url: str | None = None
    """The GitHub URL of a github task, which is also its prompt."""
    workflow: str | None = None
    """The workflow chosen at first open (`routing`). Never re-routed afterwards."""
    cleanup_eligible_at: str | None = None
    """When GitHub was seen reporting the issue closed or the PR closed/merged (ticket #22). A flag beside the
    lifecycle state: nothing is terminated or deleted because of it."""
    archived_state: str | None = None
    """The state the task had when it was archived, for `tasks unarchive`. NULL for tasks archived before it was kept."""

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
            "source": self.source,
            "url": self.url,
            "workflow": self.workflow,
            "eligible_for_cleanup": self.cleanup_eligible_at is not None,
            "cleanup_eligible_at": self.cleanup_eligible_at,
        }


_INSERT_COLUMNS = "id, title, description, repository_id, repo_path, profile, agent, state, created_at, updated_at, source"
_COLUMNS = (
    "t.id, t.title, t.description, t.repository_id, t.repo_path, t.profile, t.agent, t.state, t.created_at, "
    "t.updated_at, t.source, g.url, t.workflow, t.cleanup_eligible_at, t.archived_state"
)
_FROM = "tasks t LEFT JOIN task_github g ON g.task_id = t.id"


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
            f"INSERT INTO tasks ({_INSERT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'local')",
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
        f"SELECT {_COLUMNS} FROM {_FROM} WHERE t.id = ? OR t.id LIKE ? ESCAPE '\\'",
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
    rows = conn.execute(f"SELECT {_COLUMNS} FROM {_FROM} ORDER BY t.created_at, t.id").fetchall()
    return [_row(r) for r in rows]


def mark_active(conn: sqlite3.Connection, task_id: str, agent: str) -> None:
    """Set the lifecycle state. Call inside a transaction (it does not open one)."""
    conn.execute(
        "UPDATE tasks SET state = ?, agent = ?, updated_at = ? WHERE id = ?",
        (TASK_ACTIVE, agent, _now(), task_id),
    )


def set_state(conn: sqlite3.Connection, task_id: str, state: str) -> None:
    with transaction(conn):
        conn.execute("UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?", (state, _now(), task_id))


def set_workflow(conn: sqlite3.Connection, task_id: str, workflow: str) -> None:
    with transaction(conn):
        conn.execute("UPDATE tasks SET workflow = ?, updated_at = ? WHERE id = ?", (workflow, _now(), task_id))


def require_not_archived(task: Task) -> None:
    if task.state == TASK_ARCHIVED:
        raise TaskError(
            "task_archived",
            f"Task {task.id} is archived. It is not opened; its files, worktree and session are untouched. "
            f"Run `agent-launcher tasks unarchive {task.id}` to work on it again "
            "(a task archived by a profile reassignment also needs its repository back on the task's profile); "
            f"`agent-launcher tasks show {task.id}` still shows it.",
            task=task.id,
        )


def record_event(conn: sqlite3.Connection, task_id: str, event: str, detail: str | None = None) -> None:
    """Append to the task's lifecycle history. Call inside a transaction (it does not open one)."""
    conn.execute("INSERT INTO task_events (task_id, event, detail, at) VALUES (?, ?, ?, ?)", (task_id, event, detail, _now()))


def task_events(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT event, detail, at FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
    return [{"event": r[0], "detail": r[1], "at": r[2]} for r in rows]


def archive_tasks(conn: sqlite3.Connection, task_ids: Sequence[str], detail: str = "profile reassignment") -> None:
    """Mark tasks archived, remembering their state. Only the task's state changes. Call inside a transaction
    (it does not open one). Tasks already archived are skipped."""
    now = _now()
    for task_id in task_ids:
        done = conn.execute(
            "UPDATE tasks SET archived_state = state, state = ?, updated_at = ? WHERE id = ? AND state != ?",
            (TASK_ARCHIVED, now, task_id, TASK_ARCHIVED),
        )
        if done.rowcount:
            record_event(conn, task_id, "archived", detail)
