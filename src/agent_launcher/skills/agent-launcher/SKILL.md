---
name: agent-launcher
description: Manage Agent Launcher itself, covering its config.json, profiles, agent instances, repository-to-profile associations, workflows.json routing, prompt templates, tasks, sessions, terminal adapter and diagnostics. Use when asked to set up, change, inspect or troubleshoot the `agent-launcher` CLI or its configuration.
---

# Managing Agent Launcher

Agent Launcher launches coding agents against GitHub issues, PRs and local tasks. Everything you manage is reached through the `agent-launcher` CLI. Run `agent-launcher <command> --help` for exact flags; this skill gives the rules and the map.

## Hard rules

1. **Change configuration through the CLI** (`setup`, `profile add|edit|set`, `workflows init`) **or a validated edit of the JSON files**, then run `agent-launcher config validate`. A change is done when `config validate` passes and `agent-launcher doctor` shows no `FAIL`. Edit JSON by changing only the keys you mean to; keep unknown keys.
2. **`state.db` is read through commands only.** Use `tasks list|show`, `worktrees list|inspect`, `sessions candidates`, `profile which`. Writing the database, or a repository association, by any other route breaks the profile guarantees.
3. **A repository's profile changes only with `profile set`.** When it has tasks, the command lists them and wants one resolution (`--archive-tasks`, `--keep-tasks` or `--cancel`). Show the user that list and the choice; pass the flag only after the user picks it. Without a terminal the command refuses (`reassignment_requires_resolution`) and changes nothing.
4. **Stay inside the launcher's home** (`~/.agent-launcher/`, or `$AGENT_LAUNCHER_HOME`). Every agent keeps its own config directory (`~/.claude*`, `~/.codex*`, `$COPILOT_HOME`); you may name one in an instance's `env`, and read nothing from it and write nothing to it.
5. **Keep secrets out of config.** Instance `env` holds paths and names; the terminal scrollback and cmux manifests keep them in plaintext.
6. **Workflows never grant anything.** A workflow picks a skill, template and preferred agent. It never selects a profile or adds an agent.
7. **Ask before irreversible or identity-changing steps**: reassigning a profile, `restart`, `resume --force`, `sessions adopt`, `worktrees associate --force`.

## Orient first

```
agent-launcher doctor --json          # tools, config, profiles, database
agent-launcher config show --json     # effective config, defaults overlaid
agent-launcher profile list --json
agent-launcher tasks list --json
agent-launcher workflows list --json
```

Prefer `--json` when you parse output; failures print `{"error": {"code", "message", ...}}` with exit 1. The `code` tells you the next step.

## Task map

| The user wants... | Do |
| --- | --- |
| first-time setup, add a profile with agents | `setup` (interactive) or `setup --answers FILE --dry-run`, then `--yes`. See [reference/config.md](reference/config.md) |
| a new profile or agent instance | `profile add` / `profile edit`, then `profile check <name>` |
| a repository on a profile | `profile set <repo> <profile>`; inspect with `profile which <repo>` |
| route issues/PRs to skills | `workflows init`, edit `workflows.json`, `workflows test <url-or-id>`. See [reference/workflows.md](reference/workflows.md) |
| prompt templates, execute mode | `templates/<name>.txt`, workflow `template` / `prompt_execution`. See [reference/workflows.md](reference/workflows.md) |
| a stuck, exited or missing session | `tasks show`, then `open`, `prompt`, `resume`, `restart`. See [reference/sessions.md](reference/sessions.md) |
| attach a task to an issue/PR | `tasks link <task> <github-url>` |
| something is broken | `doctor`, `--debug <command>`, `diagnostics export`. See [reference/sessions.md](reference/sessions.md#diagnostics) |

## Concepts

- **Profile**: a named identity (`work`, `personal`). Holds its own agent instances and a `default_agent`. Profiles are not OS sandboxes.
- **Agent type** (global: `claude`, `codex`, `copilot` built in) versus **agent instance** (`profiles.<name>.agents.<type>`: executable, args, env, ...). An instance inherits the caller's environment; set every identity variable (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `COPILOT_HOME`) explicitly on each instance. Two profiles using one agent need different values.
- **Repository association**: one explicit profile per repository, kept in `state.db`, keyed by GitHub repository ID when known. Nothing is inferred from owner, default or workflow.
- **Task** (`t-xxxxxxxx`): durable work item, local or GitHub; owns one worktree, one primary session. Task IDs can be given as a unique prefix.
- **Session**: task + profile + agent + agent conversation + terminal session. `open` focuses an existing one.
- **Workflow**: a rule in `workflows.json` chosen once at a task's first open.

## Verify your work

After any change: `agent-launcher config validate`, `agent-launcher doctor`, and for profile work `agent-launcher profile check <name>`. Report the commands you ran and their results to the user.
