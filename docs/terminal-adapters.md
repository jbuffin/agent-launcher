# Terminal adapters

The core never imports a terminal's types. It talks to `terminals.TerminalAdapter` using typed dataclasses.

## Interface (`agent_launcher/terminals.py`)

- `capabilities() -> set[str]`: any of `create_session`, `focus_session`, `discover_sessions`, `prepare_prompt`, `submit_prompt`, `restore_session`, `close_session`.
- `available() -> bool`: can this adapter be used right now?
- `create_session(CreateSessionRequest) -> CreateSessionResult` (required). The request has the title, working directory, `command` argv (run directly, never through a shell), the environment, an optional prompt and `submit_prompt` (false = prepare it for review; true when config `prompt_execution` is `execute`). The result carries a `TerminalSessionRef` (`adapter`, `workspace_id`, `surface_id`) and whether the prompt was prepared or submitted.
- `read_screen(session) -> str | None` (None: the session no longer exists) and `prepare_prompt(session, prompt, spec)`: used to reopen, resume and re-prepare an existing session. `focus_session` raises `StaleTerminalSession` (`terminal_session_stale`) for a session that is gone. Resuming is a `create_session` whose request has `resume_check`; the result's `resume_state` says `confirmed`, `failed` or `unconfirmed`. See [sessions.md](sessions.md).
- `focus_session`, `discover_sessions`, `close_session`: optional. The base class raises `UnsupportedCapability` (code `unsupported_capability`); override only what the adapter declares. `require(capability)` raises the same error up front.

`send_text` (SPEC §19) is still not in the interface: the cmux adapter pastes the prompt itself, as part of `create_session`. `open` requires `create_session`, plus `prepare_prompt` (or `submit_prompt` when config `prompt_execution` is `execute`), before it starts a session.

Environment values may hold secrets. An adapter must not log or record them.

## Selection

`terminal.adapter` in config names the adapter; `open --terminal NAME` overrides it for one run. An unknown or unimplemented name is a `terminal_adapter_unavailable` error. The launcher never switches adapters on its own.

## Mock adapter

`terminal_mock.MockTerminalAdapter` launches nothing. It keeps its calls in `calls`, and when built with a path appends them to a JSON file. Workspace IDs are numbered from the calls already recorded, so they stay unique across invocations. Selected by name, it writes `mock-terminal.json` under the launcher home, so after `agent-launcher open <task> --terminal mock` you can read what would have launched (environment variable names only, no values). 

## cmux adapter (`terminal_cmux.py`)

The only module that knows cmux commands or IDs. Selected with `terminal.adapter: cmux` (the default) or `--terminal cmux`.

**Setup and limits**

- Install cmux (tested with 0.65.0) and run `agent-launcher` **from a cmux terminal**. cmux's default socket mode (`cmuxOnly`, see `cmux socket-status`) refuses commands from any other process: `cmux ping` fails with `Access denied - only processes started inside cmux can connect`, and `open` reports that and stops. The launcher does not change cmux settings.
- Capabilities: `create_session`, `focus_session`, `close_session`, `prepare_prompt`, `submit_prompt`. Not `discover_sessions`, not `restore_session`.
- The workspace is named `<repo> — <task title>` (for example `platform — Fix login`), opens in the task's repository directory and is focused.
- The agent is not run directly: `cmux new-workspace --command` types the command into the workspace's interactive shell (which runs the user's rc files first). Each argv element is shell-quoted (`shlex.join`); the shell stays alive after the agent exits.
- Environment: every variable the profile's instance sets is passed, whether or not the launcher's own environment already holds the same value, plus any other variable that differs from it. They go through a private (0600) temporary `--env-file` deleted straight after, and the profile's variables are also set on the typed command, ` /usr/bin/env K=V … agent` (shell-quoted, with a leading space), so the user's shell start-up files cannot override them. Because the command is typed into the shell, those values appear in the workspace's scrollback and, unless the shell ignores space-prefixed commands (`HIST_IGNORE_SPACE` in zsh, `ignorespace` in bash), in its shell history. That is why profile env must hold paths and names, not secrets. cmux stores a workspace's variables in its session manifest in plaintext (cmux documents this), so a profile's environment should hold paths and names, not secrets. `CMUX_*` keys are never passed.
- Closing uses `cmux workspace close`. `--force` (cmux otherwise answers `confirmation_required` while the agent runs) is added only when the session is recorded as created by the launcher (`terminal_sessions.created_by_launcher`, set from the `create_session` result; rows from before that column default to not owned) **and** a surface was recorded **and** that surface is still listed in the workspace (`cmux list-panels`), because a cmux ref may name a different workspace after cmux restarts. Otherwise cmux is left to refuse; `restart` then stops with `close_refused`, starts nothing and tells you to close the workspace yourself.
- cmux runs with `CMUX_QUIET=1` so its alias notices stay off the stderr the adapter parses.
- Every cmux call is made with the global `--id-format uuids` flag (before the subcommand). Without it `list-panels` prints refs (`* surface:1000000000  terminal  [focused]  "title"`) while the surface recorded at create time is a UUID, so a live surface would never be found. The workspace is recorded as the ref cmux prints (see below); the surface is compared as a UUID.
- Focusing first checks the recorded workspace and surface still exist (`cmux list-panels`), then `cmux workspace select`. A stale ID is `terminal_session_stale`, but only when cmux says the workspace is not found (or `list-panels` succeeds without the recorded surface). Any other cmux failure (socket access, a flag error, a timeout) raises `cmux_failed` and `open` resumes nothing. The wording matched is the one observed on cmux 0.65.0, `Error: Workspace ref not found: workspace:N` for a ref and `Error: not_found: Workspace not found` for a UUID (exit 1; `_NOT_FOUND`); a broader "not found" (a missing socket or binary) is not taken for a gone workspace, and neither is `read-screen`'s `invalid_params: Surface is not a terminal`. Surface presence is decided from the `list-panels` listing alone. `read_screen` uses the same check plus `cmux read-screen`.
- **What is recorded, exactly (cmux 0.65.0, observed):** `new-workspace` prints a ref even with `--id-format uuids` (`OK workspace:1000000058`), so `workspace_id` is that ref as printed. The surface comes from `list-panels --workspace <ref>` (`* C7D8E9F0-A1B2-4C3D-9E4F-5A6B7C8D9E0F  terminal  [focused]  "Terminal"`) and is recorded as a UUID only when the workspace has exactly one. The surface UUID is what guards against cmux giving the same ref to a different workspace later. A session **without** one is unverifiable: it can be focused by ref, but it is never force-closed, nothing is read from or typed into it (`prompt` stops with `session_unverifiable`; a reopen only focuses it and never decides it has exited), and `restart` leaves it open and says so.
- Recorded: `workspace_id` and `surface_id` exactly as cmux prints them (a `workspace:N` ref or a UUID). No environment values.

**Preparing the prompt without submitting it**

Claude Code 2.1.295 has no flag that pre-fills its input: `claude [prompt]` submits its argument. So:

1. Wait for the input box: poll `cmux read-screen` until the agent adapter's *ready* pattern matches and two consecutive reads are identical, up to 30 s. A *blocked* pattern (trust-folder, login dialog) stops the wait.
2. `cmux paste --workspace W --surface S -` with the prompt on stdin: one bracketed paste, so newlines stay inside the input box and do not submit. `--submit`, `--force` and newline keystrokes are never used; `cmux paste` itself refuses over a draft or an open dialog.
3. `cmux read-screen` again: the first line of the prompt (or Claude's `[Pasted text …]` placeholder) must be visible and no *busy* pattern (`esc to interrupt`) may show.

If any step fails, including a cmux error or timeout, the workspace stays open, nothing more is typed, the prompt is copied to the clipboard (`pbcopy`) and `open` prints a notice (naming `agent-launcher prompt <task>` as the way to enter it once the agent is at its input box, e.g. after accepting the folder-trust dialog) (`notice` in `--json`, with `prompt_prepared: false`). Agents without a known input box (Codex, Copilot for now) always take this path.

**Execute mode (`submit_prompt`)**

Steps 1 to 3 above, unchanged. Only if they all succeeded, step 4 is `cmux send-key --workspace W --surface S enter`: a single Enter key event (cmux 0.65.0 `send-key --help`). `cmux paste --submit` is not used, because it would submit without the screen check, and `--force` is never passed. If any of steps 1 to 3 failed (dialog, no input box in time, paste refused, prompt not visible, agent already busy), nothing is sent: the result is the prepare fallback with a notice that starts "Execute mode: the prompt was not submitted". If only the Enter is refused, the prompt stays in the input box and the notice says to press Enter. `prompt_submitted` is true only when cmux accepted the key; the launcher does not watch the agent start working.

**Agent adapters** (`agent_adapters.py`) are separate from terminal adapters: the Claude Code adapter supplies the ready, blocked, busy and collapsed patterns as a `PromptInput`; the argv comes from the profile's instance. The cmux module treats them as data.

**Verification status.** Command names, flags and semantics above come from `cmux --help`, each subcommand's `--help` and cmux's published CLI contract (cmux 0.65.0). Observed live: workspace IDs look like `workspace:1000000054` and surface IDs like UUIDs; the adapter accepts both shapes and fails closed if no workspace ID appears. The surface recorded is the workspace's only panel; with several it records none and targets the workspace. The Claude Code screen patterns come from Claude Code's known idle-footer text and are likewise unobserved. Run the live check from a cmux terminal to confirm both (it also covers focus and reopen; see `tests/test_live_cmux.py`): `AGENT_LAUNCHER_LIVE=1 AGENT_LAUNCHER_LIVE_DIR=<an existing git repository Claude Code already trusts> uv run pytest tests/test_live_cmux.py -s` (goes through `open_task` with a temp launcher home and a profile setting `CLAUDE_CONFIG_DIR=~/.claude-personal`, uses that directory as the repo without creating anything, opens one workspace, never submits, closes it). In an untrusted directory Claude shows its folder-trust dialog; the adapter detects it, pastes nothing and falls back to the clipboard.

## Adding an adapter

1. Subclass `TerminalAdapter` in its own module, set `name`, implement `capabilities`, `available`, `create_session`, and any optional operation you declare.
2. Register the name in `terminal_select.select_adapter`.
3. Test it against the same expectations as the mock; the core's tests run on the mock and need no terminal installed.
