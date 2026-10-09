# Development

## Setup

```bash
uv sync          # creates .venv and installs from uv.lock
uv run pytest -q
uv run agent-launcher version
```

Dependencies: Typer (CLI), Pydantic v2 (config schema), questionary (the interactive picker and setup wizard), pytest (dev). Add `uv add <pkg>` and commit `uv.lock`.

## Conventions

**Layout.** `src/agent_launcher/`, tests in `tests/`.

- `cli.py`: Typer commands. Presentation only (parsing flags, printing, exit codes).
- `redact.py` must be used for anything written to logs or exports; `logs.trace()` is the decision-tracing call. `doctor.py` takes an injectable runner and PATH lookup: tests fake them and must never depend on installed tools.
- `state.py` owns `state.db`: connection settings, `transaction()` for writes, and the ordered `MIGRATIONS` list (append a migration; never edit a shipped one). `repositories.py` identifies repositories (git and `gh` through an injectable runner) and `associations.py` stores profile associations. Tests use real `git init` repos in temp dirs and a fake `gh` (`fake_github` fixture); an opt-in live check runs with `AGENT_LAUNCHER_LIVE=1`. See [ADR 0003](adr/0003-state-database.md).
- The task pipeline (`tasks`, `sessions`, `picker`, `prompt`, `terminals`, `terminal_mock`, `launch`) is described in [architecture.md](architecture.md); adding a terminal adapter in [terminal-adapters.md](terminal-adapters.md).
- `config.py`, `paths.py`, and later modules: logic with no Typer imports, so it is testable without the CLI.

**Config root.** Everything lives under `paths.launcher_home()`: `~/.agent-launcher/`, or `$AGENT_LAUNCHER_HOME` if set. Always get paths through `agent_launcher.paths`; never hard-code `~`. Don't write into the installed package directory.

**Tests never touch the real home.** `tests/conftest.py` has an autouse fixture that sets `AGENT_LAUNCHER_HOME` to a per-test temp dir. Use the `launcher_home` fixture to get that path and `write_config` to seed `config.json`. Tests that need other tools or the network must be opt-in (marker or env var); plain `uv run pytest` must pass offline.

**JSON output.** Commands that benefit take `--json` and print a single JSON object to stdout (2-space indent). Errors and warnings go to stderr as `error: <field>: <message>` in text mode. In `--json` mode failures are still reported in the JSON body, with a non-zero exit code. Exit 0 means success, 1 means a problem with the user's data or input.

**Config writes.** Use `config.write_json_atomic` (temp file in the same directory, fsync, `os.replace`). Use `config.update_config` to change settings: it edits the raw JSON object, so unrelated and unknown keys are preserved, and it refuses to write a result that fails validation.

**Schema changes.** Add the field to `Config`, bump `CURRENT_VERSION` only for incompatible changes (and add the migration step: [config-migrations.md](config-migrations.md)), and document the field in [configuration.md](configuration.md).

**Subprocesses.** argv lists, timeouts, checked exit codes; never `shell=True` with task content.

## Checking a pipx install without touching your real pipx

```bash
T=$(mktemp -d)
PIPX_HOME=$T/home PIPX_BIN_DIR=$T/bin PIPX_MAN_DIR=$T/man pipx install .
$T/bin/agent-launcher version
```

The same check runs automatically, with a bumped copy to prove an upgrade keeps config and state, as `AGENT_LAUNCHER_PACKAGING=1 uv run pytest tests/test_packaging.py` (needs `uv`, `pipx` and a package index).

## Tests

`uv run pytest` needs no cmux, no network and no agent; it uses real Git in temp repositories, a fake `gh` and the mock terminal. Opt-in tests, none of which run by default:

| Switch | Tests | What it touches |
| --- | --- | --- |
| `AGENT_LAUNCHER_PACKAGING=1` | `tests/test_packaging.py` | Builds wheels, installs them with pipx into temp directories |
| `AGENT_LAUNCHER_LIVE=1` and `AGENT_LAUNCHER_SANDBOX_REPO=<owner>/<repo>` | `tests/test_live_github.py`, the live tests in `tests/test_scenarios.py` | That throwaway sandbox repository (issues, PRs, labels, clones); the mock terminal; no agent |
| `AGENT_LAUNCHER_LIVE=1` and `AGENT_LAUNCHER_LIVE_DIR=<repo>` | `tests/test_live_cmux.py` | One real cmux workspace running Claude Code; never submits; run from a cmux terminal |
| plus `AGENT_LAUNCHER_LIVE_EXECUTE=1` | `test_execute_renders_a_template_and_submits_it` | Sends `say hi` to the model |

`tests/test_scenarios.py` is the end-to-end suite for the ten scenarios of SPEC §34; each test's docstring lists the expected results it checks. `tests/test_docs.py` keeps the documentation honest: every CLI option and every relative link must appear in the docs. What cannot be tested without a person at a cmux terminal is in [live-validation.md](live-validation.md); how each SPEC §37 criterion is met is in [definition-of-done.md](definition-of-done.md).
