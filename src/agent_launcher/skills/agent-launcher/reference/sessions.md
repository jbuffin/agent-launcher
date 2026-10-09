# Tasks, sessions, terminals, diagnostics

## Tasks

```
agent-launcher new --title T --repo PATH [--description D] [--offline] [--json]
agent-launcher tasks list|show <id> [--json]
agent-launcher tasks link <task> <github-url> [--json]
agent-launcher worktrees list | inspect <task> | associate <task> <path> [--force]
```

`new` needs a repository with a profile (asked once on a terminal; otherwise run `profile set`). `tasks link` keeps the task, worktree, session and profile and refuses on a repository mismatch, profile mismatch, an item owned by another task, or a task linked elsewhere; each refusal writes nothing and has a code. Each task has its own Git worktree; the launcher never resets, stashes, cleans or removes one.

## Session commands

| Command | Use |
| --- | --- |
| `open <task-or-url> [--agent A] [--terminal mock]` | first open starts a session; later opens focus it (or resume it when its agent exited or terminal vanished) |
| `prompt <task>` | prepare the prompt again, unsubmitted (after Claude Code's folder-trust dialog) |
| `resume <task> [--force --yes]` | resume the stored conversation in a new terminal session; `--force` can run a second process on a live conversation |
| `restart <task> [--yes]` | new conversation in a new terminal session; asks first |
| `sessions candidates <task>` / `sessions adopt <task> <workspace> [--agent A]` | list and adopt a terminal session the user started themselves; adopted sessions are never closed or moved |

A task has one primary session. An archived task refuses `open`, `resume`, `prompt` and `restart`. Codex has no resume; use `restart`. Run `open` from a cmux terminal; cmux refuses other processes.

## Terminal adapters

`terminal.adapter` is `cmux`; `--terminal mock` records what would launch in `mock-terminal.json` (environment variable names only). The launcher does not change cmux settings.

## Diagnostics

```
agent-launcher doctor [--json]               # exit 1 on any FAIL
agent-launcher --debug <command>             # flag goes before the command; traces to stderr and the log
agent-launcher diagnostics export [-o FILE]  # redacted .tar.gz; read it before sharing
```

Logs: `logs/agent-launcher.log` (JSON lines, rotated by `logs.max_bytes` and `logs.backup_count`). Tokens, secret-looking variables, home paths and prompts are redacted; redaction is best effort.
