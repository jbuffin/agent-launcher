"""Session records. Two identities, two tables:

- `sessions`: one launch of a task by a profile's harness. Its own ID.
- `agent_conversations`: the harness's own conversation ID (Claude/Codex/Copilot). Fixed at launch
  when the harness allows it (Claude Code: `--session-id`), NULL when it does not.

Where the harness runs is not recorded: whether it is running is the task's session lock (`locks`, ADR 0022).
"""

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from commodore_launcher.errors import LauncherError
from commodore_launcher.state import transaction
from commodore_launcher.tasks import ID_ALPHABET, mark_active

SESSION_LAUNCHED = "launched"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class SessionRecord:
    id: str
    task_id: str
    profile: str
    agent: str
    state: str
    agent_conversation_id: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "profile": self.profile,
            "harness": self.agent,
            "state": self.state,
            "harness_conversation_id": self.agent_conversation_id,
            "created_at": self.created_at,
        }


def record_session(
    conn: sqlite3.Connection,
    task_id: str,
    profile: str,
    agent: str,
    conversation_id: str | None = None,
) -> SessionRecord:
    """Write the session, its conversation ID and the task's new state in one transaction.

    A task has one primary session for its whole life: a second one is refused here, whatever the caller
    thought it knew. Reopening and resuming update that session in place, and so does a fresh conversation.
    """
    now = _now()
    session_id = "s-" + "".join(secrets.choice(ID_ALPHABET) for _ in range(8))
    with transaction(conn):
        if conn.execute("SELECT 1 FROM sessions WHERE task_id = ?", (task_id,)).fetchone():
            raise LauncherError(
                "session_exists",
                f"Task {task_id} already has a session. A task never gets a second one.",
                task=task_id,
            )
        conn.execute(
            "INSERT INTO sessions (id, task_id, profile, agent, state, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, task_id, profile, agent, SESSION_LAUNCHED, now),
        )
        conn.execute(
            "INSERT INTO agent_conversations (session_id, agent, conversation_id) VALUES (?, ?, ?)",
            (session_id, agent, conversation_id),
        )
        mark_active(conn, task_id, agent)
        # The launch is complete in the same transaction that makes the task running (ADR 0007).
        conn.execute("DELETE FROM launches WHERE task_id = ?", (task_id,))
    return SessionRecord(session_id, task_id, profile, agent, SESSION_LAUNCHED, conversation_id, now)


def sessions_for_task(conn: sqlite3.Connection, task_id: str) -> list[SessionRecord]:
    rows = conn.execute(
        """SELECT s.id, s.task_id, s.profile, s.agent, s.state, s.created_at,
                  c.conversation_id
           FROM sessions s
           LEFT JOIN agent_conversations c ON c.session_id = s.id
           WHERE s.task_id = ? ORDER BY s.created_at, s.id""",
        (task_id,),
    ).fetchall()
    return [SessionRecord(r[0], r[1], r[2], r[3], r[4], r[6], r[5]) for r in rows]


def last_used_agent(conn: sqlite3.Connection, profile: str) -> str | None:
    """The harness most recently launched under `profile`. Only that profile's sessions count."""
    row = conn.execute(
        "SELECT agent FROM sessions WHERE profile = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (profile,)
    ).fetchone()
    return row[0] if row else None


def primary_session(conn: sqlite3.Connection, task_id: str) -> SessionRecord | None:
    """The task's one session, if it has been opened."""
    found = sessions_for_task(conn, task_id)
    return found[-1] if found else None


def replace_conversation(conn: sqlite3.Connection, session_id: str, conversation_id: str | None) -> None:
    """Record a fresh harness conversation on the session (None: the harness takes no ID). The session and the task
    stay."""
    with transaction(conn):
        conn.execute("UPDATE agent_conversations SET conversation_id = ? WHERE session_id = ?", (conversation_id, session_id))
