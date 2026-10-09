"""`config edit`: change config.json in the user's editor, and write it only if it validates.

The editor works on a temporary copy. Nothing reaches `config.json` until the saved text parses, is a JSON
object and passes the same validation `config validate` uses.
"""

import json
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_launcher import config as config_module
from agent_launcher.errors import LauncherError
from agent_launcher.interaction import Choice, Prompter

FALLBACK_EDITOR = "vi"


def editor_argv() -> list[str]:
    """`$VISUAL`, else `$EDITOR`, else `vi`. The value may carry arguments (`code --wait`)."""
    for var in ("VISUAL", "EDITOR"):
        value = os.environ.get(var, "").strip()
        if value:
            try:
                argv = shlex.split(value)
            except ValueError as exc:
                raise LauncherError("editor_invalid", f"${var} cannot be parsed: {exc}") from exc
            if argv:
                return argv
    return [FALLBACK_EDITOR]


@dataclass
class EditOutcome:
    status: str
    """`saved`, `unchanged` or `discarded`."""
    warnings: list[str]


def check_text(text: str) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    """Parse and validate edited text. Returns (object or None, problems, unknown-field warnings)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [f"not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"], []
    if not isinstance(data, dict):
        return None, ["the top level must be a JSON object"], []
    errors, unknown = config_module.validate_data(data)
    return data, [f"{e.field}: {e.message}" for e in errors], [f"{name}: unknown field" for name in unknown]


def _run_editor(argv: list[str], path: Path) -> None:
    # No timeout: an editor is interactive and stays open as long as the user wants. argv list, never a shell.
    try:
        done = subprocess.run([*argv, str(path)], check=False)
    except FileNotFoundError as exc:
        raise LauncherError("editor_not_found", f"editor {argv[0]!r} was not found; set $VISUAL or $EDITOR") from exc
    if done.returncode != 0:
        raise LauncherError("editor_failed", f"editor {argv[0]!r} exited with status {done.returncode}; nothing was saved")


def edit_config(prompter: Prompter | None, path: Path | None = None) -> EditOutcome:
    """Edit the config file. `prompter` is None when nobody can be asked: an invalid result is then discarded."""
    path = path or config_module.config_path()
    argv = editor_argv()
    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = json.dumps({"version": config_module.CURRENT_VERSION}, indent=2) + "\n"
        existed = False
    except (OSError, UnicodeDecodeError) as exc:
        raise LauncherError("config_unreadable", f"{path} cannot be read: {exc}") from exc
    else:
        existed = True
    with tempfile.TemporaryDirectory(prefix="agent-launcher-edit-") as tmp_dir:
        scratch = Path(tmp_dir) / "config.json"
        scratch.write_text(original, encoding="utf-8")
        scratch.chmod(0o600)
        while True:
            _run_editor(argv, scratch)
            try:
                text = scratch.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                if prompter is None:
                    raise LauncherError("config_invalid", f"the edited file cannot be read as UTF-8 text and was discarded: {exc}") from exc
                prompter.say(f"error: the edited file cannot be read as UTF-8 text: {exc}")
                if prompter.select("The edited config cannot be read.", [Choice("edit", "Edit again"), Choice("discard", "Discard my changes")], default="edit") == "discard":
                    return EditOutcome("discarded", [])
                continue
            if text == original:
                return EditOutcome("unchanged", [])
            data, problems, warnings = check_text(text)
            if not problems and data is not None:
                break
            if prompter is None:
                raise LauncherError("config_invalid", "the edited config is invalid and was discarded: " + "; ".join(problems), problems=problems)
            for problem in problems:
                prompter.say(f"error: {problem}")
            choice = prompter.select(
                "The edited config is not valid and will not be saved.",
                [Choice("edit", "Edit again"), Choice("discard", "Discard my changes")],
                default="edit",
            )
            if choice == "discard":
                return EditOutcome("discarded", [])
    current = _current_text(path)
    if current != (original if existed else None):
        raise LauncherError("config_changed", f"{path} changed while you were editing; nothing was saved. Run `config edit` again.")
    config_module.write_json_atomic(path, data)
    return EditOutcome("saved", warnings)


def _current_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
