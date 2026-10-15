"""`open` and `resume`: from a task to its one recorded session.

A launch is a recoverable transaction (ADR 0007). `open` holds the task's lock (`locks`) from resolving the task
until the session is recorded, and writes each stage and resource to `launches` as it goes, so two simultaneous
opens converge on one session and a retry after a failure or crash reuses what exists. The task is `active`
only once its harness session is recorded.

Stages, each a small unit tested on its own: task registry (`tasks`), repository plus
profile (`repositories`, `associations.ensure_profile`), harness picker (`picker`), terminal adapter
(`terminals`) and session record (`sessions`).
The harness starts in the task's repository checkout (`task.repo_path`), whatever branch it is on. The launcher creates
no worktrees and does not check or change the checkout (ADR 0021).

The prompt of a new session is always the harness's invocation of a skill on an argument (`skill_and_argument`,
`Invocation`), given on the harness's command line and submitted at launch (ADR 0020). A GitHub task's is `commodore`
on its URL; the maintenance task `configure` and `integrate gh-dash` start is `commodore-launcher` on its request text.
Titles and bodies are never part of it. Reopening builds no prompt.

A task has one primary session. Opening a task that has one focuses it; if its harness has exited or its
terminal identifiers are stale, the harness adapter's resume mechanism starts the conversation again in a
new terminal session recorded on the same session. Where a harness cannot resume (Codex, or a conversation with no
recorded ID), a fresh conversation starts on that same session, with the Commodore invocation submitted at launch.
Nothing here ever creates a second session.

The first-run copy archived every copied task. `open` of one of those unarchives it and starts fresh the same way: its
old conversation was started in a worktree Commodore does not use (`completion.unarchive_locked`).
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from commodore_launcher.agent_adapters import agent_adapter_for
from commodore_launcher.agents import AgentResolutionError, ResolvedAgent, resolve_agent
from commodore_launcher.associations import ensure_profile, find_repository
from commodore_launcher.completion import is_copy_archived, unarchive_locked
from commodore_launcher.config import Config
from commodore_launcher.github import GitHub
from commodore_launcher.errors import LauncherError
from commodore_launcher import launches
from commodore_launcher.interaction import Prompter
from commodore_launcher.locks import task_lock
from commodore_launcher.logs import trace
from commodore_launcher.picker import pick_agent
from commodore_launcher.repositories import identify_reference
from commodore_launcher.sessions import (
    SessionRecord,
    last_used_agent,
    primary_session,
    record_session,
    replace_terminal,
    terminal_in_use,
)
from commodore_launcher.tasks import (
    TASK_LAUNCH_FAILED,
    TASK_LAUNCHING,
    Task,
    get_task,
    require_not_archived,
    require_openable_kind,
    set_state,
)
from commodore_launcher.terminals import (
    CLOSE_SESSION,
    CREATE_SESSION,
    FOCUS_SESSION,
    CreateSessionRequest,
    CreateSessionResult,
    StaleTerminalSession,
    TerminalAdapter,
    TerminalError,
    TerminalSessionRef,
    UnsupportedCapability,
)


@dataclass(frozen=True)
class OpenResult:
    task: Task
    session: SessionRecord
    prompt: str | None
    prompt_submitted: bool
    notice: str | None = None
    action: str = "created"
    """`created` (a first launch, or a fresh conversation on an existing session), `focused` or `resumed`."""
    resume_state: str | None = None
    """After a resume: `confirmed` (the harness showed the resumed conversation), `failed`, or
    `unconfirmed` (resume attempted, not confirmed)."""


COMMODORE_SKILL = "commodore"
MANAGEMENT_SKILL = "commodore-launcher"


@dataclass(frozen=True)
class Invocation:
    """A harness started on a skill with an argument: the prompt and the launch arguments that carry it."""

    prompt: str
    args: tuple[str, ...]


def skill_and_argument(task: Task) -> tuple[str, str]:
    """What a task starts: `commodore` on a GitHub issue or PR URL, or `commodore-launcher` on the request text of the
    maintenance task (`configure`, `integrate gh-dash`). Any other task has no URL to invoke Commodore on."""
    require_openable_kind(task)
    if task.is_maintenance:
        return MANAGEMENT_SKILL, f"{task.title}\n\n{task.description}" if task.description else task.title
    assert task.url
    return COMMODORE_SKILL, task.url


def require_launch_form(harness: str, adapter_name: str) -> None:
    """Refuse a harness whose adapter has no form for a prompt on the command line. Nothing is pasted instead."""
    if agent_adapter_for(adapter_name).launch_prompt_args() is None:
        raise LauncherError(
            "harness_no_launch_form",
            f"harness: {harness} cannot be given a prompt at launch (its adapter {adapter_name!r} has no launch "
            "form for one). No harness was started.",
            harness=harness, adapter=adapter_name,
        )


def invocation_for(ctx: "_Context", skill: str, argument: str) -> Invocation:
    """The one internal path for a harness started on a skill with an argument: `open`, `configure` and
    `integrate gh-dash` all come here."""
    adapter = agent_adapter_for(ctx.instance.adapter)
    require_launch_form(ctx.agent, ctx.instance.adapter)
    # All three adapters with a launch form also have a skill syntax, so neither is None here.
    prompt, args = adapter.skill_invocation(skill, argument), adapter.launch_prompt_args()
    assert prompt is not None and args is not None
    return Invocation(prompt, args)


@dataclass(frozen=True)
class _Context:
    task: Task
    profile: str
    agent: str
    instance: ResolvedAgent


def _require_available(adapter: TerminalAdapter) -> None:
    if not adapter.available():
        raise TerminalError(
            "terminal_unavailable",
            adapter.unavailable_reason() or f"Terminal adapter {adapter.name!r} is not available.",
            adapter=adapter.name,
        )


def _context(
    conn: sqlite3.Connection,
    task: Task,
    *,
    config: Config,
    prompter: Prompter | None,
    agent: str | None,
    fixed_agent: str | None,
    offline: bool,
    base_env: Mapping[str, str] | None,
) -> _Context:
    """The task's repository, profile and harness, checked as `open` always has. `fixed_agent` is the harness of
    an existing session: it is not picked again."""
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
    if fixed_agent is not None:
        if agent is not None and agent != fixed_agent:
            raise LauncherError(
                "harness_mismatch",
                f"Task {task.id} already has a session with {fixed_agent!r}; it cannot be reopened with {agent!r}.",
                session_harness=fixed_agent,
            )
        chosen = fixed_agent
    else:
        chosen = pick_agent(
            resolved.profile,
            list(profile.harnesses),
            mode=config.harness_selection,
            default=profile.default_harness,
            last_used=last_used_agent(conn, resolved.profile),
            prompter=prompter,
            requested=agent,
        ).agent
    try:
        instance = resolve_agent(resolved.profile, chosen, config=config, base_env=base_env)
    except AgentResolutionError as exc:
        hint = (
            f" This task's session was started with {chosen!r}: add it back to the profile with `commodore profile "
            f"edit {resolved.profile} --harness {chosen} --executable <path>`."
            if fixed_agent is not None else ""
        )
        raise LauncherError(
            "harness_unresolved", f"{exc}{hint}", profile=resolved.profile, harness=chosen
        ) from exc
    return _Context(task, resolved.profile, chosen, instance)


def _start(
    ctx: _Context, adapter: TerminalAdapter, *, command: tuple[str, ...], invocation: Invocation | None,
    directory: str, resume: bool = False,
) -> CreateSessionResult:
    agent_adapter = agent_adapter_for(ctx.instance.adapter)
    return adapter.create_session(
        CreateSessionRequest(
            title=f"{Path(ctx.task.repo_path).name} — {ctx.task.title}",
            working_directory=directory,
            command=command,
            env=ctx.instance.env,
            pinned_env=ctx.instance.pinned_env,
            prompt=invocation.prompt if invocation else None,
            submit_prompt=invocation is not None,
            prompt_args=invocation.args if invocation else (),
            resume_check=agent_adapter.resume_check() if resume else None,
        )
    )


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
    force_resume: bool = False,
    confirmed: bool = False,
    github: GitHub | None = None,
    refresh: bool = True,
) -> OpenResult:
    task = get_task(conn, task_ref)
    require_openable_kind(task)  # a copied local task is never opened, whatever its state
    copied = is_copy_archived(conn, task)
    if not copied:
        require_not_archived(task)
    if refresh and not offline and task.source == "github":
        # Best effort: the stored title, state and head SHA follow GitHub. It never stops an open.
        # Imported here: github_tasks imports this module, so a top-level import would be circular.
        from commodore_launcher.github_tasks import refresh_task

        refresh_task(conn, task, github, archived_ok=copied)
    # Held from here to "ready". A second open of this task waits, then finds the session and focuses it; other
    # tasks use other locks.
    with task_lock(task.id):
        task = get_task(conn, task.id)
        copied = is_copy_archived(conn, task)
        if not copied:
            require_not_archived(task)
        trace("open task", task=task.id)
        if copied:
            return _open_copy_archived(
                conn, task, config=config, adapter=adapter, prompter=prompter, agent=agent, offline=offline,
                base_env=base_env,
            )
        existing = primary_session(conn, task.id)
        if existing is not None:
            return _reopen(
                conn, task, existing, config=config, adapter=adapter, prompter=prompter, agent=agent,
                offline=offline, base_env=base_env, force_resume=force_resume, confirmed=confirmed,
            )
        return _first_open(
            conn, task, config=config, adapter=adapter, prompter=prompter, agent=agent, offline=offline,
            base_env=base_env,
        )


def _open_copy_archived(
    conn: sqlite3.Connection,
    task: Task,
    *,
    config: Config,
    adapter: TerminalAdapter,
    prompter: Prompter | None,
    agent: str | None,
    offline: bool,
    base_env: Mapping[str, str] | None,
) -> OpenResult:
    """A task the first-run copy archived: its conversation started in a worktree Commodore does not use, so it is
    unarchived and launched fresh. Every check that can refuse runs first and yields the context and invocation the
    launch then uses, so a refusal leaves the task archived with its rows intact."""
    session = primary_session(conn, task.id)
    ctx, invocation = _check_launch(
        conn, task, config=config, adapter=adapter, prompter=prompter, agent=agent,
        fixed_agent=session.agent if session else None, offline=offline, base_env=base_env,
    )
    forgotten = session.terminal if session else None
    unarchive_locked(conn, task)
    task = get_task(conn, task.id)
    ctx = replace(ctx, task=task)  # the title as the refresh just stored it
    if session is None:
        return _first_open(
            conn, task, config=config, adapter=adapter, prompter=prompter, agent=ctx.agent, offline=offline,
            base_env=base_env, plan=(ctx, invocation),
        )
    where = f" Its old terminal session ({forgotten.adapter} workspace {forgotten.workspace_id}) is left open; close it yourself if it is still running." if forgotten else ""
    return _start_fresh(
        conn, task, session, ctx, invocation, adapter,
        "This task was copied from the old launcher: its conversation was started in a worktree Commodore does not "
        f"use and was forgotten, so a fresh one was started.{where}",
    )


def _check_launch(
    conn: sqlite3.Connection,
    task: Task,
    *,
    config: Config,
    adapter: TerminalAdapter,
    prompter: Prompter | None,
    agent: str | None,
    fixed_agent: str | None,
    offline: bool,
    base_env: Mapping[str, str] | None,
) -> tuple[_Context, Invocation]:
    """Everything that can refuse a launch, in one place, before anything is written. Returns what the launch uses."""
    adapter.require(CREATE_SESSION)
    _require_available(adapter)
    skill, argument = skill_and_argument(task)  # a task with no URL is refused before the pickers
    ctx = _context(
        conn, task, config=config, prompter=prompter, agent=agent, fixed_agent=fixed_agent, offline=offline,
        base_env=base_env,
    )
    return ctx, invocation_for(ctx, skill, argument)  # a harness with no launch form is refused here


def _first_open(
    conn: sqlite3.Connection,
    task: Task,
    *,
    config: Config,
    adapter: TerminalAdapter,
    prompter: Prompter | None,
    agent: str | None,
    offline: bool,
    base_env: Mapping[str, str] | None,
    plan: tuple[_Context, Invocation] | None = None,
) -> OpenResult:
    """`plan` is the context and invocation a caller has already validated (the copy-archived launch)."""
    if plan is None:
        prior = launches.get_launch(conn, task.id)
        if prior is not None and prior.terminal is not None and prior.agent:
            agent = prior.agent  # a terminal session already runs this harness: a retry does not change it
        ctx, invocation = _check_launch(
            conn, task, config=config, adapter=adapter, prompter=prompter, agent=agent, fixed_agent=None,
            offline=offline, base_env=base_env,
        )
    else:
        ctx, invocation = plan
    # Every check that can refuse the launch is behind us; from here each stage is written down.
    launches.begin(conn, task.id)
    set_state(conn, task.id, TASK_LAUNCHING)
    try:
        return _run_stages(conn, task, ctx, invocation, config=config, adapter=adapter, prompter=prompter, offline=offline)
    except BaseException:
        # Ctrl-C included. A SIGKILL cannot run this, so `launching` also means "interrupted"; `open` resumes either.
        try:
            set_state(conn, task.id, TASK_LAUNCH_FAILED)
        except sqlite3.Error:
            pass
        raise


def _run_stages(
    conn: sqlite3.Connection,
    task: Task,
    ctx: _Context,
    invocation: Invocation,
    *,
    config: Config,
    adapter: TerminalAdapter,
    prompter: Prompter | None,
    offline: bool,
) -> OpenResult:
    launch = launches.get_launch(conn, task.id)
    assert launch is not None
    agent_adapter = agent_adapter_for(ctx.instance.adapter)
    notices: list[str] = []

    result: CreateSessionResult | None = None
    conversation_id = launch.conversation_id
    if launch.terminal is not None:
        # An earlier attempt created this terminal session and died before recording the session.
        ref = launch.terminal
        where = f"{ref.adapter} workspace {ref.workspace_id}"
        try:
            if ref.adapter != adapter.name:
                found, why = False, "belongs to another terminal adapter, which cannot be asked about it"
            else:
                found, why = adapter.read_screen(ref) is not None, ""
        except Exception as exc:
            # Unsure is not gone: the earlier harness may still be running there. The record is kept.
            raise LauncherError(
                "terminal_check_failed",
                f"An earlier launch of task {task.id} created the terminal session {where}, and it could not be "
                f"checked ({exc}). Its harness may still be running, so no second one was started and the record "
                f"was kept. Check or close that terminal session yourself, then run `commodore open {task.id}` "
                "again.",
                task=task.id,
                terminal=ref.to_dict(),
            ) from exc
        if found:
            result = CreateSessionResult(
                ref, notice="Reused the terminal session an earlier launch of this task had created."
            )
        else:
            launches.forget_terminal(conn, task.id)
            conversation_id = None
            if why:
                notices.append(
                    f"An earlier launch of this task created the terminal session {where}, which {why}. It was not "
                    "reused and not closed; close it yourself if it is still open."
                )
    if result is None:
        # Fix the harness's conversation ID now, where the harness allows it: far more reliable than finding it later.
        conversation_id = agent_adapter.new_conversation_id()
        command = ctx.instance.argv + (agent_adapter.conversation_args(conversation_id) if conversation_id else ())
        launches.note_agent(conn, task.id, ctx.agent, conversation_id)
        result = _start(ctx, adapter, command=command, invocation=invocation, directory=task.repo_path)
    recorded = False
    try:
        launches.note_terminal(conn, task.id, result.session)
        recorded = True
        session = record_session(conn, task.id, ctx.profile, ctx.agent, result.session, conversation_id)
    except Exception as exc:
        raise _roll_back_terminal(conn, adapter, task, result.session, exc, recorded=recorded) from exc
    notice = " ".join((*notices, result.notice or "")).strip() or None
    return OpenResult(get_task(conn, task.id), session, invocation.prompt, result.prompt_submitted, notice)


def _roll_back_terminal(
    conn: sqlite3.Connection,
    adapter: TerminalAdapter,
    task: Task,
    ref: TerminalSessionRef,
    cause: Exception,
    *,
    recorded: bool = True,
) -> LauncherError:
    """The terminal session exists but the session record failed. Close it only if this launch created it, the
    adapter can identify it, and no session record uses it; otherwise keep it and say where it is. `recorded` says whether `launches` holds the terminal for the retry."""
    where = f"{ref.adapter} workspace {ref.workspace_id}"
    head = f"Task {task.id} was not started: recording its session failed ({cause})."
    try:
        verified = ref.created_by_launcher and bool(ref.surface_id) and not terminal_in_use(conn, ref)
    except sqlite3.Error:
        verified = False
    closed = False
    if verified:
        try:
            adapter.close_session(ref)
            closed = True
        except Exception:
            pass
    if closed:
        try:
            launches.forget_terminal(conn, task.id)
        except sqlite3.Error:
            pass  # the terminal is gone; a retry finds the stale record, sees it is gone and replaces it
        return LauncherError(
            "launch_failed",
            f"{head} The terminal session it created ({where}) was closed. "
            f"Run `commodore open {task.id}` again.",
            task=task.id,
        )
    reason = (
        "it could not be closed"
        if verified
        else "it could not be verified as created by the launcher, identifiable and unused"
    )
    after = (
        f"`commodore open {task.id}` again reuses it if it still exists."
        if recorded
        else f"It is not recorded, so `commodore open {task.id}` again creates another: close this one yourself first."
    )
    return LauncherError(
        "launch_incomplete",
        f"{head} The terminal session ({where}) was left open because {reason}. {after}",
        task=task.id,
        terminal=ref.to_dict(),
    )


def _terminal_state(adapter: TerminalAdapter, ctx: _Context, session: SessionRecord) -> str:
    """`gone` (no such terminal session), `exited` (positive sign the harness ended), `alive` (the harness is
    there, or we cannot tell: never resume over a harness that may be running)."""
    ref = session.terminal
    if ref is None:
        return "gone"
    if ref.adapter != adapter.name:
        # Another adapter says nothing about a session that lives elsewhere: never resume over it.
        raise LauncherError(
            "terminal_mismatch",
            f"This task's session lives in {ref.adapter}; use --terminal {ref.adapter} "
            f"(or set terminal.adapter to it). Nothing was changed.",
            session_adapter=ref.adapter,
            adapter=adapter.name,
        )
    try:
        screen = adapter.read_screen(ref)
    except UnsupportedCapability:
        return "alive"
    if screen is None:
        return "gone"
    return "exited" if agent_adapter_for(ctx.instance.adapter).has_exited(screen) else "alive"


def _reopen(
    conn: sqlite3.Connection,
    task: Task,
    session: SessionRecord,
    *,
    config: Config,
    adapter: TerminalAdapter,
    prompter: Prompter | None,
    agent: str | None,
    offline: bool,
    base_env: Mapping[str, str] | None,
    force_resume: bool,
    confirmed: bool = False,
) -> OpenResult:
    """Scenario B: no picker, no new prompt, no new session. Focus it, or resume it where it can't be focused."""
    _require_available(adapter)
    ctx = _context(
        conn, task, config=config, prompter=prompter, agent=agent, fixed_agent=session.agent,
        offline=offline, base_env=base_env,
    )
    state = _terminal_state(adapter, ctx, session)
    if state == "alive" and not force_resume:
        adapter.require(FOCUS_SESSION)
        assert session.terminal is not None
        try:
            adapter.focus_session(session.terminal)
            return OpenResult(task, session, None, False, action="focused")
        except StaleTerminalSession:
            state = "gone"  # it vanished between the check and the focus
    if state == "alive":
        assert session.terminal is not None
        if _resume_command(ctx, session) is None:
            # Starting a fresh conversation would leave this harness running, untracked, in the same checkout.
            raise _harness_running(task, session.terminal, f"its conversation can't be resumed ({_cannot_resume(session)})")
        # Knowingly a second harness process on one conversation: the user must say so.
        _confirm(
            prompter, confirmed, task,
            "The harness may still be running in the old terminal session. Resuming starts a second process on the "
            "same conversation. Pass --yes to confirm.",
            f"Task {task.id}'s harness may still be running. Start a second process on its conversation anyway?",
        )
    notice_head = {
        "gone": "The recorded terminal session no longer exists.",
        "exited": "The harness has exited.",
        "alive": "Resuming as asked, in a new terminal session.",
    }[state]
    return _resume(conn, task, session, ctx, adapter, notice_head, state)


def _harness_running(task: Task, ref: TerminalSessionRef, why: str, left: str | None = None) -> LauncherError:
    return LauncherError(
        "harness_running",
        f"The harness is still running in {ref.adapter} workspace {ref.workspace_id}, and {why}, so no second process "
        f"was started. Focus it, or close it and run `commodore open {task.id}`." + (f" {left}" if left else ""),
        task=task.id,
    )


def _cannot_resume(session: SessionRecord) -> str:
    return "no conversation ID is recorded" if not session.agent_conversation_id else "the harness has no resume mechanism"


def _resume_command(ctx: _Context, session: SessionRecord) -> tuple[str, ...] | None:
    """The harness's resume arguments for the stored conversation, or None when it can't be resumed (no recorded ID, or
    no mechanism). `resume_args` is only checked when it would be used."""
    conversation_id = session.agent_conversation_id
    if not conversation_id:
        return None
    if ctx.instance.resume_args and not any("{id}" in a for a in ctx.instance.resume_args):
        raise LauncherError(
            "resume_args_invalid",
            f"{ctx.profile}/{ctx.agent}: resume_args must contain {{id}}, where the conversation ID goes; "
            "without it the conversation would not be resumed.",
            profile=ctx.profile,
            agent=ctx.agent,
        )
    return tuple(a.replace("{id}", conversation_id) for a in ctx.instance.resume_args) or agent_adapter_for(
        ctx.instance.adapter
    ).resume_args(conversation_id)


def _resume(
    conn: sqlite3.Connection,
    task: Task,
    session: SessionRecord,
    ctx: _Context,
    adapter: TerminalAdapter,
    notice_head: str,
    old_state: str,
) -> OpenResult:
    """Resume the session's conversation in a new terminal session, recorded on the same session. Where that is
    impossible, or the harness says it has no such conversation, a fresh one starts on the same session."""
    resume = _resume_command(ctx, session)
    skill, argument = skill_and_argument(task)
    invocation = invocation_for(ctx, skill, argument)  # refused here, before any terminal session exists
    exited = session.terminal if old_state == "exited" else None
    if resume is None:
        return _start_fresh(
            conn, task, session, ctx, invocation, adapter,
            f"{notice_head} {ctx.agent!r} cannot resume this conversation ({_cannot_resume(session)}), so a fresh one "
            "was started in a new terminal session.",
            close_old=exited,
        )
    adapter.require(CREATE_SESSION)
    result = _start(ctx, adapter, command=ctx.instance.argv + resume, invocation=None, resume=True, directory=task.repo_path)
    state = result.resume_state or "unconfirmed"
    if state == "failed":
        # The harness has no such conversation (nothing was ever submitted in it). The terminal session just created
        # is the launcher's own, so it is closed. A harness that is still running keeps the session: nothing fresh
        # starts beside it. Otherwise a fresh conversation starts on the same session.
        left = _close_ours(adapter, result.session)
        if old_state == "alive":
            assert session.terminal is not None
            raise _harness_running(task, session.terminal, "it has no saved conversation to resume", left)
        return _start_fresh(
            conn, task, session, ctx, invocation, adapter,
            f"{notice_head} The harness could not resume the conversation (it had no saved conversation, usually "
            "because nothing was submitted before it exited), so a fresh one was started.",
            close_old=exited, extra=left, resume_state="failed",
        )
    replace_terminal(conn, session.id, result.session)
    verdict = {
        "confirmed": "The conversation was restored: the harness showed it.",
        "unconfirmed": "Resume attempted, not confirmed: check the terminal to see the conversation.",
    }[state]
    return OpenResult(
        task, replace(session, terminal=result.session), None, False, f"{notice_head} {verdict}", "resumed", state
    )


def _close_ours(adapter: TerminalAdapter, ref: TerminalSessionRef) -> str | None:
    """Close a terminal session the launcher created and can still identify. Otherwise, or when the terminal refuses
    (any failure), leave it and return a notice naming it."""
    where = f"{ref.adapter} workspace {ref.workspace_id}"
    if ref.adapter != adapter.name or not (ref.created_by_launcher and ref.surface_id):
        return (
            f"The old terminal session ({where}) was left open: it is not recorded as created by the launcher with an "
            "identifiable surface here. Close it yourself if you want to."
        )
    try:
        adapter.require(CLOSE_SESSION)
        adapter.close_session(ref)
    except Exception as exc:  # as `_roll_back_terminal`: whatever the terminal says, nothing else is aborted
        return f"The old terminal session ({where}) was left open ({getattr(exc, 'message', exc)}). Close it yourself if you want to."
    return None


def _start_fresh(
    conn: sqlite3.Connection,
    task: Task,
    session: SessionRecord,
    ctx: _Context,
    invocation: Invocation,
    adapter: TerminalAdapter,
    notice: str,
    *,
    close_old: TerminalSessionRef | None = None,
    extra: str | None = None,
    resume_state: str | None = None,
) -> OpenResult:
    """Resume is impossible, so start a new conversation on the same session record (ADR 0005): a new terminal
    session, a new conversation ID where the harness allows one, and the Commodore invocation submitted at launch.
    `close_old` is the exited terminal session this replaces, closed first if it is the launcher's."""
    adapter.require(CREATE_SESSION)
    agent_adapter = agent_adapter_for(ctx.instance.adapter)
    conversation_id = agent_adapter.new_conversation_id()
    command = ctx.instance.argv + (agent_adapter.conversation_args(conversation_id) if conversation_id else ())
    result = _start(ctx, adapter, command=command, invocation=invocation, directory=task.repo_path)
    replace_terminal(conn, session.id, result.session, conversation_id=conversation_id, new_conversation=True)
    # Closed only once the new one runs and is recorded, so a failed start never leaves the row on a closed workspace.
    left = [n for n in (extra, _close_ours(adapter, close_old) if close_old else None) if n]
    return OpenResult(
        task, replace(session, terminal=result.session, agent_conversation_id=conversation_id), invocation.prompt,
        result.prompt_submitted, " ".join(n for n in (notice, *left, result.notice) if n), "created", resume_state,
    )


def _confirm(prompter: Prompter | None, confirmed: bool, task: Task, refusal: str, question: str) -> None:
    if confirmed:
        return
    if prompter is None:
        raise LauncherError("confirmation_required", refusal, task=task.id)
    if not prompter.confirm(question, default=False):
        raise LauncherError("declined", "Cancelled. Nothing was changed.", task=task.id)


def resume_task(
    conn: sqlite3.Connection, task_ref: str, *, force: bool = False, confirmed: bool = False, **kwargs
) -> OpenResult:
    """`resume`: like `open` of a task that has a session, but a task that was never opened is an error.
    With `force`, resume in a new terminal session even though the old one looks alive."""
    task = get_task(conn, task_ref)
    require_openable_kind(task)
    if primary_session(conn, task.id) is None:
        raise LauncherError(
            "no_session", f"Task {task.id} has not been opened, so there is nothing to resume. Run `commodore open {task.id}`."
        )
    return open_task(conn, task.id, force_resume=force, confirmed=confirmed, **kwargs)
