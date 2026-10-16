# Session lifecycle

A task has **one primary session** for its whole life. `open` and `resume` act on that one session; neither ever creates a second. The registry enforces it: recording a second session for a task fails with `session_exists`.

Every harness runs in the terminal `open` (or `resume`, or `configure`) was typed in, and `commodore` waits for it to exit. See [session-host.md](session-host.md).

## The two identities

- **Task** (`t-xxxxxxxx`): never changes.
- **Harness conversation** (`agent_conversations.conversation_id`): the harness's own ID. For Claude Code and Copilot CLI the launcher generates a UUID at launch, starts the harness with `--session-id <uuid>` and stores it. Nothing is discovered afterwards. Harnesses that cannot be given an ID at launch (Codex CLI) store none, and so cannot be resumed.

Where a harness runs is not recorded. Whether it is running is the task's **session lock**, which the harness holds for as long as it runs ([session-host.md](session-host.md#the-session-lock)).

## `open <task>`

| The task has... | What happens |
| --- | --- |
| no session | The first-open flow: pick a harness, build the invocation (`/commodore <url>` for Claude Code), record the session and its conversation ID, and run the harness here with the invocation on its command line. The harness submits it at launch. A task copied from the old launcher is unarchived first (see [Copied tasks](#copied-tasks)); a copied local task is refused (`task_needs_url`). |
| a session whose harness is running (its session lock is held) | **Refused** with `harness_running`, naming the harness's process ID. The launcher cannot reach another terminal, so it starts nothing: switch to the terminal the harness runs in. |
| a session whose harness is not running | **Resume** (below), here. No harness picker, no regenerated prompt. `--harness` is only accepted if it names the session's harness (`harness_mismatch` otherwise). |

The profile, repository and harness checks of the first open still run before anything is resumed. The harness of the existing session is used; it is not picked again.

## `resume <task> [--force]`

The same as `open` for a task that has a session (an unopened task is `no_session`). With `--force` it resumes even though the session lock is held: it asks first (or takes `--yes`) because it starts a second process on the same conversation, and that process runs without the lock. Use it when you know nothing is running, for example when a process the harness started outlived it and kept its lock.

Resuming runs the harness through the harness adapter's mechanism (Claude Code: `claude --resume <stored conversation ID>`) on the **same** session, and enters no prompt. The profile's instance `resume_args` (with `{id}` standing for the conversation ID) overrides the adapter's mechanism when set.

**A conversation the harness never saved is not resumed.** Before resuming, the harness adapter checks that the harness saved the conversation. Claude Code saves `<config>/projects/<directory>/<id>.jsonl` (`<config>` is `CLAUDE_CONFIG_DIR` or `~/.claude`) once something has been submitted; resuming an ID it never saved prints `No conversation found with session ID: …` and exits (observed, 2.1.295). Copilot CLI keeps `<COPILOT_HOME>/session-state/<id>/`. When the conversation is certainly missing, a fresh one starts instead (below). When the adapter cannot tell (an unknown layout, an unreadable directory), the resume is tried.

**A fresh conversation on the same session.** A harness with no recorded conversation ID, no resume mechanism (Codex CLI) or no saved conversation starts a fresh conversation: a new conversation ID where the harness takes one at launch, and the Commodore invocation submitted at launch, in the repository's checkout. The task and session row stay (one session per task, ADR 0005); the old conversation is not deleted. If the old harness is still running (`resume --force` on a Codex session, or on a conversation that was never saved), nothing starts: `harness_running` says to switch to it, or quit it and run `open`.

## Copied tasks

The first-run copy archives every task it brings from the old launcher, because their conversations were started in worktrees Commodore does not use. `open <url>` or `open <id>` of one of those tasks unarchives it and starts a fresh conversation on its session, once every check that can refuse the launch has passed (a refusal leaves it archived). `tasks unarchive` of one forgets the copied conversation ID, so the next `open` does the same. A task you archived yourself stays refused (`task_archived`) until `tasks unarchive`, and keeps its conversation. A copied local task (one the old launcher made without a GitHub URL) stays listed and archived and can't be opened.

## Sessions recorded before the upgrade

Releases before [ADR 0022](adr/0022-harness-runs-in-the-invoking-terminal.md) ran harnesses in cmux workspaces and recorded them. State migration 12 drops those records and keeps each session and its conversation ID, so the next `open` resumes the conversation here. The first `open` after the upgrade says so once. If a harness is still running in an old cmux workspace, quit it before you open that task, or two processes will share its conversation.

## Output

`open`, `resume` and `configure` print what they start on stderr, then the harness has the terminal; when it exits they print how to resume and exit with the harness's status. `--json` needs `--dry-run`, which prints `action` (`created` or `resumed`), `task`, `session` (null before a first open), `command`, `working_directory`, `profile_env`, `prompt` and `notice`, and starts nothing.

## Transactional launches

A first `open` is a recoverable transaction (ADR 0007). It holds a per-task launch lock (`<home>/locks/<task-id>.lock`, an OS `flock`, released automatically if the process dies) from resolving the task until the session is recorded, so two simultaneous opens of one task converge on one task and one session: the second waits (up to 60 s, then fails with `task_busy` and changes nothing), then finds the session and its harness running, and is refused with `harness_running`. The lock is held while `open` asks you questions (harness picker, confirmations), so a prompt left unanswered for 60 s makes a concurrent `open` of that task fail with `task_busy`. Different tasks launch at the same time.

The session lock is taken before anything is written, and the harness starts only after the session is recorded, so nothing outside the database exists to clean up after a failure. While a first launch is incomplete the task is `launching` (`launch_failed` after an error or Ctrl-C; `launching` also remains if the process was killed), never `active`, and `state.db` has a `launches` row with the harness and conversation chosen. `commodore open <task>` again simply launches. A harness that is recorded but then cannot be started (`harness_start_failed`) leaves a session whose conversation was never saved, so the next `open` starts it fresh.

Older databases may hold several sessions for one task; the latest one is the primary.

## What is not covered yet

`open` and `resume` start the harness in the task's repository checkout and never touch it (Commodore creates and owns every worktree). A harness runs only in the terminal it was opened in: there is no way to attach to it from another one. A headless session host that a dashboard could attach to is the planned way (see [session-host.md](session-host.md#another-host)). A session you started in a harness yourself can't be adopted: the launcher records only sessions it created.
