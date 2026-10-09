"""The persisted state of a launch in progress (ADR 0007).

A first `open` moves through stages, each written down before the next starts:

    started -> worktree_pending -> worktree_ready -> terminal_created -> (session recorded = ready)

The row is deleted in the same transaction that records the session, so a task with a `launches` row is never
running. A retry reads the row and resumes: a recorded worktree is reused, a recorded terminal session that
still exists is adopted. Resources are recorded with the ownership the launcher can prove: a worktree intent
is only written by the launcher just before `git worktree add`, a terminal reference only from the adapter's
own answer to `create_session`.
"""

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from agent_launcher.state import transaction
from agent_launcher.terminals import TerminalSessionRef

STARTED = "started"
WORKTREE_PENDING = "worktree_pending"
WORKTREE_READY = "worktree_ready"
TERMINAL_CREATED = "terminal_created"
STAGES = (STARTED, WORKTREE_PENDING, WORKTREE_READY, TERMINAL_CREATED)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Launch:
    task_id: str
    stage: str
    agent: str | None
    conversation_id: str | None
    worktree_path: str | None
    worktree_branch: str | None
    worktree_base_ref: str | None
    terminal: TerminalSessionRef | None


_COLUMNS = (
    "task_id, stage, agent, conversation_id, worktree_path, worktree_branch, worktree_base_ref, "
    "terminal_adapter, terminal_workspace_id, terminal_surface_id, terminal_created_by_launcher"
)


def get_launch(conn: sqlite3.Connection, task_id: str) -> Launch | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM launches WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    terminal = TerminalSessionRef(row[7], row[8], row[9], bool(row[10])) if row[7] else None
    return Launch(row[0], row[1], row[2], row[3], row[4], row[5], row[6], terminal)


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


def note_worktree_intent(conn: sqlite3.Connection, task_id: str, path: str, branch: str, base_ref: str) -> None:
    """Written just before `git worktree add`: if the process dies after git creates the tree but before it is
    recorded, this is the proof that the launcher, and nobody else, was making it."""
    _update(
        conn, task_id, stage=WORKTREE_PENDING, worktree_path=path, worktree_branch=branch, worktree_base_ref=base_ref
    )


def note_worktree_ready(conn: sqlite3.Connection, task_id: str) -> None:
    _update(conn, task_id, stage=WORKTREE_READY)


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
        stage=WORKTREE_READY,
        terminal_adapter=None,
        terminal_workspace_id=None,
        terminal_surface_id=None,
        terminal_created_by_launcher=None,
    )
