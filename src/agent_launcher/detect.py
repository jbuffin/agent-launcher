"""What `setup` can see about this machine. Read-only, and never authoritative.

Tool probing is `doctor.tool_checks`, so the wizard and the doctor cannot disagree.
Existing agent configuration directories (`~/.claude*`, `~/.codex*`) are listed by
name only: their contents are never opened, read, copied or written.
"""

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path

from agent_launcher import doctor
from agent_launcher.config import Config, builtin_agent_types
from agent_launcher.doctor import (
    Check,
    Runner,
    Which,
    check_python,
    tool_checks,
)

IDENTITY_ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "copilot": "COPILOT_HOME"}
"""The variable that points each agent at its own configuration (and so its identity)."""

CONFIG_DIR_PREFIX = {"claude": ".claude", "codex": ".codex"}


@dataclass
class Detection:
    os: str
    checks: list[Check]
    agent_paths: dict[str, str | None]
    """Agent type name -> where its default executable was found on PATH, if anywhere."""
    wrappers: dict[str, str] = field(default_factory=dict)
    """Other executables on PATH named like an agent (`claude-work`, `codex-personal`), by name."""
    config_dirs: dict[str, list[str]] = field(default_factory=dict)
    """Agent type name -> names of candidate directories in HOME, e.g. `.claude-personal`."""


def find_wrappers(path_env: str, known: dict[str, str]) -> dict[str, str]:
    """Executables on PATH that start with an agent's executable name but are not it."""
    prefixes = set(known.values())
    found: dict[str, str] = {}
    for directory in path_env.split(os.pathsep):
        if not directory:
            continue
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name in prefixes or name in found:
                continue
            if any(name.startswith(prefix) and name[len(prefix)] in "-_." for prefix in prefixes if len(name) > len(prefix)):
                try:
                    if entry.is_file() and os.access(entry.path, os.X_OK):
                        found[name] = entry.path
                except OSError:
                    continue
    return found


def find_config_dirs(home: Path) -> dict[str, list[str]]:
    """Names of `~/.claude*` and `~/.codex*` directories. Only HOME is listed, nothing inside them."""
    try:
        names = sorted(e.name for e in os.scandir(home) if e.is_dir())
    except OSError:
        return {}
    return {
        agent: [n for n in names if n.startswith(prefix)]
        for agent, prefix in CONFIG_DIR_PREFIX.items()
        if any(n.startswith(prefix) for n in names)
    }


def detect(
    config: Config | None = None,
    *,
    which: Which | None = None,
    runner: Runner | None = None,
    home: Path | None = None,
    path_env: str | None = None,
    os_name: str | None = None,
    python_version: tuple[int, ...] | None = None,
) -> Detection:
    which = which or doctor._default_which
    runner = runner or doctor.run_command
    types = config.agent_types if config is not None else builtin_agent_types()
    executables = {name: t.executable for name, t in types.items()}
    return Detection(
        os=os_name or f"{platform.system()} {platform.release()}",
        checks=[check_python(python_version), *tool_checks(config, which, runner)],
        agent_paths={name: which(exe) for name, exe in sorted(executables.items())},
        wrappers=find_wrappers(os.environ.get("PATH", "") if path_env is None else path_env, executables),
        config_dirs=find_config_dirs(home or Path.home()),
    )
