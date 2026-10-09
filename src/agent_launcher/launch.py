"""`open`: the pipeline from a task to a recorded session.

Stages, each a small unit tested on its own: task registry (`tasks`), repository plus
profile (`repositories`, `associations.ensure_profile`), agent picker (`picker`), prompt
builder (`prompt`), terminal adapter (`terminals`) and session record (`sessions`).
No worktree yet: the agent runs in the repository directory.
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_launcher.agents import AgentResolutionError, resolve_agent
from agent_launcher.associations import ensure_profile, find_repository
from agent_launcher.config import Config
from agent_launcher.errors import LauncherError
from agent_launcher.interaction import Prompter
from agent_launcher.logs import trace
from agent_launcher.picker import pick_agent
from agent_launcher.prompt import build_prompt
from agent_launcher.repositories import identify_reference
from agent_launcher.sessions import SessionRecord, last_used_agent, record_session
from agent_launcher.tasks import Task, get_task
from agent_launcher.terminals import (
    CREATE_SESSION,
    PREPARE_PROMPT,
    SUBMIT_PROMPT,
    CreateSessionRequest,     TerminalAdapter,
    TerminalError,
)


@dataclass(frozen=True)
class OpenResult:
    task: Task
    session: SessionRecord
    prompt: str
    prompt_prepared: bool
    prompt_submitted: bool


def open_task(
    conn: sqlite3.Connection,
    task_ref: str,
    *,
    config: Config,
    adapter: TerminalAdapter,
    prompter: Prompter | None,
    agent: str | None = None,
    offline: bool = False,
    base_env: Mapping[str, str] | None = None,
) -> OpenResult:
    task = get_task(conn, task_ref)
    trace("open task", task=task.id)
    submit = config.prompt_execution == "execute"
    adapter.require(CREATE_SESSION)
    # The prompt is always passed, so the adapter must be able to handle it as asked (SPEC §18).
    adapter.require(SUBMIT_PROMPT if submit else PREPARE_PROMPT)
    if not adapter.available():
        raise TerminalError(
            "terminal_unavailable", f"Terminal adapter {adapter.name!r} is not available.", adapter=adapter.name
        )
    if not Path(task.repo_path).is_dir():
        raise LauncherError(
            "repo_missing", f"The repository directory {task.repo_path} no longer exists.", repo_path=task.repo_path
        )

    # The association is authoritative: a task never runs under a profile other than the
    # repository's current one.
    identity = identify_reference(task.repo_path, fetch_github=not offline)
    # Read-only: a different repository now at this path must not get an association prompt or write.
    if find_repository(conn, identity) != task.repository_id:
        raise LauncherError(
            "repository_changed",
            f"The checkout at {task.repo_path} is no longer the repository task {task.id} was created for. "
            "It is not launched.",
            repo_path=task.repo_path,
        )
    resolved = ensure_profile(conn, identity, sorted(config.profiles), prompter)
    if resolved.profile != task.profile:
        raise LauncherError(
            "profile_mismatch",
            f"Task {task.id} was created under profile {task.profile!r} but its repository now uses "
            f"{resolved.profile!r}. It is not launched under either until you resolve this.",
            task_profile=task.profile,
            repository_profile=resolved.profile,
        )

    profile = config.profiles[resolved.profile]
    pick = pick_agent(
        resolved.profile,
        list(profile.agents),
        mode=config.agent_selection,
        default=profile.default_agent,
        last_used=last_used_agent(conn, resolved.profile),
        prompter=prompter,
        requested=agent,
    )
    try:
        agent_instance = resolve_agent(resolved.profile, pick.agent, config=config, base_env=base_env)
    except AgentResolutionError as exc:
        raise LauncherError("agent_unresolved", str(exc), profile=resolved.profile, agent=pick.agent) from exc

    prompt = build_prompt(task)
    result = adapter.create_session(
        CreateSessionRequest(
            title=task.title,
            working_directory=task.repo_path,
            command=agent_instance.argv,
            env=agent_instance.env,
            prompt=prompt,
            submit_prompt=submit,
        )
    )
    # If recording fails here the terminal session is orphaned; ticket #11's state machine handles that.
    session = record_session(conn, task.id, resolved.profile, pick.agent, result.session)
    return OpenResult(get_task(conn, task.id), session, prompt, result.prompt_prepared, result.prompt_submitted)
