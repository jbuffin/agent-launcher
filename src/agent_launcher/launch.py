"""`open`, `resume`, `prompt` and `restart`: from a task to its one recorded session.

A launch is a recoverable transaction (ADR 0007). `open` holds the task's lock (`locks`) from resolving the task
until the session is recorded, and writes each stage and resource to `launches` as it goes, so two simultaneous
opens converge on one session and a retry after a failure or crash reuses what exists. The task is `active`
only once its agent session is recorded.

Stages, each a small unit tested on its own: task registry (`tasks`), repository plus
profile (`repositories`, `associations.ensure_profile`), agent picker (`picker`), prompt
builder (`prompt`), terminal adapter (`terminals`) and session record (`sessions`).
The agent runs in the task's own worktree (`worktrees`): created from the base branch on first open, or one the
user adopted. A task opened before worktrees existed keeps running in its repository directory.

The workflow (`routing`) is chosen once, at first open, and stored on the task: it highlights an agent in the picker and
shapes the prompt (`prompt`). Reopening never routes again, because reopening does not regenerate the prompt.

A task has one primary session. Opening a task that has one focuses it; if its agent has exited or its
terminal identifiers are stale, the agent adapter's resume mechanism starts the conversation again in a
new terminal session recorded on the same session. Nothing here ever creates a second session.
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from agent_launcher.agent_adapters import agent_adapter_for
from agent_launcher.agents import AgentResolutionError, ResolvedAgent, resolve_agent
from agent_launcher.associations import ensure_profile, find_repository
from agent_launcher.config import Config
from agent_launcher.github import GitHub
from agent_launcher.errors import LauncherError
from agent_launcher import launches
from agent_launcher.interaction import Prompter
from agent_launcher.locks import task_lock
from agent_launcher.logs import trace
from agent_launcher.picker import pick_agent
from agent_launcher.prompt import build_prompt, template_variables
from agent_launcher.routing import facts_from_task, preferred_agent, resolve_fallback, route, select_workflow
from agent_launcher.repositories import identify_reference
from agent_launcher.sessions import (
    SessionRecord,
    last_used_agent,
    primary_session,
    record_session,
    replace_terminal,
    terminal_in_use,
)
from agent_launcher.tasks import (
    TASK_LAUNCH_FAILED,
    TASK_LAUNCHING,
    Task,
    get_task,
    require_not_archived,
    set_state,
    set_workflow,
)
from agent_launcher.workflows import Workflow, find_workflow, load_workflows
from agent_launcher.worktrees import WorktreeRecord, ensure_worktree, working_directory
from agent_launcher.terminals import (
    CLOSE_SESSION,
    CREATE_SESSION,
    FOCUS_SESSION,
    PREPARE_PROMPT,
    SUBMIT_PROMPT,
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
    prompt_prepared: bool
    prompt_submitted: bool
    notice: str | None = None
    action: str = "created"
    """`created`, `focused`, `resumed`, `restarted` or `prompted`."""
    resume_state: str | None = None
    """After a resume: `confirmed` (the agent showed the resumed conversation), `failed`, or
    `unconfirmed` (resume attempted, not confirmed)."""
    worktree: WorktreeRecord | None = None
    workflow: str | None = None
    """The workflow the task was routed to (first open), or the one it has."""


PROMPT_RECOVERY = (
    "When the agent is waiting at its input box (for example after you accept Claude Code's folder-trust dialog, "
    "which every new worktree triggers), run `agent-launcher prompt {task}` to enter the prompt."
)


@dataclass(frozen=True)
class _Context:
    task: Task
    profile: str
    agent: str
    instance: ResolvedAgent
    workflow: Workflow | None = None
    workflow_note: str | None = None
    """Why the workflow's preferred agent was not used, if it was not."""


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
    workflow: Workflow | None = None,
) -> _Context:
    """The task's repository, profile and agent, checked as `open` always has. `fixed_agent` is the agent of
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
    note: str | None = None
    if fixed_agent is not None:
        if agent is not None and agent != fixed_agent:
            raise LauncherError(
                "agent_mismatch",
                f"Task {task.id} already has a session with {fixed_agent!r}; it cannot be reopened with {agent!r}.",
                session_agent=fixed_agent,
            )
        chosen = fixed_agent
    else:
        # The workflow's preference only highlights, and only an agent this repository's profile already has.
        preferred, note = preferred_agent(workflow, list(profile.agents)) if workflow else (None, None)
        if note:
            trace("workflow preference ignored", workflow=workflow.id if workflow else None)
        chosen = pick_agent(
            resolved.profile,
            list(profile.agents),
            mode=config.agent_selection,
            default=profile.default_agent,
            last_used=last_used_agent(conn, resolved.profile),
            prompter=prompter,
            requested=agent,
            preferred=preferred,
        ).agent
    try:
        instance = resolve_agent(resolved.profile, chosen, config=config, base_env=base_env)
    except AgentResolutionError as exc:
        raise LauncherError("agent_unresolved", str(exc), profile=resolved.profile, agent=chosen) from exc
    return _Context(task, resolved.profile, chosen, instance, workflow, note)


def _start(
    ctx: _Context, adapter: TerminalAdapter, config: Config, *, command: tuple[str, ...], prompt: str | None,
    directory: str, resume: bool = False, execution: str | None = None,
) -> CreateSessionResult:
    agent_adapter = agent_adapter_for(ctx.instance.adapter)
    submit = execution_mode(config, ctx.workflow, execution) == "execute" and not resume and prompt is not None
    # An agent that submits a prompt given on its command line gets it there: no screen check, no paste.
    at_launch = agent_adapter.launch_prompt_args() if submit else None
    return adapter.create_session(
        CreateSessionRequest(
            title=f"{Path(ctx.task.repo_path).name} — {ctx.task.title}",
            working_directory=directory,
            command=command,
            env=ctx.instance.env,
            pinned_env=ctx.instance.pinned_env,
            prompt=prompt,
            submit_prompt=submit,
            prompt_args=at_launch or (),
            prompt_input=agent_adapter.prompt_input(),
            resume_check=agent_adapter.resume_check() if resume else None,
        )
    )


def _with_recovery_hint(task: Task, result: CreateSessionResult) -> str | None:
    if result.notice is None or result.prompt_prepared:
        return result.notice
    return f"{result.notice} {PROMPT_RECOVERY.format(task=task.id)}"


def _stored_workflow(task: Task) -> Workflow | None:
    """The workflow chosen at first open, looked up by id. A task opened before workflows existed has none. A
    workflow that is no longer defined is an error: the prompt is not rebuilt with another one."""
    if task.workflow is None:
        return None
    found = find_workflow(load_workflows(), task.workflow)
    if found is None:
        raise LauncherError(
            "workflow_missing",
            f"Task {task.id} was routed to the workflow {task.workflow!r}, which workflows.json no longer defines. "
            "Nothing was substituted; add it back to workflows.json.",
            task=task.id, workflow=task.workflow,
        )
    return found


def _choose_workflow(
    conn: sqlite3.Connection,
    task: Task,
    *,
    config: Config,
    prompter: Prompter | None,
    requested: str | None,
    ask: bool,
    offline: bool,
    github: GitHub | None,
) -> Workflow:
    """Route the task (or take the workflow it already has). Network only for the `gh` user and CI status, and only
    when a rule asks. A first open that stopped part-way keeps its workflow on retry unless one is asked for."""
    if task.workflow is not None and requested is None and not ask:
        trace("workflow kept", task=task.id, workflow=task.workflow)
        stored = _stored_workflow(task)
        assert stored is not None
        return stored
    gh = None if offline else (github or GitHub())
    facts = facts_from_task(
        conn, task, viewer=(gh.viewer if gh else (lambda: None)),
        ci=(gh.check_status if gh else (lambda owner, name, sha: "unknown")),
    )
    routing = route(load_workflows(), config, facts)
    selection = select_workflow(
        routing, mode=config.workflow_routing.selection_mode, prompter=prompter, requested=requested, force_ask=ask,
        config=config,
    )
    return selection.workflow


def _skill_problem(
    ctx: _Context, directory: str | Path, config: Config
) -> tuple[str | None, LauncherError | None]:
    """`(notice, refusal)` for the workflow's skill. A lookup by name proves a skill is there, never that it is not:
    agents also provide bundled, plugin and managed skills. So a skill that is not found is launched with a notice that
    says where it was looked for (and the agent itself will say if it does not know it). Only a skill that is
    certainly missing is a refusal, and so is any unverified one when `require_verified_skills` is on. No other
    skill is ever used instead. `directory` is where the agent will run (its worktree)."""
    workflow = ctx.workflow
    if workflow is None or workflow.skill is None:
        return None, None
    check = agent_adapter_for(ctx.instance.adapter).check_skill(workflow.skill, ctx.instance.env, [Path(directory)])
    if check.status == "available":
        return None, None
    where = f"looked in: {', '.join(check.checked)}" if check.checked else "this launcher cannot look it up by name"
    if check.status == "missing" or config.workflow_routing.require_verified_skills:
        why = "does not have" if check.status == "missing" else "could not be verified to have"
        return None, LauncherError(
            "skill_missing",
            f"Workflow {workflow.id!r} needs the skill {workflow.skill!r}, which {ctx.agent} {why} ({where}). No other "
            "skill was used and nothing was started; install it, or open with another workflow (`--workflow <id>`).",
            workflow=workflow.id, skill=workflow.skill, agent=ctx.agent, checked=list(check.checked),
        )
    return (
        f"Skill {workflow.skill!r} (workflow {workflow.id!r}) was not found by name ({where}); {ctx.agent} may still "
        "provide it (bundled or from a plugin). If it does not know it, the prompt will say so.",
        None,
    )


def _verify_skill(ctx: _Context, directory: str | Path, config: Config) -> str | None:
    """`_skill_problem` for `prompt` and `restart`, which keep the stored workflow: a refusal is raised."""
    notice, refusal = _skill_problem(ctx, directory, config)
    if refusal:
        raise refusal
    return notice


def execution_mode(config: Config, workflow: Workflow | None, override: str | None = None) -> str:
    """`prepare` or `execute`: the command line's choice, else the workflow's, else the global setting."""
    return override or (workflow.prompt_execution if workflow else None) or config.prompt_execution


def _local_repository_name(conn: sqlite3.Connection, task: Task) -> str | None:
    row = conn.execute("SELECT full_name FROM repositories WHERE id = ?", (task.repository_id,)).fetchone()
    return row[0] if row and row[0] else None


def _prompt(conn: sqlite3.Connection, task: Task, ctx: _Context, config: Config, worktree_path: str | None) -> str:
    """The prompt (SPEC §17). A template that cannot be rendered raises; no other prompt replaces it."""
    from agent_launcher.github_tasks import github_details  # imported late: github_tasks imports launch

    variables = None
    agent_adapter = agent_adapter_for(ctx.instance.adapter)
    if ctx.workflow is not None:
        details = github_details(conn, task.id)
        kind = None if details is None else "pr" if details.get("pull") else "issue"  # the stored kind
        variables = template_variables(
            task, ctx.workflow, agent=ctx.agent, worktree_path=worktree_path,
            repository=_local_repository_name(conn, task), kind=kind, adapter=agent_adapter,
            style=ctx.instance.skill_invocation,
        )
    return build_prompt(
        task, ctx.workflow, adapter=agent_adapter, style=ctx.instance.skill_invocation,
        default_template=config.prompt_template, variables=variables,
    )


def _check_prompt(conn: sqlite3.Connection, task: Task, ctx: _Context, config: Config, worktree_path: str) -> None:
    """Before anything is created, closed or asked: render the prompt for real. Everything a template can need is
    known by now (the worktree path as `worktree_path`, which only has to exist), so a variable the task lacks, a bad
    template or an unusable skill stops the command here and changes nothing."""
    _prompt(conn, task, ctx, config, worktree_path)


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
    workflow: str | None = None,
    ask_workflow: bool = False,
    execution: str | None = None,
    refresh_completion: bool = True,
) -> OpenResult:
    task = get_task(conn, task_ref)
    require_not_archived(task)
    if refresh_completion and not offline and task.source == "github":
        # Opportunistic (#22): note a closed or merged item. Best effort; it never stops an open or changes anything
        # but the task's cleanup flag.
        # Imported here: github_tasks imports this module, so a top-level import would be circular.
        from agent_launcher.github_tasks import refresh_completion as refresh_github_completion

        refresh_github_completion(conn, [task], github)
    # Held from here to "ready". A second open of this task waits, then finds the session and focuses it; other
    # tasks use other locks.
    with task_lock(task.id):
        task = get_task(conn, task.id)
        require_not_archived(task)
        trace("open task", task=task.id)
        existing = primary_session(conn, task.id)
        if existing is not None:
            if workflow is not None or ask_workflow:
                raise LauncherError(
                    "workflow_fixed",
                    f"Task {task.id} was already opened (workflow {task.workflow or 'none'}); reopening does not "
                    "choose a workflow or rebuild the prompt. Nothing was changed. `agent-launcher restart` starts "
                    "afresh with the task's workflow.",
                    task=task.id, workflow=task.workflow,
                )
            return _reopen(
                conn, task, existing, config=config, adapter=adapter, prompter=prompter, agent=agent,
                offline=offline, base_env=base_env, force_resume=force_resume, confirmed=confirmed,
            )
        return _first_open(
            conn, task, config=config, adapter=adapter, prompter=prompter, agent=agent, offline=offline,
            base_env=base_env, github=github, workflow=workflow, ask_workflow=ask_workflow, execution=execution,
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
    github: GitHub | None = None,
    workflow: str | None = None,
    ask_workflow: bool = False,
    execution: str | None = None,
) -> OpenResult:
    adapter.require(CREATE_SESSION)
    if execution == "execute":
        adapter.require(SUBMIT_PROMPT)  # asked for outright: refuse before the pickers
    _require_available(adapter)
    prior = launches.get_launch(conn, task.id)
    if prior is not None and prior.terminal is not None and prior.agent:
        agent = prior.agent  # a terminal session already runs this agent: a retry does not change it
    if prior is not None and prior.terminal is not None and (workflow is not None or ask_workflow):
        raise LauncherError(
            "workflow_fixed",
            f"An earlier launch of task {task.id} already started its agent with workflow {task.workflow or 'none'}, "
            "so the workflow cannot be changed now. Nothing was changed.",
            task=task.id, workflow=task.workflow,
        )
    chosen = _choose_workflow(
        conn, task, config=config, prompter=prompter, requested=workflow, ask=ask_workflow, offline=offline,
        github=github,
    )
    ctx = _context(
        conn, task, config=config, prompter=prompter, agent=agent, fixed_agent=None, offline=offline, base_env=base_env,
        workflow=chosen,
    )
    # The prompt is always passed, so the adapter must be able to handle it as asked (SPEC §18).
    adapter.require(SUBMIT_PROMPT if execution_mode(config, chosen, execution) == "execute" else PREPARE_PROMPT)
    _check_prompt(conn, task, ctx, config, "<worktree_path>")  # the worktree is not made yet
    # Every check that can refuse the launch is behind us; from here each stage is written down.
    if task.workflow != chosen.id:
        set_workflow(conn, task.id, chosen.id)
    launches.begin(conn, task.id)
    set_state(conn, task.id, TASK_LAUNCHING)
    try:
        result = _run_stages(
            conn, task, ctx, config=config, adapter=adapter, prompter=prompter, offline=offline, execution=execution
        )
        notice = " ".join(n for n in (ctx.workflow_note, result.notice) if n) or None
        return replace(result, notice=notice)
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
    *,
    config: Config,
    adapter: TerminalAdapter,
    prompter: Prompter | None,
    offline: bool,
    execution: str | None = None,
) -> OpenResult:
    # The worktree stays recorded if a later stage fails, and the next `open` reuses it.
    tree = ensure_worktree(conn, task, config, prompter, offline=offline)
    launches.note_worktree_ready(conn, task.id)
    launch = launches.get_launch(conn, task.id)
    assert launch is not None
    agent_adapter = agent_adapter_for(ctx.instance.adapter)
    notices = list(tree.notices)
    # The skill is looked for where the agent runs: the worktree, which may hold skills its branch added.
    ctx, skill_notices = _settle_skill(conn, task, ctx, tree.record.path, config, prompter)
    notices.extend(skill_notices)
    prompt = _prompt(conn, task, ctx, config, tree.record.path)

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
            # Unsure is not gone: the earlier agent may still be running there. The record is kept.
            raise LauncherError(
                "terminal_check_failed",
                f"An earlier launch of task {task.id} created the terminal session {where}, and it could not be "
                f"checked ({exc}). Its agent may still be running, so no second one was started and the record "
                f"was kept. Check or close that terminal session yourself, then run `agent-launcher open {task.id}` "
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
        # Fix the agent's conversation ID now, where the agent allows it: far more reliable than finding it later.
        conversation_id = agent_adapter.new_conversation_id()
        command = ctx.instance.argv + (agent_adapter.conversation_args(conversation_id) if conversation_id else ())
        launches.note_agent(conn, task.id, ctx.agent, conversation_id)
        result = _start(
            ctx, adapter, config, command=command, prompt=prompt, directory=tree.record.path, execution=execution
        )
    recorded = False
    try:
        launches.note_terminal(conn, task.id, result.session)
        recorded = True
        session = record_session(conn, task.id, ctx.profile, ctx.agent, result.session, conversation_id)
    except Exception as exc:
        raise _roll_back_terminal(conn, adapter, task, result.session, exc, recorded=recorded) from exc
    notice = " ".join((*notices, _with_recovery_hint(task, result) or "")).strip() or None
    return OpenResult(
        get_task(conn, task.id), session, prompt, result.prompt_prepared, result.prompt_submitted, notice,
        worktree=tree.record, workflow=ctx.workflow.id if ctx.workflow else None,
    )


def _settle_skill(
    conn: sqlite3.Connection, task: Task, ctx: _Context, directory: str, config: Config, prompter: Prompter | None
) -> tuple[_Context, list[str]]:
    """Check the workflow's skill. If it is certainly missing (or unverified under `require_verified_skills`) and
    someone is at a terminal, offer, defaulting to No, to continue with the fallback workflow instead; otherwise
    stop. Without a terminal it stops (`skill_missing`)."""
    notice, refusal = _skill_problem(ctx, directory, config)
    if refusal is None:
        return ctx, [notice] if notice else []
    workflow = ctx.workflow
    assert workflow is not None
    fallback = resolve_fallback(load_workflows(), config)
    if prompter is None or fallback.id == workflow.id or not prompter.confirm(
        f"{refusal.message.split(' No other')[0]}. Continue with the fallback workflow {fallback.id!r} instead?",
        default=False,
    ):
        raise refusal
    switched = replace(ctx, workflow=fallback)
    again, refusal = _skill_problem(switched, directory, config)
    if refusal is not None:
        raise refusal
    set_workflow(conn, task.id, fallback.id)
    note = f"Skill {workflow.skill!r} was not available, so you chose the fallback workflow {fallback.id!r} instead."
    return switched, [note, *([again] if again else [])]


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
            f"Run `agent-launcher open {task.id}` again.",
            task=task.id,
        )
    reason = (
        "it could not be closed"
        if verified
        else "it could not be verified as created by the launcher, identifiable and unused"
    )
    after = (
        f"`agent-launcher open {task.id}` again reuses it if it still exists."
        if recorded
        else f"It is not recorded, so `agent-launcher open {task.id}` again creates another: close this one yourself first."
    )
    return LauncherError(
        "launch_incomplete",
        f"{head} The terminal session ({where}) was left open because {reason}. {after}",
        task=task.id,
        terminal=ref.to_dict(),
    )


def _terminal_state(adapter: TerminalAdapter, ctx: _Context, session: SessionRecord) -> str:
    """`gone` (no such terminal session), `exited` (positive sign the agent ended), `alive` (the agent is
    there, or we cannot tell: never resume over an agent that may be running)."""
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
            return OpenResult(task, session, None, False, False, action="focused")
        except StaleTerminalSession:
            state = "gone"  # it vanished between the check and the focus
    if state == "alive":
        working_directory(conn, task)  # a missing worktree is refused before the user is asked to confirm
        # Knowingly a second agent process on one conversation: the user must say so.
        _confirm(
            prompter, confirmed, task,
            "The agent may still be running in the old terminal session. Resuming starts a second process on the "
            "same conversation. Pass --yes to confirm.",
            f"Task {task.id}'s agent may still be running. Start a second process on its conversation anyway?",
        )
    notice_head = {
        "gone": "The recorded terminal session no longer exists.",
        "exited": "The agent has exited.",
        "alive": "Resuming as asked, in a new terminal session.",
    }[state]
    return _resume(conn, task, session, ctx, adapter, config, notice_head)


def _resume(
    conn: sqlite3.Connection,
    task: Task,
    session: SessionRecord,
    ctx: _Context,
    adapter: TerminalAdapter,
    config: Config,
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
            f"`agent-launcher restart {task.id}` starts a fresh conversation in a new terminal session.",
            task=task.id,
        )
    adapter.require(CREATE_SESSION)
    result = _start(
        ctx, adapter, config, command=ctx.instance.argv + resume, prompt=None, resume=True,
        directory=directory,
    )
    replace_terminal(conn, session.id, result.session)
    state = result.resume_state or "unconfirmed"
    verdict = {
        "confirmed": "The conversation was restored: the agent showed it.",
        "failed": (
            "The agent could not resume the conversation (it has no saved conversation, usually because "
            f"nothing was submitted before it exited). Run `agent-launcher restart {task.id}` to start afresh."
        ),
        "unconfirmed": "Resume attempted, not confirmed: check the terminal to see the conversation.",
    }[state]
    notice = f"{notice_head} {verdict}"
    return OpenResult(
        task, replace(session, terminal=result.session), None, False, False, notice, "resumed", state
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
            "no_session", f"Task {task.id} has not been opened, so there is nothing to resume. Run `agent-launcher open {task.id}`."
        )
    return open_task(conn, task.id, force_resume=force, confirmed=confirmed, **kwargs)


def prompt_task(
    conn: sqlite3.Connection,
    task_ref: str,
    *,
    config: Config,
    adapter: TerminalAdapter,
    offline: bool = False,
    base_env: Mapping[str, str] | None = None,
) -> OpenResult:
    """Prepare the task's prompt again, unsubmitted, in its existing session: the same path as `open`.
    This is the recovery after accepting Claude Code's folder-trust dialog."""
    task = get_task(conn, task_ref)
    require_not_archived(task)
    session = primary_session(conn, task.id)
    if session is None or session.terminal is None:
        raise LauncherError("no_session", f"Task {task.id} has not been opened. Run `agent-launcher open {task.id}`.")
    adapter.require(PREPARE_PROMPT)
    _require_available(adapter)
    ctx = _context(
        conn, task, config=config, prompter=None, agent=None, fixed_agent=session.agent, offline=offline,
        base_env=base_env, workflow=_stored_workflow(task),
    )
    _verify_skill(ctx, working_directory(conn, task), config)
    state = _terminal_state(adapter, ctx, session)
    if state != "alive":
        raise LauncherError(
            "no_agent_running",
            f"The agent for task {task.id} is not running in its terminal session. "
            f"Run `agent-launcher resume {task.id}` first.",
            task=task.id,
        )
    prompt = _prompt(conn, task, ctx, config, str(working_directory(conn, task)))
    result = adapter.prepare_prompt(session.terminal, prompt, agent_adapter_for(ctx.instance.adapter).prompt_input())
    return OpenResult(
        task, session, prompt, result.prompt_prepared, False, _with_recovery_hint(task, result), "prompted"
    )


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
    execution: str | None = None,
) -> OpenResult:
    """Start the task's agent afresh (a new conversation, the prompt prepared) in a new terminal session.

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
            base_env=base_env, execution=execution,
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
    execution: str | None = None,
) -> OpenResult:
    session = primary_session(conn, task.id)
    if session is None:
        raise LauncherError("no_session", f"Task {task.id} has not been opened. Run `agent-launcher open {task.id}`.")
    adapter.require(CREATE_SESSION)
    if execution == "execute":
        adapter.require(SUBMIT_PROMPT)
    _require_available(adapter)
    ctx = _context(
        conn, task, config=config, prompter=prompter, agent=None, fixed_agent=session.agent,
        offline=offline, base_env=base_env, workflow=_stored_workflow(task),
    )
    adapter.require(SUBMIT_PROMPT if execution_mode(config, ctx.workflow, execution) == "execute" else PREPARE_PROMPT)
    directory = working_directory(conn, task)  # raises worktree_missing before anything is asked, closed or started
    _verify_skill(ctx, directory, config)  # before anything is asked, closed or started
    _check_prompt(conn, task, ctx, config, str(directory))  # same: a prompt that cannot be built closes nothing
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
                "recorded as created by the launcher with an identifiable surface, so the agent there may still be running. "
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
                f"`agent-launcher restart {task.id}` again. Nothing was started.",
                task=task.id,
            ) from exc
    agent_adapter = agent_adapter_for(ctx.instance.adapter)
    conversation_id = agent_adapter.new_conversation_id()
    command = ctx.instance.argv + (agent_adapter.conversation_args(conversation_id) if conversation_id else ())
    prompt = _prompt(conn, task, ctx, config, str(directory))
    result = _start(ctx, adapter, config, command=command, prompt=prompt, directory=directory, execution=execution)
    replace_terminal(conn, session.id, result.session, conversation_id=conversation_id)
    return OpenResult(
        task, replace(session, terminal=result.session, agent_conversation_id=conversation_id), prompt,
        result.prompt_prepared, result.prompt_submitted,
        " ".join(n for n in (skipped, _with_recovery_hint(task, result)) if n) or None, "restarted",
    )
