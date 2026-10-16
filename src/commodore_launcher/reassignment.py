"""What a profile reassignment would affect, and the explicit resolutions (ticket #18).

Changing a repository's profile never closes a session or rewrites a stored
`sessions.profile` / `tasks.profile`. The only effects are the association itself and, with
`ARCHIVE_TASKS`, the state of the repository's tasks. Everything here is read-only.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from commodore_launcher.sessions import SessionRecord, primary_session
from commodore_launcher.tasks import TASK_ARCHIVED, maintenance_sql

ARCHIVE_TASKS = "archive-tasks"
KEEP_TASKS = "keep-tasks"
CANCEL = "cancel"
RESOLUTIONS = (ARCHIVE_TASKS, KEEP_TASKS, CANCEL)

Liveness = Callable[[SessionRecord], bool | None]
"""Whether a session's harness is running: True, False, or None when it cannot be told."""


@dataclass(frozen=True)
class AffectedTask:
    task_id: str
    title: str
    state: str
    profile: str
    session_id: str | None
    session_profile: str | None
    agent: str | None
    live: bool | None
    conversation_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task_id,
            "title": self.title,
            "state": self.state,
            "profile": self.profile,
            "session": (
                {
                    "id": self.session_id,
                    "profile": self.session_profile,
                    "harness": self.agent,
                    "live": self.live,
                }
                if self.session_id
                else None
            ),
            "conversation_id": self.conversation_id,
        }

    def describe(self) -> str:
        session = "no session"
        if self.session_id:
            live = {True: "live", False: "not live", None: "liveness unknown"}[self.live]
            session = f"session {self.session_id} ({self.agent}, {live})"
        conversation = f"conversation {self.conversation_id}" if self.conversation_id else "no conversation ID"
        return f"{self.task_id} [{self.state}] {self.title!r}: profile {self.profile}, {session}, {conversation}"


def affected_tasks(
    conn: sqlite3.Connection, repository_id: int, new_profile: str, liveness: Liveness | None = None
) -> list[AffectedTask]:
    """The repository's tasks in any state that a change to `new_profile` leaves under another profile.

    Archived tasks are already resolved, and tasks already on `new_profile` (kept earlier, then the repository
    went back) simply become openable again. The hidden maintenance task is not a user's task and is left out.
    """
    hidden, hidden_params = maintenance_sql()
    rows = conn.execute(
        "SELECT id, title, state, profile FROM tasks WHERE repository_id = ? AND state != ? AND profile != ? "
        f"AND NOT {hidden} ORDER BY created_at, id",
        (repository_id, TASK_ARCHIVED, new_profile, *hidden_params),
    ).fetchall()
    found: list[AffectedTask] = []
    for task_id, title, state, profile in rows:
        session = primary_session(conn, task_id)
        live: bool | None = None
        if session is not None and liveness is not None:
            try:
                live = liveness(session)
            except Exception:  # a probe must never block listing what a reassignment would affect
                live = None
        found.append(
            AffectedTask(
                task_id, title, state, profile,
                session.id if session else None,
                session.profile if session else None,
                session.agent if session else None,
                live,
                session.agent_conversation_id if session else None,
            )
        )
    return found
