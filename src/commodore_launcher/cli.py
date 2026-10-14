"""Command-line interface. Presentation only: logic lives in the other modules."""

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from commodore_launcher import __version__
from commodore_launcher.agents import AgentResolutionError, resolve_agent
from commodore_launcher.associations import AssociationError, ensure_profile, plan_reassignment, set_profile
from commodore_launcher.configure import prepare_task
from commodore_launcher.integrate import GH_DASH_TASK_TITLE, gh_dash_detected, integration_prompt
from commodore_launcher import copied_state, migrations
from commodore_launcher.config_edit import edit_config
from commodore_launcher.config import (
    ConfigError, LogSettings, effective_config, load_config, outdated_message, outdated_version, read_raw, validate_config,
)
from commodore_launcher.errors import LauncherError
from commodore_launcher.diagnostics import default_archive_name, export_diagnostics
from commodore_launcher.doctor import run_doctor
from commodore_launcher.completion import archive_task, unarchive_task
from commodore_launcher.github_tasks import github_details, is_issue_reference, is_task_id_shaped, needs_issue_or_pr_url_error, open_github
from commodore_launcher.interaction import Choice, QuestionaryPrompter, SetupCancelled
from commodore_launcher.reassignment import Liveness
from commodore_launcher.launch import OpenResult, open_task, resume_task
from commodore_launcher.logs import setup_logging, trace
from commodore_launcher.paths import config_path, copy_old_home
from commodore_launcher.profiles import (
    InstanceEdit,
    add_profile,
    edit_profile,
    list_profiles,
)
from commodore_launcher.repositories import RepositoryError, identify_reference
from commodore_launcher.sessions import sessions_for_task
from commodore_launcher.skill_bundle import skill_directory
from commodore_launcher.state import StateError, open_state
from commodore_launcher.tasks import get_user_task, list_user_tasks
from commodore_launcher.terminal_select import select_adapter
from commodore_launcher.wizard import SetupError, load_answers, run_setup

app = typer.Typer(help="Start a harness on GitHub issues and PRs.")
_TerminalOpt = Annotated[str | None, typer.Option("--terminal", help="Terminal adapter, overriding config (e.g. mock).")]
config_app = typer.Typer(help="Inspect, validate, migrate and edit configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")
profile_app = typer.Typer(help="Manage profiles and their harnesses.", no_args_is_help=True)
app.add_typer(profile_app, name="profile")

diagnostics_app = typer.Typer(help="Export sanitised diagnostics.", no_args_is_help=True)
app.add_typer(diagnostics_app, name="diagnostics")

tasks_app = typer.Typer(help="List, inspect and archive tasks (GitHub issues and PRs the launcher has opened).", no_args_is_help=True)
app.add_typer(tasks_app, name="tasks")

skill_app = typer.Typer(help="The bundled management skill.", no_args_is_help=True)
app.add_typer(skill_app, name="skill")

integrate_app = typer.Typer(help="Bootstrap integrations with other tools, with a harness.", no_args_is_help=True)
app.add_typer(integrate_app, name="integrate")

JsonOption = Annotated[bool, typer.Option("--json", help="Machine-readable JSON output.")]


def emit_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2))


@app.command()
def version(as_json: JsonOption = False) -> None:
    """Print the installed version."""
    if as_json:
        emit_json({"version": __version__})
    else:
        typer.echo(f"commodore {__version__}")


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


def _outdated_note(report) -> str | None:
    """A valid file older than this release writes still works for `show` and `validate`; nothing else reads it."""
    try:
        version = outdated_version(read_raw(report.path))
    except ConfigError:
        return None
    return outdated_message(version) if version is not None else None


@config_app.command("validate")
def config_validate(
    as_json: JsonOption = False,
    strict: Annotated[bool, typer.Option(help="Treat unknown fields as errors.")] = False,
) -> None:
    """Check config.json. Unknown fields are reported; --strict makes them fail."""
    report = validate_config()
    failed = not report.valid or (strict and bool(report.unknown_fields))
    outdated = _outdated_note(report) if report.valid else None
    if as_json:
        emit_json({**report.to_dict(), "outdated": outdated})
    else:
        if outdated:
            typer.echo(f"warning: version: {outdated}", err=True)
        if not report.exists:
            typer.echo(f"No config file at {report.path}; defaults apply.")
        for issue in report.errors:
            typer.echo(f"error: {issue.field}: {issue.message}", err=True)
        for name in report.unknown_fields:
            level = "error" if strict else "warning"
            typer.echo(f"{level}: {name}: unknown field (not recognised by this version)", err=True)
        if not failed:
            typer.echo(f"{report.path}: ok")
    if failed:
        raise typer.Exit(1)


@config_app.command("migrate")
def config_migrate(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the plan and a diff; write nothing.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Apply a significant migration without asking.")] = False,
    as_json: JsonOption = False,
) -> None:
    """Bring config.json up to the version this release writes (backed up first)."""
    plans = migrations.plan_migrations()
    blocked = [p for p in plans if p.status in ("error", "too_new")]
    pending = [p for p in plans if p.status == "pending"]
    if dry_run or blocked or not pending:
        if as_json:
            emit_json({"dry_run": dry_run, "files": [p.to_dict() for p in plans]})
        else:
            for plan in plans:
                _echo_plan(plan, with_diff=True)
        if blocked:
            raise typer.Exit(1)
        return
    significant = [p for p in pending if p.significant]
    if significant and not yes:
        if as_json or not is_interactive():
            raise _fail("this migration changes more than the version number; review it with --dry-run, then pass --yes", as_json)
        for plan in significant:
            _echo_plan(plan, with_diff=True)
        if not make_prompter().confirm("Back up and migrate these files?", default=False):
            typer.echo("Cancelled. Nothing was written.", err=True)
            raise typer.Exit(130)
    results = []
    for plan in pending:
        try:
            result = migrations.apply_migration(plan)
        except LauncherError as exc:
            if results:
                done = ", ".join(r.plan.target.name for r in results if r.ok)
                typer.echo(f"already migrated before this failure: {done or 'none'}", err=True)
            raise _fail(exc.message, as_json)
        results.append(result)
        if not result.ok:
            break
    if as_json:
        emit_json({"dry_run": False, "files": [r.to_dict() for r in results]})
    else:
        for result in results:
            name = result.plan.target.name
            if result.ok:
                typer.echo(f"{name}: migrated from version {result.plan.from_version} to {result.plan.target.current_version} (backup: {result.backup})")
            else:
                typer.echo(f"error: {name}: {'; '.join(result.problems)}; the original was restored (backup: {result.backup})", err=True)
    if any(not r.ok for r in results):
        raise typer.Exit(1)


def _echo_plan(plan: "migrations.MigrationPlan", with_diff: bool) -> None:
    name = plan.target.name
    if plan.status == "absent":
        typer.echo(f"{name}: not present; nothing to migrate.")
    elif plan.status == "current":
        typer.echo(f"{name}: version {plan.from_version} is current; nothing to do.")
    elif plan.status == "pending":
        extra = f"; also changes: {', '.join(plan.changed_keys)}" if plan.changed_keys else ""
        typer.echo(f"{name}: would migrate from version {plan.from_version} to {plan.target.current_version}{extra}")
        if with_diff:
            typer.echo(plan.diff)
    else:
        for problem in plan.problems:
            typer.echo(f"error: {name}: {problem}", err=True)


@config_app.command("edit")
def config_edit() -> None:
    """Open config.json in $VISUAL/$EDITOR on a copy; it is saved only if it validates."""
    prompter = make_prompter() if is_interactive() else None
    try:
        outcome = edit_config(prompter)
    except LauncherError as exc:
        raise _fail(exc.message)
    except SetupCancelled:
        typer.echo("Cancelled. Nothing was written.", err=True)
        raise typer.Exit(130)
    for warning in outcome.warnings:
        typer.echo(f"warning: {warning}", err=True)
    typer.echo(
        {"saved": f"Saved {config_path()}.", "unchanged": "No changes.", "discarded": "Discarded. Nothing was written."}[outcome.status]
    )


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


def _instance_edit(
    agent: str | None,
    executable: str | None,
    args: list[str] | None,
    env: list[str] | None,
    unset_env: list[str] | None,
    cwd: str | None,
    clear_cwd: bool,
    resume_args: list[str] | None,
) -> list[InstanceEdit]:
    options = (executable, args, env, unset_env, cwd, resume_args)
    if agent is None:
        if clear_cwd or any(o is not None for o in options):
            raise ConfigError("instance options need --harness to say which harness they apply to")
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
        )
    ]


AgentOpt = Annotated[str | None, typer.Option("--harness", help="Harness to configure for this profile (e.g. claude).")]
ExecutableOpt = Annotated[str | None, typer.Option(help="Executable or wrapper script: absolute path, or a name found on the instance's PATH.")]
ArgOpt = Annotated[list[str] | None, typer.Option("--arg", help="Argument passed to the executable. Repeat for several; replaces the existing list.")]
EnvOpt = Annotated[list[str] | None, typer.Option("--env", help="NAME=VALUE for the instance's environment. Repeatable; merged.")]
UnsetEnvOpt = Annotated[list[str] | None, typer.Option("--unset-env", help="Remove a variable from the instance's environment. Repeatable.")]
CwdOpt = Annotated[str | None, typer.Option("--cwd", help="Working directory (absolute path).")]
ClearCwdOpt = Annotated[bool, typer.Option("--clear-cwd", help="Remove the instance's working directory.")]
ResumeOpt = Annotated[list[str] | None, typer.Option("--resume-arg", help="Argument used to resume a session. Repeatable; replaces the list.")]


@profile_app.command("list")
def profile_list(as_json: JsonOption = False) -> None:
    """List profiles, their default harness and configured harnesses."""
    try:
        profiles = list_profiles()
    except ConfigError as exc:
        raise _fail(str(exc), as_json)
    if as_json:
        emit_json({"profiles": profiles})
        return
    if not profiles:
        typer.echo("No profiles. Add one with `commodore profile add <name> --harness claude`.")
    for profile in profiles:
        harnesses = ", ".join(
            f"{name} (default)" if name == profile["default_harness"] else name for name in profile["harnesses"]
        )
        typer.echo(f"{profile['name']}: {harnesses or 'no harnesses'}")


@profile_app.command("add")
def profile_add(
    name: str,
    agent: AgentOpt = None,
    default_harness: Annotated[str | None, typer.Option(help="Default harness. Defaults to the only harness, if one.")] = None,
    executable: ExecutableOpt = None,
    arg: ArgOpt = None,
    env: EnvOpt = None,
    cwd: CwdOpt = None,
    resume_arg: ResumeOpt = None,
) -> None:
    """Create a profile. Use --harness (with the instance options) to configure its first harness."""
    try:
        edits = _instance_edit(agent, executable, arg, env, None, cwd, False, resume_arg)
        add_profile(name, edits, default_harness)
    except ConfigError as exc:
        raise _fail(str(exc))
    typer.echo(f"Added profile {name}.")


@profile_app.command("edit")
def profile_edit(
    name: str,
    agent: AgentOpt = None,
    default_harness: Annotated[str | None, typer.Option(help="Set the profile's default harness.")] = None,
    remove_harness: Annotated[list[str] | None, typer.Option(help="Remove a harness from the profile. Repeatable.")] = None,
    executable: ExecutableOpt = None,
    arg: ArgOpt = None,
    env: EnvOpt = None,
    unset_env: UnsetEnvOpt = None,
    cwd: CwdOpt = None,
    clear_cwd: ClearCwdOpt = False,
    resume_arg: ResumeOpt = None,
) -> None:
    """Change a profile. With --harness, create or change that harness's configuration."""
    try:
        edits = _instance_edit(agent, executable, arg, env, unset_env, cwd, clear_cwd, resume_arg)
        edit_profile(name, edits, default_harness, remove_harness)
    except ConfigError as exc:
        raise _fail(str(exc))
    typer.echo(f"Updated profile {name}.")


@profile_app.command("check")
def profile_check(
    name: str,
    agent: Annotated[str | None, typer.Option("--harness", help="Harness to check. Defaults to the profile's default.")] = None,
    as_json: JsonOption = False,
) -> None:
    """Verify a harness resolves for the profile. Never falls back to another harness or profile."""
    try:
        resolved = resolve_agent(name, agent)
    except AgentResolutionError as exc:
        raise _fail(str(exc), as_json)
    if as_json:
        emit_json(
            {
                "profile": resolved.profile,
                "harness": resolved.agent,
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
            if exc.details.get("available_harnesses"):
                typer.echo("Available harnesses: " + ", ".join(exc.details["available_harnesses"]), err=True)
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
    archive_tasks: Annotated[bool, typer.Option("--archive-tasks", help="When changing the profile: mark the repository's tasks archived (files and sessions are kept).")] = False,
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
                        Choice("archive-tasks", "Archive them (files and sessions are kept)"),
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


@tasks_app.command("list")
def tasks_list(as_json: JsonOption = False) -> None:
    """List tasks."""
    try:
        with open_state() as conn:
            tasks = list_user_tasks(conn)
    except _TASK_ERRORS as exc:
        raise _association_failure(exc, as_json)
    if as_json:
        emit_json({"tasks": [t.to_dict() for t in tasks]})
        return
    if not tasks:
        typer.echo("No tasks. Open a GitHub issue or PR with `commodore open <url>`.")
    for t in tasks:
        typer.echo(f"{t.id}  {t.state:<8}  {t.profile:<10}  {t.title}")


@tasks_app.command("show")
def tasks_show(task: str, as_json: JsonOption = False) -> None:
    """Show a task and its sessions."""
    try:
        with open_state() as conn:
            found = get_user_task(conn, task)
            sessions = sessions_for_task(conn, found.id)
            github = github_details(conn, found.id)
    except _TASK_ERRORS as exc:
        raise _association_failure(exc, as_json)
    if as_json:
        emit_json(
            {
                "task": found.to_dict(),
                "github": github,
                "sessions": [s.to_dict() for s in sessions],
            }
        )
        return
    typer.echo(f"{found.id}: {found.title}")
    if found.description:
        typer.echo(f"  {found.description}")
    typer.echo(f"  state: {found.state}   profile: {found.profile}   harness: {found.agent or '-'}")
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
    for s in sessions:
        where = f"{s.terminal.adapter}:{s.terminal.workspace_id}" if s.terminal else "no terminal"
        typer.echo(f"  session {s.id}: {s.agent} [{s.state}] {where}")


@tasks_app.command("archive")
def tasks_archive(task: str, as_json: JsonOption = False) -> None:
    """Archive a task: metadata only. Files and the session record stay; `open` refuses it until unarchived."""
    try:
        with open_state() as conn:
            result = archive_task(conn, task)
    except _TASK_ERRORS as exc:
        raise _association_failure(exc, as_json)
    if as_json:
        emit_json({"task": result.task.to_dict(), "changed": result.changed})
        return
    typer.echo(
        f"Archived task {result.task.id}. Nothing was deleted or closed; `commodore tasks unarchive {result.task.id}` undoes it."
        if result.changed else f"Task {result.task.id} is already archived."
    )


@tasks_app.command("unarchive")
def tasks_unarchive(task: str, as_json: JsonOption = False) -> None:
    """Restore an archived task to the state it had, so it can be opened again."""
    try:
        with open_state() as conn:
            result = unarchive_task(conn, task)
    except _TASK_ERRORS as exc:
        raise _association_failure(exc, as_json)
    if as_json:
        emit_json({"task": result.task.to_dict(), "changed": result.changed})
        return
    typer.echo(f"Unarchived task {result.task.id} (state {result.task.state}).")


NEEDS_CHOICE = frozenset({
    "unknown_repository_profile", "harness_selection_needed", "ambiguous_repository_location",
    "confirmation_required",
})
"""Refusals that mean "this needs an answer from a person, and there is no terminal to ask on"."""


def _run_session_command(
    action, task: str, terminal: str | None, as_json: bool, *, interactive: bool = True, on_needs_choice=None
) -> None:
    """Shared body of open, resume, configure and integrate: load config and state, run `action`, print the result."""
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
        if on_needs_choice is not None and getattr(exc, "code", None) in NEEDS_CHOICE and on_needs_choice(adapter):
            return
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
                "prompt_submitted": result.prompt_submitted,
                "resume_state": result.resume_state,
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
    }[result.action]
    what = "the management skill" if result.task.is_maintenance else f"task {result.task.id}"
    typer.echo(f"{verb} {what} with {result.session.agent} ({result.session.profile}){where}.")
    if result.notice:
        typer.echo(result.notice, err=True)


_OfflineOpt = Annotated[bool, typer.Option("--offline", help="Do not ask GitHub for the repository ID.")]

@app.command("open")
def open_command(
    task: str,
    agent: Annotated[str | None, typer.Option("--harness", help="Harness to run; must belong to the task's profile.")] = None,
    terminal: _TerminalOpt = None,
    offline: _OfflineOpt = False,
    picker_surface: Annotated[bool, typer.Option(
        "--picker-surface",
        help="With no terminal to ask on, run this same command in a new terminal surface that has one, when it needs a choice.",
    )] = False,
    as_json: JsonOption = False,
) -> None:
    """Open a GitHub issue or pull request URL, or the ID of a task made from one: start its session, or focus the one it already has.

    Nothing else is accepted. A URL finds the task by GitHub's stable IDs (creating it, and cloning the repository after asking, on first
    use). The harness is started on its invocation of Commodore on the URL, submitted at launch, in the repository's checkout; the launcher checks nothing out and changes nothing
    there.
    """
    fallback = None
    if picker_surface and not as_json and not is_interactive():
        argv = [sys.executable, "-m", "commodore_launcher", "open", task]
        for flag, value in (("--harness", agent), ("--terminal", terminal)):
            if value:
                argv += [flag, value]
        if offline:
            argv.append("--offline")
        fallback = lambda adapter: _open_picker_surface(adapter, argv)  # noqa: E731
    if is_issue_reference(task):
        _run_session_command(
            lambda conn, config, adapter, prompter: open_github(
                conn, task, config=config, adapter=adapter, prompter=prompter, agent=agent, offline=offline,
            ),
            task, terminal, as_json, on_needs_choice=fallback,
        )
        return

    def open_by_id(conn, config, adapter, prompter):
        if not is_task_id_shaped(task):
            raise needs_issue_or_pr_url_error(task)
        found = get_user_task(conn, task)  # the maintenance task is not a user's to open
        return open_task(
            conn, found.id, config=config, adapter=adapter, prompter=prompter, agent=agent, offline=offline,
        )

    _run_session_command(open_by_id, task, terminal, as_json, on_needs_choice=fallback)


def _open_picker_surface(adapter, argv: list[str]) -> bool:
    """Run `argv` (the same `open`, without `--picker-surface`) in a temporary terminal surface with a TTY, where the
    picker can ask. False when the adapter cannot, so the original refusal is shown instead."""
    try:
        adapter.run_interactive("Commodore Launcher: choose", str(Path.cwd()), argv)
    except LauncherError:
        return False
    typer.echo("This needs a choice from you. Continue in the new terminal surface; close it when it is done.", err=True)
    return True


@skill_app.command("path")
def skill_path(as_json: JsonOption = False) -> None:
    """Print the directory of the bundled `commodore-launcher` management skill, to copy it into a harness's skills directory."""
    try:
        path = skill_directory()
    except LauncherError as exc:
        raise _association_failure(exc, as_json)
    if as_json:
        emit_json({"name": "commodore-launcher", "path": str(path)})
    else:
        typer.echo(str(path))


@app.command()
def configure(
    request: Annotated[str | None, typer.Argument(help="What you want done. Used when the session is first started.")] = None,
    repo: Annotated[str | None, typer.Option("--repo", help="Repository for the maintenance session (default: a directory the launcher owns).")] = None,
    profile: Annotated[str | None, typer.Option("--profile", help="Profile for a repository that has none yet.")] = None,
    agent: Annotated[str | None, typer.Option("--harness", help="Harness to run; must belong to the repository's profile.")] = None,
    terminal: _TerminalOpt = None,
    as_json: JsonOption = False,
) -> None:
    """Launch a harness with the management skill to help configure Commodore Launcher.

    Starts (or finds again) one maintenance session and runs its harness on the management skill: the
    repository's profile and the profile's harnesses decide who runs. The harness is started on the management
    skill and your request, submitted at launch.
    """
    _run_session_command(_maintenance_action(repo, profile, request, agent), "configure", terminal, as_json)


def _maintenance_action(
    repo: str | None, profile: str | None, request: str | None, agent: str | None, title: str | None = None,
    default_request: str | None = None,
):
    def action(conn, config, adapter, prompter):
        extra = {"title": title, "default_request": default_request} if title and default_request else {}
        prepared = prepare_task(
            conn, repo=repo, profile=profile, request=request, available_profiles=sorted(config.profiles),
            prompter=prompter, **extra,
        )
        return open_task(  # a first open starts the management skill on its request
            conn, prepared.task.id, config=config, adapter=adapter, prompter=prompter, agent=agent, offline=True,
        )

    return action


@integrate_app.command("gh-dash")
def integrate_gh_dash(
    repo: Annotated[str | None, typer.Option("--repo", help="Repository for the maintenance session (default: a directory the launcher owns).")] = None,
    profile: Annotated[str | None, typer.Option("--profile", help="Profile for a repository that has none yet.")] = None,
    agent: Annotated[str | None, typer.Option("--harness", help="Harness to run; must belong to the repository's profile.")] = None,
    print_prompt: Annotated[bool, typer.Option("--print-prompt", help="Only print the integration prompt, to use by hand.")] = False,
    terminal: _TerminalOpt = None,
    as_json: JsonOption = False,
) -> None:
    """Have a harness set up the gh-dash shortcut for issues and PRs.

    Commodore Launcher does not edit gh-dash's configuration. This starts one maintenance session, launching a harness
    with the management skill, and starts it on the integration request, submitted at launch. The harness inspects
    your gh-dash, keeps what you have, adds the shortcut without overwriting any, and validates and reports.
    """
    if print_prompt:
        if as_json:
            emit_json({"prompt": integration_prompt()})
        else:
            typer.echo(integration_prompt())
        return
    if not gh_dash_detected():
        raise _association_failure(
            LauncherError(
                "gh_dash_not_found",
                "gh-dash was not found (as `gh-dash` or a `gh` extension). Install it with `gh extension install "
                "dlvhdr/gh-dash`, or add the keybindings by hand: see docs/gh-dash.md (`--print-prompt` prints the "
                "instructions a harness would follow).",
            ),
            as_json,
        )
    _run_session_command(
        _maintenance_action(repo, profile, None, agent, GH_DASH_TASK_TITLE, integration_prompt()),
        "integrate gh-dash", terminal, as_json,
    )


@app.command("resume")
def resume_command(
    task: str,
    force: Annotated[bool, typer.Option("--force", help="Resume in a new terminal session even if the old one looks alive.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Confirm --force without asking.")] = False,
    terminal: _TerminalOpt = None,
    offline: _OfflineOpt = False,
    as_json: JsonOption = False,
) -> None:
    """Resume a task's harness conversation where it can't be focused (harness exited, terminal session gone).

    A harness that cannot resume (no recorded conversation ID, or no resume mechanism) is started on a fresh conversation
    on the same session instead."""
    def resume_by_id(conn, config, adapter, prompter):
        if not is_task_id_shaped(task):
            raise needs_issue_or_pr_url_error(task)
        found = get_user_task(conn, task)
        return resume_task(
            conn, found.id, force=force, confirmed=yes, config=config, adapter=adapter, prompter=prompter, offline=offline
        )

    _run_session_command(resume_by_id, task, terminal, as_json)


@app.command()
def setup(
    answers: Annotated[Path | None, typer.Option("--answers", help="JSON file of answers; nothing is asked or detected.")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Apply without asking for confirmation (needed with --answers when not on a terminal).")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the diff and write nothing.")] = False,
    as_json: JsonOption = False,
) -> None:
    """Interactive setup wizard: profiles, harnesses, defaults. Safe to re-run; never resets config."""
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
    if prompter is not None and not as_json and not dry_run and (result.applied or not result.changed):
        _offer_gh_dash(prompter)
    if as_json:
        emit_json(result.to_dict())
    elif parsed is not None and prompter is None:
        typer.echo(result.diff or "Nothing to change.")
        typer.echo(f"Wrote {result.path}." if result.applied else "Nothing was written.")


def _offer_gh_dash(prompter) -> None:
    """After setup: if gh-dash is installed, offer the harness-assisted integration. Default No; never required, and a
    failure here does not undo or fail setup."""
    if not gh_dash_detected():
        return
    if not prompter.confirm(
        "gh-dash is installed. Have a harness add a Commodore Launcher shortcut to it? (You review its prompt first.)",
        default=False,
    ):
        typer.echo("Skipped. Later: `commodore integrate gh-dash`.")
        return
    try:
        _run_session_command(
            _maintenance_action(None, None, None, None, GH_DASH_TASK_TITLE, integration_prompt()), "integrate gh-dash", None, False
        )
    except typer.Exit:
        typer.echo("Setup is finished. Retry the integration with `commodore integrate gh-dash`.", err=True)


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


def _copy_old_home() -> None:
    """Runs before anything that could create the new home. The one line goes to stderr, so `--json` stays clean.
    A failed copy stops the command: once anything creates the new home the copy would never be tried again."""
    try:
        copied = copy_old_home(migrations.prepare_copied_config, copied_state.archive_copied_tasks)
    except OSError as exc:
        where = f" {exc.filename}:" if exc.filename else ""
        typer.echo(
            f"error: could not copy the old home to the new one:{where} {exc.strerror or exc}. Fix that and run the "
            "command again, or set COMMODORE_LAUNCHER_HOME to start with a fresh home. Nothing has been changed.",
            err=True,
        )
        raise typer.Exit(1)
    if copied is not None:
        line = (
            f"Copied {copied.old} to {copied.new} (Commodore Launcher's home). The old directory is untouched and "
            "will not be copied again. Its tasks were archived; `commodore tasks unarchive <id>` brings one back."
        )
        typer.echo(f"{line} {copied.config_note}" if copied.config_note else line, err=True)
        if copied.worktrees:
            typer.echo("Task worktrees the old launcher created are left on disk; nothing removes them:", err=True)
            for path in copied.worktrees:
                typer.echo(f"  {path}", err=True)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    debug: Annotated[bool, typer.Option("--debug", help="Trace decisions to stderr and the log. Goes before the command: commodore --debug doctor.")] = False,
) -> None:
    """Commodore Launcher."""
    _copy_old_home()
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
    """`commodore` alone: offer setup on a first run, otherwise show help."""
    if not config_path().exists() and is_interactive():
        typer.echo("No configuration found.")
        try:
            wanted = make_prompter().confirm("Set up Commodore Launcher now?")
        except SetupCancelled:
            wanted = False
        if wanted:
            ctx.invoke(setup, answers=None, yes=False, dry_run=False, as_json=False)
            return
        typer.echo("Skipped. Run `commodore setup` whenever you want.\n")
    elif not config_path().exists():
        typer.echo("No configuration found. Run `commodore setup`.\n")
    typer.echo(ctx.get_help())


if __name__ == "__main__":
    app()
