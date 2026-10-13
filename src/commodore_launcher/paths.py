"""Where Commodore Launcher keeps its files.

Everything lives under one root, `~/.commodore-launcher/` by default. Setting
`COMMODORE_LAUNCHER_HOME` moves the whole root; tests always do this.
"""

import os
import shutil
import tempfile
from pathlib import Path

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


def copy_old_home() -> Path | None:
    """Copy `~/.agent-launcher` to `~/.commodore-launcher` the first time, and return the new home (else `None`).

    Only when `COMMODORE_LAUNCHER_HOME` is unset or empty, the new home does not exist and the old one is a real
    directory. The old home is only read: files are copied as bytes (the state database is never opened), a symlink
    is skipped rather than followed, and nothing in it is changed or removed. The copy is built in a temporary
    sibling and renamed into place, so a first run racing another cannot leave two copies or a half-built home.
    Raises `OSError` if the copy fails; nothing is left behind and the next run tries again."""
    if os.environ.get(HOME_ENV_VAR):
        return None
    old, new = old_launcher_home(), launcher_home()
    if os.path.lexists(new) or old.is_symlink() or not old.is_dir():
        return None
    staging = Path(tempfile.mkdtemp(prefix=f"{new.name}.copying-", dir=new.parent))
    try:
        for name in _COPIED:
            _copy_entry(old / name, staging / name)
        os.rename(staging, new)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        if new.is_dir():  # another first run got there first
            return None
        raise
    return new


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
