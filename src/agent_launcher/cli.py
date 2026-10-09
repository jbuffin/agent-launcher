"""Command-line interface. Presentation only: logic lives in the other modules."""

import json
from typing import Annotated, Any

import typer

from agent_launcher import __version__
from agent_launcher.agents import AgentResolutionError, resolve_agent
from agent_launcher.config import ConfigError, effective_config, read_raw, validate_config
from agent_launcher.paths import config_path
from agent_launcher.profiles import (
    PROMPT_MODES,
    SKILL_INVOCATIONS,
    InstanceEdit,
    add_profile,
    edit_profile,
    list_profiles,
)

app = typer.Typer(help="Launch AI coding agents against issues, PRs and local tasks.", no_args_is_help=True)
config_app = typer.Typer(help="Inspect and validate configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")
profile_app = typer.Typer(help="Manage profiles and their agent instances.", no_args_is_help=True)
app.add_typer(profile_app, name="profile")

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


@app.callback()
def main() -> None:
    """Agent Launcher."""


if __name__ == "__main__":
    app()
