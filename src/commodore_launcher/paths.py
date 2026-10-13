"""Where Commodore Launcher keeps its files.

Everything lives under one root, `~/.commodore-launcher/` by default. Setting
`COMMODORE_LAUNCHER_HOME` moves the whole root; tests always do this.
"""

import json
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


def copy_old_home() -> tuple[Path, Path] | None:
    """Copy `~/.agent-launcher` to `~/.commodore-launcher` the first time, and return `(old, new)` (else `None`).

    Only when `COMMODORE_LAUNCHER_HOME` is unset or empty, the new home does not exist and the old one is a
    directory. The old home is only read: files are copied as bytes (the state database is never opened) and
    nothing in it is changed or removed. If the old home itself is a symlink to a directory, that one link is
    resolved; below it a symlink is skipped, never followed. The copy is built in a temporary sibling and renamed
    into place, so a first run racing another cannot leave two copies or a half-built home, and an interrupted or
    failed copy leaves nothing behind. One change is made to the new home's copy of `config.json`: a
    `repositories.worktree_root` inside the old home becomes the new default, so no new worktree lands in the old home.

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
        _repoint_worktree_root(staging / "config.json", {old, source})
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
    return old, new


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


def _repoint_worktree_root(config_file: Path, old_homes: set[Path]) -> None:
    """Point a copied config's `repositories.worktree_root` at the new default if it is inside the old home.
    A file that is absent or not a JSON object is left as copied."""
    from commodore_launcher.config import DEFAULT_WORKTREE_ROOT

    if not config_file.is_file():
        return
    try:
        data = json.loads(config_file.read_text())
        root = data["repositories"]["worktree_root"]
        chosen = Path(os.path.abspath(Path(root).expanduser()))
    except (ValueError, KeyError, TypeError):
        return
    if not any(chosen == home or home in chosen.parents for home in old_homes):
        return
    data["repositories"]["worktree_root"] = DEFAULT_WORKTREE_ROOT
    config_file.write_text(json.dumps(data, indent=2) + "\n")
