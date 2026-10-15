"""The persisted state of a launch in progress (ADR 0007).

A first `open` moves through stages, each written down before the next starts:

    started -> terminal_created -> (session recorded = ready)

The row is deleted in the same transaction that records the session, so a task with a `launches` row is never
running. A retry reads the row and resumes: a recorded terminal session that still exists is adopted. A terminal
reference is only written from the adapter's own answer to `create_session`.
"""

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from commodore_launcher.state import transaction
from commodore_launcher.terminals import TerminalSessionRef

STARTED = "started"
TERMINAL_CREATED = "terminal_created"
STAGES = (STARTED, TERMINAL_CREATED)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Launch:
    task_id: str
    stage: str
    agent: str | None
    conversation_id: str | None
    terminal: TerminalSessionRef | None


_COLUMNS = (
    "task_id, stage, agent, conversation_id, "
    "terminal_adapter, terminal_workspace_id, terminal_surface_id, terminal_created_by_launcher"
)


def get_launch(conn: sqlite3.Connection, task_id: str) -> Launch | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM launches WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    terminal = TerminalSessionRef(row[4], row[5], row[6], bool(row[7])) if row[4] else None
    return Launch(row[0], row[1], row[2], row[3], terminal)


def begin(conn: sqlite3.Connection, task_id: str) -> Launch:
    """Start a launch, or note that a retry is continuing one. Keeps what an earlier attempt recorded."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO launches (task_id, stage, pid, started_at, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET pid = excluded.pid, updated_at = excluded.updated_at",
            (task_id, STARTED, os.getpid(), _now(), _now()),
        )
    launch = get_launch(conn, task_id)
    assert launch is not None
    return launch


def _update(conn: sqlite3.Connection, task_id: str, **fields: object) -> None:
    sets = ", ".join(f"{k} = ?" for k in (*fields, "updated_at"))
    with transaction(conn):
        conn.execute(f"UPDATE launches SET {sets} WHERE task_id = ?", (*fields.values(), _now(), task_id))


def note_agent(conn: sqlite3.Connection, task_id: str, agent: str, conversation_id: str | None) -> None:
    _update(conn, task_id, agent=agent, conversation_id=conversation_id)


def note_terminal(conn: sqlite3.Connection, task_id: str, terminal: TerminalSessionRef) -> None:
    _update(
        conn,
        task_id,
        stage=TERMINAL_CREATED,
        terminal_adapter=terminal.adapter,
        terminal_workspace_id=terminal.workspace_id,
        terminal_surface_id=terminal.surface_id,
        terminal_created_by_launcher=int(terminal.created_by_launcher),
    )


def forget_terminal(conn: sqlite3.Connection, task_id: str) -> None:
    _update(
        conn,
        task_id,
        stage=STARTED,
        terminal_adapter=None,
        terminal_workspace_id=None,
        terminal_surface_id=None,
        terminal_created_by_launcher=None,
    )
