"""Session records. Three identities, three tables (SPEC §3):

- `sessions`: one launch of a task by a profile's agent. Its own ID.
- `agent_conversations`: the agent's own conversation ID (Claude/Codex/Copilot). Fixed at launch
  when the agent allows it (Claude Code: `--session-id`), NULL when it does not.
- `terminal_sessions`: the workspace/surface the adapter created. Can vanish while the
  task, session and conversation remain.
"""

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from agent_launcher.errors import LauncherError
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
    conn: sqlite3.Connection,
    task_id: str,
    profile: str,
    agent: str,
    terminal: TerminalSessionRef,
    conversation_id: str | None = None,
) -> SessionRecord:
    """Write the session, its terminal identity and the task's new state in one transaction.

    A task has one primary session for its whole life: a second one is refused here, whatever the caller
    thought it knew. Reopening, resuming and restarting update that session in place.
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
        conn.execute(
            "INSERT INTO terminal_sessions (session_id, adapter, workspace_id, surface_id, created_by_launcher) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, terminal.adapter, terminal.workspace_id, terminal.surface_id, int(terminal.created_by_launcher)),
        )
        mark_active(conn, task_id, agent)
        # The launch is complete in the same transaction that makes the task running (ADR 0007).
        conn.execute("DELETE FROM launches WHERE task_id = ?", (task_id,))
    return SessionRecord(session_id, task_id, profile, agent, SESSION_LAUNCHED, terminal, conversation_id, now)


def sessions_for_task(conn: sqlite3.Connection, task_id: str) -> list[SessionRecord]:
    rows = conn.execute(
        """SELECT s.id, s.task_id, s.profile, s.agent, s.state, s.created_at,
                  c.conversation_id, t.adapter, t.workspace_id, t.surface_id, t.created_by_launcher
           FROM sessions s
           LEFT JOIN agent_conversations c ON c.session_id = s.id
           LEFT JOIN terminal_sessions t ON t.session_id = s.id
           WHERE s.task_id = ? ORDER BY s.created_at, s.id""",
        (task_id,),
    ).fetchall()
    return [
        SessionRecord(
            r[0], r[1], r[2], r[3], r[4],
            TerminalSessionRef(r[7], r[8], r[9], bool(r[10])) if r[7] else None,
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


def primary_session(conn: sqlite3.Connection, task_id: str) -> SessionRecord | None:
    """The task's one session, if it has been opened."""
    found = sessions_for_task(conn, task_id)
    return found[-1] if found else None


def replace_terminal(
    conn: sqlite3.Connection, session_id: str, terminal: TerminalSessionRef, *, conversation_id: str | None = None
) -> None:
    """Point the session at a new terminal session (after a resume or restart). With `conversation_id`,
    also record a new agent conversation. The session, the task and their worktree stay as they are."""
    with transaction(conn):
        conn.execute(
            "UPDATE terminal_sessions SET adapter = ?, workspace_id = ?, surface_id = ?, created_by_launcher = ? "
            "WHERE session_id = ?",
            (terminal.adapter, terminal.workspace_id, terminal.surface_id, int(terminal.created_by_launcher), session_id),
        )
        if conversation_id is not None:
            conn.execute(
                "UPDATE agent_conversations SET conversation_id = ? WHERE session_id = ?", (conversation_id, session_id)
            )


def terminal_in_use(conn: sqlite3.Connection, terminal: TerminalSessionRef) -> bool:
    """Whether any session record already points at this terminal session."""
    return (
        conn.execute(
            "SELECT 1 FROM terminal_sessions WHERE adapter = ? AND workspace_id = ? AND surface_id IS ?",
            (terminal.adapter, terminal.workspace_id, terminal.surface_id),
        ).fetchone()
        is not None
    )
