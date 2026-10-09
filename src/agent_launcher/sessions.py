"""Session records. Three identities, three tables (SPEC §3):

- `sessions`: one launch of a task by a profile's agent. Its own ID.
- `agent_conversations`: the agent's own conversation ID (Claude/Codex/Copilot), when known.
  Unknown at launch for now; later tickets fill it in.
- `terminal_sessions`: the workspace/surface the adapter created. Can vanish while the
  task, session and conversation remain.
"""

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from agent_launcher.state import transaction
from agent_launcher.tasks import ID_ALPHABET, mark_active
from agent_launcher.terminals import TerminalSessionRef

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
    terminal: TerminalSessionRef | None
    agent_conversation_id: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "profile": self.profile,
            "agent": self.agent,
            "state": self.state,
            "agent_conversation_id": self.agent_conversation_id,
            "terminal": self.terminal.to_dict() if self.terminal else None,
            "created_at": self.created_at,
        }


def record_session(
    conn: sqlite3.Connection, task_id: str, profile: str, agent: str, terminal: TerminalSessionRef
) -> SessionRecord:
    """Write the session, its terminal identity and the task's new state in one transaction."""
    now = _now()
    session_id = "s-" + "".join(secrets.choice(ID_ALPHABET) for _ in range(8))
    with transaction(conn):
        conn.execute(
            "INSERT INTO sessions (id, task_id, profile, agent, state, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, task_id, profile, agent, SESSION_LAUNCHED, now),
        )
        conn.execute(
            "INSERT INTO agent_conversations (session_id, agent, conversation_id) VALUES (?, ?, NULL)",
            (session_id, agent),
        )
        conn.execute(
            "INSERT INTO terminal_sessions (session_id, adapter, workspace_id, surface_id) VALUES (?, ?, ?, ?)",
            (session_id, terminal.adapter, terminal.workspace_id, terminal.surface_id),
        )
        mark_active(conn, task_id, agent)
    return SessionRecord(session_id, task_id, profile, agent, SESSION_LAUNCHED, terminal, None, now)


def sessions_for_task(conn: sqlite3.Connection, task_id: str) -> list[SessionRecord]:
    rows = conn.execute(
        """SELECT s.id, s.task_id, s.profile, s.agent, s.state, s.created_at,
                  c.conversation_id, t.adapter, t.workspace_id, t.surface_id
           FROM sessions s
           LEFT JOIN agent_conversations c ON c.session_id = s.id
           LEFT JOIN terminal_sessions t ON t.session_id = s.id
           WHERE s.task_id = ? ORDER BY s.created_at, s.id""",
        (task_id,),
    ).fetchall()
    return [
        SessionRecord(
            r[0], r[1], r[2], r[3], r[4],
            TerminalSessionRef(r[7], r[8], r[9]) if r[7] else None,
            r[6], r[5],
        )
        for r in rows
    ]


def last_used_agent(conn: sqlite3.Connection, profile: str) -> str | None:
    """The agent most recently launched under `profile`. Only that profile's sessions count."""
    row = conn.execute(
        "SELECT agent FROM sessions WHERE profile = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (profile,)
    ).fetchone()
    return row[0] if row else None
