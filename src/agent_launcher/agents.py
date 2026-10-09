"""Resolving a profile's agent instance to something that can be executed.

Safety rule (SPEC §6): resolution only ever looks at the named profile's own
instance. If that instance is missing or cannot be verified, resolution fails;
no other instance, profile or executable is substituted.

Nothing here starts a process. Verifying means finding the executable explicitly
(an absolute path, or a lookup on the PATH of the instance's own environment) and
checking it is an executable file.
"""

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_launcher.config import Config, ConfigError, load_config


class AgentResolutionError(Exception):
    """The requested agent cannot be resolved or verified. Nothing else is used instead."""


@dataclass(frozen=True)
class ResolvedAgent:
    profile: str
    agent: str
    adapter: str
    executable: Path
    argv: tuple[str, ...]
    """`executable` followed by the instance's args: pass straight to a subprocess, never a shell."""
    env: dict[str, str]
    """Full environment for the process: the caller's environment plus the instance's `env`."""
    working_directory: Path | None
    resume_args: tuple[str, ...]
    prompt_mode: str
    skill_invocation: str


def _expand(value: str, home: str) -> str:
    if value == "~" or value.startswith("~/"):
        return home + value[1:]
    return value


def _instance_env(base: Mapping[str, str], overrides: Mapping[str, str]) -> dict[str, str]:
    """The caller's environment plus the instance's, with a leading `~` expanded in the latter.

    `~` means the instance's HOME if it sets one, else the caller's. In PATH each entry is expanded.
    """
    caller_home = base.get("HOME") or str(Path.home())
    home = _expand(overrides["HOME"], caller_home) if "HOME" in overrides else caller_home
    env = dict(base)
    for key, value in overrides.items():
        if key == "HOME":
            env[key] = home
        elif key == "PATH":
            env[key] = ":".join(_expand(part, home) for part in value.split(":"))
        else:
            env[key] = _expand(value, home)
    return env


def _find_executable(value: str, env: Mapping[str, str], where: str) -> Path:
    value = _expand(value, env.get("HOME") or str(Path.home()))
    if "/" in value:
        path = Path(value)
        if not path.is_absolute():
            raise AgentResolutionError(
                f"{where}: executable {value!r} is a relative path; use an absolute path "
                "or a bare command name found on PATH"
            )
    else:
        found = shutil.which(value, path=env.get("PATH", ""))
        if found is None:
            raise AgentResolutionError(
                f"{where}: executable {value!r} was not found on the PATH of this instance's environment"
            )
        path = Path(found)
    if not path.exists():
        raise AgentResolutionError(f"{where}: executable {str(path)!r} does not exist")
    if not path.is_file():
        raise AgentResolutionError(f"{where}: executable {str(path)!r} is not a file")
    if not os.access(path, os.X_OK):
        raise AgentResolutionError(f"{where}: executable {str(path)!r} is not executable")
    return path


def resolve_agent(
    profile: str,
    agent: str | None = None,
    *,
    config: Config | None = None,
    base_env: Mapping[str, str] | None = None,
) -> ResolvedAgent:
    """Resolve `agent` (default: the profile's default agent) for `profile`.

    `base_env` is the environment the instance inherits (default `os.environ`).
    Raises AgentResolutionError with a diagnostic; callers must stop, not retry elsewhere.
    """
    if config is None:
        try:
            config = load_config()
        except ConfigError as exc:
            raise AgentResolutionError(f"configuration is not usable: {exc}") from exc
    env_in = os.environ if base_env is None else base_env

    prof = config.profiles.get(profile)
    if prof is None:
        known = ", ".join(sorted(config.profiles)) or "none"
        raise AgentResolutionError(f"profile {profile!r} does not exist (profiles: {known})")

    if agent is None:
        agent = prof.default_agent
        if agent is None:
            raise AgentResolutionError(
                f"profile {profile!r} has no default agent; choose one with "
                f"`agent-launcher profile edit {profile} --default-agent <agent>`"
            )
    where = f"profile {profile!r}, agent {agent!r}"

    instance = prof.agents.get(agent)
    if instance is None:
        have = ", ".join(sorted(prof.agents)) or "none"
        raise AgentResolutionError(
            f"{where}: not configured for this profile (configured: {have}); "
            "no other profile's agent is used instead"
        )
    agent_type = config.agent_types.get(agent)
    if agent_type is None:
        raise AgentResolutionError(f"{where}: no agent type named {agent!r}")

    env = _instance_env(env_in, instance.env)
    executable = _find_executable(instance.executable or agent_type.executable, env, where)

    cwd: Path | None = None
    if instance.working_directory is not None:
        cwd = Path(_expand(instance.working_directory, env.get("HOME") or str(Path.home())))
        if not cwd.is_absolute():
            raise AgentResolutionError(f"{where}: working_directory {str(cwd)!r} must be absolute")
        if not cwd.is_dir():
            raise AgentResolutionError(f"{where}: working_directory {str(cwd)!r} is not a directory")

    return ResolvedAgent(
        profile=profile,
        agent=agent,
        adapter=agent_type.adapter,
        executable=executable,
        argv=(str(executable), *instance.args),
        env=env,
        working_directory=cwd,
        resume_args=tuple(instance.resume_args),
        prompt_mode=instance.prompt_mode,
        skill_invocation=instance.skill_invocation,
    )
