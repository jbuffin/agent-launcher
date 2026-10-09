"""Migrations for the versioned JSON files under the launcher home (`config.json`, `workflows.json`).

Each file has an ordered chain of pure functions `vN -> vN+1`: raw dict in, raw dict out. Unknown fields are
kept because a step only touches the keys it means to. `state.db` has its own migrations in `state.py`.

`plan_migrations` reads and computes without writing. `apply_migration` backs the file up, writes atomically,
validates what is on disk and restores the backup if that fails.
"""

import copy
import difflib
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_launcher import config as config_module
from agent_launcher import workflows as workflows_module
from agent_launcher.errors import LauncherError
from agent_launcher.paths import config_path, launcher_home, workflows_path

Step = Callable[[dict[str, Any]], dict[str, Any]]
Validator = Callable[[dict[str, Any]], list[str]]


@dataclass(frozen=True)
class MigrationTarget:
    """One versioned file: where it is, the version this release writes, and how to get there."""

    name: str
    path: Path
    current_version: int
    steps: dict[int, Step]
    """`steps[n]` migrates a version `n` object to version `n + 1`."""
    validate: Validator
    """Problems that make an object unusable (empty when it is valid)."""


def _config_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """Version 2 added `agent_types` and `profiles`, both optional: only the version changes."""
    return {**data, "version": 2}


def _config_problems(data: dict[str, Any]) -> list[str]:
    errors, _ = config_module.validate_data(data)
    return [f"{e.field}: {e.message}" for e in errors]


def _workflow_problems(data: dict[str, Any]) -> list[str]:
    return [f"{e.field}: {e.message}" for e in workflows_module.validate_data(data)]


def default_targets() -> list[MigrationTarget]:
    return [
        MigrationTarget("config.json", config_path(), config_module.CURRENT_VERSION, {1: _config_v1_to_v2}, _config_problems),
        MigrationTarget("workflows.json", workflows_path(), workflows_module.CURRENT_VERSION, {}, _workflow_problems),
    ]


@dataclass
class MigrationPlan:
    target: MigrationTarget
    status: str
    """`absent`, `current`, `pending`, `too_new` or `error`."""
    from_version: int | None = None
    old: dict[str, Any] | None = None
    old_bytes: bytes | None = None
    """The file as planned from; a write is refused if the file is no longer exactly this."""
    new: dict[str, Any] | None = None
    problems: list[str] = field(default_factory=list)
    changed_keys: list[str] = field(default_factory=list)
    """Top-level keys other than `version` that the migration adds, removes or changes."""

    @property
    def significant(self) -> bool:
        """More than the version number changes, so the user confirms before anything is written."""
        return bool(self.changed_keys)

    @property
    def diff(self) -> str:
        if self.old is None or self.new is None:
            return ""
        before = json.dumps(self.old, indent=2).splitlines()
        after = json.dumps(self.new, indent=2).splitlines()
        return "\n".join(
            difflib.unified_diff(before, after, f"{self.target.name} (current)", f"{self.target.name} (migrated)", lineterm="")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.target.name,
            "path": str(self.target.path),
            "status": self.status,
            "from_version": self.from_version,
            "to_version": self.target.current_version if self.status == "pending" else self.from_version,
            "significant": self.significant,
            "changed_keys": self.changed_keys,
            "problems": self.problems,
        }


def _load(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path.name} cannot be read: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object at the top level")
    return data


def migrate_data(target: MigrationTarget, data: dict[str, Any]) -> dict[str, Any]:
    """Run the steps from `data`'s version up to the current one. `data` is not modified."""
    current = copy.deepcopy(data)
    version = current["version"]
    while version < target.current_version:
        step = target.steps.get(version)
        if step is None:
            raise ValueError(f"no migration from version {version} to {version + 1}")
        current = step(copy.deepcopy(current))
        if not isinstance(current, dict) or current.get("version") != version + 1:
            raise ValueError(f"migration from version {version} did not produce version {version + 1}")
        version += 1
    return current


def plan_migration(target: MigrationTarget) -> MigrationPlan:
    """What migrating this file would do. Reads only."""
    try:
        raw = target.path.read_bytes() if target.path.exists() else None
        data = _load(target.path)
    except (ValueError, OSError) as exc:
        return MigrationPlan(target, "error", problems=[str(exc)])
    if data is None:
        return MigrationPlan(target, "absent")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        return MigrationPlan(target, "error", old=data, old_bytes=raw, problems=['"version" is missing or not a whole number; set it by hand first'])
    if version < 1:
        return MigrationPlan(target, "error", from_version=version, old=data, old_bytes=raw, problems=["version must be 1 or greater"])
    if version > target.current_version:
        return MigrationPlan(
            target, "too_new", from_version=version, old=data, old_bytes=raw,
            problems=[f"version {version} is newer than this release supports (max {target.current_version}); upgrade agent-launcher"],
        )
    if version == target.current_version:
        return MigrationPlan(target, "current", from_version=version, old=data)
    try:
        new = migrate_data(target, data)
    except ValueError as exc:
        return MigrationPlan(target, "error", from_version=version, old=data, old_bytes=raw, problems=[str(exc)])
    problems = target.validate(new)
    keys = sorted(k for k in data.keys() | new.keys() if k != "version" and data.get(k, _MISSING) != new.get(k, _MISSING))
    status = "error" if problems else "pending"
    return MigrationPlan(
        target, status, from_version=version, old=data, old_bytes=raw, new=new,
        problems=[f"the migrated file would not be valid, so nothing is written: {p}" for p in problems], changed_keys=keys,
    )


_MISSING = object()


def plan_migrations(targets: list[MigrationTarget] | None = None) -> list[MigrationPlan]:
    return [plan_migration(t) for t in (targets if targets is not None else default_targets())]


def backups_dir() -> Path:
    return launcher_home() / "backups"


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def make_backup(path: Path, directory: Path | None = None) -> Path:
    """Copy `path` to `<home>/backups/<name>.<timestamp>.json`; never overwrites an earlier backup."""
    directory = directory or backups_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = path.stem
    candidate = directory / f"{stem}.{stamp}.json"
    n = 1
    while candidate.exists():
        n += 1
        candidate = directory / f"{stem}.{stamp}-{n}.json"
    shutil.copy2(path, candidate)
    return candidate


@dataclass
class MigrationResult:
    plan: MigrationPlan
    backup: Path
    restored: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {**self.plan.to_dict(), "backup": str(self.backup), "restored": self.restored, "ok": self.ok}


def apply_migration(plan: MigrationPlan) -> MigrationResult:
    """Back up, write atomically, validate what is on disk, restore the backup if that fails.

    Raises LauncherError, writing nothing, when the plan is not pending.
    """
    if plan.status != "pending" or plan.new is None:
        raise LauncherError("nothing_to_migrate", f"{plan.target.name} has no pending migration")
    path = plan.target.path
    try:
        original = path.read_bytes()
    except OSError as exc:
        raise LauncherError("migration_failed", f"{plan.target.name} cannot be read: {exc.strerror or exc}") from exc
    if original != plan.old_bytes:
        raise LauncherError("config_changed", f"{plan.target.name} changed since the migration was planned; nothing was written. Run `config migrate` again.")
    try:
        backup = make_backup(path)
    except OSError as exc:
        raise LauncherError("backup_failed", f"could not back up {plan.target.name}, so nothing was written: {exc.strerror or exc}") from exc
    result = MigrationResult(plan, backup)
    try:
        config_module.write_json_atomic(path, plan.new)
        problems = _validate_on_disk(plan.target)
    except Exception as exc:  # the write itself failed: the file may be half-replaced, so restore
        problems = [f"writing failed: {exc}"]
    if problems:
        _write_bytes_atomic(path, original)
        result.restored = True
        result.problems = problems
    return result


def _validate_on_disk(target: MigrationTarget) -> list[str]:
    try:
        data = _load(target.path)
    except ValueError as exc:
        return [str(exc)]
    if data is None:
        return [f"{target.name} is missing after the write"]
    if data.get("version") != target.current_version:
        return [f"version is {data.get('version')!r}, expected {target.current_version}"]
    return target.validate(data)
