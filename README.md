# Agent Launcher

A Python CLI that launches AI coding agents against GitHub issues, PRs and local tasks. The full design is in [SPEC.md](SPEC.md). This repo is at the foundation stage: only the commands below work so far.

## Install

Requires Python 3.11+.

```bash
pipx install .        # regular install
pipx install -e .     # editable, for development
agent-launcher version
```

## Commands

| Command | What it does |
| --- | --- |
| `agent-launcher version [--json]` | Print the installed version. |
| `agent-launcher config show [--json]` | Show the effective config: defaults overlaid with `config.json`, unknown fields included. |
| `agent-launcher config validate [--json] [--strict]` | Check `config.json`. Errors name the field and exit 1. Unknown fields are reported as warnings; `--strict` makes them fail. |
| `agent-launcher setup [--answers FILE --yes] [--dry-run]` | Setup wizard (also offered when you run `agent-launcher` with no config). Shows a diff before writing; safe to re-run. See [docs/setup.md](docs/setup.md). |
| `agent-launcher profile list\|add\|edit\|check` | Manage profiles and their agent instances. See [docs/profiles.md](docs/profiles.md) and [docs/agents.md](docs/agents.md). |
| `agent-launcher profile set <repo> <profile>` / `profile which <repo>` | Associate a repository (path, `owner/name` or GitHub URL) with a profile, or show it. Unknown repositories are never auto-assigned. See [docs/security.md](docs/security.md). |
| `agent-launcher new --title T --repo PATH [--description D]` | Create a local task with a stable ID (`t-xxxxxxxx`). The repository's profile is looked up or asked for once. |
| `agent-launcher tasks list\|show <id>` | List tasks, or show one with its sessions. IDs can be given as a unique prefix. |
| `agent-launcher open <id> [--agent A] [--terminal mock]` | Pick one of the profile's agents (config `agent_selection`), build the prompt (title plus description) and start a session through the terminal adapter. Runs in the task's own Git worktree (created from the base branch, or one you adopt; see [docs/worktrees.md](docs/worktrees.md)). A task that already has a session is focused instead (no picker, no new prompt, nothing created); see [docs/sessions.md](docs/sessions.md). |
| `agent-launcher worktrees list\|inspect <id>\|associate <id> <path>` | See which worktree each task has and who owns it, or adopt an existing worktree for a task. See [docs/worktrees.md](docs/worktrees.md). |
| `agent-launcher resume <id> [--force]` | Resume the task's agent conversation in a new terminal session when it cannot be focused. See [docs/sessions.md](docs/sessions.md). |
| `agent-launcher prompt <id>` | Prepare the task's prompt again, unsubmitted, in its existing session (the recovery after accepting Claude Code's folder-trust dialog). |
| `agent-launcher restart <id> [--yes]` | Start the agent afresh in a new terminal session after confirmation; keeps the task and its worktree. |
| `agent-launcher doctor [--json]` | Health checks with pass/warn/fail and remediation hints. Exit 1 if any check fails. |
| `agent-launcher diagnostics export [-o FILE]` | Write a sanitised `.tar.gz` for bug reports. |
| `agent-launcher --debug <command>` | Trace decisions to stderr and the log. Works with every command. |

See [docs/diagnostics.md](docs/diagnostics.md) for checks, logs and redaction.

## Configuration

Config lives in `~/.agent-launcher/config.json` (set `AGENT_LAUNCHER_HOME` to use a different root). It is plain JSON you can edit by hand. A missing file is fine: defaults apply and nothing is created.

```json
{
  "version": 2,
  "debug": false
}
```

- `version`: schema version (currently 2; version 1 files are still accepted). Required; a version newer than the installed release supports is rejected.
- `debug`: boolean, default `false`. `true` behaves like always passing `--debug`.
- `logs`: `{"max_bytes": 1000000, "backup_count": 5}`, log rotation. See [docs/diagnostics.md](docs/diagnostics.md).
- `terminal`, `repositories`, `workflow_routing`, `prompt_execution`, `agent_selection`: stored by `setup` with defaults; see [docs/setup.md](docs/setup.md). `agent_selection` and `terminal.adapter` are in use: see [docs/architecture.md](docs/architecture.md). `open` launches in a cmux workspace and leaves the prompt unsubmitted in Claude Code's input box (run it from a cmux terminal; see [docs/terminal-adapters.md](docs/terminal-adapters.md)); `--terminal mock` (or `terminal.adapter: mock`) records launches in `mock-terminal.json` instead. The others have no behaviour yet.
- `agent_types`, `profiles`: see [docs/agents.md](docs/agents.md) and [docs/profiles.md](docs/profiles.md).

Fields the installed version doesn't recognise are never dropped: `validate` reports them and programmatic updates keep them. Do not put tokens or credentials in config.

## Development

See [docs/development.md](docs/development.md).
