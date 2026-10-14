"""Listing, adding and editing profiles in `config.json`.

Edits are made on the raw JSON object, so unrelated and unknown keys, in other
profiles and inside the edited profile, survive. Every write goes through
`update_config`, which refuses a result that fails validation.
"""

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from commodore_launcher.config import (
    CURRENT_VERSION,
    ConfigOutdatedError,
    NAME_PATTERN,
    ConfigError,
    load_config,
    outdated_message,
    outdated_version,
    read_raw,
    update_config,
)

PROMPT_MODES = ("argument", "stdin", "interactive")
SKILL_INVOCATIONS = ("slash", "prompt", "none")


@dataclass
class InstanceEdit:
    """Changes to one harness. `None` means leave that field alone."""

    agent: str
    executable: str | None = None
    args: list[str] | None = None
    """Replaces the whole list."""
    env: dict[str, str] = field(default_factory=dict)
    """Merged into the existing environment."""
    unset_env: list[str] = field(default_factory=list)
    working_directory: str | None = None
    clear_working_directory: bool = False
    resume_args: list[str] | None = None
    prompt_mode: str | None = None
    skill_invocation: str | None = None


def list_profiles() -> list[dict[str, Any]]:
    """Each profile with its default harness and the harnesses configured for it."""
    config = load_config()
    return [
        {
            "name": name,
            "default_harness": profile.default_harness,
            "harnesses": {
                aname: inst.model_dump(exclude_defaults=True) for aname, inst in profile.harnesses.items()
            },
        }
        for name, profile in sorted(config.profiles.items())
    ]


def _obj(parent: dict[str, Any], key: str, where: str, create: bool = False) -> dict[str, Any]:
    """`parent[key]` as a dict, or ConfigError if hand-editing left something else there."""
    value = parent.setdefault(key, {}) if create else parent.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be an object in config.json; fix it by hand first")
    return value


def _raw_profiles() -> dict[str, Any]:
    data = read_raw() or {}
    profiles = data.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ConfigError("profiles must be an object")
    return copy.deepcopy(profiles)


def _apply(instance: dict[str, Any], edit: InstanceEdit) -> None:
    if edit.executable is not None:
        instance["executable"] = edit.executable
    if edit.args is not None:
        instance["args"] = edit.args
    if edit.env or edit.unset_env:
        merged = {**_obj(instance, "env", "env"), **edit.env}
        for key in edit.unset_env:
            merged.pop(key, None)
        instance["env"] = merged
    if edit.working_directory is not None:
        instance["working_directory"] = edit.working_directory
    if edit.clear_working_directory:
        instance.pop("working_directory", None)
    if edit.resume_args is not None:
        instance["resume_args"] = edit.resume_args
    if edit.prompt_mode is not None:
        instance["prompt_mode"] = edit.prompt_mode
    if edit.skill_invocation is not None:
        instance["skill_invocation"] = edit.skill_invocation


def _save(profiles: dict[str, Any]) -> None:
    # An old file is migrated explicitly, never bumped as a side effect.
    version = outdated_version(read_raw())
    if version is not None:
        raise ConfigOutdatedError(outdated_message(version))
    update_config({"profiles": profiles})


def add_profile(name: str, edits: list[InstanceEdit], default_agent: str | None = None) -> None:
    """Create a profile with the given harnesses. Fails if the name is taken."""
    if not re.fullmatch(NAME_PATTERN, name):
        raise ConfigError(f"invalid profile name {name!r}: use letters, digits, '-' and '_'")
    profiles = _raw_profiles()
    if name in profiles:
        raise ConfigError(f"profile {name!r} already exists; use `profile edit`")
    agents: dict[str, Any] = {}
    for edit in edits:
        _apply(_obj(agents, edit.agent, f"profiles.{name}.harnesses.{edit.agent}", create=True), edit)
    profile: dict[str, Any] = {"harnesses": agents}
    if default_agent is None and len(agents) == 1:
        default_agent = next(iter(agents))
    if default_agent is not None:
        profile["default_harness"] = default_agent
    profiles[name] = profile
    _save(profiles)


def edit_profile(
    name: str,
    edits: list[InstanceEdit],
    default_agent: str | None = None,
    remove_agents: list[str] | None = None,
) -> None:
    """Change an existing profile. Harnesses named in `edits` are created if absent."""
    profiles = _raw_profiles()
    if name not in profiles:
        known = ", ".join(sorted(profiles)) or "none"
        raise ConfigError(f"profile {name!r} does not exist (profiles: {known})")
    profile = _obj(profiles, name, f"profiles.{name}")
    agents = _obj(profile, "harnesses", f"profiles.{name}.harnesses", create=True)
    for agent in remove_agents or []:
        if agent not in agents:
            raise ConfigError(f"profile {name!r} has no harness {agent!r} to remove")
        del agents[agent]
        if profile.get("default_harness") == agent:
            del profile["default_harness"]
    for edit in edits:
        _apply(_obj(agents, edit.agent, f"profiles.{name}.harnesses.{edit.agent}", create=True), edit)
    if default_agent is not None:
        profile["default_harness"] = default_agent
    _save(profiles)


def merge_profile(
    profiles: dict[str, Any], name: str, edits: list[InstanceEdit], default_agent: str | None = None
) -> None:
    """Create or extend `profiles[name]` in place (a raw config object); nothing is removed.

    Unlike `add_profile` and `edit_profile` this does not write, so a caller can combine
    several changes into one validated, atomic `update_config`. The setup wizard uses it.
    """
    if not re.fullmatch(NAME_PATTERN, name):
        raise ConfigError(f"invalid profile name {name!r}: use letters, digits, '-' and '_'")
    created = name not in profiles
    profile = _obj(profiles, name, f"profiles.{name}", create=True)
    agents = _obj(profile, "harnesses", f"profiles.{name}.harnesses", create=True)
    for edit in edits:
        _apply(_obj(agents, edit.agent, f"profiles.{name}.harnesses.{edit.agent}", create=True), edit)
    if default_agent is None and created and len(agents) == 1:
        default_agent = next(iter(agents))
    if default_agent is not None:
        profile["default_harness"] = default_agent
