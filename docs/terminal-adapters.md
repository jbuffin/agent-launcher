# Terminal adapters

The core never imports a terminal's types. It talks to `terminals.TerminalAdapter` using typed dataclasses.

## Interface (`agent_launcher/terminals.py`)

- `capabilities() -> set[str]`: any of `create_session`, `focus_session`, `discover_sessions`, `prepare_prompt`, `submit_prompt`, `restore_session`, `close_session`.
- `available() -> bool`: can this adapter be used right now?
- `create_session(CreateSessionRequest) -> CreateSessionResult` (required). The request has the title, working directory, `command` argv (run directly, never through a shell), the environment, an optional prompt and `submit_prompt` (false = prepare it for review; true when config `prompt_execution` is `execute`). The result carries a `TerminalSessionRef` (`adapter`, `workspace_id`, `surface_id`) and whether the prompt was prepared or submitted.
- `focus_session`, `discover_sessions`, `close_session`: optional. The base class raises `UnsupportedCapability` (code `unsupported_capability`); override only what the adapter declares. `require(capability)` raises the same error up front.

`send_text` (SPEC §19) is not in the interface yet; it arrives with the first adapter that needs it. `open` requires `create_session`, plus `prepare_prompt` (or `submit_prompt` when config `prompt_execution` is `execute`), before it starts a session.

Environment values may hold secrets. An adapter must not log or record them.

## Selection

`terminal.adapter` in config names the adapter; `open --terminal NAME` overrides it for one run. An unknown or unimplemented name is a `terminal_adapter_unavailable` error. The launcher never switches adapters on its own.

## Mock adapter

`terminal_mock.MockTerminalAdapter` launches nothing. It keeps its calls in `calls`, and when built with a path appends them to a JSON file. Workspace IDs are numbered from the calls already recorded, so they stay unique across invocations. Selected by name, it writes `mock-terminal.json` under the launcher home, so after `agent-launcher open <task> --terminal mock` you can read what would have launched (environment variable names only, no values). The cmux adapter is not implemented yet.

## Adding an adapter

1. Subclass `TerminalAdapter` in its own module, set `name`, implement `capabilities`, `available`, `create_session`, and any optional operation you declare.
2. Register the name in `terminal_select.select_adapter`.
3. Test it against the same expectations as the mock; the core's tests run on the mock and need no terminal installed.
