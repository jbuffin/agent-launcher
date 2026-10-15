"""The one-time copy archives every copied task and lists the launcher-created worktrees once (issue #16, ADR 0021).

The listing is read from the *staging* copy of `state.db`, before it is migrated and archived: `worktrees` rows with
`ownership = 'created'`, not removed, whose directory still exists. Adopted worktrees are not the launcher's and are not
listed. Nothing removes a worktree. Driven through the CLI callback, as `test_first_run_copy.py` is.
"""

import json
import os
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

import old_state

runner = CliRunner()


def invoke(*args: str):
    from commodore_launcher.cli import app

    return runner.invoke(app, list(args))


@pytest.fixture(autouse=True)
def home_variable_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COMMODORE_LAUNCHER_HOME", raising=False)


@pytest.fixture
def old_home(fake_default_home: Path) -> Path:
    home = fake_default_home / ".agent-launcher"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"version": 2, "profiles": {"work": {}}}))
    return home


@pytest.fixture
def new_home(fake_default_home: Path) -> Path:
    return fake_default_home / ".commodore-launcher"


def snapshot(root: Path) -> dict[str, tuple]:
    seen: dict[str, tuple] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        seen[rel] = ("dir",) if path.is_dir() else ("file", path.read_bytes(), path.stat().st_mtime_ns)
    return seen


def listing_lines(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if "left on disk" in line]


class OldWorktrees:
    """An old home whose database has four worktree rows of different kinds, and tasks in several states."""

    def __init__(self, home: Path, tmp: Path, version: int = 10) -> None:
        self.created = home / "worktrees" / "widgets-1" / "t-created"
        self.missing = home / "worktrees" / "widgets-1" / "t-missing"
        self.adopted = tmp / "elsewhere" / "adopted-checkout"
        self.removed = home / "worktrees" / "widgets-1" / "t-removed"
        for path in (self.created, self.adopted, self.removed):
            path.mkdir(parents=True)
            (path / "work.txt").write_text("uncommitted work\n")
        conn = old_state.build(home / "state.db", version)
        for task_id, task_state in (
            ("t-created", "active"), ("t-missing", "launch_failed"), ("t-adopted", "active"),
            ("t-removed", "active"), ("t-archived", "archived"),
        ):
            old_state.add_task(conn, task_id, task_state)
        if version >= 10:
            conn.execute("UPDATE tasks SET archived_state = 'completed' WHERE id = 't-archived'")
            conn.execute(
                "INSERT INTO task_events (task_id, event, detail, at) VALUES ('t-archived', 'archived', 'earlier', ?)",
                (old_state.NOW,),
            )
        old_state.add_worktree(conn, "t-created", str(self.created))
        old_state.add_worktree(conn, "t-missing", str(self.missing))
        old_state.add_worktree(conn, "t-adopted", str(self.adopted), ownership="adopted")
        old_state.add_worktree(
            conn, "t-removed", str(self.removed), removed_at=old_state.NOW if version >= 10 else None
        )
        conn.close()


def new_tasks(new_home: Path) -> dict[str, tuple]:
    conn = sqlite3.connect(new_home / "state.db")
    try:
        return {row[0]: (row[1], row[2]) for row in conn.execute("SELECT id, state, archived_state FROM tasks")}
    finally:
        conn.close()


def archive_events(new_home: Path) -> list[tuple]:
    conn = sqlite3.connect(new_home / "state.db")
    try:
        return conn.execute("SELECT task_id, event, detail FROM task_events ORDER BY id").fetchall()
    finally:
        conn.close()


def test_every_copied_task_is_archived_and_the_created_worktree_is_listed_once(old_home, new_home, tmp_path):
    trees = OldWorktrees(old_home, tmp_path)
    before = snapshot(old_home)

    first = invoke("profile", "list", "--json")
    assert first.exit_code == 0, first.output
    assert [p["name"] for p in json.loads(first.stdout)["profiles"]] == ["work"]  # stdout stays one JSON object

    assert new_tasks(new_home) == {
        "t-created": ("archived", "active"), "t-missing": ("archived", "launch_failed"),
        "t-adopted": ("archived", "active"), "t-removed": ("archived", "active"),
        "t-archived": ("archived", "completed"),  # already archived: kept as it was
    }
    events = archive_events(new_home)
    assert sorted(e for e in events if e[2] == "copied from ~/.agent-launcher") == [
        (task, "archived", "copied from ~/.agent-launcher") for task in ("t-adopted", "t-created", "t-missing", "t-removed")
    ]
    assert ("t-archived", "archived", "earlier") in events and len(events) == 5

    assert len(listing_lines(first.stderr)) == 1
    assert first.stderr.count(str(trees.created)) == 1
    for other in (trees.missing, trees.adopted, trees.removed):
        assert str(other) not in first.stderr
    assert "worktrees" not in first.stdout

    for path in (trees.created, trees.adopted, trees.removed):  # nothing removes a worktree
        assert (path / "work.txt").read_text() == "uncommitted work\n"
    assert snapshot(old_home) == before  # the old home is never opened for writing


def test_the_listing_is_printed_once_and_a_second_run_prints_nothing(old_home, new_home, tmp_path):
    trees = OldWorktrees(old_home, tmp_path)
    assert invoke("version").exit_code == 0
    second = invoke("version")
    assert second.exit_code == 0
    assert str(trees.created) not in second.stderr and listing_lines(second.stderr) == []
    assert second.stderr.strip() == ""
    assert archive_events(new_home).count(("t-created", "archived", "copied from ~/.agent-launcher")) == 1


def test_the_listing_says_the_worktrees_are_left_on_disk_and_that_nothing_removes_them(old_home, new_home, tmp_path):
    trees = OldWorktrees(old_home, tmp_path)
    done = invoke("version")
    (line,) = listing_lines(done.stderr)
    assert "worktree" in line.lower()
    assert str(trees.created) in done.stderr


def test_a_copy_with_no_worktrees_still_archives_and_prints_only_the_copy_line(old_home, new_home):
    conn = old_state.build(old_home / "state.db", 10)
    old_state.add_task(conn, "t-1")
    conn.close()
    done = invoke("version")
    assert done.exit_code == 0
    assert new_tasks(new_home) == {"t-1": ("archived", "active")}
    assert len([line for line in done.stderr.splitlines() if line.strip()]) == 1


def test_a_schema_9_database_without_removed_at_is_listed_too(old_home, new_home, tmp_path):
    trees = OldWorktrees(old_home, tmp_path, version=9)
    done = invoke("version")
    assert done.exit_code == 0, done.output
    assert done.stderr.count(str(trees.created)) == 1
    tasks = new_tasks(new_home)
    assert len(tasks) == 5 and {state for state, _ in tasks.values()} == {"archived"}


def test_a_schema_3_database_has_no_worktree_table_and_copies_cleanly(old_home, new_home):
    conn = old_state.build(old_home / "state.db", 3)
    old_state.add_task(conn, "t-old")
    conn.close()
    done = invoke("version")
    assert done.exit_code == 0, done.output
    assert new_tasks(new_home) == {"t-old": ("archived", "active")}
    assert listing_lines(done.stderr) == []


def test_a_home_with_no_state_database_copies_cleanly(old_home, new_home):
    done = invoke("version")
    assert done.exit_code == 0
    assert listing_lines(done.stderr) == []
    assert (new_home / "config.json").is_file()


def test_a_state_database_that_cannot_be_read_fails_the_copy_and_renames_nothing(old_home, new_home, fake_default_home):
    (old_home / "state.db").write_bytes(b"this is not a database\n" * 40)
    before = snapshot(old_home)
    done = invoke("version")
    assert done.exit_code == 1
    assert "error:" in done.stderr
    assert not new_home.exists()
    assert sorted(p.name for p in fake_default_home.iterdir()) == [".agent-launcher"]  # no staging directory left
    assert snapshot(old_home) == before


def test_the_copied_config_no_longer_carries_worktree_root_or_base_branches(old_home, new_home):
    (old_home / "config.json").write_text(
        json.dumps(
            {
                "version": 2,
                "profiles": {"work": {}},
                "repositories": {
                    "search_roots": ["~/code"],
                    "worktree_root": "~/.agent-launcher/worktrees",
                    "base_branches": {"~/code/widgets": "develop"},
                },
            }
        )
    )
    done = invoke("version")
    assert done.exit_code == 0, done.output
    copied = json.loads((new_home / "config.json").read_text())
    assert copied["version"] == 3
    assert copied["repositories"] == {"search_roots": ["~/code"]}
    assert "worktree" not in done.stderr.lower()  # the repoint note is gone: nothing creates worktrees any more
