"""The persisted state of a launch in progress (ADR 0007, ADR 0022).

A first `open` writes a `launches` row once every check has passed, then records the session; the row is deleted in
the same transaction that records it, so a task with a `launches` row has no session yet. Nothing outside the
database is created before the session is recorded (the harness starts after), so there is nothing to roll back: a
retry simply launches again.
"""

import os
import sqlite3
from datetime import datetime, timezone

from commodore_launcher.state import transaction


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def begin(conn: sqlite3.Connection, task_id: str, agent: str, conversation_id: str | None) -> None:
    """Start a launch, or note that a retry is continuing one."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO launches (task_id, agent, conversation_id, pid, started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET agent = excluded.agent, conversation_id = excluded.conversation_id, "
            "pid = excluded.pid, updated_at = excluded.updated_at",
            (task_id, agent, conversation_id, os.getpid(), _now(), _now()),
        )
