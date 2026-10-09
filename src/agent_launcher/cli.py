"""Command-line interface. Presentation only: logic lives in the other modules."""

import json
from typing import Annotated, Any

import typer

from agent_launcher import __version__
from agent_launcher.config import ConfigError, effective_config, read_raw, validate_config
from agent_launcher.paths import config_path

app = typer.Typer(help="Launch AI coding agents against issues, PRs and local tasks.", no_args_is_help=True)
config_app = typer.Typer(help="Inspect and validate configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")

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


@app.callback()
def main() -> None:
    """Agent Launcher."""


if __name__ == "__main__":
    app()
