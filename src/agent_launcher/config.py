"""Versioned, human-editable configuration (`config.json`).

The file on disk is the source of truth. Unknown fields are reported by
`validate_config` and are never dropped: writes go through `update_config`,
which edits the raw JSON object and leaves every other key untouched.
"""

import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, ValidationError

from agent_launcher.paths import config_path

CURRENT_VERSION = 1


class Config(BaseModel):
    """Schema for `config.json`. Later tickets add fields and bump `version`."""

    model_config = ConfigDict(extra="forbid")

    version: StrictInt = CURRENT_VERSION
    debug: StrictBool = False


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


def validate_data(data: dict[str, Any]) -> tuple[list[FieldIssue], list[str]]:
    """Check a config object. Returns (errors, unknown field paths)."""
    errors: list[FieldIssue] = []
    unknown: list[str] = []

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
        Config.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            name = ".".join(str(part) for part in err["loc"]) or "(root)"
            if err["type"] == "extra_forbidden":
                unknown.append(name)
            elif name == "version" and any(e.field == "version" for e in errors):
                continue
            else:
                errors.append(FieldIssue(name, err["msg"]))
    return errors, unknown


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
