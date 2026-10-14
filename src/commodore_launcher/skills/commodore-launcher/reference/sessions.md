# Tasks, sessions, terminals, diagnostics

## Tasks

```
commodore tasks list|show <id> [--json]
commodore tasks archive|unarchive <id> [--json]
```

Only `open <github-url>` creates a task. A task starts in the repository's checkout; the launcher never checks out, resets, stashes or cleans it. Tasks copied from the old launcher are archived; `open <url>` of one starts a fresh session. Copied local tasks stay listed and cannot be opened.

## Session commands

| Command | Use |
| --- | --- |
| `open <github-url-or-task-id> [--harness A] [--terminal mock]` | first open starts a session; later opens focus it (or resume it when its harness exited or terminal vanished; a harness that cannot resume starts a fresh conversation on the same session) |
| `resume <task> [--force --yes]` | resume the stored conversation in a new terminal session; `--force` can run a second process on a live conversation |

A task has one primary session. A task the user archived refuses `open` and `resume` until `tasks unarchive`. Codex has no resume, so `open` starts a fresh conversation. Run `open` from a cmux terminal; cmux refuses other processes.

## Terminal adapters

`terminal.adapter` is `cmux`; `--terminal mock` records what would launch in `mock-terminal.json` (environment variable names only). The launcher does not change cmux settings.

## Diagnostics

```
commodore doctor [--json]               # exit 1 on any FAIL
commodore --debug <command>             # flag goes before the command; traces to stderr and the log
commodore diagnostics export [-o FILE]  # redacted .tar.gz; read it before sharing
```

Logs: `logs/commodore-launcher.log` (JSON lines, rotated by `logs.max_bytes` and `logs.backup_count`). Tokens, secret-looking variables, home paths and prompts are redacted; redaction is best effort.
