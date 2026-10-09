# Agent Launcher

Agent Launcher is a command-line tool that starts an AI coding agent (Claude Code, Codex CLI or GitHub Copilot CLI) on a GitHub issue, a pull request or a local task. For each task it:

- finds the repository on your machine (or clones it after asking) and uses the **profile** you assigned to that repository, so work and personal identities never mix;
- creates an isolated Git worktree for the task;
- starts the agent in a [cmux](https://cmux.com) workspace in that worktree and leaves the prompt in its input box for you to review;
- remembers the task, its worktree and its agent conversation, so opening the same issue again focuses the session you already have.

It runs only when you call it (no server). Configuration is plain JSON under `~/.agent-launcher/`, state is a SQLite file next to it. The full design is in [SPEC.md](SPEC.md); this README describes what works today.

**Profiles separate identities by configuration, not by sandboxing.** A profile decides which executable, `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and environment an agent starts with, and Agent Launcher never falls back to another profile's agent. The agent itself still runs as you and can read everything you can. See [docs/security.md](docs/security.md).

## Install

Requires Python 3.11+, [pipx](https://pipx.pypa.io), and Git. To launch tasks you also need [`gh`](https://cli.github.com) (logged in), [cmux](https://cmux.com) and at least one agent CLI. `agent-launcher doctor` reports what is missing.

```bash
pipx install git+https://github.com/jbuffin/agent-launcher.git   # from GitHub
pipx install .                                                   # from a checkout
pipx install -e .                                                # editable, for development
pipx upgrade agent-launcher                                      # later; config and state are kept
agent-launcher version
```

Configuration and state live outside the installed package (`~/.agent-launcher/`), so installing, upgrading or removing the package never touches them. The bundled management skill ships inside the package; `agent-launcher skill path` prints where.

## Quick start

```bash
agent-launcher setup                       # profiles and agents, once (see "Initial setup")
agent-launcher profile set ~/code/widgets work
agent-launcher open https://github.com/acme/widgets/issues/7     # from a cmux terminal
```

The first `open` of a repository you have not used before asks which profile it belongs to (and offers to clone it if it is not on this machine). From then on `open <url>` for the same issue focuses the existing session.

## Initial setup

`agent-launcher setup` detects git, `gh`, cmux and the agents on your machine, shows what it found, and asks for at least one profile: its name, its agents, each agent's executable and identity directory. It shows a diff of `config.json` before writing and is safe to re-run (it only adds). For scripts: `setup --answers answers.json --yes`. Details, the answers file and every setting it stores are in [docs/setup.md](docs/setup.md).

Run `agent-launcher doctor` afterwards: it checks the configuration, tools, profiles and database, and says how to fix each problem.

## Creating profiles

A profile is a named identity such as `work` or `personal`. Names are yours to choose.

```bash
agent-launcher profile add work --agent claude --env CLAUDE_CONFIG_DIR=~/.claude-work
agent-launcher profile add personal --agent claude --executable ~/bin/claude-personal
agent-launcher profile list
agent-launcher profile check work                   # resolves the agent without starting it
agent-launcher profile set <repo> work              # assign a repository (path, owner/name or URL)
agent-launcher profile which <repo>
```

A repository has exactly one profile, chosen by you. It is stored under GitHub's immutable repository ID, so renames and transfers keep it. Unknown repositories are never assigned automatically, and changing an assignment while the repository has tasks needs an explicit decision. See [docs/profiles.md](docs/profiles.md) and [docs/security.md](docs/security.md).

## Configuring agents

Each profile has its own agent instances: executable (or wrapper script), arguments, environment such as `CLAUDE_CONFIG_DIR`, `CODEX_HOME` or `COPILOT_HOME`, working directory, and how prompts, skills and resume work. Claude Code, Codex CLI and GitHub Copilot CLI are built in. If two profiles use the same agent, each must set a different identity directory. See [docs/agents.md](docs/agents.md).

Which agent runs is chosen with `agent_selection` (`always_ask` by default, `use_default`, `ask_if_multiple`) or `open --agent NAME`. Only the repository's profile's agents are ever offered.

## Launching GitHub tasks

```bash
agent-launcher open https://github.com/acme/widgets/issues/7
agent-launcher open https://github.com/acme/widgets/pull/12
```

Agent Launcher reads the issue or PR through `gh`, finds the task by GitHub's stable IDs (or creates it), finds or clones the repository, makes sure it has a profile, creates the worktree, and starts the agent. Your own PR is checked out on its head branch; anyone else's (or a fork's) in an isolated `review/pr-N` worktree. The agent's prompt is the URL (or the workflow's skill invocation of it); it is prepared but **not submitted** unless you pass `--execute` or set `prompt_execution` to `execute`. Issue titles and bodies are untrusted and never reach a command or the prompt. See [docs/github-integration.md](docs/github-integration.md).

Opening the same issue again focuses the existing session: no new worktree, agent choice or prompt. To open from [gh-dash](docs/gh-dash.md), `agent-launcher integrate gh-dash` has an agent add the shortcut for you.

## Launching local tasks

```bash
agent-launcher new --title "Fix the parser" --repo ~/code/widgets
agent-launcher open t-3k9m2x7q
agent-launcher tasks list
agent-launcher tasks link t-3k9m2x7q https://github.com/acme/widgets/issues/9   # later, if it becomes an issue
```

A local task has a stable ID that never changes, whatever happens to it later. Linking it to an issue keeps its ID, worktree, session and conversation; opening the issue then finds the same task.

## Configuring workflows

A workflow rule in `~/.agent-launcher/workflows.json` picks the skill the agent runs for a task, for example `/code-review <url>` for a PR that requests your review. Rules are matched by priority, then file order; a task is routed once, at its first open.

```bash
agent-launcher workflows init               # writes an example file
agent-launcher workflows list
agent-launcher workflows test <url-or-id>   # which rule matches, and why (read only)
```

See [docs/workflows.md](docs/workflows.md), including prompt templates and execute mode.

## Managing worktrees

Each task gets its own Git worktree under `~/.agent-launcher/worktrees`, cut from the repository's base branch. Existing worktrees that fit a task are offered for adoption instead of creating another. The launcher never removes a worktree on its own.

```bash
agent-launcher worktrees list
agent-launcher worktrees inspect <task>
agent-launcher worktrees associate <task> <path>    # adopt one explicitly
agent-launcher tasks completed                      # which tasks' issues or PRs are closed or merged
agent-launcher cleanup --dry-run                    # what could be removed, and why not the rest
```

Cleanup is conservative: only launcher-created worktrees of completed or archived tasks, only when clean, fully pushed and unused, and only after you confirm. See [docs/worktrees.md](docs/worktrees.md) and [docs/cleanup.md](docs/cleanup.md).

## Resuming sessions

```bash
agent-launcher open <task>                  # focuses the session; resumes it if the agent exited or the workspace is gone
agent-launcher resume <task> [--force]
agent-launcher prompt <task>                # type the prepared prompt again, unsubmitted
agent-launcher restart <task>               # a fresh conversation in the same worktree
agent-launcher sessions candidates <task>   # agents you started yourself that may belong to the task
agent-launcher sessions adopt <task> <workspace>
```

A task has one primary session. See [docs/sessions.md](docs/sessions.md) for what is resumable per agent and what "exited" means.

## Troubleshooting

- `agent-launcher doctor` first: it names what is missing and the fix. `doctor --json` is for scripts.
- `agent-launcher --debug <command>` traces each decision to stderr and to `~/.agent-launcher/logs/`.
- `agent-launcher diagnostics export` writes a redacted `.tar.gz` for a bug report. Read it before sharing.
- "cmux did not answer": cmux accepts commands only from terminals it started. Run `agent-launcher` from a cmux terminal, or use `--terminal mock` to try the flow without one.
- The prompt landed on the clipboard instead of the input box: the agent was showing a dialog (folder trust, login) or its screen was not recognised. Handle the dialog, then `agent-launcher prompt <task>`.
- A launch that failed half way (for example cmux was down) leaves the task `launch_failed` with its worktree; run `open` again and it continues without duplicates.
- Config errors name the field. `config validate` checks the file, `config migrate` upgrades an older version (with a backup), `config edit` edits a copy and saves it only if it validates. See [docs/config-migrations.md](docs/config-migrations.md).

More: [docs/diagnostics.md](docs/diagnostics.md). Before you rely on a new machine's cmux and agents, work through the [live validation checklist](docs/live-validation.md).

## Command reference

| Command | What it does |
| --- | --- |
| `agent-launcher version [--json]` | Print the installed version. |
| `agent-launcher config show [--json]` | Show the effective config: defaults overlaid with `config.json`, unknown fields included. |
| `agent-launcher config validate [--json] [--strict]` | Check `config.json`. Errors name the field and exit 1. Unknown fields are reported as warnings; `--strict` makes them fail. |
| `agent-launcher config migrate [--dry-run] [--yes] [--json]` | Bring `config.json` and `workflows.json` to the version this release writes: shows the plan and a diff, backs the file up to `backups/`, writes atomically, validates, restores the backup on failure. See [docs/config-migrations.md](docs/config-migrations.md). |
| `agent-launcher config edit` | Edit `config.json` in `$VISUAL`/`$EDITOR` on a copy; saved only if it validates. See [docs/config-migrations.md](docs/config-migrations.md). |
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

Config lives in `~/.agent-launcher/config.json` (set `AGENT_LAUNCHER_HOME` to use a different root, for example in tests). It is plain JSON you can edit by hand. A missing file is fine: defaults apply and nothing is created. Unknown fields are never dropped; `config validate` reports them. Do not put tokens or credentials in it.

```json
{
  "version": 2,
  "terminal": {"adapter": "cmux"},
  "agent_selection": "always_ask",
  "prompt_execution": "prepare",
  "profiles": {
    "work": {
      "default_agent": "claude",
      "agents": {"claude": {"executable": "claude", "env": {"CLAUDE_CONFIG_DIR": "~/.claude-work"}}}
    }
  }
}
```

Every key, its values and its default are in [docs/configuration.md](docs/configuration.md).

## Documentation

| Document | Covers |
| --- | --- |
| [docs/setup.md](docs/setup.md) | The setup wizard and the answers file |
| [docs/configuration.md](docs/configuration.md) | Every `config.json` key, files and directories |
| [docs/profiles.md](docs/profiles.md), [docs/agents.md](docs/agents.md) | Profiles, repository associations, agent instances, Claude Code, Codex, Copilot |
| [docs/github-integration.md](docs/github-integration.md) | Issues, PRs, task identity, cloning, linking |
| [docs/workflows.md](docs/workflows.md) | Workflow rules, templates, execute mode |
| [docs/worktrees.md](docs/worktrees.md), [docs/cleanup.md](docs/cleanup.md) | Task worktrees, adoption, completion, archiving, cleanup |
| [docs/sessions.md](docs/sessions.md), [docs/terminal-adapters.md](docs/terminal-adapters.md) | Reopen, resume, restart, adoption of sessions; the terminal adapter contract and how to write one |
| [docs/gh-dash.md](docs/gh-dash.md), [docs/management-skill.md](docs/management-skill.md) | gh-dash integration; the bundled management skill |
| [docs/security.md](docs/security.md) | Profile safety rules and what they do not protect against |
| [docs/diagnostics.md](docs/diagnostics.md), [docs/config-migrations.md](docs/config-migrations.md) | `doctor`, logs, redaction; config versions and migrations |
| [docs/architecture.md](docs/architecture.md), [docs/development.md](docs/development.md), [docs/adr/](docs/adr/) | How the pieces fit; working on the code; decisions |
| [docs/definition-of-done.md](docs/definition-of-done.md), [docs/live-validation.md](docs/live-validation.md) | SPEC §37 checked off with evidence; what still needs a person at a cmux terminal |

## Development

```bash
uv sync
uv run pytest -q      # no cmux, no network
```

See [docs/development.md](docs/development.md). Opt-in live tests (`AGENT_LAUNCHER_LIVE=1`, `AGENT_LAUNCHER_PACKAGING=1`) are described there and in [docs/live-validation.md](docs/live-validation.md).

## License

MIT. See [LICENSE](LICENSE).
