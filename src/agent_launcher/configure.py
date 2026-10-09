"""`agent-launcher configure`: a maintenance task whose agent runs the bundled management skill (SPEC §24, ADR 0016).

It is an ordinary local task launched through the normal path (`open_task`): the repository must have an explicit
profile, the agent is one of that profile's, and the task gets its own worktree and session. Only three things are
particular to it: the repository defaults to a launcher-owned directory, the task is found again instead of
duplicated, and the workflow is the built-in one whose skill is the management skill.
"""

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from agent_launcher.associations import AssociationError, ensure_profile, get_association, set_profile
from agent_launcher.errors import LauncherError
from agent_launcher.git import GitError, run_git
from agent_launcher.interaction import Prompter
from agent_launcher.paths import launcher_home
from agent_launcher.repositories import RepositoryIdentity, identify_reference
from agent_launcher.sessions import primary_session
from agent_launcher.tasks import TASK_ARCHIVED, Task, create_task, list_tasks, set_workflow
from agent_launcher.workflows import BUILTIN_CONFIGURE_ID

MAINTENANCE_TITLE = "Configure Agent Launcher"
DEFAULT_REQUEST = "Review my Agent Launcher setup and help me change it. Start with `agent-launcher doctor`."


def maintenance_directory() -> Path:
    return launcher_home() / "maintenance"


def _initial_commit(path: Path) -> None:
    """One empty commit that does not depend on the user's git configuration: no signing, no hooks, and an identity
    only if none resolves (given for this command with `-c`; no config file is written)."""
    options = ["-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]
    for key, fallback in (("user.name", "Agent Launcher"), ("user.email", "agent-launcher@localhost")):
        if not run_git(path, ["config", "--get", key], ok=(0, 1)).stdout.strip():
            options += ["-c", f"{key}={fallback}"]
    run_git(path, [*options, "commit", "-q", "--allow-empty", "--no-verify", "-m", "Agent Launcher maintenance"])


def ensure_maintenance_repository() -> Path:
    """The launcher-owned repository for configure tasks. Tasks need a repository to cut a worktree from, so this is a
    plain `git init` with one empty commit (so there is a base branch). It has no remote, so it never has a GitHub ID.
    A repository that already has commits is left exactly as it is; one with none (an earlier attempt failed after
    `git init`) gets its initial commit now."""
    path = maintenance_directory()
    path.mkdir(parents=True, exist_ok=True)
    try:
        top = run_git(path, ["rev-parse", "--show-toplevel"], ok=(0, 128))
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != path.resolve():
            run_git(path, ["init", "-q"])
        if run_git(path, ["rev-parse", "--verify", "-q", "HEAD"], ok=(0, 1)).returncode != 0:
            _initial_commit(path)
    except GitError as exc:
        raise LauncherError(
            "maintenance_repository_failed",
            f"Could not prepare the maintenance repository {path}: {exc}. Use --repo with a repository of your own, "
            "or fix git and delete that directory.",
            path=str(path),
        ) from exc
    return path


def find_maintenance_task(
    conn: sqlite3.Connection, repository_id: int, profile: str, title: str = MAINTENANCE_TITLE
) -> Task | None:
    """The one configure task of this repository and profile, if any. It is recognised by its stored workflow (set when
    it is created) and its title (`configure` and `integrate` have their own), so a task of the user's with the same
    title is never taken. Archived ones are not reused."""
    for task in list_tasks(conn):
        if (
            task.workflow == BUILTIN_CONFIGURE_ID and task.source == "local" and task.repository_id == repository_id
            and task.profile == profile and task.title == title and task.state != TASK_ARCHIVED
        ):
            return task
    return None


@dataclass(frozen=True)
class ConfigureTask:
    task: Task
    created: bool
    has_session: bool


def prepare_task(
    conn: sqlite3.Connection,
    *,
    repo: str | None,
    profile: str | None,
    request: str | None,
    available_profiles: list[str],
    prompter: Prompter | None,
    title: str = MAINTENANCE_TITLE,
    default_request: str = DEFAULT_REQUEST,
) -> ConfigureTask:
    """Find or create the maintenance task. The repository's profile is the explicit stored one; `profile` names it
    only when the repository has none yet (the user's explicit choice, stored like `profile set`). A different
    profile for a repository that has one is refused: reassignment is `profile set`'s, with its task resolution."""
    if repo is None:
        path: Path | str = ensure_maintenance_repository()
    else:
        path = Path(repo).expanduser()
        if not path.is_dir():
            raise LauncherError("not_a_repository", f"{repo} is not a directory.")
    identity: RepositoryIdentity = identify_reference(str(path), fetch_github=False)
    assert identity.path is not None
    existing = get_association(conn, identity)
    if profile is not None:
        if profile not in available_profiles:
            raise AssociationError(
                "unknown_profile", f"No profile {profile!r}. Profiles: {', '.join(available_profiles) or 'none'}.",
                available_profiles=available_profiles,
            )
        if existing is None:
            set_profile(conn, identity, profile)
        elif existing.profile != profile:
            raise AssociationError(
                "configure_profile_mismatch",
                f"{identity.describe()} is associated with profile {existing.profile!r}, not {profile!r}. Nothing "
                "was changed. Change the association deliberately with `agent-launcher profile set`, or pick "
                "another repository with --repo.",
                profile=existing.profile, requested_profile=profile,
            )
    resolved = ensure_profile(conn, identity, available_profiles, prompter)
    task = find_maintenance_task(conn, resolved.repository_id, resolved.profile, title)
    if task is not None:
        opened = primary_session(conn, task.id) is not None
        if request and (opened or request.strip() != task.description):
            why = (
                "is already open, and reopening does not send a new prompt" if opened
                else f"already exists with a different request ({task.description!r}), and its prompt is built from that"
            )
            raise LauncherError(
                "configure_request_ignored",
                f"Task {task.id} {why}. Nothing was changed. Run `agent-launcher configure` without a request to use "
                f"it as it is; say the new request to the agent in its terminal, or `agent-launcher restart "
                f"{task.id}` for a fresh conversation.",
                task=task.id,
            )
        return ConfigureTask(task, False, opened)
    task = create_task(
        conn, title, request or default_request, resolved.repository_id, identity.path, resolved.profile
    )
    set_workflow(conn, task.id, BUILTIN_CONFIGURE_ID)  # the marker `find_maintenance_task` looks for
    return ConfigureTask(replace(task, workflow=BUILTIN_CONFIGURE_ID), True, False)
