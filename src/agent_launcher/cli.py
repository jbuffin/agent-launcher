"""Command-line interface. Presentation only: logic lives in the other modules."""

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from agent_launcher import __version__
from agent_launcher.agents import AgentResolutionError, resolve_agent
from agent_launcher.associations import AssociationError, ensure_profile, set_profile
from agent_launcher.config import ConfigError, LogSettings, effective_config, load_config, read_raw, validate_config
from agent_launcher.diagnostics import default_archive_name, export_diagnostics
from agent_launcher.doctor import run_doctor
from agent_launcher.interaction import QuestionaryPrompter, SetupCancelled
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
from agent_launcher.state import StateError, open_state
from agent_launcher.wizard import SetupError, load_answers, run_setup

app = typer.Typer(help="Launch AI coding agents against issues, PRs and local tasks.")
config_app = typer.Typer(help="Inspect and validate configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")
profile_app = typer.Typer(help="Manage profiles and their agent instances.", no_args_is_help=True)
app.add_typer(profile_app, name="profile")

diagnostics_app = typer.Typer(help="Export sanitised diagnostics.", no_args_is_help=True)
app.add_typer(diagnostics_app, name="diagnostics")

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
    failed = not report.valid or (strict and bool(report.unknown_fields))
    if as_json:
        emit_json(report.to_dict())
    else:
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


def _association_failure(exc: AssociationError | RepositoryError | StateError | ConfigError, as_json: bool) -> typer.Exit:
    """Print a repository/association problem (structured under --json) and return Exit(1)."""
    if isinstance(exc, AssociationError):
        payload = exc.to_dict()
    else:
        default_code = "config_invalid" if isinstance(exc, ConfigError) else "state_unavailable"
        payload = {"error": {"code": getattr(exc, "code", default_code), "message": str(exc)}}
    if as_json:
        emit_json(payload)
    else:
        typer.echo(f"error: {payload['error']['message']}", err=True)
        if isinstance(exc, AssociationError) and exc.details.get("available_profiles"):
            typer.echo("Available profiles: " + ", ".join(exc.details["available_profiles"]), err=True)
    return typer.Exit(1)


@profile_app.command("set")
def profile_set(
    repository: Annotated[str, typer.Argument(help="Local path, owner/name, or GitHub URL.")],
    profile: str,
    force: Annotated[bool, typer.Option("--force", help="Change an existing association. Safe reassignment (sessions, worktrees) is coming in a later release.")] = False,
    offline: Annotated[bool, typer.Option("--offline", help="Do not ask GitHub for the repository ID.")] = False,
    as_json: JsonOption = False,
) -> None:
    """Associate a repository with a profile. The only way an association changes."""
    try:
        known = sorted(load_config().profiles)
        if profile not in known:
            raise AssociationError("unknown_profile", f"No profile named {profile!r}.", available_profiles=known)
        identity = identify_reference(repository, fetch_github=not offline)
        with open_state() as conn:
            result = set_profile(conn, identity, profile, force=force)
    except (AssociationError, RepositoryError, StateError, ConfigError) as exc:
        raise _association_failure(exc, as_json)
    if as_json:
        emit_json(
            {
                "repository": identity.to_dict(),
                "profile": result.profile,
                "previous_profile": result.previous,
                "changed": result.changed,
            }
        )
    elif not result.changed:
        typer.echo(f"{identity.describe()} already uses profile {profile}.")
    else:
        typer.echo(f"{identity.describe()} now uses profile {profile}.")


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
