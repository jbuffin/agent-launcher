"""Persistent repository-to-profile associations (SPEC §6).

The rules this module enforces:

- A repository has a profile only because the user chose one (`set_profile`, or the
  interactive prompt in `ensure_profile`). Nothing is inferred, defaulted or copied from
  another repository. A *suggestion* is shown to the user and never stored.
- Lookup order: GitHub repository ID first, then local path and remote URLs. A rename or
  transfer keeps the same ID, so the association follows it; the new name and remotes are
  recorded against the same repository.
- A path or remote that belongs to a repository with a *different* GitHub ID is not a
  match, so a reused name or path never inherits the previous owner's profile.
- Anything ambiguous is an error, never a guess.
- An existing association is changed only by `set_profile`. If the repository has tasks, the caller must
  name one explicit resolution (`reassignment`); sessions, worktrees and stored profiles are never touched.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from agent_launcher.interaction import Choice, Prompter
from agent_launcher.logs import trace
from agent_launcher.reassignment import ARCHIVE_TASKS, KEEP_TASKS, AffectedTask, Liveness, affected_tasks
from agent_launcher.repositories import RepositoryIdentity
from agent_launcher.state import transaction
from agent_launcher.tasks import archive_tasks


class AssociationError(Exception):
    """A refusal the caller can act on. `code` is stable; `details` is JSON-safe."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, **self.details}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _remote_state_agrees(conn: sqlite3.Connection, repo_id: int, identity: RepositoryIdentity) -> bool:
    """Remote state the same on both sides: neither has a remote, or the checkout's primary
    remote is one the stored repository has had. A path match is accepted only then."""
    stored = {r[0] for r in conn.execute("SELECT url FROM repository_remotes WHERE repository_id = ?", (repo_id,))}
    if not identity.remotes:
        return not stored
    return identity.remotes[0] in stored


def find_repository(conn: sqlite3.Connection, identity: RepositoryIdentity) -> int | None:
    """The stored repository this identity refers to, or None. Read-only."""
    if identity.github_id is not None:
        row = conn.execute("SELECT id FROM repositories WHERE github_id = ?", (identity.github_id,)).fetchone()
        if row:
            return row[0]
    keys: list[tuple[str, str]] = []
    if identity.path:
        keys.append(("SELECT repository_id FROM repository_paths WHERE path = ?", identity.path))
    keys.extend(("SELECT repository_id FROM repository_remotes WHERE url = ?", url) for url in identity.remotes)
    candidates: set[int] = set()
    for sql, value in keys:
        for (repo_id,) in conn.execute(sql, (value,)):
            stored = conn.execute("SELECT github_id FROM repositories WHERE id = ?", (repo_id,)).fetchone()[0]
            if identity.github_id is not None and stored != identity.github_id:
                # Same path or name, but a different repository, or one stored without an ID
                # that we cannot prove is this one (a freed name may now belong to someone else).
                # Not a match: the user is asked.
                continue
            if not _remote_state_agrees(conn, repo_id, identity):
                continue  # e.g. a clone replaced by `git init` at the same path, or the reverse
            candidates.add(repo_id)
    if len(candidates) > 1:
        raise AssociationError(
            "ambiguous_repository",
            f"{identity.describe()} matches more than one known repository; "
            "set the profile explicitly with `agent-launcher profile set`.",
            repository=identity.to_dict(),
        )
    return candidates.pop() if candidates else None


def _remember(conn: sqlite3.Connection, identity: RepositoryIdentity) -> int:
    """Find or create the repository row and record what we now know. Call inside a transaction."""
    repo_id = find_repository(conn, identity)
    now = _now()
    if repo_id is None:
        repo_id = conn.execute(
            "INSERT INTO repositories (github_id, node_id, full_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (identity.github_id, identity.node_id, identity.full_name, now, now),
        ).lastrowid
        assert repo_id is not None
    else:
        conn.execute(
            """UPDATE repositories SET
                 node_id = COALESCE(?, node_id),
                 full_name = COALESCE(?, full_name),
                 updated_at = ?
               WHERE id = ?""",
            (identity.node_id, identity.full_name, now, repo_id),
        )
    if identity.path:
        conn.execute("INSERT OR IGNORE INTO repository_paths (repository_id, path) VALUES (?, ?)", (repo_id, identity.path))
    for url in identity.remotes:
        conn.execute("INSERT OR IGNORE INTO repository_remotes (repository_id, url) VALUES (?, ?)", (repo_id, url))
    return repo_id


@dataclass(frozen=True)
class Association:
    repository_id: int
    profile: str


def get_association(conn: sqlite3.Connection, identity: RepositoryIdentity) -> Association | None:
    """The profile this repository was explicitly assigned, or None when it is unknown.

    A known repository also has its name, path and remotes refreshed (a rename shows up here).
    """
    with transaction(conn):
        repo_id = find_repository(conn, identity)
        if repo_id is None:
            return None
        repo_id = _remember(conn, identity)
        row = conn.execute("SELECT profile FROM profile_associations WHERE repository_id = ?", (repo_id,)).fetchone()
    return Association(repo_id, row[0]) if row else None


@dataclass(frozen=True)
class SetResult:
    repository_id: int
    profile: str
    previous: str | None
    changed: bool
    recorded_id: bool = False
    """The repository was stored without a GitHub ID and this command recorded it."""
    affected_task_ids: Sequence[str] = ()
    """Tasks the change affected, and how they were resolved (`reassignment`)."""
    reassignment: str | None = None


def _adoptable(conn: sqlite3.Connection, identity: RepositoryIdentity) -> int | None:
    """The ID-less repository row `set_profile` would give this identity's ID to (read-only), or None."""
    if identity.github_id is None or not identity.path:
        return None
    if conn.execute("SELECT 1 FROM repositories WHERE github_id = ?", (identity.github_id,)).fetchone():
        return None
    rows = conn.execute(
        """SELECT r.id FROM repositories r JOIN repository_paths p ON p.repository_id = r.id
           WHERE r.github_id IS NULL AND p.path = ?""",
        (identity.path,),
    ).fetchall()
    return rows[0][0] if len(rows) == 1 and _remote_state_agrees(conn, rows[0][0], identity) else None


def _adopt_id(conn: sqlite3.Connection, identity: RepositoryIdentity) -> bool:
    """Give a repository stored without a GitHub ID its ID, because the user named exactly its checkout path.

    Only `set_profile` calls this: an explicit command is the consent. Nothing else ever gives an ID-less row an
    ID (a freed name could belong to someone else now). Skipped when the ID is already stored for another row.
    Call inside a transaction; a refusal later in `set_profile` rolls this back. Returns whether an ID was recorded.
    """
    repo_id = _adoptable(conn, identity)
    if repo_id is not None:
        conn.execute(
            "UPDATE repositories SET github_id = ?, node_id = ?, updated_at = ? WHERE id = ?",
            (identity.github_id, identity.node_id, _now(), repo_id),
        )
        trace("repository id recorded", repository=identity.describe())
        return True
    return False


def plan_reassignment(
    conn: sqlite3.Connection, identity: RepositoryIdentity, profile: str, liveness: Liveness | None = None
) -> tuple[str | None, list[AffectedTask]]:
    """Read-only: the current profile, and the tasks a change to `profile` would affect (empty when it is no change)."""
    repo_id = find_repository(conn, identity)
    if repo_id is None:
        repo_id = _adoptable(conn, identity)  # the row `set_profile` will give this identity's ID to
    if repo_id is None:
        return None, []
    row = conn.execute("SELECT profile FROM profile_associations WHERE repository_id = ?", (repo_id,)).fetchone()
    previous = row[0] if row else None
    if previous is None or previous == profile:
        return previous, []
    return previous, affected_tasks(conn, repo_id, profile, liveness)


def set_profile(
    conn: sqlite3.Connection,
    identity: RepositoryIdentity,
    profile: str,
    *,
    reassignment: str | None = None,
    planned: Sequence[str] | None = None,
) -> SetResult:
    """Associate the repository with `profile`. The only writer of `profile_associations`.

    Changing an existing association while the repository has tasks needs `reassignment` (`ARCHIVE_TASKS` or
    `KEEP_TASKS`); without it nothing is written and `reassignment_requires_resolution` lists what is affected.
    `planned` is the task IDs the caller planned for (`plan_reassignment`); if the set differs inside the
    transaction, nothing is written and `reassignment_changed` is raised. Nothing is probed in the transaction.
    The association and the archiving commit together. Sessions, worktrees and the profile stored on tasks and
    sessions are never changed.
    """
    with transaction(conn):
        recorded = _adopt_id(conn, identity)
        repo_id = _remember(conn, identity)
        row = conn.execute("SELECT profile FROM profile_associations WHERE repository_id = ?", (repo_id,)).fetchone()
        previous = row[0] if row else None
        if previous == profile:
            return SetResult(repo_id, profile, previous, False, recorded)
        affected = affected_tasks(conn, repo_id, profile) if previous is not None else []
        if affected and reassignment in (ARCHIVE_TASKS, KEEP_TASKS) and planned is not None:
            if sorted(t.task_id for t in affected) != sorted(planned):
                raise AssociationError(
                    "reassignment_changed",
                    f"The tasks affected by changing {identity.describe()} to {profile!r} changed since they were "
                    "listed. Nothing was changed; run the command again to see the current list.",
                    repository=identity.to_dict(),
                    planned_tasks=sorted(planned),
                    affected_tasks=[t.to_dict() for t in affected],
                )
        if affected:
            if reassignment not in (ARCHIVE_TASKS, KEEP_TASKS):
                raise AssociationError(
                    "reassignment_requires_resolution",
                    f"{identity.describe()} has {len(affected)} task(s) under profile {previous!r}. Changing its "
                    f"profile to {profile!r} needs one explicit choice: --archive-tasks (mark them archived, keeping "
                    "their files), --keep-tasks (they keep profile "
                    f"{previous!r} and cannot be opened until the repository goes back), or --cancel. "
                    "Nothing was changed.",
                    repository=identity.to_dict(),
                    current_profile=previous,
                    requested_profile=profile,
                    affected_tasks=[t.to_dict() for t in affected],
                )
            if reassignment == ARCHIVE_TASKS:
                archive_tasks(conn, [t.task_id for t in affected])
        now = _now()
        conn.execute(
            """INSERT INTO profile_associations (repository_id, profile, created_at, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(repository_id) DO UPDATE SET profile = excluded.profile, updated_at = excluded.updated_at""",
            (repo_id, profile, now, now),
        )
    trace(
        "profile association set", repository=identity.describe(), profile=profile, previous=previous,
        resolution=reassignment if affected else None, tasks=len(affected),
    )
    return SetResult(repo_id, profile, previous, True, recorded, tuple(t.task_id for t in affected), reassignment if affected else None)


@dataclass(frozen=True)
class ResolvedProfile:
    profile: str
    repository_id: int
    newly_associated: bool


def ensure_profile(
    conn: sqlite3.Connection,
    identity: RepositoryIdentity,
    available_profiles: Sequence[str],
    prompter: Prompter | None,
    suggested: str | None = None,
) -> ResolvedProfile:
    """The repository's profile: the stored one, or one the user picks right now.

    `suggested` (for example from a routing rule) only changes what the prompt highlights
    and what an error reports. With no `prompter` an unknown repository is an error.
    """
    available = sorted(available_profiles)
    if suggested not in available:
        suggested = None
    existing = get_association(conn, identity)
    if existing is not None:
        if existing.profile not in available:
            raise AssociationError(
                "associated_profile_missing",
                f"{identity.describe()} is associated with profile {existing.profile!r}, which is not in the "
                "configuration. It will not be launched under any other profile; restore that profile or "
                "change the association with `agent-launcher profile set`.",
                repository=identity.to_dict(),
                profile=existing.profile,
                available_profiles=available,
            )
        trace("profile resolved", repository=identity.describe(), profile=existing.profile)
        return ResolvedProfile(existing.profile, existing.repository_id, False)
    if not available:
        raise AssociationError(
            "no_profiles",
            "No profiles are configured, so this repository cannot be given one. "
            "Add one with `agent-launcher profile add <name> --agent claude`.",
            repository=identity.to_dict(),
            available_profiles=[],
        )
    if prompter is None:
        raise AssociationError(
            "unknown_repository_profile",
            f"{identity.describe()} has no profile. Choose one with "
            "`agent-launcher profile set <repo> <profile>`; none is assigned automatically.",
            repository=identity.to_dict(),
            available_profiles=available,
            suggested_profile=suggested,
        )
    prompter.say(f"New repository: {identity.describe()}")
    choices = [Choice(name, f"{name} (suggested)" if name == suggested else name) for name in available]
    chosen = prompter.select("Which profile should this repository use?", choices, default=suggested)
    result = set_profile(conn, identity, chosen)
    return ResolvedProfile(chosen, result.repository_id, True)
