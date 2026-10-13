"""Where Commodore Launcher keeps its files.

Everything lives under one root, `~/.commodore-launcher/` by default. Setting
`COMMODORE_LAUNCHER_HOME` moves the whole root; tests always do this.
"""

import os
import shutil
import tempfile
from pathlib import Path
from typing import NamedTuple

HOME_ENV_VAR = "COMMODORE_LAUNCHER_HOME"


def launcher_home() -> Path:
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".commodore-launcher"


def config_path() -> Path:
    return launcher_home() / "config.json"


def workflows_path() -> Path:
    return launcher_home() / "workflows.json"


def templates_dir() -> Path:
    return launcher_home() / "templates"


OLD_HOME_NAME = ".agent-launcher"
"""The home of the product's earlier name (Agent Launcher). Read once, by `copy_old_home`, and never written."""

_COPIED = ("config.json", "workflows.json", "state.db", "state.db-wal", "state.db-shm", "templates", "logs")
"""All that `copy_old_home` takes. Not the worktrees, locks, backups, maintenance repository or mock terminal."""


def old_launcher_home() -> Path:
    return Path.home() / OLD_HOME_NAME


class CopiedHome(NamedTuple):
    old: Path
    new: Path
    config_note: str | None = None
    """What happened to the copied `config.json`, for the one line the caller prints: migrated to the current
    version, or left as copied and needing `config migrate`. None when it needed neither."""


def copy_old_home() -> CopiedHome | None:
    """Copy `~/.agent-launcher` to `~/.commodore-launcher` the first time, and return what was copied (else `None`).

    Only when `COMMODORE_LAUNCHER_HOME` is unset or empty, the new home does not exist and the old one is a
    directory. The old home is only read: files are copied as bytes (the state database is never opened) and
    nothing in it is changed or removed. If the old home itself is a symlink to a directory, that one link is
    resolved; below it a symlink is skipped, never followed. The copy is built in a temporary sibling and renamed
    into place, so a first run racing another cannot leave two copies or a half-built home, and an interrupted or
    failed copy leaves nothing behind. Two changes are made to the new home's copy of `config.json`: a
    `repositories.worktree_root` inside the old home becomes the new default, so no new worktree lands in the old home,
    and a config older than the current version is migrated in memory (the old home is the backup, so there is no
    `backups/` entry). If that migration fails, the config is copied as it was and the note says it needs
    `config migrate`.

    Raises `OSError` (with `filename`) if the copy fails. The caller must stop before anything creates the new home,
    or the copy is never retried."""
    if os.environ.get(HOME_ENV_VAR):
        return None
    old, new = old_launcher_home(), launcher_home()
    if os.path.lexists(new) or not old.is_dir():
        return None
    source = old.resolve()
    staging = Path(tempfile.mkdtemp(prefix=f"{new.name}.copying-", dir=new.parent))
    done = False
    try:
        for name in _COPIED:
            _copy_entry(source / name, staging / name)
        note = prepare_copied_config(staging / "config.json", {old, source})
        try:
            os.rename(staging, new)
        except OSError:
            if new.is_dir():  # another first run got there first
                return None
            raise
        done = True
    finally:
        if not done:
            shutil.rmtree(staging, ignore_errors=True)
    return CopiedHome(old, new, note)


def _copy_entry(source: Path, target: Path) -> None:
    if source.is_symlink():
        return
    if source.is_dir():
        target.mkdir()
        for child in sorted(source.iterdir()):
            _copy_entry(child, target / child.name)
    elif source.is_file():
        shutil.copyfile(source, target, follow_symlinks=False)
        shutil.copystat(source, target, follow_symlinks=False)


def prepare_copied_config(config_file: Path, old_homes: set[Path]) -> str | None:
    """What the copy does to the new home's `config.json`: see `migrations.prepare_copied_config`. Lives there
    because it reads config contents, which `paths` does not."""
    from commodore_launcher import migrations

    return migrations.prepare_copied_config(config_file, old_homes)
