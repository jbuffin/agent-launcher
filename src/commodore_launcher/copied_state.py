"""The state database of the first-run copy (`paths.copy_old_home`, ADR 0021).

Agent Launcher kept a worktree per task, and a task's conversation was tied to that directory. Commodore launches
nothing in them, so every copied task is archived: nothing is lost, and `tasks unarchive` brings one back. The
launcher-created worktrees that are still on disk are listed once, for the person to remove or keep; nothing here
removes one.

Works on the staging copy of `state.db` only. The old home's database is never opened.
"""

import sqlite3
from pathlib import Path

from commodore_launcher import state
from commodore_launcher.tasks import archive_tasks

ARCHIVE_DETAIL = "copied from ~/.agent-launcher"


def _created_worktrees(state_file: Path) -> list[str]:
    """Paths of `created`, not removed worktrees that still exist. Read before the schema is migrated away from them;
    `removed_at` exists from schema 10, the table from schema 4."""
    conn = sqlite3.connect(state_file)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(worktrees)")]
        if not columns:
            return []
        still_there = " AND removed_at IS NULL" if "removed_at" in columns else ""
        rows = conn.execute(
            f"SELECT path FROM worktrees WHERE ownership = 'created'{still_there} ORDER BY path"
        ).fetchall()
    finally:
        conn.close()
    return [path for (path,) in rows if Path(path).is_dir()]


def archive_copied_tasks(state_file: Path) -> list[str]:
    """Archive every task in the copied database, migrating it first. Returns the launcher-created worktrees to list.
    An unreadable database fails the copy (`OSError`), as any other copy failure does."""
    if not state_file.is_file():
        return []
    try:
        listed = _created_worktrees(state_file)
        conn = state.connect(state_file)
        try:
            ids = [row[0] for row in conn.execute("SELECT id FROM tasks")]
            with state.transaction(conn):
                archive_tasks(conn, ids, ARCHIVE_DETAIL)
        finally:
            conn.close()
    except (sqlite3.Error, state.StateError) as exc:
        raise OSError(0, f"its state database cannot be read ({exc})", str(state_file)) from exc
    return listed
