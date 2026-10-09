# Architecture

`agent-launcher open <task>` runs one thin pipeline. Each stage is a small module with no Typer imports, tested alone; later tickets deepen them.

| Stage | Module | Job |
| --- | --- | --- |
| Task registry | `tasks.py` | Create, list and look up local tasks. |
| Repository + profile | `repositories.py`, `associations.ensure_profile` | Identify the checkout; use its stored profile or ask once. Nothing is inferred. |
| Agent picker | `picker.py` | Choose among the **profile's own** agents. |
| Prompt builder | `prompt.py` | Local task: title, plus the description if any. Nothing else. |
| Terminal adapter | `terminals.py`, `terminal_mock.py`, `terminal_select.py` | Start the session in a terminal. See [terminal-adapters.md](terminal-adapters.md). |
| Session record | `sessions.py` | Write the session, conversation and terminal rows. |

`launch.open_task` calls them in that order. It first checks the checkout at the task's path is still the task's repository (`repository_changed` otherwise, with no prompt and no write), and that the adapter can handle the prompt (`prepare_prompt`, or `submit_prompt` when `prompt_execution` is `execute`). If the repository's profile no longer matches the task's, or the agent or adapter cannot be resolved, it stops; nothing falls back to another profile, agent or adapter.

## Three identities

Kept in separate tables in `state.db` (migration 2), because they live and die independently:

- **Task** (`tasks.id`): the durable work. An opaque ID like `t-3k9m2x7q`: random, never derived from a title, path or GitHub issue, so it stays the same when a task is later linked to an issue.
- **Agent conversation** (`agent_conversations.conversation_id`): the agent's own conversation ID. Empty at launch for now; filled in by later tickets.
- **Terminal session** (`terminal_sessions`): adapter, workspace and surface. May disappear while the task survives.

A `sessions` row (`s-xxxxxxxx`) ties one launch of one task to one profile/agent and points at the other two. `tasks.state` is `created`, then `launching` (a first launch in progress or interrupted) or `launch_failed` (it stopped with an error), then `active` once the session is recorded; a task is never `active` before its agent session exists. The launch stages and the locks that make launches safe are in [ADR 0007](adr/0007-transactional-launch.md).

## Agent selection

Config `agent_selection`:

- `always_ask` (default): ask every time, even with one agent.
- `use_default`: the profile's default agent without asking. With no default, behaves like `ask_if_multiple`.
- `ask_if_multiple`: ask only when the profile has more than one agent.

The prompt highlights the agent last launched under this profile, then the profile's default. `--agent` skips the prompt but must name one of the profile's agents. Without a terminal (or with `--json`) a needed question is an `agent_selection_needed` error listing the choices.

## Non-interactive use and errors

With `--json`, every command prints one JSON object. Failures are `{"error": {"code", "message", ...}}` with exit code 1. Codes from this pipeline: `task_not_found`, `ambiguous_task`, `invalid_task`, `agent_selection_needed`, `agent_not_in_profile`, `no_agents`, `agent_unresolved`, `profile_mismatch`, `repo_missing`, `terminal_unavailable`, `terminal_adapter_unavailable`, `repository_changed`, `unsupported_capability`, plus the repository and profile codes from [security.md](security.md).

A task that already has a session is not run through this pipeline again: `open` focuses it or resumes it, and `resume`, `prompt` and `restart` act on it. See [sessions.md](sessions.md).
