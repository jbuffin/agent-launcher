"""`agent-launcher doctor`: read-only health checks.

Config and profile checks reuse `validate_config`, `load_config` and `resolve_agent`, so the
doctor reports exactly what a launch would hit. External tools are probed with short,
read-only commands (`--version`, `gh auth status`, `gh extension list`) under a timeout.
Process execution and PATH lookup are injectable so tests never depend on what is installed.
"""

import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agent_launcher import state, templates
from agent_launcher.agents import AgentResolutionError, resolve_agent
from agent_launcher.config import Config, ConfigError, builtin_agent_types, load_config, validate_config
from agent_launcher.logs import trace
from agent_launcher.paths import config_path, launcher_home
from agent_launcher.redact import redact_text
from agent_launcher.workflows import BUILTIN_FALLBACK_ID, validate_workflows, load_workflows

Status = Literal["pass", "warn", "fail"]
MIN_PYTHON = (3, 11)
TOOL_TIMEOUT_SECONDS = 5.0


class CommandError(Exception):
    """A probe could not run to completion (missing binary, timeout, OS error)."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], float], CommandResult]
Which = Callable[[str], str | None]


def run_command(argv: Sequence[str], timeout: float) -> CommandResult:
    """Run an argv list (never a shell) with a timeout and no stdin."""
    try:
        done = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise CommandError(exc.strerror or str(exc)) from exc
    return CommandResult(done.returncode, done.stdout or "", done.stderr or "")


@dataclass(frozen=True)
class Check:
    id: str
    name: str
    status: Status
    detail: str
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class DoctorReport:
    checks: list[Check]

    @property
    def ok(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)

    def count(self, status: Status) -> int:
        return sum(1 for c in self.checks if c.status == status)

    def summary(self) -> dict[str, int]:
        """Counts, under keys that are not secret-looking names (`pass` would be redacted)."""
        return {"passed": self.count("pass"), "warnings": self.count("warn"), "failures": self.count("fail")}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary(),
            "checks": [c.to_dict() for c in self.checks],
        }


def _check(id: str, name: str, status: Status, detail: str, remediation: str | None = None) -> Check:
    home = str(Path.home())
    return Check(id, name, status, redact_text(detail, home), redact_text(remediation, home) if remediation else None)


def check_python(version: tuple[int, ...] | None = None) -> Check:
    have = tuple(version or sys.version_info[:3])
    text = ".".join(str(p) for p in have)
    if have[:2] >= MIN_PYTHON:
        return _check("python", "Python version", "pass", f"Python {text}")
    need = ".".join(str(p) for p in MIN_PYTHON)
    return _check("python", "Python version", "fail", f"Python {text}; {need}+ is required",
                  f"Install Python {need} or newer and reinstall agent-launcher with it.")


def check_config(path: Path | None = None) -> Check:
    report = validate_config(path)
    if not report.exists:
        return _check("config", "Configuration", "pass", f"no config file at {report.path}; defaults apply")
    if report.errors:
        detail = "; ".join(f"{e.field}: {e.message}" for e in report.errors)
        return _check("config", "Configuration", "fail", detail,
                      "Fix the fields above in config.json (see `agent-launcher config validate`). "
                      "One invalid profile makes the whole config unusable.")
    if report.unknown_fields:
        return _check("config", "Configuration", "warn",
                      f"{report.path} is valid; unknown fields: {', '.join(report.unknown_fields)}",
                      "Remove the unknown fields, or upgrade agent-launcher if they come from a newer release.")
    return _check("config", "Configuration", "pass", f"{report.path} is valid")


def _template_check(config: Config, name: str) -> Check | None:
    broken = templates.config_problem(config)
    if broken:
        return _check("workflows", name, "fail", broken, "Create the template file, or remove or fix prompt_template in config.json.")
    advice = templates.config_warning(config)
    if advice:
        return _check("workflows", name, "warn", advice,
                      "Use only variables every task has in the global template, or give the workflows that need $task_url their own.")
    return None


def check_workflows(config: Config | None, path: Path | None = None) -> Check:
    report = validate_workflows(path)
    name = "Workflows"
    if not report.exists:
        wanted = config.workflow_routing.fallback if config is not None else BUILTIN_FALLBACK_ID
        if wanted != BUILTIN_FALLBACK_ID:
            return _check("workflows", name, "fail", f"workflow_routing.fallback is {wanted!r}, but there is no workflows.json",
                          "Define that workflow, or set workflow_routing.fallback back to \"default\".")
        if config is not None:
            early = _template_check(config, name)
            if early:
                return early
        return _check("workflows", name, "pass", f"no workflows.json at {report.path}; every task uses the fallback")
    if report.errors:
        return _check("workflows", name, "fail", "; ".join(f"{e.field}: {e.message}" for e in report.errors),
                      "Fix the fields above in workflows.json (see `agent-launcher config validate`). "
                      "An invalid file stops routing, so tasks cannot be opened until it is fixed.")
    file = load_workflows(path)
    if config is not None:
        early = _template_check(config, name)
        if early:
            return early
        wanted = config.workflow_routing.fallback
        if wanted != BUILTIN_FALLBACK_ID and wanted not in [w.id for w in file.workflows]:
            return _check("workflows", name, "fail", f"workflow_routing.fallback is {wanted!r}, which {report.path} does not define",
                          "Define that workflow, or set workflow_routing.fallback back to \"default\".")
        known = {a for p in config.profiles.values() for a in p.agents}
        stray = sorted({f"{w.id}->{w.preferred_agent}" for w in file.workflows if w.preferred_agent and w.preferred_agent not in known})
        if stray:
            return _check("workflows", name, "warn",
                          f"{report.path}: {len(file.workflows)} rules; preferred agent in no profile: {', '.join(stray)}",
                          "A preference for an agent a repository's profile lacks is ignored. Check the spelling.")
    return _check("workflows", name, "pass", f"{report.path}: {len(file.workflows)} rules, valid")


def _first_line(result: CommandResult) -> str:
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        if line.strip():
            return line.strip()
    return ""


def _probe_version(name: str, executable: str, runner: Runner) -> str:
    """Found-on-PATH detail with the version when the tool reports one."""
    try:
        result = runner([executable, "--version"], TOOL_TIMEOUT_SECONDS)
    except CommandError as exc:
        return f"found at {executable} (version not reported: {exc})"
    line = _first_line(result)
    if result.returncode != 0 or not line:
        return f"found at {executable} (version not reported)"
    return f"{line} ({executable})"


def _tool(id: str, name: str, command: str, severity: Status, hint: str, which: Which, runner: Runner) -> Check:
    found = which(command)
    if found is None:
        return _check(id, name, severity, f"`{command}` not found on PATH", hint)
    return _check(id, name, "pass", _probe_version(command, found, runner))


def check_gh_auth(which: Which, runner: Runner) -> Check:
    name = "GitHub authentication"
    gh = which("gh")
    if gh is None:
        return _check("gh-auth", name, "warn", "cannot check: `gh` not found", "Install the GitHub CLI first.")
    try:
        result = runner([gh, "auth", "status"], TOOL_TIMEOUT_SECONDS)
    except CommandError as exc:
        return _check("gh-auth", name, "warn", f"`gh auth status` did not complete: {exc}",
                      "Run `gh auth status` yourself to see what is wrong.")
    summary = " | ".join(
        line.strip() for line in (result.stdout + "\n" + result.stderr).splitlines() if line.strip()
    )[:300]
    if result.returncode == 0:
        return _check("gh-auth", name, "pass", summary or "authenticated")
    return _check("gh-auth", name, "warn", summary or "not authenticated", "Run `gh auth login`.")


def check_gh_dash(which: Which, runner: Runner) -> Check:
    """gh-dash is either a standalone binary or a `gh` extension (`gh dash`)."""
    name = "gh-dash"
    hint = "Optional. Install it with `gh extension install dlvhdr/gh-dash`."
    standalone = which("gh-dash")
    if standalone is not None:
        return _check("gh-dash", name, "pass", f"standalone binary at {standalone}")
    gh = which("gh")
    if gh is not None:
        try:
            result = runner([gh, "extension", "list"], TOOL_TIMEOUT_SECONDS)
        except CommandError as exc:
            return _check("gh-dash", name, "warn", f"could not list gh extensions: {exc}", hint)
        if result.returncode == 0 and any(
            "gh-dash" in line or "gh dash" in line for line in result.stdout.splitlines()
        ):
            return _check("gh-dash", name, "pass", "installed as a gh extension (`gh dash`)")
    return _check("gh-dash", name, "warn", "not found as a binary or a gh extension", hint)


def check_agent_types(config: Config | None, which: Which, runner: Runner) -> list[Check]:
    types = config.agent_types if config is not None else builtin_agent_types()
    checks = []
    for name in sorted(types):
        exe = types[name].executable
        found = which(exe)
        if found is None:
            checks.append(_check(f"agent:{name}", f"Agent {name}", "warn",
                                 f"`{exe}` not found on PATH",
                                 f"Install {name}, or point a profile at it with "
                                 f"`agent-launcher profile edit <profile> --agent {name} --executable <path>`."))
        else:
            checks.append(_check(f"agent:{name}", f"Agent {name}", "pass", _probe_version(name, found, runner)))
    return checks


def tool_checks(config: Config | None, which: Which, runner: Runner) -> list[Check]:
    """Probe git, gh (and its auth), cmux, gh-dash and the agent types. Shared with `setup`."""
    checks = [
        _tool("git", "git", "git", "fail", "Install git (https://git-scm.com).", which, runner),
        _tool("gh", "GitHub CLI", "gh", "warn", "Install it from https://cli.github.com.", which, runner),
        check_gh_auth(which, runner),
        _tool("cmux", "cmux", "cmux", "warn", "Install cmux; terminal workspaces need it.", which, runner),
        check_gh_dash(which, runner),
    ]
    checks.extend(check_agent_types(config, which, runner))
    return checks


def check_profiles(config: Config | None, config_error: str | None) -> list[Check]:
    if config is None:
        return [_check("profiles", "Profiles", "warn", f"not checked: configuration is unusable ({config_error})",
                       "Fix the configuration first.")]
    if not config.profiles:
        return [_check("profiles", "Profiles", "warn", "no profiles configured",
                       "Add one with `agent-launcher profile add <name> --agent claude`.")]
    checks = []
    for pname, profile in sorted(config.profiles.items()):
        if not profile.agents:
            checks.append(_check(f"profile:{pname}", f"Profile {pname}", "warn", "no agents configured",
                                 f"Add one with `agent-launcher profile edit {pname} --agent claude`."))
            continue
        if profile.default_agent is None:
            checks.append(_check(f"profile:{pname}", f"Profile {pname}", "warn", "no default agent",
                                 f"Set one with `agent-launcher profile edit {pname} --default-agent <agent>`."))
        for aname in sorted(profile.agents):
            id_, label = f"profile:{pname}:{aname}", f"Profile {pname}, agent {aname}"
            try:
                resolved = resolve_agent(pname, aname, config=config)
            except AgentResolutionError as exc:
                checks.append(_check(id_, label, "fail", str(exc),
                                     f"Fix the instance with `agent-launcher profile edit {pname} --agent {aname}`."))
            else:
                checks.append(_check(id_, label, "pass", f"resolves to {resolved.executable}"))
    return checks


def check_database(path: Path | None = None) -> Check:
    path = path or launcher_home() / "state.db"
    name = "Database"
    reset = "Restore it from a backup, or move it aside and let agent-launcher recreate it."
    try:
        found = state.inspect(path)
    except sqlite3.Error as exc:
        return _check("database", name, "fail", f"{path} cannot be read: {exc}", reset)
    if not found.exists:
        return _check("database", name, "pass", f"{path} not created yet")
    if found.too_new:
        return _check("database", name, "fail",
                      f"{path} is schema version {found.version}; this release understands up to {state.SCHEMA_VERSION}",
                      "Upgrade agent-launcher. The database is left untouched.")
    if found.integrity:
        return _check("database", name, "fail", f"{path} failed its integrity check: {found.integrity[0]}", reset)
    if found.missing_tables:
        return _check("database", name, "fail",
                      f"{path} is missing tables for schema version {found.version}: {', '.join(found.missing_tables)}",
                      reset)
    if found.needs_migration or found.version == 0:
        return _check("database", name, "warn",
                      f"{path} is schema version {found.version}; it will be upgraded to {state.SCHEMA_VERSION} on next use")
    return _check("database", name, "pass", f"{path}: schema version {found.version}, integrity ok")


def _default_which(name: str) -> str | None:
    return shutil.which(name)


def run_doctor(
    *,
    runner: Runner | None = None,
    which: Which | None = None,
    config_file: Path | None = None,
    db_path: Path | None = None,
    python_version: tuple[int, ...] | None = None,
) -> DoctorReport:
    runner = runner or run_command
    which = which or _default_which
    path = config_file or config_path()
    trace("doctor started", config=str(path))

    config: Config | None = None
    config_error: str | None = None
    try:
        config = load_config(path)
    except ConfigError as exc:
        config_error = str(exc)

    checks = [check_python(python_version), check_config(path)]
    checks.extend(tool_checks(config, which, runner))
    checks.extend(check_profiles(config, config_error))
    checks.append(check_workflows(config))
    checks.append(check_database(db_path))
    report = DoctorReport(checks)
    trace("doctor finished", ok=report.ok, **report.summary())
    return report
