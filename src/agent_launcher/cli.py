"""Command-line interface. Presentation only: logic lives in the other modules."""

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from agent_launcher import __version__, templates
from agent_launcher.agents import AgentResolutionError, resolve_agent
from agent_launcher.adoption import adopt_session, find_candidates
from agent_launcher.associations import AssociationError, ensure_profile, plan_reassignment, set_profile
from agent_launcher.configure import prepare_task
from agent_launcher.config import ConfigError, LogSettings, effective_config, load_config, read_raw, validate_config
from agent_launcher.errors import LauncherError
from agent_launcher.explain import explain
from agent_launcher.github import GitHubError
from agent_launcher.diagnostics import default_archive_name, export_diagnostics
from agent_launcher.doctor import run_doctor
from agent_launcher.github_tasks import github_details, is_issue_reference, link_task, open_github
from agent_launcher.interaction import Choice, QuestionaryPrompter, SetupCancelled
from agent_launcher.reassignment import Liveness
from agent_launcher.launch import OpenResult, open_task, prompt_task, restart_task, resume_task
from agent_launcher.logs import setup_logging, trace
from agent_launcher.paths import config_path
from agent_launcher.profiles import (
    PROMPT_MODES,
    SKILL_INVOCATIONS,
    InstanceEdit,
    add_profile,
    edit_profile,
    list_profiles,
)
from agent_launcher.repositories import RepositoryError, identify_reference
from agent_launcher.sessions import sessions_for_task
from agent_launcher.skill_bundle import skill_directory
from agent_launcher.state import StateError, open_state
from agent_launcher.tasks import create_task, get_task, list_tasks
from agent_launcher.terminal_select import select_adapter
from agent_launcher.worktrees import (
    associate,
    get_worktree,
    inspect_worktree,
    list_worktree_records,
)
from agent_launcher.wizard import SetupError, load_answers, run_setup
from agent_launcher.workflows import (
    BUILTIN_FALLBACK_ID,
    builtin_fallback,
    example_file,
    load_workflows,
    save_workflows,
    validate_workflows,
)

app = typer.Typer(help="Launch AI coding agents against issues, PRs and local tasks.")
config_app = typer.Typer(help="Inspect and validate configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")
profile_app = typer.Typer(help="Manage profiles and their agent instances.", no_args_is_help=True)
app.add_typer(profile_app, name="profile")

diagnostics_app = typer.Typer(help="Export sanitised diagnostics.", no_args_is_help=True)
app.add_typer(diagnostics_app, name="diagnostics")

tasks_app = typer.Typer(help="List and inspect tasks.", no_args_is_help=True)
app.add_typer(tasks_app, name="tasks")
worktrees_app = typer.Typer(help="List, inspect and adopt task worktrees.", no_args_is_help=True)
app.add_typer(worktrees_app, name="worktrees")

sessions_app = typer.Typer(help="Find and adopt terminal sessions the launcher did not create.", no_args_is_help=True)
app.add_typer(sessions_app, name="sessions")
workflows_app = typer.Typer(help="List workflow rules and explain which one a task matches.", no_args_is_help=True)
app.add_typer(workflows_app, name="workflows")

skill_app = typer.Typer(help="The bundled management skill.", no_args_is_help=True)
app.add_typer(skill_app, name="skill")

JsonOption = Annotated[bool, typer.Option("--json", help="Machine-readable JSON output.")]


def emit_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2))


@app.command()
def version(as_json: JsonOption = False) -> None:
    """Print the installed version."""
    if as_json:
        emit_json({"version": __version__})
    else:
        typer.echo(f"agent-launcher {__version__}")


@config_app.command("show")
def config_show(as_json: JsonOption = False) -> None:
    """Show the effective configuration (defaults plus config.json)."""
    path = config_path()
    try:
        config = effective_config(path)
    except ConfigError as exc:
        if as_json:
            emit_json({"path": str(path), "error": str(exc)})
        else:
            typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    if as_json:
        emit_json({"path": str(path), "exists": read_raw(path) is not None, "config": config})
    else:
        typer.echo(f"# {path}" + ("" if path.exists() else " (not created yet; showing defaults)"))
        emit_json(config)


@config_app.command("validate")
def config_validate(
    as_json: JsonOption = False,
    strict: Annotated[bool, typer.Option(help="Treat unknown fields as errors.")] = False,
) -> None:
    """Check config.json. Unknown fields are reported; --strict makes them fail."""
    report = validate_config()
    flows = validate_workflows()
    # The fallback must name a workflow that exists (the built-in `default` always does).
    fallback_error = None
    if report.valid and flows.valid:
        try:
            chosen = load_config().workflow_routing.fallback
            if chosen != BUILTIN_FALLBACK_ID and chosen not in [w.id for w in load_workflows().workflows]:
                fallback_error = f"workflow_routing.fallback: {chosen!r} is not defined in {flows.path}"
        except ConfigError:
            pass
    template_error = None
    if report.valid:
        try:
            template_error = templates.config_problem(load_config())
        except ConfigError:
            pass
    failed = (
        not report.valid or not flows.valid or fallback_error is not None or template_error is not None
        or (strict and bool(report.unknown_fields))
    )
    if as_json:
        emit_json({**report.to_dict(), "workflows": flows.to_dict(), "fallback_error": fallback_error, "template_error": template_error})
    else:
        if not report.exists:
            typer.echo(f"No config file at {report.path}; defaults apply.")
        for issue in report.errors:
            typer.echo(f"error: {issue.field}: {issue.message}", err=True)
        for name in report.unknown_fields:
            level = "error" if strict else "warning"
            typer.echo(f"{level}: {name}: unknown field (not recognised by this version)", err=True)
        for issue in flows.errors:
            typer.echo(f"error: {flows.path.name}: {issue.field}: {issue.message}", err=True)
        if fallback_error:
            typer.echo(f"error: {fallback_error}", err=True)
        if template_error:
            typer.echo(f"error: {template_error}", err=True)
        if not failed:
            typer.echo(f"{report.path}: ok")
            if flows.exists:
                typer.echo(f"{flows.path}: ok")
    if failed:
        raise typer.Exit(1)


def _fail(message: str, as_json: bool = False) -> typer.Exit:
    if as_json:
        emit_json({"error": message})
    else:
        typer.echo(f"error: {message}", err=True)
    return typer.Exit(1)


def _parse_env(pairs: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ConfigError(f"--env expects NAME=VALUE, got {pair!r}")
        env[key] = value
    return env


def _choice(value: str | None, allowed: tuple[str, ...], flag: str) -> str | None:
    if value is not None and value not in allowed:
        raise ConfigError(f"{flag} must be one of: {', '.join(allowed)}")
    return value


def _instance_edit(
    agent: str | None,
    executable: str | None,
    args: list[str] | None,
    env: list[str] | None,
    unset_env: list[str] | None,
    cwd: str | None,
    clear_cwd: bool,
    resume_args: list[str] | None,
    prompt_mode: str | None,
    skill_invocation: str | None,
) -> list[InstanceEdit]:
    options = (executable, args, env, unset_env, cwd, resume_args, prompt_mode, skill_invocation)
    if agent is None:
        if clear_cwd or any(o is not None for o in options):
            raise ConfigError("instance options need --agent to say which agent they apply to")
        return []
    return [
        InstanceEdit(
            agent=agent,
            executable=executable,
            args=args,
            env=_parse_env(env or []),
            unset_env=unset_env or [],
            working_directory=cwd,
            clear_working_directory=clear_cwd,
            resume_args=resume_args,
            prompt_mode=_choice(prompt_mode, PROMPT_MODES, "--prompt-mode"),
            skill_invocation=_choice(skill_invocation, SKILL_INVOCATIONS, "--skill-invocation"),
        )
    ]


AgentOpt = Annotated[str | None, typer.Option("--agent", help="Agent type to configure for this profile (e.g. claude).")]
ExecutableOpt = Annotated[str | None, typer.Option(help="Executable or wrapper script: absolute path, or a name found on the instance's PATH.")]
ArgOpt = Annotated[list[str] | None, typer.Option("--arg", help="Argument passed to the executable. Repeat for several; replaces the existing list.")]
EnvOpt = Annotated[list[str] | None, typer.Option("--env", help="NAME=VALUE for the instance's environment. Repeatable; merged.")]
UnsetEnvOpt = Annotated[list[str] | None, typer.Option("--unset-env", help="Remove a variable from the instance's environment. Repeatable.")]
CwdOpt = Annotated[str | None, typer.Option("--cwd", help="Working directory (absolute path).")]
ClearCwdOpt = Annotated[bool, typer.Option("--clear-cwd", help="Remove the instance's working directory.")]
ResumeOpt = Annotated[list[str] | None, typer.Option("--resume-arg", help="Argument used to resume a session. Repeatable; replaces the list.")]
PromptModeOpt = Annotated[str | None, typer.Option(help="How a prompt is submitted: argument, stdin or interactive.")]
SkillOpt = Annotated[str | None, typer.Option(help="How a skill is invoked: slash, prompt or none.")]


@profile_app.command("list")
def profile_list(as_json: JsonOption = False) -> None:
    """List profiles, their default agent and configured agents."""
    try:
        profiles = list_profiles()
    except ConfigError as exc:
        raise _fail(str(exc), as_json)
    if as_json:
        emit_json({"profiles": profiles})
        return
    if not profiles:
        typer.echo("No profiles. Add one with `agent-launcher profile add <name> --agent claude`.")
    for profile in profiles:
        agents = ", ".join(
            f"{name} (default)" if name == profile["default_agent"] else name for name in profile["agents"]
        )
        typer.echo(f"{profile['name']}: {agents or 'no agents'}")


@profile_app.command("add")
def profile_add(
    name: str,
    agent: AgentOpt = None,
    default_agent: Annotated[str | None, typer.Option(help="Default agent. Defaults to the only agent, if one.")] = None,
    executable: ExecutableOpt = None,
    arg: ArgOpt = None,
    env: EnvOpt = None,
    cwd: CwdOpt = None,
    resume_arg: ResumeOpt = None,
    prompt_mode: PromptModeOpt = None,
    skill_invocation: SkillOpt = None,
) -> None:
    """Create a profile. Use --agent (with the instance options) to configure its first agent."""
    try:
        edits = _instance_edit(agent, executable, arg, env, None, cwd, False, resume_arg, prompt_mode, skill_invocation)
        add_profile(name, edits, default_agent)
    except ConfigError as exc:
        raise _fail(str(exc))
    typer.echo(f"Added profile {name}.")


@profile_app.command("edit")
def profile_edit(
    name: str,
    agent: AgentOpt = None,
    default_agent: Annotated[str | None, typer.Option(help="Set the profile's default agent.")] = None,
    remove_agent: Annotated[list[str] | None, typer.Option(help="Remove an agent from the profile. Repeatable.")] = None,
    executable: ExecutableOpt = None,
    arg: ArgOpt = None,
    env: EnvOpt = None,
    unset_env: UnsetEnvOpt = None,
    cwd: CwdOpt = None,
    clear_cwd: ClearCwdOpt = False,
    resume_arg: ResumeOpt = None,
    prompt_mode: PromptModeOpt = None,
    skill_invocation: SkillOpt = None,
) -> None:
    """Change a profile. With --agent, create or change that agent's instance."""
    try:
        edits = _instance_edit(
            agent, executable, arg, env, unset_env, cwd, clear_cwd, resume_arg, prompt_mode, skill_invocation
        )
        edit_profile(name, edits, default_agent, remove_agent)
    except ConfigError as exc:
        raise _fail(str(exc))
    typer.echo(f"Updated profile {name}.")


@profile_app.command("check")
def profile_check(
    name: str,
    agent: Annotated[str | None, typer.Option("--agent", help="Agent to check. Defaults to the profile's default.")] = None,
    as_json: JsonOption = False,
) -> None:
    """Verify an agent resolves for the profile. Never falls back to another agent or profile."""
    try:
        resolved = resolve_agent(name, agent)
    except AgentResolutionError as exc:
        raise _fail(str(exc), as_json)
    if as_json:
        emit_json(
            {
                "profile": resolved.profile,
                "agent": resolved.agent,
                "executable": str(resolved.executable),
                "argv": list(resolved.argv),
                "working_directory": str(resolved.working_directory) if resolved.working_directory else None,
            }
        )
    else:
        typer.echo(f"{resolved.profile}/{resolved.agent}: {resolved.executable}")


def make_prompter() -> QuestionaryPrompter:
    return QuestionaryPrompter()


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _association_failure(
    exc: AssociationError | LauncherError | RepositoryError | StateError | ConfigError, as_json: bool
) -> typer.Exit:
    """Print a repository/association problem (structured under --json) and return Exit(1)."""
    if isinstance(exc, (AssociationError, LauncherError)):
        payload = exc.to_dict()
    else:
        default_code = "config_invalid" if isinstance(exc, ConfigError) else "state_unavailable"
        payload = {"error": {"code": getattr(exc, "code", default_code), "message": str(exc)}}
    if as_json:
        emit_json(payload)
    else:
        typer.echo(f"error: {payload['error']['message']}", err=True)
        if isinstance(exc, (AssociationError, LauncherError)):
            if exc.details.get("available_profiles"):
                typer.echo("Available profiles: " + ", ".join(exc.details["available_profiles"]), err=True)
            if exc.details.get("available_agents"):
                typer.echo("Available agents: " + ", ".join(exc.details["available_agents"]), err=True)
    return typer.Exit(1)


def _terminal_liveness(config) -> Liveness:
    """Best-effort probe for the reassignment listing: a session is live when its terminal still shows a screen."""
    adapter = select_adapter(config.terminal.adapter)

    def probe(session) -> bool | None:
        ref = session.terminal
        if ref is None:
            return False
        if ref.adapter != adapter.name:
            return None
        return adapter.read_screen(ref) is not None

    return probe


@profile_app.command("set")
def profile_set(
    repository: Annotated[str, typer.Argument(help="Local path, owner/name, or GitHub URL.")],
    profile: str,
    archive_tasks: Annotated[bool, typer.Option("--archive-tasks", help="When changing the profile: mark the repository's tasks archived (files, worktrees and sessions are kept).")] = False,
    keep_tasks: Annotated[bool, typer.Option("--keep-tasks", help="When changing the profile: the tasks keep their profile and cannot be opened until the repository goes back.")] = False,
    cancel: Annotated[bool, typer.Option("--cancel", help="Change nothing.")] = False,
    offline: Annotated[bool, typer.Option("--offline", help="Do not ask GitHub for the repository ID.")] = False,
    as_json: JsonOption = False,
) -> None:
    """Associate a repository with a profile. The only way an association changes.

    Changing an existing association while the repository has tasks needs exactly one of
    --archive-tasks, --keep-tasks or --cancel (asked on a terminal, otherwise required).
    """
    chosen = [name for name, on in (("archive-tasks", archive_tasks), ("keep-tasks", keep_tasks), ("cancel", cancel)) if on]
    try:
        if len(chosen) > 1:
            raise AssociationError(
                "conflicting_resolution", "Pass at most one of --archive-tasks, --keep-tasks and --cancel.", given=chosen
            )
        config = load_config()
        known = sorted(config.profiles)
        if profile not in known:
            raise AssociationError("unknown_profile", f"No profile named {profile!r}.", available_profiles=known)
        identity = identify_reference(repository, fetch_github=not offline)
        resolution = chosen[0] if chosen else None
        with open_state() as conn:
            # The only terminal probe: before any transaction. `set_profile` re-reads task IDs, never probes.
            previous, affected = plan_reassignment(conn, identity, profile, _terminal_liveness(config))
            if affected and resolution is None and is_interactive() and not as_json:
                prompter = make_prompter()
                prompter.say(f"{identity.describe()} is on profile {previous}; changing it to {profile} affects:")
                for task in affected:
                    prompter.say("  " + task.describe())
                resolution = prompter.select(
                    "What should happen to these tasks?",
                    [
                        Choice("archive-tasks", "Archive them (files, worktrees and sessions are kept)"),
                        Choice("keep-tasks", f"Keep them on {previous} (they cannot be opened until the repository goes back)"),
                        Choice("cancel", "Cancel: change nothing"),
                    ],
                    default="cancel",
                )
            if resolution == "cancel":
                if as_json:
                    emit_json({"repository": identity.to_dict(), "profile": previous, "changed": False, "cancelled": True})
                else:
                    typer.echo("Cancelled. Nothing was changed.")
                return
            result = set_profile(
                conn, identity, profile, reassignment=resolution, planned=[t.task_id for t in affected]
            )
    except SetupCancelled:
        typer.echo("Cancelled. Nothing was changed.", err=True)
        raise typer.Exit(130)
    except (AssociationError, RepositoryError, StateError, ConfigError) as exc:
        raise _association_failure(exc, as_json)
    if as_json:
        emit_json(
            {
                "repository": identity.to_dict(),
                "profile": result.profile,
                "previous_profile": result.previous,
                "changed": result.changed,
                "reassignment": result.reassignment,
                "affected_tasks": list(result.affected_task_ids),
                "recorded_github_id": (
                    {"full_name": identity.full_name, "id": identity.github_id, "path": identity.path}
                    if result.recorded_id else None
                ),
            }
        )
        return
    if result.recorded_id:
        typer.echo(f"Recorded GitHub repository {identity.full_name} (ID {identity.github_id}) for {identity.path}.")
    if not result.changed:
        typer.echo(f"{identity.describe()} already uses profile {profile}.")
    else:
        typer.echo(f"{identity.describe()} now uses profile {profile}.")
        if result.reassignment == "archive-tasks":
            typer.echo(f"Archived {len(result.affected_task_ids)} task(s): " + ", ".join(result.affected_task_ids))
        elif result.reassignment == "keep-tasks":
            typer.echo(
                f"Kept {len(result.affected_task_ids)} task(s) on {result.previous}; they cannot be opened until "
                f"the repository goes back to {result.previous}."
            )


@profile_app.command("which")
def profile_which(
    repository: Annotated[str, typer.Argument(help="Local path, owner/name, or GitHub URL.")],
    offline: Annotated[bool, typer.Option("--offline", help="Do not ask GitHub for the repository ID.")] = False,
    as_json: JsonOption = False,
) -> None:
    """Show a repository's profile. Unknown repositories prompt (on a terminal) or fail; never auto-assigned."""
    try:
        profiles = sorted(load_config().profiles)
        identity = identify_reference(repository, fetch_github=not offline)
        prompter = make_prompter() if is_interactive() and not as_json else None
        with open_state() as conn:
            resolved = ensure_profile(conn, identity, profiles, prompter)
    except SetupCancelled:
        typer.echo("Cancelled. Nothing was saved.", err=True)
        raise typer.Exit(130)
    except (AssociationError, RepositoryError, StateError, ConfigError) as exc:
        raise _association_failure(exc, as_json)
    if as_json:
        emit_json({"repository": identity.to_dict(), "profile": resolved.profile, "newly_associated": resolved.newly_associated})
    else:
        typer.echo(f"{identity.describe()}: {resolved.profile}")


_TASK_ERRORS = (AssociationError, LauncherError, RepositoryError, StateError, ConfigError)


@app.command()
def new(
    title: Annotated[str, typer.Option("--title", help="Short task title.")],
    repo: Annotated[str, typer.Option("--repo", help="Path of the local repository checkout.")],
    description: Annotated[str, typer.Option("--description", help="Optional longer description.")] = "",
    offline: Annotated[bool, typer.Option("--offline", help="Do not ask GitHub for the repository ID.")] = False,
    as_json: JsonOption = False,
) -> None:
    """Create a local task. The repository's profile is looked up, or asked for once on a terminal."""
    try:
        if not Path(repo).expanduser().is_dir():
            raise RepositoryError("not_a_repository", f"{repo} is not a directory")
        identity = identify_reference(repo, fetch_github=not offline)
        assert identity.path is not None
        prompter = make_prompter() if is_interactive() and not as_json else None
        with open_state() as conn:
            resolved = ensure_profile(conn, identity, sorted(load_config().profiles), prompter)
            task = create_task(conn, title, description, resolved.repository_id, identity.path, resolved.profile)
    except SetupCancelled:
        typer.echo("Cancelled. Nothing was saved.", err=True)
        raise typer.Exit(130)
    except _TASK_ERRORS as exc:
        raise _association_failure(exc, as_json)
    if as_json:
        emit_json({"task": task.to_dict()})
    else:
        typer.echo(f"Created task {task.id} ({task.profile}): {task.title}")


@tasks_app.command("list")
def tasks_list(as_json: JsonOption = False) -> None:
    """List tasks."""
    try:
        with open_state() as conn:
            tasks = list_tasks(conn)
    except _TASK_ERRORS as exc:
        raise _association_failure(exc, as_json)
    if as_json:
        emit_json({"tasks": [t.to_dict() for t in tasks]})
        return
    if not tasks:
        typer.echo('No tasks. Create one with `agent-launcher new --title "..." --repo <path>`.')
    for t in tasks:
        typer.echo(f"{t.id}  {t.state:<8}  {t.profile:<10}  {t.title}")


@tasks_app.command("show")
def tasks_show(task: str, as_json: JsonOption = False) -> None:
    """Show a task and its sessions."""
    try:
        with open_state() as conn:
            found = get_task(conn, task)
            sessions = sessions_for_task(conn, found.id)
            tree = get_worktree(conn, found.id)
            github = github_details(conn, found.id)
    except _TASK_ERRORS as exc:
        raise _association_failure(exc, as_json)
    if as_json:
        emit_json(
            {
                "task": found.to_dict(),
                "github": github,
                "worktree": tree.to_dict() if tree else None,
                "sessions": [s.to_dict() for s in sessions],
            }
        )
        return
    typer.echo(f"{found.id}: {found.title}")
    if found.description:
        typer.echo(f"  {found.description}")
    typer.echo(f"  state: {found.state}   profile: {found.profile}   agent: {found.agent or '-'}")
    typer.echo(f"  repository: {found.repo_path}")
    if github:
        typer.echo(f"  github: {github['kind']} #{github['number']} [{github['state']}] {github['url']}")
        if "pull" in github:
            pr = github["pull"]
            typer.echo(
                f"  pull request: {pr['head_ref']} -> {pr['base_ref']}, head {(pr['head_sha'] or '')[:12]}"
                f"{' (fork)' if pr['head_fork'] else ''}{', draft' if pr['draft'] else ''}"
                f"{', yours' if pr['own'] else ''}{', review requested' if pr['review_requested'] else ''}"
            )
    if tree:
        typer.echo(f"  worktree: {tree.path} ({tree.ownership}, branch {tree.branch or '-'})")
    for s in sessions:
        where = f"{s.terminal.adapter}:{s.terminal.workspace_id}" if s.terminal else "no terminal"
        typer.echo(f"  session {s.id}: {s.agent} [{s.state}] {where}")


@tasks_app.command("link")
def tasks_link(
    task: str,
    url: Annotated[str, typer.Argument(help="GitHub issue or pull request URL.")],
    as_json: JsonOption = False,
) -> None:
    """Link a local task to a GitHub issue or pull request, keeping its ID, worktree, session and conversation.

    Refused, with nothing changed, when the item is in another repository, when the repository belongs to another
    profile, when the item already belongs to another task, or when the task is linked to a different item.
    """
    try:
        with open_state() as conn:
            result = link_task(conn, task, url)
    except _TASK_ERRORS as exc:
        raise _association_failure(exc, as_json)
    if as_json:
        emit_json({"task": result.task.to_dict(), "github": result.github, "linked": result.linked})
        return
    verb = "Linked" if result.linked else "Already linked:"
    typer.echo(f"{verb} task {result.task.id} to {result.task.url}")


def _run_session_command(action, task: str, terminal: str | None, as_json: bool, *, interactive: bool = True) -> None:
    """Shared body of open/resume/prompt/restart: load config and state, run `action`, print the result."""
    try:
        config = load_config()
        adapter = select_adapter(terminal or config.terminal.adapter)
        prompter = make_prompter() if interactive and is_interactive() and not as_json else None
        with open_state() as conn:
            result = action(conn, config, adapter, prompter)
    except SetupCancelled:
        typer.echo("Cancelled. Nothing was changed.", err=True)
        raise typer.Exit(130)
    except _TASK_ERRORS as exc:
        raise _association_failure(exc, as_json)
    _print_result(result, as_json)


def _print_result(result: OpenResult, as_json: bool) -> None:
    if as_json:
        emit_json(
            {
                "action": result.action,
                "task": result.task.to_dict(),
                "session": result.session.to_dict(),
                "prompt": result.prompt,
                "prompt_prepared": result.prompt_prepared,
                "prompt_submitted": result.prompt_submitted,
                "resume_state": result.resume_state,
                "worktree": result.worktree.to_dict() if result.worktree else None,
                "workflow": result.workflow or result.task.workflow,
                "notice": result.notice,
            }
        )
        return
    ref = result.session.terminal
    where = f" in {ref.adapter} workspace {ref.workspace_id}" if ref else ""
    verb = {
        "created": "Opened",
        "focused": "Focused",
        "resumed": "Resumed",
        "restarted": "Restarted",
        "prompted": "Prepared the prompt for",
    }[result.action]
    typer.echo(f"{verb} task {result.task.id} with {result.session.agent} ({result.session.profile}){where}.")
    if result.worktree:
        typer.echo(f"Worktree ({result.worktree.ownership}): {result.worktree.path}")
    if result.workflow:
        typer.echo(f"Workflow: {result.workflow}")
    if result.notice:
        typer.echo(result.notice, err=True)


_TerminalOpt = Annotated[str | None, typer.Option("--terminal", help="Terminal adapter, overriding config (e.g. mock).")]
_OfflineOpt = Annotated[bool, typer.Option("--offline", help="Do not ask GitHub for the repository ID.")]
_ExecuteOpt = Annotated[bool, typer.Option("--execute", help="Submit the prompt automatically, whatever the config says.")]
_PrepareOpt = Annotated[bool, typer.Option("--prepare", help="Only prepare the prompt for review, whatever the config says.")]


def _execution(execute: bool, prepare: bool) -> str | None:
    if execute and prepare:
        raise typer.BadParameter("--execute and --prepare cannot be combined.")
    return "execute" if execute else "prepare" if prepare else None


@app.command("open")
def open_command(
    task: str,
    agent: Annotated[str | None, typer.Option("--agent", help="Agent to run; must belong to the task's profile.")] = None,
    workflow: Annotated[str | None, typer.Option("--workflow", help="Use this workflow instead of routing (first open only).")] = None,
    ask_workflow: Annotated[bool, typer.Option("--ask-workflow", help="Ask which workflow to use instead of routing (first open only).")] = False,
    execute: _ExecuteOpt = False,
    prepare: _PrepareOpt = False,
    terminal: _TerminalOpt = None,
    offline: _OfflineOpt = False,
    as_json: JsonOption = False,
) -> None:
    """Open a task, or a GitHub issue or pull request URL: start its session, or focus the one it already has.

    A URL finds the task by GitHub's stable IDs (creating it, and cloning the repository after asking, on first
    use). The agent's prompt is the workflow's template, else its skill invocation of the URL, else the URL (see
    `workflows test`). It is only prepared for you to review, unless `--execute` (or `prompt_execution`) says to submit it. Your own pull request is checked out on its head branch; anyone else's, or
    a fork's, in an isolated review worktree that cannot push to the contributor's branch.
    """
    if is_issue_reference(task):
        _run_session_command(
            lambda conn, config, adapter, prompter: open_github(
                conn, task, config=config, adapter=adapter, prompter=prompter, agent=agent, offline=offline,
                workflow=workflow, ask_workflow=ask_workflow, execution=_execution(execute, prepare),
            ),
            task, terminal, as_json,
        )
        return
    _run_session_command(
        lambda conn, config, adapter, prompter: open_task(
            conn, task, config=config, adapter=adapter, prompter=prompter, agent=agent, offline=offline,
            workflow=workflow, ask_workflow=ask_workflow, execution=_execution(execute, prepare),
        ),
        task, terminal, as_json,
    )


@skill_app.command("path")
def skill_path(as_json: JsonOption = False) -> None:
    """Print the directory of the bundled `agent-launcher` management skill, to copy it into an agent's skills directory."""
    try:
        path = skill_directory()
    except LauncherError as exc:
        raise _association_failure(exc, as_json)
    if as_json:
        emit_json({"name": "agent-launcher", "path": str(path)})
    else:
        typer.echo(str(path))


@app.command()
def configure(
    request: Annotated[str | None, typer.Argument(help="What you want done. Used when the task is first created.")] = None,
    repo: Annotated[str | None, typer.Option("--repo", help="Repository for the maintenance task (default: a directory the launcher owns).")] = None,
    profile: Annotated[str | None, typer.Option("--profile", help="Profile for a repository that has none yet.")] = None,
    agent: Annotated[str | None, typer.Option("--agent", help="Agent to run; must belong to the repository's profile.")] = None,
    terminal: _TerminalOpt = None,
    as_json: JsonOption = False,
) -> None:
    """Launch an agent with the management skill to help configure Agent Launcher.

    Creates (or finds again) one local maintenance task and starts it through the normal `open` path: the
    repository's profile and the profile's agents decide who runs. The prompt is prepared for you to review.
    """
    def action(conn, config, adapter, prompter):
        prepared = prepare_task(
            conn, repo=repo, profile=profile, request=request, available_profiles=sorted(config.profiles),
            prompter=prompter,
        )
        return open_task(  # the task already carries its workflow, which a first open keeps
            conn, prepared.task.id, config=config, adapter=adapter, prompter=prompter, agent=agent, offline=True,
        )

    _run_session_command(action, "configure", terminal, as_json)


@app.command("resume")
def resume_command(
    task: str,
    force: Annotated[bool, typer.Option("--force", help="Resume in a new terminal session even if the old one looks alive.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Confirm --force without asking.")] = False,
    terminal: _TerminalOpt = None,
    offline: _OfflineOpt = False,
    as_json: JsonOption = False,
) -> None:
    """Resume a task's agent conversation where it can't be focused (agent exited, terminal session gone)."""
    _run_session_command(
        lambda conn, config, adapter, prompter: resume_task(
            conn, task, force=force, confirmed=yes, config=config, adapter=adapter, prompter=prompter, offline=offline
        ),
        task, terminal, as_json,
    )


@app.command("prompt")
def prompt_command(
    task: str,
    terminal: _TerminalOpt = None,
    offline: _OfflineOpt = False,
    as_json: JsonOption = False,
) -> None:
    """Prepare the task's prompt again, unsubmitted, in its existing session.

    Use it after accepting Claude Code's folder-trust dialog (every new worktree shows it), when the
    prompt was left on the clipboard instead of entered.
    """
    _run_session_command(
        lambda conn, config, adapter, prompter: prompt_task(conn, task, config=config, adapter=adapter, offline=offline),
        task, terminal, as_json, interactive=False,
    )


@app.command("restart")
def restart_command(
    task: str,
    yes: Annotated[bool, typer.Option("--yes", help="Confirm without asking.")] = False,
    execute: _ExecuteOpt = False,
    prepare: _PrepareOpt = False,
    terminal: _TerminalOpt = None,
    offline: _OfflineOpt = False,
    as_json: JsonOption = False,
) -> None:
    """Start a task's agent afresh in a new terminal session. Keeps the task and its worktree. Asks first."""
    _run_session_command(
        lambda conn, config, adapter, prompter: restart_task(
            conn, task, config=config, adapter=adapter, prompter=prompter, confirmed=yes, offline=offline,
            execution=_execution(execute, prepare),
        ),
        task, terminal, as_json,
    )


@sessions_app.command("candidates")
def sessions_candidates(
    task: str,
    terminal: Annotated[str | None, typer.Option("--terminal", help="Terminal adapter (default: from config).")] = None,
    as_json: JsonOption = False,
) -> None:
    """List terminal sessions that may be this task's, with the evidence. Changes nothing and reads no screen."""
    try:
        config = load_config()
        adapter = select_adapter(terminal or config.terminal.adapter)
        with open_state() as conn:
            found = get_task(conn, task)
            candidates = find_candidates(conn, found, adapter)
    except _TASK_ERRORS as exc:
        raise _association_failure(exc, as_json)
    if as_json:
        emit_json({"task": found.id, "candidates": [c.to_dict() for c in candidates]})
        return
    if not candidates:
        typer.echo(f"No session has evidence for task {found.id}. A session is offered only when its working directory is the task's worktree or its title names the task.")
    for c in candidates:
        s = c.external.session
        typer.echo(f"{s.workspace_id}  {c.external.title or '-'}")
        for line in c.evidence:
            typer.echo(f"    {line}")
        typer.echo(f"    adopt: agent-launcher sessions adopt {found.id} {s.workspace_id}")


@sessions_app.command("adopt")
def sessions_adopt(
    task: str,
    workspace: Annotated[str, typer.Argument(help="A workspace (or surface) ID from `sessions candidates`.")],
    agent: Annotated[str | None, typer.Option("--agent", help="The agent running there (default: the task's).")] = None,
    terminal: Annotated[str | None, typer.Option("--terminal", help="Terminal adapter (default: from config).")] = None,
    as_json: JsonOption = False,
) -> None:
    """Record an existing terminal session as the task's session. It is never moved, closed or typed into."""
    try:
        config = load_config()
        adapter = select_adapter(terminal or config.terminal.adapter)
        with open_state() as conn:
            found = get_task(conn, task)
            session = adopt_session(conn, found, adapter, workspace, config, agent=agent)
    except _TASK_ERRORS as exc:
        raise _association_failure(exc, as_json)
    if as_json:
        emit_json({"action": "adopted", "task": found.id, "session": session.to_dict()})
        return
    typer.echo(
        f"Adopted {session.terminal.workspace_id if session.terminal else workspace} as the session of task {found.id}. "
        "The launcher did not create it, so it will never close or move it."
    )


@worktrees_app.command("list")
def worktrees_list(as_json: JsonOption = False) -> None:
    """List the worktrees recorded for tasks, and whether each still exists."""
    try:
        with open_state() as conn:
            records = list_worktree_records(conn)
            rows = [(r, Path(r.path).is_dir()) for r in records]
    except _TASK_ERRORS as exc:
        raise _association_failure(exc, as_json)
    if as_json:
        emit_json({"worktrees": [{**r.to_dict(), "exists": exists} for r, exists in rows]})
        return
    if not rows:
        typer.echo("No task worktrees yet. `agent-launcher open <task>` creates one.")
    for r, exists in rows:
        typer.echo(f"{r.task_id}  {r.ownership:<7}  {r.branch or '-':<40}  {r.path}{'' if exists else '  (missing)'}")


@worktrees_app.command("inspect")
def worktrees_inspect(task: str, as_json: JsonOption = False) -> None:
    """Show a task's worktree: how it was recorded, and what git says about it now. Changes nothing."""
    try:
        with open_state() as conn:
            found = get_task(conn, task)
            record = get_worktree(conn, found.id)
            if record is None:
                raise LauncherError(
                    "no_worktree",
                    f"Task {found.id} has no worktree yet. `agent-launcher open {found.id}` creates one, or "
                    f"`agent-launcher worktrees associate {found.id} <path>` adopts one.",
                    task=found.id,
                )
            info = inspect_worktree(record, found.repo_path)
    except _TASK_ERRORS as exc:
        raise _association_failure(exc, as_json)
    if as_json:
        emit_json({"worktree": info})
        return
    typer.echo(f"{found.id}: {info['path']}")
    typer.echo(f"  ownership: {info['ownership']}   branch: {info['branch_now'] or info['branch'] or '-'}   base: {info['base_ref'] or '-'}")
    typer.echo(
        f"  exists: {info['exists']}   known to git: {info['listed']}   "
        f"uncommitted changes: {'unknown' if info['dirty'] is None else info['dirty']}"
    )
    if info["locked"] or info["prunable"]:
        typer.echo(f"  locked: {info['locked']}   prunable: {info['prunable']}")


@worktrees_app.command("associate")
def worktrees_associate(
    task: str,
    path: Annotated[Path, typer.Argument(help="An existing worktree of the task's repository.")],
    force: Annotated[
        bool, typer.Option("--force", help="Also for a task that already has a session (its conversation is tied to the old directory).")
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Adopt an existing worktree as the task's. Remembered; nothing in the tree is changed."""
    try:
        with open_state() as conn:
            found = get_task(conn, task)
            record, dirty = associate(conn, found, path, force=force)
    except _TASK_ERRORS as exc:
        raise _association_failure(exc, as_json)
    if as_json:
        emit_json({"worktree": record.to_dict(), "dirty": dirty})
        return
    typer.echo(f"Task {found.id} now uses {record.path} (adopted).")
    if dirty:
        typer.echo("It has uncommitted changes; they are left as they are.", err=True)


@workflows_app.command("list")
def workflows_list(as_json: JsonOption = False) -> None:
    """List the workflow rules in the order they are tried (priority, then position in the file)."""
    try:
        config = load_config()
        file = load_workflows()
    except _TASK_ERRORS as exc:
        raise _association_failure(exc, as_json)
    fallback = next((w for w in file.workflows if w.id == config.workflow_routing.fallback), None) or (
        builtin_fallback() if config.workflow_routing.fallback == BUILTIN_FALLBACK_ID else None
    )
    ranked = sorted(enumerate(file.workflows), key=lambda item: (-item[1].priority, item[0]))
    if as_json:
        emit_json(
            {
                "selection_mode": config.workflow_routing.selection_mode,
                "fallback": fallback.model_dump(mode="json") if fallback else None,
                "workflows": [{"position": i + 1, **w.model_dump(mode="json")} for i, w in ranked],
            }
        )
        return
    typer.echo(f"selection mode: {config.workflow_routing.selection_mode}   fallback: {config.workflow_routing.fallback}")
    if not file.workflows:
        typer.echo("No rules. `agent-launcher workflows init` writes an example workflows.json.")
    for i, w in ranked:
        conditions = ", ".join(f"{k}={v}" for k, v in w.match.model_dump(exclude_none=True, exclude_defaults=True).items())
        extras = "".join(
            f"  {label}: {value}" for label, value in (("skill", w.skill), ("agent", w.preferred_agent)) if value
        )
        typer.echo(f"{w.priority:>5}  #{i + 1} {w.id}  [{conditions or 'matches everything'}]{extras}")


@workflows_app.command("init")
def workflows_init() -> None:
    """Write an example workflows.json (issue triage, code review, PR review fixer). Never overwrites a file."""
    from agent_launcher.paths import workflows_path

    path = workflows_path()
    if path.exists():
        raise _fail(f"{path} already exists; it was not changed")
    save_workflows(example_file(), path)
    typer.echo(f"Wrote {path}. Edit it, then try `agent-launcher workflows test <url>`.")


@workflows_app.command("test")
def workflows_test(
    reference: Annotated[str, typer.Argument(help="A GitHub issue or pull request URL, or a task ID.")],
    offline: _OfflineOpt = False,
    as_json: JsonOption = False,
) -> None:
    """Explain which workflow a task would get, rule by rule. Read only: creates nothing."""
    try:
        result = explain(reference, config=load_config(), offline=offline)
    except _TASK_ERRORS + (GitHubError,) as exc:
        raise _association_failure(exc, as_json)
    if as_json:
        emit_json(result)
        return
    task = result["task"]
    typer.echo(f"{task['type']} {task['repository'] or '(repository unknown)'}" + (f"#{task['number']}" if task["number"] else "")
               + (f" [{task['state']}]" if task["state"] else "") + f"   (from: {result['source']})")
    typer.echo(f"profile: {result['profile'] or 'not known yet (the repository has none stored)'}   "
               f"selection mode: {result['selection_mode']}")
    typer.echo("")
    for rule in result["rules"]:
        verdict = f"MATCHED, rank {rule['rank']}" if rule["matched"] else "not matched"
        typer.echo(f"#{rule['position']} {rule['id']}  priority {rule['priority']}  {verdict}")
        for c in rule["conditions"]:
            typer.echo(f"     {'yes' if c['matched'] else 'no '}  {c['condition']}: {c['reason']}")
        if not rule["conditions"]:
            typer.echo("     (no conditions: matches every task)")
    typer.echo("")
    if result["tie"]:
        typer.echo(f"tie: {result['tie']}")
    win = result["winner"]
    how = "fallback (no rule matched)" if win["fallback"] else f"priority {win['priority']}, best of {len(result['matched'])} matching"
    typer.echo(f"winner: {win['id']} - {how}")
    if result["would_ask"]:
        typer.echo(f"open would ask which workflow to use ({result['selection_mode']}); it would offer {win['id']} first")
    typer.echo(f"skill: {win['skill'] or 'none'}" + (f"   template: {win['template']}" if win["template"] else ""))
    if win["preferred_agent"]:
        typer.echo(
            f"preferred agent: {win['preferred_agent']}"
            + (" (highlighted first)" if win["highlighted_agent"] else " (ignored)")
        )
    if win["preferred_agent_note"]:
        typer.echo(f"note: {win['preferred_agent_note']}")
    if win["prompt"]:
        typer.echo(f"prompt: {win['prompt']}")


@app.command()
def setup(
    answers: Annotated[Path | None, typer.Option("--answers", help="JSON file of answers; nothing is asked or detected.")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Apply without asking for confirmation (needed with --answers when not on a terminal).")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the diff and write nothing.")] = False,
    as_json: JsonOption = False,
) -> None:
    """Interactive setup wizard: profiles, agents, defaults. Safe to re-run; never resets config."""
    try:
        parsed = load_answers(answers) if answers else None
        prompter = make_prompter() if is_interactive() and not as_json else None
        if parsed is None and prompter is None:
            raise SetupError("needs_input", "setup needs a terminal; use --answers FILE --yes to run it unattended")
        result = run_setup(prompter=prompter, answers=parsed, assume_yes=yes, dry_run=dry_run)
    except SetupCancelled:
        typer.echo("Setup cancelled. Nothing was written.", err=True)
        raise typer.Exit(130)
    except SetupError as exc:
        if as_json:
            emit_json(exc.to_dict())
        else:
            typer.echo(f"error: {exc.message}" + (f" ({exc.field})" if exc.field else ""), err=True)
        raise typer.Exit(1)
    if as_json:
        emit_json(result.to_dict())
    elif parsed is not None and prompter is None:
        typer.echo(result.diff or "Nothing to change.")
        typer.echo(f"Wrote {result.path}." if result.applied else "Nothing was written.")


@app.command()
def doctor(as_json: JsonOption = False) -> None:
    """Check configuration, tools, profiles and the database. Exit code 1 if any check fails."""
    report = run_doctor()
    if as_json:
        emit_json(report.to_dict())
    else:
        marks = {"pass": "ok  ", "warn": "warn", "fail": "FAIL"}
        for check in report.checks:
            typer.echo(f"[{marks[check.status]}] {check.name}: {check.detail}")
            if check.remediation and check.status != "pass":
                typer.echo(f"       -> {check.remediation}")
        typer.echo(f"\n{report.count('pass')} passed, {report.count('warn')} warnings, {report.count('fail')} failed")
    if not report.ok:
        raise typer.Exit(1)


@diagnostics_app.command("export")
def diagnostics_export(
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Archive path (.tar.gz). Default: current directory.")] = None,
) -> None:
    """Write a sanitised .tar.gz (doctor report, config, recent logs) for bug reports."""
    target = output or Path.cwd() / default_archive_name()
    try:
        names = export_diagnostics(target, run_doctor())
    except FileExistsError:
        raise _fail(f"{target} already exists; choose another --output")
    except OSError as exc:
        raise _fail(f"cannot write {target}: {exc.strerror or exc}")
    typer.echo(f"Wrote {target} ({', '.join(names)}). Secrets, tokens and home paths are redacted; review before sharing.")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    debug: Annotated[bool, typer.Option("--debug", help="Trace decisions to stderr and the log. Goes before the command: agent-launcher --debug doctor.")] = False,
) -> None:
    """Agent Launcher."""
    settings = LogSettings()
    config_debug = False
    try:
        config = load_config()
        settings, config_debug = config.logs, config.debug
    except ConfigError:
        pass
    setup_logging(debug or config_debug, settings)
    trace("command started", command=ctx.invoked_subcommand, debug=debug or config_debug)
    if ctx.invoked_subcommand is None:
        _bare_invocation(ctx)


def _bare_invocation(ctx: typer.Context) -> None:
    """`agent-launcher` alone: offer setup on a first run, otherwise show help."""
    if not config_path().exists() and is_interactive():
        typer.echo("No configuration found.")
        try:
            wanted = make_prompter().confirm("Set up Agent Launcher now?")
        except SetupCancelled:
            wanted = False
        if wanted:
            ctx.invoke(setup, answers=None, yes=False, dry_run=False, as_json=False)
            return
        typer.echo("Skipped. Run `agent-launcher setup` whenever you want.\n")
    elif not config_path().exists():
        typer.echo("No configuration found. Run `agent-launcher setup`.\n")
    typer.echo(ctx.get_help())


if __name__ == "__main__":
    app()
