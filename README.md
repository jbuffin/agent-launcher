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
| `agent-launcher tasks link <task> <github-url>` | Attach a GitHub issue or PR to an existing local task. The ID, worktree, session, conversation, profile and workflow stay; `open <url>` then finds the task. Refused (nothing changed) for another repository, another profile, an item that belongs to another task, or a task already linked elsewhere. See [docs/github-integration.md](docs/github-integration.md). |
| `agent-launcher tasks completed [--offline]` | Re-read each open GitHub task from GitHub and list those whose issue is closed or whose PR is closed or merged (eligible for cleanup). Nothing is terminated or deleted. |
| `agent-launcher tasks archive <task>` / `tasks unarchive <task>` | Archive a task (metadata only: files, worktree and session record stay; `open` refuses it) or restore it so it can be opened again. |
| `agent-launcher cleanup [<task>...] [--dry-run] [--yes] [--json]` | Offer to remove launcher-created worktrees of completed or archived tasks, each only after every safety check passes and you confirm. Adopted and dirty worktrees are never removed. See [docs/cleanup.md](docs/cleanup.md). |
| `agent-launcher open <id> [--agent A] [--terminal mock]` | Pick one of the profile's agents (config `agent_selection`), build the prompt (title plus description) and start a session through the terminal adapter. Runs in the task's own Git worktree (created from the base branch, or one you adopt; see [docs/worktrees.md](docs/worktrees.md)). A task that already has a session is focused instead (no picker, no new prompt, nothing created); see [docs/sessions.md](docs/sessions.md). |
| `agent-launcher open <issue-or-pr-url> [--agent A] [--terminal mock]` | Open a GitHub issue or pull request (your own PR on its head branch, anyone else's in an isolated review worktree): fetch it through `gh`, find the task by GitHub's stable IDs (or create it), find the repository checkout (offering to clone it, never silently), make sure the repository has its profile, create the issue's worktree and start the agent with the URL as its prompt. See [docs/github-integration.md](docs/github-integration.md). |
| `agent-launcher open <url-or-id> --workflow ID \| --ask-workflow` | Choose the workflow yourself at a task's first open instead of routing it. |
| `agent-launcher open <url-or-id> --execute \| --prepare` | Submit the prompt automatically, or only prepare it, overriding the workflow's and the global `prompt_execution` (default: prepare). Prompts can come from templates in `~/.agent-launcher/templates/`; see [docs/workflows.md](docs/workflows.md). |
| `agent-launcher workflows list\|test <url-or-id>\|init` | Show the rules in `workflows.json`, explain which rule a task matches and why (read only), or write an example file. Workflows pick the skill the agent is asked to run (`/code-review <url>`). See [docs/workflows.md](docs/workflows.md). |
| `agent-launcher worktrees list\|inspect <id>\|associate <id> <path>` | See which worktree each task has and who owns it, or adopt an existing worktree for a task. See [docs/worktrees.md](docs/worktrees.md). |
| `agent-launcher sessions candidates <id>` / `sessions adopt <id> <workspace> [--agent A]` | List terminal sessions you started yourself that match a task on concrete evidence (never just "an agent is in the repo"), and adopt one explicitly. Adopted sessions are never closed or moved. See [docs/sessions.md](docs/sessions.md#adopting-an-external-session). |
| `agent-launcher resume <id> [--force]` | Resume the task's agent conversation in a new terminal session when it cannot be focused. See [docs/sessions.md](docs/sessions.md). |
| `agent-launcher prompt <id>` | Prepare the task's prompt again, unsubmitted, in its existing session (the recovery after accepting Claude Code's folder-trust dialog). |
| `agent-launcher restart <id> [--yes] [--execute\|--prepare]` | Start the agent afresh in a new terminal session after confirmation; keeps the task and its worktree. |
| `agent-launcher configure ["request"] [--repo PATH] [--profile NAME] [--agent A]` | Launch an agent with the bundled management skill on a local maintenance task (found again on repeat), through the normal `open` path. See [docs/management-skill.md](docs/management-skill.md). |
| `agent-launcher integrate gh-dash [--print-prompt] [--repo PATH] [--profile NAME] [--agent A]` | Have an agent add an Agent Launcher shortcut (issues and PRs) to gh-dash, as a `configure`-style task with a prepared prompt. Agent Launcher itself writes no gh-dash config. Offered at the end of `setup` when gh-dash is installed (default No). `open --picker-surface` runs the picker in a temporary cmux surface when there is no terminal. See [docs/gh-dash.md](docs/gh-dash.md). |
| `agent-launcher skill path [--json]` | Print where the bundled `agent-launcher` skill lives; install it with `npx skills add` (see [docs/management-skill.md](docs/management-skill.md)). |
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
- `terminal`, `repositories`, `workflow_routing`, `prompt_execution`, `agent_selection`: stored by `setup` with defaults; see [docs/setup.md](docs/setup.md). `agent_selection` and `terminal.adapter` are in use: see [docs/architecture.md](docs/architecture.md). `open` launches in a cmux workspace and leaves the prompt unsubmitted in Claude Code's input box (run it from a cmux terminal; see [docs/terminal-adapters.md](docs/terminal-adapters.md)); `--terminal mock` (or `terminal.adapter: mock`) records launches in `mock-terminal.json` instead. `workflow_routing` is in use: see [docs/workflows.md](docs/workflows.md). The others have no behaviour yet.
- `agent_types`, `profiles`: see [docs/agents.md](docs/agents.md) and [docs/profiles.md](docs/profiles.md).

Fields the installed version doesn't recognise are never dropped: `validate` reports them and programmatic updates keep them. Do not put tokens or credentials in config.

## Development

See [docs/development.md](docs/development.md).
