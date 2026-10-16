"""The task registry: GitHub issue and PR tasks and their durable identity (ADR 0008).

A task ID is opaque and stable: `t-` plus eight random characters from a lower-case
alphabet without look-alikes (short enough to type). It is never derived from a title,
a path or a GitHub issue number.

`tasks.source` is `github` for every task `open` creates. `local` rows exist only in a database copied from the old
launcher: they stay listed and archived and can't be opened. The one other row that isn't a GitHub task is the hidden
maintenance task of `configure` (`is_maintenance`); the user-facing lookups skip it.

A task is not a session. The session and its harness conversation live in `sessions.py`; a task keeps its identity
when the conversation is replaced.
"""

import secrets
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from commodore_launcher.errors import LauncherError
from commodore_launcher.locks import task_lock
from commodore_launcher.logs import trace
from commodore_launcher.state import transaction

ID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
ID_PREFIX = "t-"
ID_LENGTH = 8

TASK_CREATED = "created"
TASK_LAUNCHING = "launching"
TASK_LAUNCH_FAILED = "launch_failed"
TASK_ACTIVE = "active"
"""A task is `active` only once its harness session is recorded. `launching` is a first launch in progress or
interrupted (a killed process cannot update it); `launch_failed` is one that stopped with an error or Ctrl-C
(see `launches.py`). `open` again resumes either."""
TASK_ARCHIVED = "archived"
"""Set by `tasks archive` (ticket #22) and by a repository reassignment with `--archive-tasks` (ticket #18).
`open` and `resume` refuse an archived task, except one the first-run copy archived (`completion.is_copy_archived`). Files and sessions are untouched.
`tasks unarchive` restores the state recorded in `tasks.archived_state` (see `completion.py`)."""


class TaskError(LauncherError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


MAINTENANCE_MARKER = "commodore-launcher-configure"
"""Stored in `tasks.workflow` for the maintenance task of `configure` (`configure` re-exports it)."""


def is_maintenance_row(source: str, workflow: str | None) -> bool:
    """The one definition of the maintenance task. `maintenance_sql` says the same in SQL; change them together."""
    return workflow == MAINTENANCE_MARKER and source == "local"


def maintenance_sql(alias: str = "") -> tuple[str, tuple[str, ...]]:
    """`is_maintenance_row` as SQL over a `tasks` row: the fragment (`alias` is the table alias) and the parameters it
    binds, so a caller binds nothing by hand. Change this and `is_maintenance_row` together."""
    prefix = f"{alias}." if alias else ""
    return f"({prefix}source = 'local' AND COALESCE({prefix}workflow, '') = ?)", (MAINTENANCE_MARKER,)


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
    """`github`, or `local` for a task copied from the old launcher (and the maintenance task). What is specific to a source lives in its own table (`task_github`)."""
    url: str | None = None
    """The GitHub URL of a github task: what Commodore is invoked on."""
    workflow: str | None = None
    """A marker, not a choice: `MAINTENANCE_MARKER` for the maintenance task, else unused. Any other stored value (from
    a release that had workflows) is ignored and never read as a rule."""
    archived_state: str | None = None
    """The state the task had when it was archived, for `tasks unarchive`. NULL for tasks archived before it was kept."""

    @property
    def is_maintenance(self) -> bool:
        """The one non-GitHub task that launches: the hidden maintenance task of `configure`,
        found again by its marker."""
        return is_maintenance_row(self.source, self.workflow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "repository_id": self.repository_id,
            "repo_path": self.repo_path,
            "profile": self.profile,
            "harness": self.agent,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source": self.source,
            "url": self.url,
            "workflow": self.workflow,
        }


_INSERT_COLUMNS = "id, title, description, repository_id, repo_path, profile, agent, state, created_at, updated_at, source"
_COLUMNS = (
    "t.id, t.title, t.description, t.repository_id, t.repo_path, t.profile, t.agent, t.state, t.created_at, "
    "t.updated_at, t.source, g.url, t.workflow, t.archived_state"
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
    """Internal: the creator of the maintenance task (`configure.prepare_task`). No command creates any other task."""
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


def delete_unstarted_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Remove a task that was just created and never launched, with every row that points at it, and say whether it
    did. Internal: only the maintenance task is created without a harness, and a refused launch must leave nothing
    behind. Under the task lock, and a task with a session or a launch row is left alone."""
    with task_lock(task_id):
        if conn.execute(
            "SELECT 1 FROM sessions WHERE task_id = ? UNION ALL SELECT 1 FROM launches WHERE task_id = ?",
            (task_id, task_id),
        ).fetchone():
            return False
        _delete_task_rows(conn, task_id)
    trace("task deleted before launch", task=task_id)
    return True


def _delete_task_rows(conn: sqlite3.Connection, task_id: str) -> None:
    tables = [
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        if any(fk[2] == "tasks" for fk in conn.execute(f'PRAGMA foreign_key_list("{r[0]}")').fetchall())
    ]
    with transaction(conn):
        for table in tables:
            conn.execute(f'DELETE FROM "{table}" WHERE task_id = ?', (task_id,))
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


def _row(row: tuple) -> Task:
    return Task(*row)


def get_task(conn: sqlite3.Connection, ref: str, *, user: bool = False) -> Task:
    """A task by full ID, or by a unique prefix of it. Anything else is an error, never a guess. With `user`, the
    hidden maintenance task is not a candidate at all (so it can't make a prefix ambiguous)."""
    ref = ref.strip().lower()
    hidden, params = "", ()
    if user:
        fragment, params = maintenance_sql("t")
        hidden = f" AND NOT {fragment}"
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM {_FROM} WHERE (t.id = ? OR t.id LIKE ? ESCAPE '\\'){hidden}",
        (ref, ref.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", *params),
    ).fetchall()
    exact = [r for r in rows if r[0] == ref]
    rows = exact or rows
    if not rows or not ref:
        raise TaskError("task_not_found", f"No task matches {ref!r}. List them with `commodore tasks list`.")
    if len(rows) > 1:
        raise TaskError(
            "ambiguous_task",
            f"{ref!r} matches more than one task; type more of the ID.",
            matches=sorted(r[0] for r in rows),
        )
    return _row(rows[0])


def get_user_task(conn: sqlite3.Connection, ref: str) -> Task:
    """`get_task` for what a person types: the maintenance task is not theirs, so it is not found."""
    return get_task(conn, ref, user=True)


def list_user_tasks(conn: sqlite3.Connection) -> list["Task"]:
    return [t for t in list_tasks(conn) if not t.is_maintenance]


def require_openable_kind(task: Task) -> None:
    """A task opens only from a GitHub issue or PR URL, or as the maintenance task. A copied local task never does."""
    if not task.is_maintenance and not (task.source == "github" and task.url):
        raise TaskError(
            "task_needs_url",
            f"Task {task.id} needs a GitHub issue or PR URL: Commodore is invoked on the URL, and this task has none. "
            "Nothing was started. Open the issue or pull request by its URL instead.",
            task=task.id,
        )


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
            f"Task {task.id} is archived. It is not opened; its checkout and session are untouched. "
            f"Run `commodore tasks unarchive {task.id}` to work on it again "
            "(a task archived by a profile reassignment also needs its repository back on the task's profile); "
            f"`commodore tasks show {task.id}` still shows it.",
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
