"""Versioned, human-editable configuration (`config.json`).

The file on disk is the source of truth. Unknown fields are reported by
`validate_config` and are never dropped: writes go through `update_config`,
which edits the raw JSON object and leaves every other key untouched.
"""

import copy
import difflib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    field_validator,
)

from agent_launcher.paths import config_path

CURRENT_VERSION = 2
"""Version 2 added `agent_types` and `profiles`. Version 1 files are still valid."""

NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
"""Profile and agent type names. No dots, so a dotted field path is unambiguous."""

Name = Annotated[str, StringConstraints(pattern=NAME_PATTERN)]
PromptMode = Literal["argument", "stdin", "interactive"]
SkillInvocation = Literal["slash", "prompt", "none"]
WorkflowSelection = Literal["automatic", "ask_on_multiple", "always_ask"]
PromptExecution = Literal["prepare", "execute"]
AgentSelection = Literal["always_ask", "use_default", "ask_if_multiple"]

DEFAULT_WORKTREE_ROOT = "~/.agent-launcher/worktrees"
DEFAULT_CLONE_ROOT = "~/Projects"


class AgentType(BaseModel):
    """A kind of agent, defined once for every profile (global)."""

    model_config = ConfigDict(extra="forbid")

    adapter: StrictStr = Field(min_length=1)
    executable: StrictStr = Field(min_length=1)


class AgentInstance(BaseModel):
    """One profile's configured copy of an agent type. Nothing here is shared between profiles."""

    model_config = ConfigDict(extra="forbid")

    executable: StrictStr | None = Field(default=None, min_length=1)
    args: list[StrictStr] = Field(default_factory=list)
    env: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    working_directory: StrictStr | None = Field(default=None, min_length=1)
    resume_args: list[StrictStr] = Field(default_factory=list)
    prompt_mode: PromptMode = "argument"
    skill_invocation: SkillInvocation = "slash"

    @field_validator("env")
    @classmethod
    def _env_names(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            if not key or "=" in key or "\0" in key:
                raise ValueError(f"invalid environment variable name {key!r}")
        if "HOME" in value and not value["HOME"]:
            raise ValueError("HOME must not be empty")
        for key, val in value.items():
            if "\0" in val:
                raise ValueError(f"value of {key} contains a NUL character")
        return value


class LogSettings(BaseModel):
    """Log rotation: the active file rolls over at `max_bytes`; `backup_count` rolled files are kept."""

    model_config = ConfigDict(extra="forbid")

    max_bytes: StrictInt = Field(default=1_000_000, ge=1024)
    backup_count: StrictInt = Field(default=5, ge=0, le=1000)


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_agent: Name | None = None
    agents: dict[Name, AgentInstance] = Field(default_factory=dict)


def _check_path(value: str) -> str:
    if not value or "\0" in value:
        raise ValueError("must be a non-empty path")
    if not (value == "~" or value.startswith("~/") or value.startswith("/")):
        raise ValueError(f"{value!r} must be an absolute path or start with ~/")
    return value


PathStr = Annotated[StrictStr, AfterValidator(_check_path)]

_BAD_REF_CHARS = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


def _check_branch(value: str) -> str:
    """The rules of `git check-ref-format --branch`, so a bad name fails when the config loads."""
    bad = (
        not value
        or len(value) > 200
        or value.startswith(("-", "/"))
        or value.endswith(("/", "."))
        or value == "@"
        or ".." in value
        or "//" in value
        or "@{" in value
        or _BAD_REF_CHARS.search(value)
        or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))
    )
    if bad:
        raise ValueError(f"{value!r} is not a valid Git branch name")
    return value


def _check_repo_name(value: str) -> str:
    if not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*$", value) or ".." in value:
        raise ValueError(f"{value!r} is not an owner/name")
    return value


class TerminalSettings(BaseModel):
    """Which terminal adapter launches sessions. Only `cmux` is implemented in v1."""

    model_config = ConfigDict(extra="forbid")

    adapter: Name = "cmux"


class RepositorySettings(BaseModel):
    """Where repositories are looked for and where worktrees go. Nothing here is required."""

    model_config = ConfigDict(extra="forbid")

    search_roots: list[PathStr] = Field(default_factory=list)
    """Directories searched (two levels deep, never recursively) for checkouts of a repository."""
    clone_root: PathStr | None = None
    """Where a missing repository is cloned, after asking (`~/Projects` when unset). Searched for existing
    checkouts only when you set it yourself."""
    auto_clone: StrictBool = False
    """Clone a missing repository without asking. Off unless you turn it on."""
    mappings: dict[Annotated[str, AfterValidator(_check_repo_name)], PathStr] = Field(default_factory=dict)
    """`owner/name` -> checkout path. Wins over every other way of finding a repository."""
    worktree_root: PathStr = DEFAULT_WORKTREE_ROOT
    base_branches: dict[PathStr, Annotated[str, AfterValidator(_check_branch)]] = Field(default_factory=dict)
    """Per repository (keyed by the checkout's absolute path): the branch task worktrees are cut from.
    When absent the repository's `origin/HEAD` is used, then its local default branch."""


class WorkflowRouting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection_mode: WorkflowSelection = "automatic"


def builtin_agent_types() -> dict[str, AgentType]:
    return {
        "claude": AgentType(adapter="claude-code", executable="claude"),
        "codex": AgentType(adapter="codex-cli", executable="codex"),
        "copilot": AgentType(adapter="github-copilot-cli", executable="copilot"),
    }


class Config(BaseModel):
    """Schema for `config.json`. Later tickets add fields and bump `version`."""

    model_config = ConfigDict(extra="forbid")

    version: StrictInt = CURRENT_VERSION
    debug: StrictBool = False
    logs: LogSettings = Field(default_factory=LogSettings)
    agent_types: dict[Name, AgentType] = Field(default_factory=builtin_agent_types)
    profiles: dict[Name, Profile] = Field(default_factory=dict)
    terminal: TerminalSettings = Field(default_factory=TerminalSettings)
    repositories: RepositorySettings = Field(default_factory=RepositorySettings)
    workflow_routing: WorkflowRouting = Field(default_factory=WorkflowRouting)
    prompt_execution: PromptExecution = "prepare"
    agent_selection: AgentSelection = "always_ask"


_ALIASES = {
    "environment": "env",
    "environment_variables": "env",
    "envs": "env",
    "command": "executable",
    "cmd": "executable",
    "exe": "executable",
    "arguments": "args",
    "argv": "args",
    "cwd": "working_directory",
    "workdir": "working_directory",
    "working_dir": "working_directory",
    "default": "default_agent",
    "instances": "agents",
    "resume": "resume_args",
}


def _unknown_profile_field(loc: tuple[Any, ...]) -> FieldIssue:
    """An unknown key inside a profile or one of its agent instances (always an error).

    A misspelt identity field such as `environment` would otherwise be dropped and the
    agent would run with the caller's identity instead of the profile's.
    """
    path = ".".join(str(part) for part in loc)
    valid = list(AgentInstance.model_fields if len(loc) == 5 and loc[2] == "agents" else Profile.model_fields)
    key = str(loc[-1])
    guess = _ALIASES.get(key.lower())
    if guess not in valid:
        close = difflib.get_close_matches(key, valid, n=1)
        guess = close[0] if close else None
    hint = f"; did you mean {guess!r}?" if guess else f"; valid fields: {', '.join(valid)}"
    return FieldIssue(path, f"unknown field {key!r}{hint}")


def cross_check(config: Config) -> list["FieldIssue"]:
    """Rules that span several parts of the file: instances need a type, defaults need an instance."""
    issues: list[FieldIssue] = []
    for pname, profile in config.profiles.items():
        for aname in profile.agents:
            if aname not in config.agent_types:
                known = ", ".join(sorted(config.agent_types)) or "none"
                issues.append(
                    FieldIssue(
                        f"profiles.{pname}.agents.{aname}",
                        f"no agent type named {aname!r} (agent_types: {known})",
                    )
                )
        if profile.default_agent is not None and profile.default_agent not in profile.agents:
            issues.append(
                FieldIssue(
                    f"profiles.{pname}.default_agent",
                    f"{profile.default_agent!r} is not configured for this profile",
                )
            )
    return issues


class ConfigError(Exception):
    """The config file cannot be read as a JSON object."""


@dataclass
class FieldIssue:
    field: str
    message: str


@dataclass
class ValidationReport:
    path: Path
    exists: bool
    errors: list[FieldIssue] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "exists": self.exists,
            "valid": self.valid,
            "errors": [{"field": e.field, "message": e.message} for e in self.errors],
            "unknown_fields": self.unknown_fields,
        }


def read_raw(path: Path | None = None) -> dict[str, Any] | None:
    """Return the file's JSON object, or None when the file does not exist."""
    path = path or config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path} is not valid UTF-8 text") from exc
    except OSError as exc:
        raise ConfigError(f"{path} cannot be read: {exc.strerror or exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path} is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object at the top level")
    return data


def _validate(data: dict[str, Any]) -> tuple[list[FieldIssue], list[tuple[Any, ...]]]:
    errors: list[FieldIssue] = []
    unknown: list[tuple[Any, ...]] = []

    if "version" not in data:
        errors.append(FieldIssue("version", f"missing; add \"version\": {CURRENT_VERSION}"))
    elif isinstance(data["version"], int) and not isinstance(data["version"], bool):
        if data["version"] > CURRENT_VERSION:
            errors.append(
                FieldIssue(
                    "version",
                    f"{data['version']} is newer than this release supports "
                    f"(max {CURRENT_VERSION}); upgrade agent-launcher",
                )
            )
        elif data["version"] < 1:
            errors.append(FieldIssue("version", "must be 1 or greater"))

    try:
        model = Config.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            name = ".".join(str(part) for part in err["loc"]) or "(root)"
            if err["type"] == "extra_forbidden" and err["loc"][0] == "profiles" and len(err["loc"]) >= 3:
                errors.append(_unknown_profile_field(err["loc"]))
            elif err["type"] == "extra_forbidden":
                unknown.append(err["loc"])
            elif name == "version" and any(e.field == "version" for e in errors):
                continue
            else:
                errors.append(FieldIssue(name, err["msg"]))
    else:
        errors.extend(cross_check(model))
    return errors, unknown


def validate_data(data: dict[str, Any]) -> tuple[list[FieldIssue], list[str]]:
    """Check a config object. Returns (errors, unknown field paths)."""
    errors, unknown = _validate(data)
    return errors, [".".join(str(part) for part in loc) for loc in unknown]


def load_config(path: Path | None = None) -> Config:
    """The validated config, defaults applied. Unknown fields are ignored here, not rejected.

    Raises ConfigError if the file cannot be read or has errors. Unknown fields only
    matter to `validate`; behaviour such as agent resolution must not stop for them.
    """
    path = path or config_path()
    data = read_raw(path)
    if data is None:
        return Config()
    errors, unknown = _validate(data)
    if errors:
        raise ConfigError(f"{path}: " + "; ".join(f"{e.field}: {e.message}" for e in errors))
    data = copy.deepcopy(data)
    for loc in unknown:
        node: Any = data
        for part in loc[:-1]:
            node = node[part]
        node.pop(loc[-1], None)
    return Config.model_validate(data)


def validate_config(path: Path | None = None) -> ValidationReport:
    path = path or config_path()
    try:
        data = read_raw(path)
    except ConfigError as exc:
        return ValidationReport(path, True, errors=[FieldIssue("(file)", str(exc))])
    if data is None:
        return ValidationReport(path, False)
    errors, unknown = validate_data(data)
    return ValidationReport(path, True, errors, unknown)


def effective_config(path: Path | None = None) -> dict[str, Any]:
    """Defaults overlaid with the file's contents, unknown fields included."""
    data = read_raw(path) or {}
    return {**Config().model_dump(), **data}


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write `data` as JSON so readers see the old file or the new one, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = None
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def update_config(changes: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Set top-level keys, keep everything else (unknown fields included), write atomically.

    Raises ConfigError, writing nothing, if the result would not validate.
    """
    path = path or config_path()
    data = read_raw(path)
    if data is None:
        data = {"version": CURRENT_VERSION}
    data = {**data, **changes}
    errors, _ = validate_data(data)
    if errors:
        raise ConfigError("; ".join(f"{e.field}: {e.message}" for e in errors))
    write_json_atomic(path, data)
    return data
