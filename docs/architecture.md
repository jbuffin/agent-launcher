# Architecture

`commodore open <task>` runs one thin pipeline. Each stage is a small module with no Typer imports, tested alone.

| Stage | Module | Job |
| --- | --- | --- |
| Task registry | `tasks.py` | Create, list and look up tasks (`source` is `local` or `github`). |
| GitHub issue or PR | `github.py`, `repo_locator.py`, `github_tasks.py` | Fetch the issue or PR, find or clone the repository, find or create the task by GitHub's IDs, then run the pipeline below. See [github-integration.md](github-integration.md). |
| Repository + profile | `repositories.py`, `associations.ensure_profile` | Identify the checkout; use its stored profile or ask once. Nothing is inferred. |
| Harness picker | `picker.py` | Choose among the **profile's own** harnesses; the last harness used in the profile, then the profile's default, is highlighted. |
| Invocation | `launch.py` (`skill_and_argument`, `invocation_for`) | The one internal path for a harness started on a skill with an argument. A GitHub task: `commodore` on its URL. The maintenance session of `configure`: `commodore-launcher` on its request text. A session does not always start on Commodore; only a URL task does. The harness adapter spells the invocation (`/commodore <url>` for Claude Code) and gives the launch arguments that carry it. A harness with no launch form is refused. The title and body of an issue are never in it. |
| Session record | `sessions.py` | Write the session and conversation rows. |
| Session host | `session_host.py`, `locks.py` | Run the harness in this terminal, holding the task's session lock. See [session-host.md](session-host.md). |

`launch.open_task` calls them in that order and returns the harness command with the session lock it took; the CLI runs the harness on the session host once the database is closed. It first checks the checkout at the task's path is still the task's repository (`repository_changed` otherwise, with no prompt and no write), and that the harness can be given the prompt at launch. If the repository's profile no longer matches the task's, or the harness cannot be resolved, it stops; nothing falls back to another profile or harness.

## Two identities

Kept in separate tables in `state.db` (migration 2), because they live and die independently:

- **Task** (`tasks.id`): the durable work. An opaque ID like `t-3k9m2x7q`: random, never derived from a title, path or GitHub issue. What is specific to a source (the GitHub IDs and metadata) is in its own table, `task_github` (migration 6).
- **Harness conversation** (`agent_conversations.conversation_id`): the harness's own conversation ID. The launcher fixes it at launch for harnesses that take one (Claude Code `--session-id`, Copilot `--session-id`); Codex generates its own and none is stored ([ADR 0005](adr/0005-one-session-per-task-conversation-id-at-launch.md)).


Where the harness runs is not recorded: whether it is running is the task's session lock ([ADR 0022](adr/0022-harness-runs-in-the-invoking-terminal.md)). Migration 12 dropped the `terminal_sessions` table that once held cmux workspaces.

A `sessions` row (`s-xxxxxxxx`) ties one launch of one task to one profile/harness and points at its conversation. `tasks.state` is `created`, then `launching` (a first launch in progress or interrupted) or `launch_failed` (it stopped with an error), then `active` once the session is recorded; a task is never `active` before its harness session exists. The launch stages and the locks that make launches safe are in [ADR 0007](adr/0007-transactional-launch.md).

## Harness selection

Config `harness_selection`:

- `always_ask` (default): ask every time, even with one harness.
- `use_default`: the profile's default harness without asking. With no default, behaves like `ask_if_multiple`.
- `ask_if_multiple`: ask only when the profile has more than one harness.

The prompt highlights the harness last launched under this profile, then the profile's default. `--harness` skips the prompt but must name one of the profile's harnesses. Without a terminal (or with `--json`) a needed question is an `harness_selection_needed` error listing the choices.

## Non-interactive use and errors

With `--json`, every command prints one JSON object (`open`, `resume` and `configure` only with `--dry-run`: they run the harness in the terminal, so there is nothing to print until it exits). Failures are `{"error": {"code", "message", ...}}` with exit code 1. Codes from this pipeline: `needs_issue_or_pr_url`, `task_needs_url`, `task_not_found`, `ambiguous_task`, `invalid_task`, `harness_selection_needed`, `harness_not_in_profile`, `no_harnesses`, `harness_unresolved`, `profile_mismatch`, `task_archived`, `repo_missing`, `repository_changed`, `harness_no_launch_form`, `harness_running`, `harness_start_failed`, `json_needs_dry_run`, `skills_missing`, plus the repository and profile codes from [security.md](security.md).

A task that already has a session is not run through this pipeline again: `open` resumes it (or refuses while its harness runs), and `resume` acts on it. See [sessions.md](sessions.md).
