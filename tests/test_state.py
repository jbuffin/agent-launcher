import sqlite3
import threading

import pytest

from agent_launcher import state


def test_connect_creates_database_with_pragmas_and_version(launcher_home):
    conn = state.connect()
    assert (launcher_home / "state.db").exists()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == state.SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == state.BUSY_TIMEOUT_MS
    conn.close()


def test_reconnecting_does_not_rerun_migrations(launcher_home):
    with state.open_state() as conn:
        conn.execute("INSERT INTO repositories (github_id, created_at, updated_at) VALUES (1, 'x', 'x')")
    with state.open_state() as conn:
        assert conn.execute("SELECT count(*) FROM repositories").fetchone()[0] == 1


def test_migrations_run_in_order_and_each_bumps_the_version(tmp_path, monkeypatch):
    ran = []

    def v1(conn):
        ran.append(1)
        conn.execute("CREATE TABLE a (x)")

    def v2(conn):
        ran.append(2)
        conn.execute("CREATE TABLE b (x)")

    monkeypatch.setattr(state, "MIGRATIONS", [v1, v2])
    monkeypatch.setattr(state, "SCHEMA_VERSION", 2)
    conn = state.connect(tmp_path / "s.db")
    assert ran == [1, 2]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2

    # An existing v1 database only runs the missing migration.
    ran.clear()
    path = tmp_path / "old.db"
    monkeypatch.setattr(state, "MIGRATIONS", [v1])
    monkeypatch.setattr(state, "SCHEMA_VERSION", 1)
    state.connect(path).close()
    monkeypatch.setattr(state, "MIGRATIONS", [v1, v2])
    monkeypatch.setattr(state, "SCHEMA_VERSION", 2)
    ran.clear()
    state.connect(path).close()
    assert ran == [2]


def test_failed_migration_leaves_the_previous_version(tmp_path, monkeypatch):
    def v1(conn):
        conn.execute("CREATE TABLE a (x)")

    def v2(conn):
        conn.execute("CREATE TABLE b (x)")
        raise RuntimeError("boom")

    path = tmp_path / "s.db"
    monkeypatch.setattr(state, "MIGRATIONS", [v1])
    monkeypatch.setattr(state, "SCHEMA_VERSION", 1)
    state.connect(path).close()
    monkeypatch.setattr(state, "MIGRATIONS", [v1, v2])
    monkeypatch.setattr(state, "SCHEMA_VERSION", 2)
    with pytest.raises(RuntimeError):
        state.connect(path)
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
    assert raw.execute("SELECT name FROM sqlite_master WHERE name = 'b'").fetchone() is None


def test_newer_database_is_refused_and_not_modified(tmp_path):
    path = tmp_path / "s.db"
    state.connect(path).close()
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {state.SCHEMA_VERSION + 5}")
    raw.close()
    with pytest.raises(state.StateError, match="newer"):
        state.connect(path)
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == state.SCHEMA_VERSION + 5


def test_unreadable_file_is_a_state_error(tmp_path):
    path = tmp_path / "s.db"
    path.write_bytes(b"not a database" * 100)
    with pytest.raises(state.StateError):
        state.connect(path)


def test_transaction_commits_and_rolls_back(tmp_path):
    conn = state.connect(tmp_path / "s.db")
    with state.transaction(conn):
        conn.execute("INSERT INTO repositories (github_id, created_at, updated_at) VALUES (1, 'x', 'x')")
    with pytest.raises(ValueError):
        with state.transaction(conn):
            conn.execute("INSERT INTO repositories (github_id, created_at, updated_at) VALUES (2, 'x', 'x')")
            raise ValueError
    assert [r[0] for r in conn.execute("SELECT github_id FROM repositories")] == [1]
    assert not conn.in_transaction


def test_foreign_keys_are_enforced(tmp_path):
    conn = state.connect(tmp_path / "s.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO profile_associations VALUES (999, 'work', 'x', 'x')")


def test_concurrent_writers_do_not_fail_with_locked(tmp_path):
    path = tmp_path / "s.db"
    state.connect(path).close()
    errors = []

    def writer(n):
        try:
            conn = state.connect(path)
            for i in range(20):
                with state.transaction(conn):
                    conn.execute(
                        "INSERT INTO repositories (github_id, created_at, updated_at) VALUES (?, 'x', 'x')",
                        (n * 1000 + i,),
                    )
            conn.close()
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert state.connect(path).execute("SELECT count(*) FROM repositories").fetchone()[0] == 80


def test_inspect_is_read_only(launcher_home):
    assert state.inspect().exists is False
    assert not (launcher_home / "state.db").exists()
    state.connect().close()
    found = state.inspect()
    assert found.exists and found.version == state.SCHEMA_VERSION and found.integrity == []
