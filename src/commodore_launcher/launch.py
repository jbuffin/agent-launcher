"""`open`, `resume` and `restart`: from a task to its one recorded session.

A launch is a recoverable transaction (ADR 0007). `open` holds the task's lock (`locks`) from resolving the task
until the session is recorded, and writes each stage and resource to `launches` as it goes, so two simultaneous
opens converge on one session and a retry after a failure or crash reuses what exists. The task is `active`
only once its harness session is recorded.

Stages, each a small unit tested on its own: task registry (`tasks`), repository plus
profile (`repositories`, `associations.ensure_profile`), harness picker (`picker`), terminal adapter
(`terminals`) and session record (`sessions`).
The harness runs in the task's own worktree (`worktrees`): created from the base branch on first open, or one the
user adopted. A task opened before worktrees existed keeps running in its repository directory.

The prompt of a new session is always the harness's invocation of a skill on an argument (`skill_and_argument`,
`Invocation`), given on the harness's command line and submitted at launch (ADR 0020). A GitHub task's is `commodore`
on its URL; the maintenance task `configure` and `integrate gh-dash` start is `commodore-launcher` on its request text.
Titles and bodies are never part of it. Reopening builds no prompt.

A task has one primary session. Opening a task that has one focuses it; if its harness has exited or its
terminal identifiers are stale, the harness adapter's resume mechanism starts the conversation again in a
new terminal session recorded on the same session. Nothing here ever creates a second session.
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from commodore_launcher.agent_adapters import agent_adapter_for
from commodore_launcher.agents import AgentResolutionError, ResolvedAgent, resolve_agent
from commodore_launcher.associations import ensure_profile, find_repository
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
    set_state,
)
from commodore_launcher.worktrees import WorktreeRecord, ensure_worktree, working_directory
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
    """`created`, `focused`, `resumed` or `restarted`."""
    resume_state: str | None = None
    """After a resume: `confirmed` (the harness showed the resumed conversation), `failed`, or
    `unconfirmed` (resume attempted, not confirmed)."""
    worktree: WorktreeRecord | None = None


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
    if task.is_maintenance:
        return MANAGEMENT_SKILL, f"{task.title}\n\n{task.description}" if task.description else task.title
    if task.source == "github" and task.url:
        return COMMODORE_SKILL, task.url
    raise LauncherError(
        "task_needs_url",
        f"Task {task.id} needs a GitHub issue or PR URL: Commodore is invoked on the URL, and this task has none. "
        "Nothing was started. Open the issue or pull request by its URL instead.",
        task=task.id,
    )


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
    """The one internal path for a harness started on a skill with an argument: `open`, `restart`, `configure` and
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
        raise LauncherError("harness_unresolved", str(exc), profile=resolved.profile, harness=chosen) from exc
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
    refresh_completion: bool = True,
) -> OpenResult:
    task = get_task(conn, task_ref)
    require_not_archived(task)
    if refresh_completion and not offline and task.source == "github":
        # Opportunistic (#22): note a closed or merged item. Best effort; it never stops an open or changes anything
        # but the task's cleanup flag.
        # Imported here: github_tasks imports this module, so a top-level import would be circular.
        from commodore_launcher.github_tasks import refresh_completion as refresh_github_completion

        refresh_github_completion(conn, [task], github)
    # Held from here to "ready". A second open of this task waits, then finds the session and focuses it; other
    # tasks use other locks.
    with task_lock(task.id):
        task = get_task(conn, task.id)
        require_not_archived(task)
        trace("open task", task=task.id)
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
) -> OpenResult:
    adapter.require(CREATE_SESSION)
    _require_available(adapter)
    skill, argument = skill_and_argument(task)  # a task with no URL is refused before the pickers
    prior = launches.get_launch(conn, task.id)
    if prior is not None and prior.terminal is not None and prior.agent:
        agent = prior.agent  # a terminal session already runs this harness: a retry does not change it
    ctx = _context(
        conn, task, config=config, prompter=prompter, agent=agent, fixed_agent=None, offline=offline, base_env=base_env,
    )
    invocation = invocation_for(ctx, skill, argument)  # a harness with no launch form is refused here
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
    # The worktree stays recorded if a later stage fails, and the next `open` reuses it.
    tree = ensure_worktree(conn, task, config, prompter, offline=offline)
    launches.note_worktree_ready(conn, task.id)
    launch = launches.get_launch(conn, task.id)
    assert launch is not None
    agent_adapter = agent_adapter_for(ctx.instance.adapter)
    notices = list(tree.notices)

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
        result = _start(ctx, adapter, command=command, invocation=invocation, directory=tree.record.path)
    recorded = False
    try:
        launches.note_terminal(conn, task.id, result.session)
        recorded = True
        session = record_session(conn, task.id, ctx.profile, ctx.agent, result.session, conversation_id)
    except Exception as exc:
        raise _roll_back_terminal(conn, adapter, task, result.session, exc, recorded=recorded) from exc
    notice = " ".join((*notices, result.notice or "")).strip() or None
    return OpenResult(
        get_task(conn, task.id), session, invocation.prompt, result.prompt_submitted, notice, worktree=tree.record
    )


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
    adapter can identify it, and no session record uses it; otherwise keep it and say where it is. The worktree is
    never removed: the retry reuses it. `recorded` says whether `launches` holds the terminal for the retry."""
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
            f"{head} The terminal session it created ({where}) was closed; the worktree is kept. "
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
        working_directory(conn, task)  # a missing worktree is refused before the user is asked to confirm
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
    return _resume(conn, task, session, ctx, adapter, notice_head)


def _resume(
    conn: sqlite3.Connection,
    task: Task,
    session: SessionRecord,
    ctx: _Context,
    adapter: TerminalAdapter,
    notice_head: str,
) -> OpenResult:
    """Resume the session's conversation in a new terminal session, recorded on the same session."""
    agent_adapter = agent_adapter_for(ctx.instance.adapter)
    directory = working_directory(conn, task)  # raises worktree_missing before anything is started
    conversation_id = session.agent_conversation_id
    resume = None
    if ctx.instance.resume_args and not any("{id}" in a for a in ctx.instance.resume_args):
        raise LauncherError(
            "resume_args_invalid",
            f"{ctx.profile}/{ctx.agent}: resume_args must contain {{id}}, where the conversation ID goes; "
            "without it the conversation would not be resumed.",
            profile=ctx.profile,
            agent=ctx.agent,
        )
    if conversation_id:
        resume = tuple(a.replace("{id}", conversation_id) for a in ctx.instance.resume_args) or agent_adapter.resume_args(
            conversation_id
        )
    if resume is None:
        raise LauncherError(
            "resume_unsupported",
            f"{notice_head} {ctx.agent!r} has no supported way to resume this task's conversation "
            f"({'its ID was never recorded' if not conversation_id else 'no resume mechanism'}). "
            f"`commodore restart {task.id}` starts a fresh conversation in a new terminal session.",
            task=task.id,
        )
    adapter.require(CREATE_SESSION)
    result = _start(ctx, adapter, command=ctx.instance.argv + resume, invocation=None, resume=True, directory=directory)
    replace_terminal(conn, session.id, result.session)
    state = result.resume_state or "unconfirmed"
    verdict = {
        "confirmed": "The conversation was restored: the harness showed it.",
        "failed": (
            "The harness could not resume the conversation (it has no saved conversation, usually because "
            f"nothing was submitted before it exited). Run `commodore restart {task.id}` to start afresh."
        ),
        "unconfirmed": "Resume attempted, not confirmed: check the terminal to see the conversation.",
    }[state]
    notice = f"{notice_head} {verdict}"
    return OpenResult(
        task, replace(session, terminal=result.session), None, False, notice, "resumed", state
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
    if primary_session(conn, task.id) is None:
        raise LauncherError(
            "no_session", f"Task {task.id} has not been opened, so there is nothing to resume. Run `commodore open {task.id}`."
        )
    return open_task(conn, task.id, force_resume=force, confirmed=confirmed, **kwargs)


def restart_task(
    conn: sqlite3.Connection,
    task_ref: str,
    *,
    config: Config,
    adapter: TerminalAdapter,
    prompter: Prompter | None,
    confirmed: bool = False,
    offline: bool = False,
    base_env: Mapping[str, str] | None = None,
) -> OpenResult:
    """Start the task's harness afresh (a new conversation, the prompt submitted at launch) in a new terminal session.

    Needs confirmation. The old terminal session is closed first; the adapter
    force-closes it only if it is recorded as launcher-created and still identifiable (see `close_session`), and
    otherwise the terminal may refuse, which stops the restart before anything starts. The task, its session
    and its worktree are kept; nothing under the repository is touched.
    """
    task = get_task(conn, task_ref)
    require_not_archived(task)
    with task_lock(task.id):
        return _restart_locked(
            conn, task, config=config, adapter=adapter, prompter=prompter, confirmed=confirmed, offline=offline,
            base_env=base_env,
        )


def _restart_locked(
    conn: sqlite3.Connection,
    task: Task,
    *,
    config: Config,
    adapter: TerminalAdapter,
    prompter: Prompter | None,
    confirmed: bool,
    offline: bool,
    base_env: Mapping[str, str] | None,
) -> OpenResult:
    session = primary_session(conn, task.id)
    if session is None:
        raise LauncherError("no_session", f"Task {task.id} has not been opened. Run `commodore open {task.id}`.")
    adapter.require(CREATE_SESSION)
    _require_available(adapter)
    skill, argument = skill_and_argument(task)  # a task with no URL is refused before anything is asked or closed
    ctx = _context(
        conn, task, config=config, prompter=prompter, agent=None, fixed_agent=session.agent,
        offline=offline, base_env=base_env,
    )
    invocation = invocation_for(ctx, skill, argument)  # same: a harness with no launch form closes nothing
    directory = working_directory(conn, task)  # raises worktree_missing before anything is asked, closed or started
    _confirm(
        prompter, confirmed, task,
        f"Restarting task {task.id} closes its terminal session and starts a new conversation. Pass --yes to confirm.",
        f"Restart task {task.id}? Its terminal session is closed and a new conversation starts. "
        "The task and its worktree are kept.",
    )
    skipped: str | None = None
    old_state = _terminal_state(adapter, ctx, session)
    if old_state != "gone":
        assert session.terminal is not None
        ref = session.terminal
        if not (ref.created_by_launcher and ref.surface_id):
            # Not recorded as ours, or nothing to tell it apart from a reused ID: it is left open, not guessed at.
            skipped = (
                f"The old terminal session ({ref.adapter} workspace {ref.workspace_id}) was left open: it is not "
                "recorded as created by the launcher with an identifiable surface, so the harness there may still be running. "
                "Close it yourself if you want to."
            )
    if old_state != "gone" and skipped is None:
        assert session.terminal is not None
        adapter.require(CLOSE_SESSION)
        try:
            # As recorded: the adapter force-closes only a workspace the launcher created and can still identify.
            adapter.close_session(session.terminal)
        except TerminalError as exc:
            raise LauncherError(
                "close_refused",
                f"The old terminal session was not closed ({exc.message}). Close it yourself, then run "
                f"`commodore restart {task.id}` again. Nothing was started.",
                task=task.id,
            ) from exc
    agent_adapter = agent_adapter_for(ctx.instance.adapter)
    conversation_id = agent_adapter.new_conversation_id()
    command = ctx.instance.argv + (agent_adapter.conversation_args(conversation_id) if conversation_id else ())
    result = _start(ctx, adapter, command=command, invocation=invocation, directory=directory)
    replace_terminal(conn, session.id, result.session, conversation_id=conversation_id)
    return OpenResult(
        task, replace(session, terminal=result.session, agent_conversation_id=conversation_id), invocation.prompt,
        result.prompt_submitted, " ".join(n for n in (skipped, result.notice) if n) or None, "restarted",
    )
