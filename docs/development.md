# Development

## Setup

```bash
uv sync          # creates .venv and installs from uv.lock
uv run pytest -q
uv run agent-launcher version
```

Dependencies: Typer (CLI), Pydantic v2 (config schema), questionary (interactive picker, used by later tickets), pytest (dev). Add `uv add <pkg>` and commit `uv.lock`.

## Conventions

**Layout.** `src/agent_launcher/`, tests in `tests/`.

- `cli.py`: Typer commands. Presentation only (parsing flags, printing, exit codes).
- `config.py`, `paths.py`, and later modules: logic with no Typer imports, so it is testable without the CLI.

**Config root.** Everything lives under `paths.launcher_home()`: `~/.agent-launcher/`, or `$AGENT_LAUNCHER_HOME` if set. Always get paths through `agent_launcher.paths`; never hard-code `~`. Don't write into the installed package directory.

**Tests never touch the real home.** `tests/conftest.py` has an autouse fixture that sets `AGENT_LAUNCHER_HOME` to a per-test temp dir. Use the `launcher_home` fixture to get that path and `write_config` to seed `config.json`. Tests that need other tools or the network must be opt-in (marker or env var); plain `uv run pytest` must pass offline.

**JSON output.** Commands that benefit take `--json` and print a single JSON object to stdout (2-space indent). Errors and warnings go to stderr as `error: <field>: <message>` in text mode. In `--json` mode failures are still reported in the JSON body, with a non-zero exit code. Exit 0 means success, 1 means a problem with the user's data or input.

**Config writes.** Use `config.write_json_atomic` (temp file in the same directory, fsync, `os.replace`). Use `config.update_config` to change settings: it edits the raw JSON object, so unrelated and unknown keys are preserved, and it refuses to write a result that fails validation.

**Schema changes.** Add the field to `Config`, bump `CURRENT_VERSION` only for incompatible changes (migrations arrive in a later ticket), and document the field in the README.

**Subprocesses.** argv lists, timeouts, checked exit codes; never `shell=True` with task content.

## Checking a pipx install without touching your real pipx

```bash
T=$(mktemp -d)
PIPX_HOME=$T/home PIPX_BIN_DIR=$T/bin PIPX_MAN_DIR=$T/man pipx install .
$T/bin/agent-launcher version
```
