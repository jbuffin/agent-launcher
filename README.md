<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/hero-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/hero-light.png">
  <img alt="Commodore Launcher: plans the mission, assembles a task force of coding agents and sees the work through to a merged PR" src="docs/images/hero-light.png" width="1280">
</picture>

# Commodore Launcher

Commodore Launcher is a command-line tool that starts a harness (Claude Code, Codex CLI or GitHub Copilot CLI) on a GitHub issue, a pull request or a local task. For each task it:

- finds the repository on your machine (or clones it after asking) and uses the **profile** you assigned to that repository, so work and personal identities never mix;
- creates an isolated Git worktree for the task;
- starts the harness in a [cmux](https://cmux.com) workspace in that worktree and leaves the prompt in its input box for you to review;
- remembers the task, its worktree and its harness conversation, so opening the same issue again focuses the session you already have.

It runs only when you call it (no server). Configuration is plain JSON under `~/.commodore-launcher/`, state is a SQLite file next to it. The full design is in [SPEC.md](SPEC.md); this README describes what works today.

**Profiles separate identities by configuration, not by sandboxing.** A profile decides which executable, `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and environment a harness starts with, and Commodore Launcher never falls back to another profile's harness. The harness itself still runs as you and can read everything you can. See [docs/security.md](docs/security.md).

## Install

Requires Python 3.11+, [pipx](https://pipx.pypa.io), and Git. To launch tasks you also need [`gh`](https://cli.github.com) (logged in), [cmux](https://cmux.com) and at least one harness CLI. `commodore doctor` reports what is missing.

```bash
pipx install git+https://github.com/jbuffin/agent-launcher.git   # from GitHub
pipx install .                                                   # from a checkout
pipx install -e .                                                # editable, for development
pipx upgrade commodore                                      # later; config and state are kept
commodore version
```

Configuration and state live outside the installed package (`~/.commodore-launcher/`), so installing, upgrading or removing the package never touches them.

### Install the agent skill

`pipx install` does not install the `commodore-launcher` skill for your harnesses. Install it next: with the skill, a harness can change your configuration for you (add a search root, a profile), and `commodore configure` depends on it.

```bash
npx skills add jbuffin/agent-launcher --skill commodore-launcher -g    # asks which harnesses
```

`npx skills` writes only to the default directories (`~/.claude/skills`, `~/.agents/skills`) and ignores `CLAUDE_CONFIG_DIR`. If a profile uses its own config directory, copy the skill into it as well:

```bash
cp -R "$(commodore skill path)" ~/.claude-work/skills/
```

A copy does not follow upgrades, so copy it again after `pipx upgrade`. See [docs/management-skill.md](docs/management-skill.md).

## Quick start

```bash
commodore setup                       # profiles and harnesses, once (see "Initial setup")
npx skills add jbuffin/agent-launcher --skill commodore-launcher -g   # the agent skill (see "Install the agent skill")
commodore profile set ~/code/widgets work
commodore open https://github.com/acme/widgets/issues/7     # from a cmux terminal
```

The first `open` of a repository you have not used before asks which profile it belongs to (and offers to clone it if it is not on this machine). From then on `open <url>` for the same issue focuses the existing session.

## Initial setup

`commodore setup` detects git, `gh`, cmux and the harnesses on your machine, shows what it found, and asks for at least one profile: its name, its harnesses, each harness's executable and identity directory. It shows a diff of `config.json` before writing and is safe to re-run (it only adds). For scripts: `setup --answers answers.json --yes`. Details, the answers file and every setting it stores are in [docs/setup.md](docs/setup.md).

Repositories are found only under `repositories.search_roots` (two levels deep). To add a root later, for example when a work checkout lives outside your first one, re-run `setup`, pass `setup --answers` a file holding only `{"search_roots": ["~/work"]}` (roots are added, never replaced), or `commodore config edit`.

Run `commodore doctor` afterwards: it checks the configuration, tools, profiles and database, and says how to fix each problem.

## Creating profiles

A profile is a named identity such as `work` or `personal`. Names are yours to choose.

```bash
commodore profile add work --harness claude --env CLAUDE_CONFIG_DIR=~/.claude-work
commodore profile add personal --harness claude --executable ~/bin/claude-personal
commodore profile list
commodore profile check work                   # resolves the harness without starting it
commodore profile set <repo> work              # assign a repository (path, owner/name or URL)
commodore profile which <repo>
```

A repository has exactly one profile, chosen by you. It is stored under GitHub's immutable repository ID, so renames and transfers keep it. Unknown repositories are never assigned automatically, and changing an assignment while the repository has tasks needs an explicit decision. See [docs/profiles.md](docs/profiles.md) and [docs/security.md](docs/security.md).

## Configuring harnesses

Each profile has its own harnesses: executable (or wrapper script), arguments, environment such as `CLAUDE_CONFIG_DIR`, `CODEX_HOME` or `COPILOT_HOME`, working directory, and how prompts, skills and resume work. Claude Code, Codex CLI and GitHub Copilot CLI are built in. If two profiles use the same harness, each must set a different identity directory. See [docs/agents.md](docs/agents.md).

Which harness runs is chosen with `harness_selection` (`always_ask` by default, `use_default`, `ask_if_multiple`) or `open --harness NAME`. Only the repository's profile's harnesses are ever offered.

## Launching GitHub tasks

```bash
commodore open https://github.com/acme/widgets/issues/7
commodore open https://github.com/acme/widgets/pull/12
```

Commodore Launcher reads the issue or PR through `gh`, finds the task by GitHub's stable IDs (or creates it), finds or clones the repository, makes sure it has a profile, creates the worktree, and starts the harness. Your own PR is checked out on its head branch; anyone else's (or a fork's) in an isolated `review/pr-N` worktree. The harness starts on its invocation of Commodore on the URL (`/commodore <url>` for Claude Code), passed on its command line and **submitted at launch**; there is nothing to configure. Issue titles and bodies are untrusted and never reach a command or the prompt. See [docs/github-integration.md](docs/github-integration.md).

Opening the same issue again focuses the existing session: no new worktree, harness choice or prompt. To open from [gh-dash](docs/gh-dash.md), `commodore integrate gh-dash` has a harness add the shortcut for you.

## Launching local tasks

```bash
commodore new --title "Fix the parser" --repo ~/code/widgets
commodore tasks list
commodore tasks link t-3k9m2x7q https://github.com/acme/widgets/issues/9   # later, if it becomes an issue
```

A local task has a stable ID that never changes, whatever happens to it later. `open` refuses it for now (`needs a GitHub issue or PR URL`): Commodore is invoked on a URL, which a local task has not got. Linking it to an issue keeps its ID, worktree, session and conversation; opening the issue then finds the same task.

## Configuring workflows

There is nothing to configure: workflows, `workflows.json`, prompt templates and the prepare/execute choice were removed. Every new session starts the harness on Commodore, which picks the mission. A `workflows.json` or `templates/` left in your home is ignored, and `commodore doctor` says so once. See [docs/workflows.md](docs/workflows.md).

## Managing worktrees

Each task gets its own Git worktree under `~/.commodore-launcher/worktrees`, cut from the repository's base branch. Existing worktrees that fit a task are offered for adoption instead of creating another. The launcher never removes a worktree on its own.

```bash
commodore worktrees list
commodore worktrees inspect <task>
commodore worktrees associate <task> <path>    # adopt one explicitly
commodore tasks completed                      # which tasks' issues or PRs are closed or merged
commodore cleanup --dry-run                    # what could be removed, and why not the rest
```

Cleanup is conservative: only launcher-created worktrees of completed or archived tasks, only when clean, fully pushed and unused, and only after you confirm. See [docs/worktrees.md](docs/worktrees.md) and [docs/cleanup.md](docs/cleanup.md).

## Resuming sessions

```bash
commodore open <task>                  # focuses the session; resumes it if the harness exited or the workspace is gone
commodore resume <task> [--force]
commodore restart <task>               # a fresh conversation in the same worktree
commodore sessions candidates <task>   # harnesses you started yourself that may belong to the task
commodore sessions adopt <task> <workspace>
```

A task has one primary session. See [docs/sessions.md](docs/sessions.md) for what is resumable per harness and what "exited" means.

## Troubleshooting

- `commodore doctor` first: it names what is missing and the fix. `doctor --json` is for scripts.
- `commodore --debug <command>` traces each decision to stderr and to `~/.commodore-launcher/logs/`.
- `commodore diagnostics export` writes a redacted `.tar.gz` for a bug report. Read it before sharing.
- "cmux did not answer": cmux accepts commands only from terminals it started. Run `commodore` from a cmux terminal, or use `--terminal mock` to try the flow without one.
- The harness showed a dialog (folder trust, login) before it took its prompt: answer it. The prompt was given on the harness's command line, so it is submitted once the dialog is gone.
- A launch that failed half way (for example cmux was down) leaves the task `launch_failed` with its worktree; run `open` again and it continues without duplicates.
- Config errors name the field. `config validate` checks the file, `config migrate` upgrades an older version (with a backup), `config edit` edits a copy and saves it only if it validates. See [docs/config-migrations.md](docs/config-migrations.md).

More: [docs/diagnostics.md](docs/diagnostics.md). Before you rely on a new machine's cmux and harnesses, work through the [live validation checklist](docs/live-validation.md).

## Command reference

| Command | What it does |
| --- | --- |
| `commodore version [--json]` | Print the installed version. |
| `commodore config show [--json]` | Show the effective config: defaults overlaid with `config.json`, unknown fields included. |
| `commodore config validate [--json] [--strict]` | Check `config.json`. Errors name the field and exit 1. Unknown fields are reported as warnings; `--strict` makes them fail. |
| `commodore config migrate [--dry-run] [--yes] [--json]` | Bring `config.json` to the version this release writes: shows the plan and a diff, backs the file up to `backups/`, writes atomically, validates, restores the backup on failure. See [docs/config-migrations.md](docs/config-migrations.md). |
| `commodore config edit` | Edit `config.json` in `$VISUAL`/`$EDITOR` on a copy; saved only if it validates. See [docs/config-migrations.md](docs/config-migrations.md). |
| `commodore setup [--answers FILE --yes] [--dry-run]` | Setup wizard (also offered when you run `commodore` with no config). Shows a diff before writing; safe to re-run. See [docs/setup.md](docs/setup.md). |
| `commodore profile list\|add\|edit\|check` | Manage profiles and their harnesses. See [docs/profiles.md](docs/profiles.md) and [docs/agents.md](docs/agents.md). |
| `commodore profile set <repo> <profile>` / `profile which <repo>` | Associate a repository (path, `owner/name` or GitHub URL) with a profile, or show it. Unknown repositories are never auto-assigned. See [docs/security.md](docs/security.md). |
| `commodore new --title T --repo PATH [--description D]` | Create a local task with a stable ID (`t-xxxxxxxx`). The repository's profile is looked up or asked for once. |
| `commodore tasks list\|show <id>` | List tasks, or show one with its sessions. IDs can be given as a unique prefix. |
| `commodore tasks link <task> <github-url>` | Attach a GitHub issue or PR to an existing local task. The ID, worktree, session, conversation, and profile stay; `open <url>` then finds the task. Refused (nothing changed) for another repository, another profile, an item that belongs to another task, or a task already linked elsewhere. See [docs/github-integration.md](docs/github-integration.md). |
| `commodore tasks completed [--offline]` | Re-read each open GitHub task from GitHub and list those whose issue is closed or whose PR is closed or merged (eligible for cleanup). Nothing is terminated or deleted. |
| `commodore tasks archive <task>` / `tasks unarchive <task>` | Archive a task (metadata only: files, worktree and session record stay; `open` refuses it) or restore it so it can be opened again. |
| `commodore cleanup [<task>...] [--dry-run] [--yes] [--json]` | Offer to remove launcher-created worktrees of completed or archived tasks, each only after every safety check passes and you confirm. Adopted and dirty worktrees are never removed. See [docs/cleanup.md](docs/cleanup.md). |
| `commodore open <id> [--harness A] [--terminal mock]` | Pick one of the profile's harnesses (config `harness_selection`), and start a session through the terminal adapter on the harness's Commodore invocation (GitHub tasks only; a local task is refused). Runs in the task's own Git worktree (created from the base branch, or one you adopt; see [docs/worktrees.md](docs/worktrees.md)). A task that already has a session is focused instead (no picker, no new prompt, nothing created); see [docs/sessions.md](docs/sessions.md). |
| `commodore open <issue-or-pr-url> [--harness A] [--terminal mock]` | Open a GitHub issue or pull request (your own PR on its head branch, anyone else's in an isolated review worktree): fetch it through `gh`, find the task by GitHub's stable IDs (or create it), find the repository checkout (offering to clone it, never silently), make sure the repository has its profile, create the issue's worktree and start the harness on its Commodore invocation of the URL, submitted at launch. See [docs/github-integration.md](docs/github-integration.md). |
| `commodore worktrees list\|inspect <id>\|associate <id> <path>` | See which worktree each task has and who owns it, or adopt an existing worktree for a task. See [docs/worktrees.md](docs/worktrees.md). |
| `commodore sessions candidates <id>` / `sessions adopt <id> <workspace> [--harness A]` | List terminal sessions you started yourself that match a task on concrete evidence (never just "a harness is in the repo"), and adopt one explicitly. Adopted sessions are never closed or moved. See [docs/sessions.md](docs/sessions.md#adopting-an-external-session). |
| `commodore resume <id> [--force]` | Resume the task's harness conversation in a new terminal session when it cannot be focused. See [docs/sessions.md](docs/sessions.md). |
| `commodore restart <id> [--yes]` | Start the harness afresh in a new terminal session after confirmation; keeps the task and its worktree. |
| `commodore configure ["request"] [--repo PATH] [--profile NAME] [--harness A]` | Launch a harness with the bundled management skill on a local maintenance task (found again on repeat), through the normal `open` path. See [docs/management-skill.md](docs/management-skill.md). |
| `commodore integrate gh-dash [--print-prompt] [--repo PATH] [--profile NAME] [--harness A]` | Have a harness add a Commodore Launcher shortcut (issues and PRs) to gh-dash, as a `configure`-style task with a prepared prompt. Commodore Launcher itself writes no gh-dash config. Offered at the end of `setup` when gh-dash is installed (default No). `open --picker-surface` runs the picker in a temporary cmux surface when there is no terminal. See [docs/gh-dash.md](docs/gh-dash.md). |
| `commodore skill path [--json]` | Print where the bundled `commodore-launcher` skill lives; install it with `npx skills add` (see [docs/management-skill.md](docs/management-skill.md)). |
| `commodore doctor [--json]` | Health checks with pass/warn/fail and remediation hints. Exit 1 if any check fails. |
| `commodore diagnostics export [-o FILE]` | Write a sanitised `.tar.gz` for bug reports. |
| `commodore --debug <command>` | Trace decisions to stderr and the log. Works with every command. |

See [docs/diagnostics.md](docs/diagnostics.md) for checks, logs and redaction.

## Configuration

Config lives in `~/.commodore-launcher/config.json` (set `COMMODORE_LAUNCHER_HOME` to use a different root, for example in tests). It is plain JSON you can edit by hand. A missing file is fine: defaults apply and nothing is created. Unknown fields are never dropped; `config validate` reports them. Do not put tokens or credentials in it.

```json
{
  "version": 3,
  "terminal": {"adapter": "cmux"},
  "harness_selection": "always_ask",
  "profiles": {
    "work": {
      "default_harness": "claude",
      "harnesses": {"claude": {"executable": "claude", "env": {"CLAUDE_CONFIG_DIR": "~/.claude-work"}}}
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
| [docs/profiles.md](docs/profiles.md), [docs/agents.md](docs/agents.md) | Profiles, repository associations, harnesses, Claude Code, Codex, Copilot |
| [docs/github-integration.md](docs/github-integration.md) | Issues, PRs, task identity, cloning, linking |
| [docs/workflows.md](docs/workflows.md) | Why workflows, templates and prepare mode were removed |
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

See [docs/development.md](docs/development.md). Opt-in live tests (`COMMODORE_LAUNCHER_LIVE=1`, `COMMODORE_LAUNCHER_PACKAGING=1`) are described there and in [docs/live-validation.md](docs/live-validation.md).

## License

MIT. See [LICENSE](LICENSE).
