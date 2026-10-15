"""Build a `state.db` as an earlier release left it: the shipped migrations up to `version`, then plain rows.

Rows are inserted with SQL, never through the modules that u4 changes. The shipped migrations are `state.MIGRATIONS[:10]`
(never edited), so the same helper keeps working once a later migration is appended.
"""

import sqlite3
from pathlib import Path

from commodore_launcher import state

NOW = "2025-01-01T00:00:00+00:00"


def build(path: Path, version: int = 10) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    for number, migration in enumerate(state.MIGRATIONS[:version], 1):
        migration(conn)
        conn.execute(f"PRAGMA user_version = {number}")
    conn.execute(
        "INSERT INTO repositories (id, github_id, full_name, created_at, updated_at) VALUES (1, 101, 'acme/widgets', ?, ?)",
        (NOW, NOW),
    )
    return conn


def add_task(conn: sqlite3.Connection, task_id: str, task_state: str = "active", title: str | None = None) -> None:
    conn.execute(
        "INSERT INTO tasks (id, title, repository_id, repo_path, profile, state, created_at, updated_at) "
        "VALUES (?, ?, 1, '/home/u/code/widgets', 'work', ?, ?, ?)",
        (task_id, title or f"Task {task_id}", task_state, NOW, NOW),
    )


def add_worktree(
    conn: sqlite3.Connection, task_id: str, path: str, *, ownership: str = "created", removed_at: str | None = None
) -> None:
    columns = "task_id, repository_id, path, branch, ownership, base_ref, created_at"
    values = [task_id, 1, path, f"task/{task_id}", ownership, "refs/heads/main", NOW]
    if removed_at is not None:  # `removed_at` exists from schema 10
        columns += ", removed_at"
        values.append(removed_at)
    conn.execute(f"INSERT INTO worktrees ({columns}) VALUES ({', '.join('?' * len(values))})", values)
