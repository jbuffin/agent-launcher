# Tasks, sessions, diagnostics

## Tasks

```
commodore tasks list|show <id> [--json]
commodore tasks archive|unarchive <id> [--json]
```

Only `open <github-url>` creates a task. A task starts in the repository's checkout; the launcher never checks out, resets, stashes or cleans it. Tasks copied from the old launcher are archived; `open <url>` of one starts a fresh session. Copied tasks that have no GitHub URL stay listed and cannot be opened.

## Session commands

| Command | Use |
| --- | --- |
| `open <github-url-or-task-id> [--harness A] [--dry-run [--json]]` | runs the harness in this terminal: the first open starts a session, later opens resume its conversation (a harness that cannot resume, or never saved the conversation, starts a fresh one on the same session) |
| `resume <task> [--force --yes] [--dry-run [--json]]` | resume the stored conversation in this terminal; `--force` can run a second process on a live conversation |

A task has one primary session. While its harness runs it holds the task's session lock, and `open` or `resume` of that task anywhere else is refused (`harness_running`, with the pid); `tasks show` says whether it runs. A task the user archived refuses `open` and `resume` until `tasks unarchive`. Codex has no resume, so `open` starts a fresh conversation.

These commands take over the terminal they run in until the harness exits, so do not run them yourself from inside a harness: give the user the command to type in a terminal. `--dry-run --json` shows what would run and starts nothing; that one is safe to run.

## Diagnostics

```
commodore doctor [--json]               # exit 1 on any FAIL
commodore --debug <command>             # flag goes before the command; traces to stderr and the log
commodore diagnostics export [-o FILE]  # redacted .tar.gz; read it before sharing
```

Logs: `logs/commodore-launcher.log` (JSON lines, rotated by `logs.max_bytes` and `logs.backup_count`). Tokens, secret-looking variables, home paths and prompts are redacted; redaction is best effort.
