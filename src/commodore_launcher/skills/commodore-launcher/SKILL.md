---
name: commodore-launcher
description: Manage Commodore Launcher itself, covering its config.json, profiles, harnesses, repository-to-profile associations, tasks, sessions, terminal adapter and diagnostics. Use when asked to set up, change, inspect or troubleshoot the `commodore` CLI or its configuration.
---

# Managing Commodore Launcher

Commodore Launcher starts a harness on a GitHub issue or PR, on Commodore. Everything you manage is reached through the `commodore` CLI. Run `commodore <command> --help` for exact flags; this skill gives the rules and the map.

## Hard rules

1. **Change configuration through the CLI** (`setup`, `profile add|edit|set`) **or a validated edit of the JSON files**, then run `commodore config validate`. A change is done when `config validate` passes and `commodore doctor` shows no `FAIL`. Edit JSON by changing only the keys you mean to; keep unknown keys.
2. **`state.db` is read through commands only.** Use `tasks list|show`, `profile which`. Writing the database, or a repository association, by any other route breaks the profile guarantees.
3. **A repository's profile changes only with `profile set`.** When it has tasks, the command lists them and wants one resolution (`--archive-tasks`, `--keep-tasks` or `--cancel`). Show the user that list and the choice; pass the flag only after the user picks it. Without a terminal the command refuses (`reassignment_requires_resolution`) and changes nothing.
4. **Stay inside the launcher's home** (`~/.commodore-launcher/`, or `$COMMODORE_LAUNCHER_HOME`). Every harness keeps its own config directory (`~/.claude*`, `~/.codex*`, `$COPILOT_HOME`); you may name one in an instance's `env`, and read nothing from it and write nothing to it.
5. **Keep secrets out of config.** Instance `env` holds paths and names; the terminal scrollback and cmux manifests keep them in plaintext.
6. **The prompt is not configurable.** `open <url>` starts the harness on Commodore (`/commodore <url>` for Claude Code) for the task's issue or PR URL. `configure` and `integrate gh-dash` start `commodore-launcher` on their request text. Delete the leftover files `doctor` reports in the home; nothing reads them.
7. **Ask before irreversible or identity-changing steps**: reassigning a profile, `resume --force`.

## Orient first

```
commodore doctor --json          # tools, config, profiles, database
commodore config show --json     # effective config, defaults overlaid
commodore profile list --json
commodore tasks list --json
```

Prefer `--json` when you parse output; failures print `{"error": {"code", "message", ...}}` with exit 1. The `code` tells you the next step.

## Task map

| The user wants... | Do |
| --- | --- |
| first-time setup, add a profile with harnesses | `setup` (interactive) or `setup --answers FILE --dry-run`, then `--yes`. See [reference/config.md](reference/config.md) |
| `doctor` or `profile add` says the config is an older version | `config migrate --dry-run`, show the diff, then `config migrate`. See [reference/config.md](reference/config.md) |
| a new profile or harness | `profile add` / `profile edit`, then `profile check <name>` |
| a repository on a profile | `profile set <repo> <profile>`; inspect with `profile which <repo>` |
| a stuck, exited or missing session | `tasks show`, then `open` (or `resume`). See [reference/sessions.md](reference/sessions.md) |
| archive or restore a task | `tasks archive <task>`, `tasks unarchive <task>` |
| something is broken | `doctor`, `--debug <command>`, `diagnostics export`. See [reference/sessions.md](reference/sessions.md#diagnostics) |

## Concepts

- **Profile**: a named identity (`work`, `personal`). Holds its own harnesses and a `default_harness`. Profiles are not OS sandboxes.
- **Harness type** (global: `claude`, `codex`, `copilot` built in) versus **harness** (`profiles.<name>.harnesses.<type>`: executable, args, env, ...). An instance inherits the caller's environment; set every identity variable (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `COPILOT_HOME`) explicitly on each instance. Two profiles using one harness need different values.
- **Repository association**: one explicit profile per repository, kept in `state.db`, keyed by GitHub repository ID when known. Nothing is inferred from owner or default.
- **Task** (`t-xxxxxxxx`): one GitHub issue or PR the launcher has opened; has one primary session, started in the repository's own checkout (the launcher creates no worktrees and never changes the checkout). Task IDs can be given as a unique prefix.
- **Session**: task + profile + harness + harness conversation + terminal session. `open` focuses an existing one.

## Verify your work

After any change: `commodore config validate`, `commodore doctor`, and for profile work `commodore profile check <name>`. Report the commands you ran and their results to the user.
