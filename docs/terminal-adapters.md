# Terminal adapters

The core never imports a terminal's types. It talks to `terminals.TerminalAdapter` using typed dataclasses.

## Interface (`agent_launcher/terminals.py`)

- `capabilities() -> set[str]`: any of `create_session`, `focus_session`, `discover_sessions`, `prepare_prompt`, `submit_prompt`, `restore_session`, `close_session`.
- `available() -> bool`: can this adapter be used right now?
- `create_session(CreateSessionRequest) -> CreateSessionResult` (required). The request has the title, working directory, `command` argv (run directly, never through a shell), the environment, an optional prompt and `submit_prompt` (false = prepare it for review; true when config `prompt_execution` is `execute`). The result carries a `TerminalSessionRef` (`adapter`, `workspace_id`, `surface_id`) and whether the prompt was prepared or submitted.
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
- Capabilities: `create_session`, `focus_session`, `close_session`, `prepare_prompt`. Not `submit_prompt` (`prompt_execution: execute` is refused for cmux until that is built and checked), not `discover_sessions`, not `restore_session`.
- The workspace is named `<repo> — <task title>` (for example `platform — Fix login`), opens in the task's repository directory and is focused.
- The agent is not run directly: `cmux new-workspace --command` types the command into the workspace's interactive shell (which runs the user's rc files first). Each argv element is shell-quoted (`shlex.join`); the shell stays alive after the agent exits.
- Environment: every variable the profile's instance sets is passed, whether or not the launcher's own environment already holds the same value, plus any other variable that differs from it. They go through a private (0600) temporary `--env-file` deleted straight after, and the profile's variables are also set on the typed command, ` /usr/bin/env K=V … agent` (shell-quoted, with a leading space), so the user's shell start-up files cannot override them. Because the command is typed into the shell, those values appear in the workspace's scrollback and, unless the shell ignores space-prefixed commands (`HIST_IGNORE_SPACE` in zsh, `ignorespace` in bash), in its shell history. That is why profile env must hold paths and names, not secrets. cmux stores a workspace's variables in its session manifest in plaintext (cmux documents this), so a profile's environment should hold paths and names, not secrets. `CMUX_*` keys are never passed.
- Closing uses `cmux workspace close`, adding `--force` (cmux otherwise answers `confirmation_required` while the agent runs) only for a workspace this process created (`created_by_launcher` on the session ref). That flag is not stored, so a session loaded from the registry is closed without `--force` until ticket #11 persists ownership.
- Recorded: `workspace_id` and `surface_id` exactly as cmux prints them (a `workspace:N` ref or a UUID). No environment values.

**Preparing the prompt without submitting it**

Claude Code 2.1.295 has no flag that pre-fills its input: `claude [prompt]` submits its argument. So:

1. Wait for the input box: poll `cmux read-screen` until the agent adapter's *ready* pattern matches and two consecutive reads are identical, up to 30 s. A *blocked* pattern (trust-folder, login dialog) stops the wait.
2. `cmux paste --workspace W --surface S -` with the prompt on stdin: one bracketed paste, so newlines stay inside the input box and do not submit. `--submit`, `--force` and newline keystrokes are never used; `cmux paste` itself refuses over a draft or an open dialog.
3. `cmux read-screen` again: the first line of the prompt (or Claude's `[Pasted text …]` placeholder) must be visible and no *busy* pattern (`esc to interrupt`) may show.

If any step fails, including a cmux error or timeout, the workspace stays open, nothing more is typed, the prompt is copied to the clipboard (`pbcopy`) and `open` prints a notice (`notice` in `--json`, with `prompt_prepared: false`). Agents without a known input box (Codex, Copilot for now) always take this path.

**Agent adapters** (`agent_adapters.py`) are separate from terminal adapters: the Claude Code adapter supplies the ready, blocked, busy and collapsed patterns as a `PromptInput`; the argv comes from the profile's instance. The cmux module treats them as data.

**Verification status.** Command names, flags and semantics above come from `cmux --help`, each subcommand's `--help` and cmux's published CLI contract (cmux 0.65.0). Observed live: workspace IDs look like `workspace:1000000054` and surface IDs like UUIDs; the adapter accepts both shapes and fails closed if no workspace ID appears. The surface recorded is the workspace's only panel; with several it records none and targets the workspace. The Claude Code screen patterns come from Claude Code's known idle-footer text and are likewise unobserved. Run the live check from a cmux terminal to confirm both: `AGENT_LAUNCHER_LIVE=1 AGENT_LAUNCHER_LIVE_DIR=<an existing git repository Claude Code already trusts> uv run pytest tests/test_live_cmux.py -s` (goes through `open_task` with a temp launcher home and a profile setting `CLAUDE_CONFIG_DIR=~/.claude-personal`, uses that directory as the repo without creating anything, opens one workspace, never submits, closes it). In an untrusted directory Claude shows its folder-trust dialog; the adapter detects it, pastes nothing and falls back to the clipboard.

## Adding an adapter

1. Subclass `TerminalAdapter` in its own module, set `name`, implement `capabilities`, `available`, `create_session`, and any optional operation you declare.
2. Register the name in `terminal_select.select_adapter`.
3. Test it against the same expectations as the mock; the core's tests run on the mock and need no terminal installed.
