"""Migration 12 (ADR 0022): no terminal sessions are recorded. Sessions and conversation IDs stay; the cmux
workspaces they pointed at are forgotten, and the next `open` says so once."""

import sqlite3

from typer.testing import CliRunner

import old_state
from commodore_launcher import state
from commodore_launcher.cli import app

runner = CliRunner()


def tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def old_db(path, *, with_terminals: bool):
    conn = old_state.build(path, 11)
    old_state.add_task(conn, "t-active", "active")
    conn.execute(
        "INSERT INTO sessions (id, task_id, profile, agent, state, created_at) "
        "VALUES ('s-1', 't-active', 'work', 'claude', 'launched', ?)",
        (old_state.NOW,),
    )
    conn.execute("INSERT INTO agent_conversations (session_id, agent, conversation_id) VALUES ('s-1', 'claude', 'conv-1')")
    if with_terminals:
        conn.execute(
            "INSERT INTO terminal_sessions (session_id, adapter, workspace_id, surface_id, created_by_launcher) "
            "VALUES ('s-1', 'cmux', 'workspace:1', 'surface-uuid', 1)"
        )
    conn.close()
    return path


def test_terminal_sessions_are_dropped_and_sessions_keep_their_conversations(tmp_path):
    conn = state.connect(old_db(tmp_path / "state.db", with_terminals=True))
    assert state.SCHEMA_VERSION == 12 and conn.execute("PRAGMA user_version").fetchone()[0] == 12
    assert "terminal_sessions" not in tables(conn) and "terminal_sessions" not in state.EXPECTED_TABLES
    assert conn.execute(
        "SELECT s.id, c.conversation_id FROM sessions s JOIN agent_conversations c ON c.session_id = s.id"
    ).fetchall() == [("s-1", "conv-1")]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert state.inspect(tmp_path / "state.db").missing_tables == ()


def test_forgotten_terminals_leave_one_notice_shown_once(tmp_path):
    conn = state.connect(old_db(tmp_path / "state.db", with_terminals=True))
    (notice,) = state.take_notices(conn)
    assert "cmux" in notice and "quit it" in notice
    assert state.take_notices(conn) == []


def test_a_database_without_terminal_sessions_gets_no_notice(tmp_path):
    conn = state.connect(old_db(tmp_path / "state.db", with_terminals=False))
    assert state.take_notices(conn) == []


def test_open_prints_the_notice_before_it_launches(launcher_home, write_config):
    old_db(launcher_home / "state.db", with_terminals=True)
    write_config({"version": 4})
    first = runner.invoke(app, ["open", "t-active", "--offline"])
    assert state.FORGOTTEN_TERMINALS_NOTICE in first.output  # shown, whatever the open then says
    again = runner.invoke(app, ["open", "t-active", "--offline"])
    assert state.FORGOTTEN_TERMINALS_NOTICE not in again.output
