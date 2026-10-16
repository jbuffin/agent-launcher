"""`open` and `resume`: from a task to its one recorded session.

`open` holds the task's launch lock (`locks`) from resolving the task until the session is recorded, so two
simultaneous opens converge on one session (ADR 0007). It then returns the harness command and the task's session
lock, and the caller runs the harness on the session host (`session_host`, ADR 0022) once the database is closed. The
task is `active` only once its harness session is recorded.

Stages, each a small unit tested on its own: task registry (`tasks`), repository plus
profile (`repositories`, `associations.ensure_profile`), harness picker (`picker`) and session record (`sessions`).
The harness starts in the task's repository checkout (`task.repo_path`), whatever branch it is on. The launcher creates
no worktrees and does not check or change the checkout (ADR 0021).

The prompt of a new session is always the harness's invocation of a skill on an argument (`skill_and_argument`,
`Invocation`), given on the harness's command line and submitted at launch (ADR 0020). A GitHub task's is `commodore`
on its URL; the maintenance task `configure` starts is `commodore-launcher` on its request text.
Titles and bodies are never part of it. Reopening builds no prompt.

A task has one primary session. Opening a task whose harness is running (its session lock is held) is refused with
`harness_running`: the launcher cannot reach another terminal. Otherwise the harness adapter's resume mechanism starts
the conversation again, here, on the same session. Where a harness cannot resume (Codex, a conversation with no
recorded ID, or one the harness never saved), a fresh conversation starts on that same session, with the Commodore
invocation submitted at launch. Nothing here ever creates a second session.

With `dry_run`, every check runs and the command is built, but nothing is written and no lock is taken.

The first-run copy archived every copied task. `open` of one of those unarchives it and starts fresh the same way: its
old conversation was started in a worktree Commodore does not use (`completion.unarchive_locked`).
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from commodore_launcher.agent_adapters import PROMPT_MARKER, agent_adapter_for
from commodore_launcher.agents import AgentResolutionError, ResolvedAgent, resolve_agent
from commodore_launcher.associations import ensure_profile, find_repository
from commodore_launcher.completion import is_copy_archived, unarchive_locked
from commodore_launcher.config import Config
from commodore_launcher.github import GitHub
from commodore_launcher.skill_check import (
    COMMODORE_SKILLS, COMMODORE_SKILLS_SOURCE, MANAGEMENT_SKILLS, MANAGEMENT_SKILLS_SOURCE, check_skills, problem,
)
from commodore_launcher.errors import LauncherError
from commodore_launcher import launches
from commodore_launcher.interaction import Prompter
from commodore_launcher.locks import SessionClaim, SessionHolder, task_lock
from commodore_launcher.logs import trace
from commodore_launcher.picker import pick_agent
from commodore_launcher.repositories import identify_reference
from commodore_launcher.session_host import HarnessCommand, SessionHost
from commodore_launcher.sessions import (
    SessionRecord,
    last_used_agent,
    primary_session,
    record_session,
    replace_conversation,
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


@dataclass(frozen=True)
class OpenResult:
    task: Task
    session: SessionRecord | None
    """None only for a dry run of a task's first open: no session is recorded."""
    command: HarnessCommand
    """The harness to run, with the prompt (if any) on its command line."""
    prompt: str | None
    notice: str | None = None
    action: str = "created"
    """`created` (a first launch, or a fresh conversation on an existing session) or `resumed`."""
    claim: SessionClaim | None = field(default=None, compare=False)
    """The task's session lock, held for the harness. None on a dry run, and for `resume --force` over a held lock."""


COMMODORE_SKILL = "commodore"
MANAGEMENT_SKILL = "commodore-launcher"


@dataclass(frozen=True)
class Invocation:
    """A harness started on a skill with an argument: the prompt and the launch arguments that carry it."""

    prompt: str
    args: tuple[str, ...]


def skill_and_argument(task: Task) -> tuple[str, str]:
    """What a task starts: `commodore` on a GitHub issue or PR URL, or `commodore-launcher` on the request text of the
    maintenance task (`configure`). Any other task has no URL to invoke Commodore on."""
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
    """The one internal path for a harness started on a skill with an argument: `open` and `configure` both come
    here."""
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
    skill_notice: str | None = None
    """Set by `require_skills` when the skills could not be verified: the launch goes ahead and says so."""


def require_skills(
    instance: ResolvedAgent, task: Task | None, *, repo_path: str | None = None
) -> str | None:
    """Refuse a launch of a harness that lacks the skills the task starts (ADR 0021): Commodore's four for a task on a
    GitHub URL, `commodore-launcher` for the maintenance task. Returns a notice when plugins could not be read, so
    that not found is not known. `task` is None for a URL with no task yet, which starts Commodore."""
    maintenance = task is not None and task.is_maintenance
    skills = MANAGEMENT_SKILLS if maintenance else COMMODORE_SKILLS
    source = MANAGEMENT_SKILLS_SOURCE if maintenance else COMMODORE_SKILLS_SOURCE
    directory = repo_path or (task.repo_path if task is not None else None)
    report = check_skills(instance.adapter, instance.env, skills, [Path(directory)] if directory else [])
    message = problem(report, instance.adapter, instance.env, source)
    if message is not None:
        extra = " Run `commodore skill path` for the management skill's directory." if maintenance else ""
        raise LauncherError(
            "skills_missing", f"{instance.profile}/{instance.agent}: {message}{extra} No harness was started.",
            profile=instance.profile, harness=instance.agent, missing=list(report.missing),
        )
    if report.not_verified:
        return (
            f"Could not verify the skills {', '.join(report.not_verified)} for {instance.agent}: "
            f"{'; '.join(report.reasons)}, so they may be installed."
        )
    return None


def _noted(result: OpenResult, ctx: _Context) -> OpenResult:
    if not ctx.skill_notice:
        return result
    return replace(result, notice=" ".join(n for n in (ctx.skill_notice, result.notice) if n))


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


def _command(ctx: _Context, args: tuple[str, ...], invocation: Invocation | None = None) -> HarnessCommand:
    """The profile's harness with `args`, then the invocation's launch arguments with the prompt in place."""
    prompt_args = tuple(a.replace(PROMPT_MARKER, invocation.prompt) for a in invocation.args) if invocation else ()
    return HarnessCommand(
        ctx.instance.argv + args + prompt_args, ctx.task.repo_path, ctx.instance.env, ctx.instance.pinned_env,
        invocation.prompt if invocation else None,
    )


def _fresh_command(ctx: _Context, invocation: Invocation) -> tuple[HarnessCommand, str | None]:
    """A new conversation: its ID, fixed at launch where the harness allows it (far more reliable than finding it
    later), and the command that starts it on the invocation."""
    agent_adapter = agent_adapter_for(ctx.instance.adapter)
    conversation_id = agent_adapter.new_conversation_id()
    args = agent_adapter.conversation_args(conversation_id) if conversation_id else ()
    return _command(ctx, args, invocation), conversation_id


def _claim(host: SessionHost, task: Task) -> SessionClaim:
    claim = host.claim(task.id)
    if claim is None:
        raise _harness_running(task, host.running(task.id))
    return claim


def open_task(
    conn: sqlite3.Connection,
    task_ref: str,
    *,
    config: Config,
    host: SessionHost,
    prompter: Prompter | None,
    agent: str | None = None,
    offline: bool = False,
    base_env: Mapping[str, str] | None = None,
    force_resume: bool = False,
    confirmed: bool = False,
    github: GitHub | None = None,
    refresh: bool = True,
    dry_run: bool = False,
) -> OpenResult:
    task = get_task(conn, task_ref)
    require_openable_kind(task)  # a copied local task is never opened, whatever its state
    copied = is_copy_archived(conn, task)
    if not copied:
        require_not_archived(task)
    if refresh and not offline and task.source == "github" and not dry_run:
        # Best effort: the stored title, state and head SHA follow GitHub. It never stops an open.
        # Imported here: github_tasks imports this module, so a top-level import would be circular.
        from commodore_launcher.github_tasks import refresh_task

        refresh_task(conn, task, github, archived_ok=copied)
    # Held from here to "ready". A second open of this task waits, then finds the session and its harness running;
    # other tasks use other locks.
    with task_lock(task.id):
        task = get_task(conn, task.id)
        copied = is_copy_archived(conn, task)
        if not copied:
            require_not_archived(task)
        trace("open task", task=task.id)
        common = dict(config=config, host=host, prompter=prompter, offline=offline, base_env=base_env, dry_run=dry_run)
        if copied:
            return _open_copy_archived(conn, task, agent=agent, **common)
        existing = primary_session(conn, task.id)
        if existing is not None:
            return _reopen(conn, task, existing, agent=agent, force_resume=force_resume, confirmed=confirmed, **common)
        return _first_open(conn, task, agent=agent, **common)


def _open_copy_archived(
    conn: sqlite3.Connection,
    task: Task,
    *,
    config: Config,
    host: SessionHost,
    prompter: Prompter | None,
    agent: str | None,
    offline: bool,
    base_env: Mapping[str, str] | None,
    dry_run: bool,
) -> OpenResult:
    """A task the first-run copy archived: its conversation started in a worktree Commodore does not use, so it is
    unarchived and launched fresh. Every check that can refuse runs first and yields the context and invocation the
    launch then uses, so a refusal leaves the task archived with its rows intact."""
    session = primary_session(conn, task.id)
    ctx, invocation = _check_launch(
        conn, task, config=config, prompter=prompter, agent=agent,
        fixed_agent=session.agent if session else None, offline=offline, base_env=base_env,
    )
    common = dict(config=config, host=host, prompter=prompter, offline=offline, base_env=base_env)
    if dry_run:
        if session is None:
            return _first_open(conn, task, agent=ctx.agent, plan=(ctx, invocation), dry_run=True, **common)
        return _noted(_start_fresh(conn, task, session, ctx, invocation, host, "", dry_run=True), ctx)
    claim = _claim(host, task)
    try:
        unarchive_locked(conn, task)
    except BaseException:
        claim.release()
        raise
    task = get_task(conn, task.id)
    ctx = replace(ctx, task=task)  # the title as the refresh just stored it
    if session is None:
        return _first_open(conn, task, agent=ctx.agent, plan=(ctx, invocation), claim=claim, dry_run=False, **common)
    return _noted(_start_fresh(
        conn, task, session, ctx, invocation, host,
        "This task was copied from the old launcher: its conversation was started in a worktree Commodore does not "
        "use and was forgotten, so a fresh one was started.",
        claim=claim,
    ), ctx)


def _check_launch(
    conn: sqlite3.Connection,
    task: Task,
    *,
    config: Config,
    prompter: Prompter | None,
    agent: str | None,
    fixed_agent: str | None,
    offline: bool,
    base_env: Mapping[str, str] | None,
) -> tuple[_Context, Invocation]:
    """Everything that can refuse a launch, in one place, before anything is written. Returns what the launch uses."""
    skill, argument = skill_and_argument(task)  # a task with no URL is refused before the pickers
    ctx = _context(
        conn, task, config=config, prompter=prompter, agent=agent, fixed_agent=fixed_agent, offline=offline,
        base_env=base_env,
    )
    invocation = invocation_for(ctx, skill, argument)  # a harness with no launch form is refused here
    return replace(ctx, skill_notice=require_skills(ctx.instance, task)), invocation


def _first_open(
    conn: sqlite3.Connection,
    task: Task,
    *,
    config: Config,
    host: SessionHost,
    prompter: Prompter | None,
    agent: str | None,
    offline: bool,
    base_env: Mapping[str, str] | None,
    dry_run: bool,
    plan: tuple[_Context, Invocation] | None = None,
    claim: SessionClaim | None = None,
) -> OpenResult:
    """`plan` is the context and invocation a caller has already validated, and `claim` the session lock it already
    holds (the copy-archived launch)."""
    if plan is None:
        plan = _check_launch(
            conn, task, config=config, prompter=prompter, agent=agent, fixed_agent=None, offline=offline,
            base_env=base_env,
        )
    ctx, invocation = plan
    command, conversation_id = _fresh_command(ctx, invocation)
    if dry_run:
        return _noted(OpenResult(task, None, command, invocation.prompt), ctx)
    # Every check that can refuse the launch is behind us. Nothing outside the database exists until the caller
    # runs the harness, so a failure here leaves nothing to clean up.
    claim = claim or _claim(host, task)
    try:
        launches.begin(conn, task.id, ctx.agent, conversation_id)
        set_state(conn, task.id, TASK_LAUNCHING)
        session = record_session(conn, task.id, ctx.profile, ctx.agent, conversation_id)
    except BaseException:
        # Ctrl-C included. A SIGKILL cannot run this, so `launching` also means "interrupted"; `open` retries either.
        claim.release()
        try:
            set_state(conn, task.id, TASK_LAUNCH_FAILED)
        except sqlite3.Error:
            pass
        raise
    return _noted(OpenResult(get_task(conn, task.id), session, command, invocation.prompt, claim=claim), ctx)


def _reopen(
    conn: sqlite3.Connection,
    task: Task,
    session: SessionRecord,
    *,
    config: Config,
    host: SessionHost,
    prompter: Prompter | None,
    agent: str | None,
    offline: bool,
    base_env: Mapping[str, str] | None,
    force_resume: bool,
    confirmed: bool,
    dry_run: bool,
) -> OpenResult:
    """No picker, no new session. A running harness is refused (another terminal has it); otherwise the conversation
    is resumed here, or a fresh one started where it cannot be."""
    ctx = _context(
        conn, task, config=config, prompter=prompter, agent=agent, fixed_agent=session.agent,
        offline=offline, base_env=base_env,
    )
    running = host.running(task.id)
    if running is not None and not force_resume:
        raise _harness_running(task, running)
    ctx = replace(ctx, skill_notice=require_skills(ctx.instance, task))
    if running is not None:
        if _resume_command(ctx, session) is None:
            # Starting a fresh conversation would leave this harness running, untracked, in the same checkout.
            raise _harness_running(task, running, f"its conversation can't be resumed ({_cannot_resume(session)})")
        # Knowingly a second harness process on one conversation: the user must say so.
        _confirm(
            prompter, confirmed, task,
            "The session lock says the harness is still running. Resuming starts a second process on the same "
            "conversation. Pass --yes to confirm.",
            f"Task {task.id}'s harness looks like it is still running. Start a second process on its conversation anyway?",
        )
    return _noted(_resume(conn, task, session, ctx, host, running, dry_run), ctx)


def _harness_running(
    task: Task, holder: SessionHolder | None, why: str | None = None
) -> LauncherError:
    pid = f" (pid {holder.pid})" if holder is not None and holder.pid else ""
    because = f", and {why}," if why else ","
    return LauncherError(
        "harness_running",
        f"Task {task.id}'s harness is already running{pid} in another terminal{because} so nothing was started. Switch "
        f"to that terminal, or quit the harness there and run `commodore open {task.id}` again. If no harness is "
        f"running (a process it started may still hold the session lock), `commodore resume {task.id} --force` "
        "starts it anyway.",
        task=task.id,
        pid=holder.pid if holder is not None else None,
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
    host: SessionHost,
    running: SessionHolder | None,
    dry_run: bool,
) -> OpenResult:
    """Resume the session's conversation here. Where that is impossible, or the harness never saved the conversation,
    a fresh one starts on the same session."""
    resume = _resume_command(ctx, session)
    skill, argument = skill_and_argument(task)
    invocation = invocation_for(ctx, skill, argument)  # refused here, before anything starts
    if resume is None:
        return _start_fresh(
            conn, task, session, ctx, invocation, host,
            f"{ctx.agent!r} cannot resume this conversation ({_cannot_resume(session)}), so a fresh one was started.",
            dry_run=dry_run,
        )
    assert session.agent_conversation_id
    saved = agent_adapter_for(ctx.instance.adapter).has_saved_conversation(session.agent_conversation_id, ctx.instance.env)
    if saved is False:
        if running is not None:
            raise _harness_running(task, running, "it has no saved conversation to resume")
        return _start_fresh(
            conn, task, session, ctx, invocation, host,
            "The harness has no saved conversation for this session (usually because nothing was submitted before it "
            "exited), so a fresh one was started.",
            dry_run=dry_run,
        )
    command = _command(ctx, resume)
    if dry_run:
        return OpenResult(task, session, command, None, action="resumed")
    notice = None
    if running is not None:
        notice = "Resumed as asked although the session lock is held: this process is not tracked by it."
    claim = None if running is not None else _claim(host, task)
    return OpenResult(task, session, command, None, notice, "resumed", claim)


def _start_fresh(
    conn: sqlite3.Connection,
    task: Task,
    session: SessionRecord,
    ctx: _Context,
    invocation: Invocation,
    host: SessionHost,
    notice: str,
    *,
    dry_run: bool = False,
    claim: SessionClaim | None = None,
) -> OpenResult:
    """Resume is impossible, so start a new conversation on the same session record (ADR 0005): a new conversation ID
    where the harness allows one, and the invocation submitted at launch."""
    command, conversation_id = _fresh_command(ctx, invocation)
    if dry_run:
        return OpenResult(task, session, command, invocation.prompt, notice or None)
    claim = claim or _claim(host, task)
    try:
        replace_conversation(conn, session.id, conversation_id)
    except BaseException:
        claim.release()
        raise
    return OpenResult(
        task, replace(session, agent_conversation_id=conversation_id), command, invocation.prompt, notice or None,
        "created", claim,
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
    With `force`, resume even though the session lock says the harness is running."""
    task = get_task(conn, task_ref)
    require_openable_kind(task)
    if primary_session(conn, task.id) is None:
        raise LauncherError(
            "no_session", f"Task {task.id} has not been opened, so there is nothing to resume. Run `commodore open {task.id}`."
        )
    return open_task(conn, task.id, force_resume=force, confirmed=confirmed, **kwargs)
