# Terminal adapters

The core never imports a terminal's types. It talks to `terminals.TerminalAdapter` using typed dataclasses.

## Interface (`commodore_launcher/terminals.py`)

- `capabilities() -> set[str]`: any of `create_session`, `focus_session`, `restore_session`, `close_session`, `run_interactive`.
- `available() -> bool`: can this adapter be used right now?
- `create_session(CreateSessionRequest) -> CreateSessionResult` (required). The request has the title, working directory, `command` argv (run directly, never through a shell), the environment, an optional prompt with `prompt_args` (the launch arguments that carry it, one holding `PROMPT_MARKER`) and `submit_prompt` (true: the harness submits it at launch). A prompt without `prompt_args` is refused, never pasted. The result carries a `TerminalSessionRef` (`adapter`, `workspace_id`, `surface_id`) and whether the prompt was submitted.
- `run_interactive(title, working_directory, argv) -> TerminalSessionRef`: run an argv in a new temporary surface with a TTY and nothing else (no harness, no prompt). Used by `open --picker-surface` ([gh-dash.md](gh-dash.md)); not recorded as a task session; the surface ends its shell when the command ends (`exit_when_done`).
- `read_screen(session) -> str | None` (None: the session no longer exists) : used to reopen and resume an existing session. `focus_session` raises `StaleTerminalSession` (`terminal_session_stale`) for a session that is gone. Resuming is a `create_session` whose request has `resume_check`; the result's `resume_state` says `confirmed`, `failed` or `unconfirmed`. See [sessions.md](sessions.md).
- `focus_session`, `close_session`: optional. The base class raises `UnsupportedCapability` (code `unsupported_capability`); override only what the adapter declares. `require(capability)` raises the same error up front.

`send_text` is not in the interface: the prompt travels on the harness's command line, as part of `create_session`. `open` requires `create_session` before it starts a session.

Environment values may hold secrets. An adapter must not log or record them.

## Selection

`terminal.adapter` in config names the adapter; `open --terminal NAME` overrides it for one run. An unknown or unimplemented name is a `terminal_adapter_unavailable` error. The launcher never switches adapters on its own.

## Mock adapter

`terminal_mock.MockTerminalAdapter` launches nothing. It keeps its calls in `calls`, and when built with a path appends them to a JSON file. Workspace IDs are numbered from the calls already recorded, so they stay unique across invocations. Selected by name, it writes `mock-terminal.json` under the launcher home, so after `commodore open <task> --terminal mock` you can read what would have launched (environment variable names only, no values). 

## cmux adapter (`terminal_cmux.py`)

The only module that knows cmux commands or IDs. Selected with `terminal.adapter: cmux` (the default) or `--terminal cmux`.

**Setup and limits**

- Install cmux (tested with 0.65.0) and run `commodore` **from a cmux terminal**. cmux's default socket mode (`cmuxOnly`, see `cmux socket-status`) refuses commands from any other process: `cmux ping` fails with `Access denied - only processes started inside cmux can connect`, and `open` reports that and stops. The launcher does not change cmux settings.
- Capabilities: `create_session`, `focus_session`, `close_session`, `run_interactive`. Not `restore_session`.
- The workspace is named `<repo> — <task title>` (for example `platform — Fix login`), opens in the task's repository directory and is focused.
- The harness is not run directly: `cmux new-workspace --command` types the command into the workspace's interactive shell (which runs the user's rc files first). Each argv element is shell-quoted (`shlex.join`); the shell stays alive after the harness exits.
- Environment: every variable the profile's instance sets is passed, whether or not the launcher's own environment already holds the same value, plus any other variable that differs from it. They go through a private (0600) temporary `--env-file` deleted straight after, and the profile's variables are also set on the typed command, ` /usr/bin/env K=V … harness` (shell-quoted, with a leading space), so the user's shell start-up files cannot override them. Because the command is typed into the shell, those values appear in the workspace's scrollback and, unless the shell ignores space-prefixed commands (`HIST_IGNORE_SPACE` in zsh, `ignorespace` in bash), in its shell history. That is why profile env must hold paths and names, not secrets. cmux stores a workspace's variables in its session manifest in plaintext (cmux documents this), so a profile's environment should hold paths and names, not secrets. `CMUX_*` keys are never passed.
- Closing uses `cmux workspace close`. `--force` (cmux otherwise answers `confirmation_required` while the harness runs) is added only when the session is recorded as created by the launcher (`terminal_sessions.created_by_launcher`, set from the `create_session` result; rows from before that column default to not owned) **and** a surface was recorded **and** that surface is still listed in the workspace (`cmux list-panels`), because a cmux ref may name a different workspace after cmux restarts. Otherwise cmux is left to refuse. The launcher closes a workspace only to roll back a launch it just made.
- cmux runs with `CMUX_QUIET=1` so its alias notices stay off the stderr the adapter parses.
- Every cmux call is made with the global `--id-format uuids` flag (before the subcommand). Without it `list-panels` prints refs (`* surface:1000000000  terminal  [focused]  "title"`) while the surface recorded at create time is a UUID, so a live surface would never be found. The workspace is recorded as the ref cmux prints (see below); the surface is compared as a UUID.
- Focusing first checks the recorded workspace and surface still exist (`cmux list-panels`), then `cmux workspace select`. A stale ID is `terminal_session_stale`, but only when cmux says the workspace is not found (or `list-panels` succeeds without the recorded surface). Any other cmux failure (socket access, a flag error, a timeout) raises `cmux_failed` and `open` resumes nothing. The wording matched is the one observed on cmux 0.65.0, `Error: Workspace ref not found: workspace:N` for a ref and `Error: not_found: Workspace not found` for a UUID (exit 1; `_NOT_FOUND`); a broader "not found" (a missing socket or binary) is not taken for a gone workspace, and neither is `read-screen`'s `invalid_params: Surface is not a terminal`. Surface presence is decided from the `list-panels` listing alone. `read_screen` uses the same check plus `cmux read-screen`.
- **What is recorded, exactly (cmux 0.65.0, observed):** `new-workspace` prints a ref even with `--id-format uuids` (`OK workspace:1000000058`), so `workspace_id` is that ref as printed. The surface comes from `list-panels --workspace <ref>` (`* C7D8E9F0-A1B2-4C3D-9E4F-5A6B7C8D9E0F  terminal  [focused]  "Terminal"`) and is recorded as a UUID only when the workspace has exactly one. The surface UUID is what guards against cmux giving the same ref to a different workspace later. A session **without** one is unverifiable: it can be focused by ref, but it is never force-closed, nothing is read from or typed into it (a reopen only focuses it and never decides it has exited).
- Recorded: `workspace_id` and `surface_id` exactly as cmux prints them (a `workspace:N` ref or a UUID). No environment values.

**The prompt at launch (`prompt_args`)**

When the request has `prompt_args` (the harness adapter's `launch_prompt_args()`: every known harness), the prompt is written to a private temp file (0600) and the typed command ends with those arguments, the one holding the prompt as `"$(cat -- <file> && rm -f -- <file>)"`. The shell reads and removes the file; the prompt is never in the typed line. Nothing is read from the screen or pasted, and the harness submits the prompt once it is up. `prompt_submitted` is then true when cmux created the workspace ([ADR 20](adr/0020-execute-mode-prompt-at-launch.md)).

No `cmux paste`, `cmux send-key` or `cmux read-screen` call is made for a new session; `read-screen` stays for focus, resume checks and exit detection.

**Harness adapters** (`agent_adapters.py`) are separate from terminal adapters: a harness adapter supplies the launch arguments that carry a prompt, the skill invocation syntax and the resume mechanics; the argv comes from the profile's instance. The cmux module treats them as data.

**Verification status.** Command names, flags and semantics above come from `cmux --help`, each subcommand's `--help` and cmux's published CLI contract (cmux 0.65.0). Observed live: workspace IDs look like `workspace:1000000054` and surface IDs like UUIDs; the adapter accepts both shapes and fails closed if no workspace ID appears. The surface recorded is the workspace's only panel; with several it records none and targets the workspace. The resume screen patterns are unobserved. Run the live check from a cmux terminal to confirm both (it also covers focus and reopen; see `tests/test_live_cmux.py`): `COMMODORE_LAUNCHER_LIVE=1 COMMODORE_LAUNCHER_LIVE_DIR=<an existing git repository Claude Code already trusts> uv run pytest tests/test_live_cmux.py -s` (goes through `open_task` with a temp launcher home and a profile setting `CLAUDE_CONFIG_DIR=~/.claude-personal`, uses that directory as the repo without creating anything, opens one workspace, closes it; each launch submits one prompt to Claude Code). In an untrusted directory Claude shows its folder-trust dialog; it takes the prompt once the dialog is answered.

## Adding an adapter

1. Subclass `TerminalAdapter` in its own module, set `name`, implement `capabilities`, `available`, `create_session`, and any optional operation you declare.
2. Register the name in `terminal_select.select_adapter`.
3. Test it against the same expectations as the mock; the core's tests run on the mock and need no terminal installed.
