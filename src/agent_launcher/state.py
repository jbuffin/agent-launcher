"""`state.db`: the runtime state store (SPEC §26 keeps `config.json` separate; see ADR 0003).

This module owns the connection and the schema. Later tickets (tasks, sessions,
worktrees, locks) add tables by appending to `MIGRATIONS`; nothing else opens the
database for writing.

- WAL journal, a busy timeout and foreign keys are set on every connection.
- The connection is in autocommit mode; writers use `transaction()`, which is an explicit
  `BEGIN IMMEDIATE ... COMMIT` so a writer takes the lock up front and holds it briefly.
- The schema version is SQLite's `user_version`. Migrations run in order, each in its own
  transaction that also bumps the version, so a crash leaves the previous version intact.
- A database written by a newer release is refused, never touched.
"""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from agent_launcher.logs import trace
from agent_launcher.paths import launcher_home

BUSY_TIMEOUT_MS = 5000

Migration = Callable[[sqlite3.Connection], None]


class StateError(Exception):
    """The state database cannot be used (for example it comes from a newer release)."""


def state_path() -> Path:
    return launcher_home() / "state.db"


def _v1_repositories_and_associations(conn: sqlite3.Connection) -> None:
    # One row per repository we have seen. `github_id` is GitHub's immutable repository ID:
    # it is what keeps an association across renames and transfers. `full_name` is only
    # the last known owner/name, for display.
    conn.execute(
        """CREATE TABLE repositories (
            id INTEGER PRIMARY KEY,
            github_id INTEGER UNIQUE,
            node_id TEXT,
            full_name TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE repository_paths (
            repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            PRIMARY KEY (repository_id, path)
        )"""
    )
    conn.execute("CREATE INDEX repository_paths_path ON repository_paths(path)")
    conn.execute(
        """CREATE TABLE repository_remotes (
            repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            url TEXT NOT NULL,
            PRIMARY KEY (repository_id, url)
        )"""
    )
    conn.execute("CREATE INDEX repository_remotes_url ON repository_remotes(url)")
    # The explicit, persistent repository-to-profile association (SPEC §6). A row exists
    # only because the user chose it.
    conn.execute(
        """CREATE TABLE profile_associations (
            repository_id INTEGER PRIMARY KEY REFERENCES repositories(id) ON DELETE CASCADE,
            profile TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )


def _v2_tasks_and_sessions(conn: sqlite3.Connection) -> None:
    # Three identities, kept apart (SPEC §3): the task (`tasks.id`, opaque, never changes),
    # the agent's conversation (`agent_conversations`) and the terminal session
    # (`terminal_sessions`). A session ties them together for one launch. `state` is a plain
    # lifecycle label for now; ticket #11 added the launch stages (`launches`).
    conn.execute(
        """CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            repository_id INTEGER NOT NULL REFERENCES repositories(id),
            repo_path TEXT NOT NULL,
            profile TEXT NOT NULL,
            agent TEXT,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            profile TEXT NOT NULL,
            agent TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX sessions_task ON sessions(task_id)")
    conn.execute("CREATE INDEX sessions_profile ON sessions(profile, created_at)")
    conn.execute(
        """CREATE TABLE agent_conversations (
            session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
            agent TEXT NOT NULL,
            conversation_id TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE terminal_sessions (
            session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
            adapter TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            surface_id TEXT
        )"""
    )


def _v3_terminal_ownership(conn: sqlite3.Connection) -> None:
    # Whether the launcher itself created the terminal session. Existing rows predate the record: false,
    # so nothing recorded earlier is ever force-closed.
    conn.execute("ALTER TABLE terminal_sessions ADD COLUMN created_by_launcher INTEGER NOT NULL DEFAULT 0")


def _v4_worktrees(conn: sqlite3.Connection) -> None:
    # One worktree per task. `ownership` says how it came to be the task's: `created` by the launcher, or
    # `adopted` by an explicit choice. A branch-name match is never recorded as either.
    conn.execute(
        """CREATE TABLE worktrees (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id),
            repository_id INTEGER NOT NULL REFERENCES repositories(id),
            path TEXT NOT NULL UNIQUE,
            branch TEXT,
            ownership TEXT NOT NULL CHECK (ownership IN ('created', 'adopted')),
            base_ref TEXT,
            created_at TEXT NOT NULL
        )"""
    )


def _v5_launches(conn: sqlite3.Connection) -> None:
    # A first launch in progress: its stage and the resources created so far, so a retry resumes instead of
    # duplicating (ADR 0007). The row exists only until the session is recorded.
    conn.execute(
        """CREATE TABLE launches (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id),
            stage TEXT NOT NULL CHECK (stage IN ('started', 'worktree_pending', 'worktree_ready', 'terminal_created')),
            agent TEXT,
            conversation_id TEXT,
            worktree_path TEXT,
            worktree_branch TEXT,
            worktree_base_ref TEXT,
            terminal_adapter TEXT,
            terminal_workspace_id TEXT,
            terminal_surface_id TEXT,
            terminal_created_by_launcher INTEGER,
            pid INTEGER,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )


def _v6_github_tasks(conn: sqlite3.Connection) -> None:
    # Tasks become source-independent: `source` says where the work came from, and everything specific to a
    # source lives in its own table, so linking a local task to an issue later (#17) is one INSERT and keeps
    # `tasks.id`. A GitHub task is found by GitHub's stable IDs, never by `owner/repo#N`, which a rename or
    # transfer changes. Only what later tickets need is kept; bodies and comments are not stored.
    conn.execute("ALTER TABLE tasks ADD COLUMN source TEXT NOT NULL DEFAULT 'local'")
    conn.execute(
        """CREATE TABLE task_github (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id),
            kind TEXT NOT NULL CHECK (kind IN ('issue', 'pull_request')),
            node_id TEXT NOT NULL UNIQUE,
            database_id INTEGER NOT NULL UNIQUE,
            repository_github_id INTEGER NOT NULL,
            repository_node_id TEXT,
            number INTEGER NOT NULL,
            title TEXT NOT NULL,
            state TEXT NOT NULL,
            labels TEXT NOT NULL DEFAULT '[]',
            author TEXT,
            assignees TEXT NOT NULL DEFAULT '[]',
            url TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX task_github_url ON task_github(url)")


def _v7_github_remote_ids(conn: sqlite3.Connection) -> None:
    # A cache of what GitHub said a normalised remote URL's repository ID is, so remote inspection does not ask
    # about every checkout on every `open`. Entries expire (see repo_locator) because a name can be reused.
    conn.execute(
        """CREATE TABLE github_remote_ids (
            url TEXT PRIMARY KEY,
            github_id INTEGER NOT NULL,
            checked_at TEXT NOT NULL
        )"""
    )


def _v8_pull_requests(conn: sqlite3.Connection) -> None:
    # What later tickets route on (#14) and complete on (#22) for a pull request. Nullable: an issue has none of
    # it. The state column holds open | closed | merged. Bodies, diffs and comments are never stored.
    for column, kind in (
        ("pr_head_repo_owner", "TEXT"),
        ("pr_head_repo_name", "TEXT"),
        ("pr_head_repo_id", "INTEGER"),
        ("pr_head_fork", "INTEGER"),
        ("pr_head_ref", "TEXT"),
        ("pr_head_sha", "TEXT"),
        ("pr_base_ref", "TEXT"),
        ("pr_draft", "INTEGER"),
        ("pr_review_requested", "INTEGER"),
        ("pr_own", "INTEGER"),
    ):
        conn.execute(f"ALTER TABLE task_github ADD COLUMN {column} {kind}")
    # An issue's and a pull request's database IDs come from different sequences and can be equal, so
    # `database_id` is unique per kind, not globally (the node ID stays globally unique). SQLite cannot drop a
    # column constraint, so the table is rebuilt.
    columns = [row[1] for row in conn.execute("PRAGMA table_info(task_github)")]
    names = ", ".join(columns)
    conn.execute("ALTER TABLE task_github RENAME TO task_github_old")
    conn.execute("DROP INDEX task_github_url")
    conn.execute(
        """CREATE TABLE task_github (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id),
            kind TEXT NOT NULL CHECK (kind IN ('issue', 'pull_request')),
            node_id TEXT NOT NULL UNIQUE,
            database_id INTEGER NOT NULL,
            repository_github_id INTEGER NOT NULL,
            repository_node_id TEXT,
            number INTEGER NOT NULL,
            title TEXT NOT NULL,
            state TEXT NOT NULL,
            labels TEXT NOT NULL DEFAULT '[]',
            author TEXT,
            assignees TEXT NOT NULL DEFAULT '[]',
            url TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            pr_head_repo_owner TEXT,
            pr_head_repo_name TEXT,
            pr_head_repo_id INTEGER,
            pr_head_fork INTEGER,
            pr_head_ref TEXT,
            pr_head_sha TEXT,
            pr_base_ref TEXT,
            pr_draft INTEGER,
            pr_review_requested INTEGER,
            pr_own INTEGER,
            UNIQUE (kind, database_id)
        )"""
    )
    conn.execute(f"INSERT INTO task_github ({names}) SELECT {names} FROM task_github_old")
    conn.execute("DROP TABLE task_github_old")
    conn.execute("CREATE INDEX task_github_url ON task_github(url)")


MIGRATIONS: list[Migration] = [
    _v1_repositories_and_associations,
    _v2_tasks_and_sessions,
    _v3_terminal_ownership,
    _v4_worktrees,
    _v5_launches,
    _v6_github_tasks,
    _v7_github_remote_ids,
    _v8_pull_requests,
]
"""Ordered. Migration N takes the schema from version N-1 to N. Never edit one that has shipped."""

SCHEMA_VERSION = len(MIGRATIONS)


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A short write transaction: takes the write lock immediately, commits or rolls back."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> None:
    """Bring the schema up to `SCHEMA_VERSION`. Safe to call from several processes at once."""
    if _version(conn) == SCHEMA_VERSION:
        return
    with transaction(conn):
        current = _version(conn)  # re-read under the lock: another process may have migrated
        if current > SCHEMA_VERSION:
            raise StateError(
                f"state.db is schema version {current}, newer than this release understands "
                f"({SCHEMA_VERSION}). Upgrade agent-launcher; the database was not changed."
            )
        for number in range(current + 1, SCHEMA_VERSION + 1):
            MIGRATIONS[number - 1](conn)
            conn.execute(f"PRAGMA user_version = {number}")
            trace("state migrated", version=number)


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) and migrate the state database."""
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        migrate(conn)
    except sqlite3.Error as exc:
        raise StateError(f"cannot open {path}: {exc}") from exc
    return conn


@contextmanager
def open_state(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@dataclass(frozen=True)
class Inspection:
    """What `inspect` found, without changing anything."""

    exists: bool
    version: int = 0
    integrity: list[str] | None = None
    """Problems from `quick_check` and `foreign_key_check`; empty means healthy."""
    missing_tables: tuple[str, ...] = ()

    @property
    def needs_migration(self) -> bool:
        return self.exists and 0 < self.version < SCHEMA_VERSION

    @property
    def too_new(self) -> bool:
        return self.version > SCHEMA_VERSION


EXPECTED_TABLES = (
    "repositories",
    "repository_paths",
    "repository_remotes",
    "profile_associations",
    "tasks",
    "sessions",
    "agent_conversations",
    "terminal_sessions",
    "worktrees",
    "launches",
    "task_github",
    "github_remote_ids",
)


def inspect(path: Path | None = None) -> Inspection:
    """Read-only health check for `doctor`. Never creates, migrates or writes the database."""
    path = path or state_path()
    if not path.exists():
        return Inspection(exists=False)
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        quick = [row[0] for row in conn.execute("PRAGMA quick_check")]
        problems = [] if quick == ["ok"] else quick or ["no result"]
        version = _version(conn)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if version == SCHEMA_VERSION:
            problems += [
                f"foreign key violation in {row[0]}" for row in conn.execute("PRAGMA foreign_key_check")
            ]
        missing = tuple(t for t in EXPECTED_TABLES if t not in tables) if version == SCHEMA_VERSION else ()
        return Inspection(True, version, problems, missing)
    finally:
        conn.close()
