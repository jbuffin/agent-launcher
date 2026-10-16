"""The state database stops using worktrees and cleanup (issue #16): a migration appended after the shipped ten.

An old database is built with the shipped migrations (`old_state.build`), given rows, and opened by `state.connect`.
Kept: `tasks.archived_state` and `task_events` (archiving needs them). Dropped: the `worktrees` table,
`tasks.cleanup_eligible_at` and the worktree columns and stages of `launches`. Migration 12 (`test_state_no_terminals`)
runs after it here, so what is checked is what both leave.
"""

import sqlite3

import pytest

import old_state
from commodore_launcher import launches, state


DROPS_COLUMNS = sqlite3.sqlite_version_info >= (3, 35, 0)
"""SQLite before 3.35 has no `DROP COLUMN`: there, migration 11 keeps the unused cleanup column."""


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


@pytest.fixture
def old_db(tmp_path):
    path = tmp_path / "old" / "state.db"
    conn = old_state.build(path, 10)
    for task_id, task_state in (("t-active", "active"), ("t-launch", "launching"), ("t-term", "launching"), ("t-done", "archived")):
        old_state.add_task(conn, task_id, task_state)
    conn.execute("UPDATE tasks SET cleanup_eligible_at = '2025-02-01T00:00:00+00:00' WHERE id = 't-active'")
    conn.execute("UPDATE tasks SET archived_state = 'completed' WHERE id = 't-done'")
    conn.execute("INSERT INTO task_events (task_id, event, detail, at) VALUES ('t-done', 'archived', 'old', ?)", (old_state.NOW,))
    old_state.add_worktree(conn, "t-active", "/home/u/.agent-launcher/worktrees/widgets-1/t-active")
    old_state.add_worktree(conn, "t-done", "/home/u/code/widgets-done", ownership="adopted", removed_at=old_state.NOW)
    conn.execute(
        "INSERT INTO launches (task_id, stage, agent, conversation_id, worktree_path, worktree_branch, worktree_base_ref, "
        "started_at, updated_at) VALUES ('t-launch', 'worktree_ready', 'claude', 'conv-1', '/x/t-launch', 'task/t-launch', "
        "'refs/heads/main', ?, ?)",
        (old_state.NOW, old_state.NOW),
    )
    conn.execute(
        "INSERT INTO launches (task_id, stage, agent, conversation_id, worktree_path, terminal_adapter, "
        "terminal_workspace_id, terminal_surface_id, terminal_created_by_launcher, started_at, updated_at) "
        "VALUES ('t-term', 'terminal_created', 'claude', 'conv-2', '/x/t-term', 'mock', 'ws-9', 'surface-9', 1, ?, ?)",
        (old_state.NOW, old_state.NOW),
    )
    conn.close()
    return path


def test_the_schema_has_two_more_migrations_than_the_shipped_ten():
    assert state.SCHEMA_VERSION == 12


def test_an_old_database_migrates_and_its_tasks_survive(old_db):
    conn = state.connect(old_db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
    rows = conn.execute("SELECT id, title, state, repo_path, profile, archived_state FROM tasks ORDER BY id").fetchall()
    assert rows == [
        ("t-active", "Task t-active", "active", "/home/u/code/widgets", "work", None),
        ("t-done", "Task t-done", "archived", "/home/u/code/widgets", "work", "completed"),
        ("t-launch", "Task t-launch", "launching", "/home/u/code/widgets", "work", None),
        ("t-term", "Task t-term", "launching", "/home/u/code/widgets", "work", None),
    ]
    assert conn.execute("SELECT task_id, event, detail FROM task_events").fetchall() == [("t-done", "archived", "old")]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_on_sqlite_before_3_35_the_migration_keeps_the_unused_cleanup_column(old_db, monkeypatch):
    """`ALTER TABLE … DROP COLUMN` is SQLite 3.35+: on an older SQLite the migration must still succeed."""
    monkeypatch.setattr(state.sqlite3, "sqlite_version_info", (3, 34, 1))
    conn = state.connect(old_db)
    assert "worktrees" not in tables(conn)
    assert "cleanup_eligible_at" in columns(conn, "tasks")
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_the_worktree_table_and_the_cleanup_column_are_gone(old_db):
    conn = state.connect(old_db)
    assert "worktrees" not in tables(conn)
    assert ("cleanup_eligible_at" in columns(conn, "tasks")) != DROPS_COLUMNS
    assert {"archived_state", "state", "source", "workflow"} <= set(columns(conn, "tasks"))
    assert "task_events" in tables(conn)
    assert "worktrees" not in state.EXPECTED_TABLES
    assert state.inspect(old_db).missing_tables == ()  # `doctor` does not report the dropped table as missing


def test_launches_lose_the_worktree_columns_and_stages(old_db):
    conn = state.connect(old_db)
    assert not [c for c in columns(conn, "launches") if "worktree" in c or "terminal" in c or c == "stage"]
    (sql,) = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'launches'").fetchone()
    assert "worktree" not in sql


def test_a_launch_in_a_removed_stage_keeps_what_it_recorded(old_db):
    conn = state.connect(old_db)
    rows = conn.execute("SELECT task_id, agent, conversation_id FROM launches ORDER BY task_id").fetchall()
    assert rows == [("t-launch", "claude", "conv-1"), ("t-term", "claude", "conv-2")]


def test_migrating_twice_changes_nothing(old_db):
    first = state.connect(old_db)
    before = first.execute("SELECT id, state FROM tasks ORDER BY id").fetchall()
    first.close()
    second = state.connect(old_db)
    assert second.execute("SELECT id, state FROM tasks ORDER BY id").fetchall() == before
    assert second.execute("PRAGMA user_version").fetchone()[0] == 12


def test_the_shipped_migrations_are_unchanged_and_a_fresh_database_has_no_worktree_state(tmp_path):
    fresh = state.connect(tmp_path / "fresh" / "state.db")
    assert "worktrees" not in tables(fresh)
    assert ("cleanup_eligible_at" in columns(fresh, "tasks")) != DROPS_COLUMNS
    assert "worktrees" not in state.EXPECTED_TABLES and state.inspect(tmp_path / "fresh" / "state.db").missing_tables == ()
    assert len(state.MIGRATIONS) == 12
    assert [m.__name__ for m in state.MIGRATIONS[:10]][3] == "_v4_worktrees"  # a shipped migration is never edited


def test_a_launch_records_its_harness_and_conversation(tmp_path):
    conn = state.connect(tmp_path / "state.db")
    conn.execute(
        "INSERT INTO repositories (id, github_id, full_name, created_at, updated_at) VALUES (1, 101, 'acme/widgets', 'x', 'x')"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, repository_id, repo_path, profile, state, created_at, updated_at) "
        "VALUES ('t-1', 'x', 1, '/home/u/code/widgets', 'work', 'launching', 'x', 'x')"
    )
    launches.begin(conn, "t-1", "claude", "conv")
    launches.begin(conn, "t-1", "codex", None)  # a retry with another harness replaces what the first attempt chose
    assert conn.execute("SELECT agent, conversation_id FROM launches").fetchall() == [("codex", None)]
