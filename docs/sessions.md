# Session lifecycle

A task has **one primary session** for its whole life. `open` and `resume` act on that one session; neither ever creates a second. The registry enforces it: recording a second session for a task fails with `session_exists`.

## The three identities

- **Task** (`t-xxxxxxxx`): never changes.
- **Harness conversation** (`agent_conversations.conversation_id`): the harness's own ID. For Claude Code and Copilot CLI the launcher generates a UUID at launch, starts the harness with `--session-id <uuid>` and stores it. Nothing is discovered afterwards. Harnesses that cannot be given an ID at launch (Codex CLI) store none, and so cannot be resumed.
- **Terminal session** (`terminal_sessions`): the workspace and surface. It can vanish; the task, session and conversation stay.

## `open <task>`

| The task has... | What happens |
| --- | --- |
| no session | The first-open flow: pick a harness, build the invocation (`/commodore <url>` for Claude Code), create the terminal session with the invocation on the harness's command line, store the conversation ID. The harness submits it at launch. A task copied from the old launcher is unarchived first (see [Copied tasks](#copied-tasks)); a copied local task is refused (`task_needs_url`). |
| a session whose terminal session is there | **Focus it.** No harness picker, no regenerated prompt, nothing created. `--harness` is only accepted if it names the session's harness (`harness_mismatch` otherwise). |
| a session whose harness has exited | **Resume** (below) in a new terminal session. |
| a session whose workspace or surface is gone (stale IDs) | **Resume** in a new terminal session. |

The profile, repository and harness checks of the first open still run before anything is focused or resumed. The harness of the existing session is used; it is not picked again.

Before focusing, the cmux adapter checks that the recorded workspace (and surface, if one was recorded) still exists (`cmux list-panels`). A missing one is `terminal_session_stale`, and `open` then recovers by resuming.

**How "exited" is decided.** Only on positive evidence: the harness adapter must recognise its own exit text on the screen. Claude Code's is `Resume this conversation with …` (the wording is unobserved, so check it against a live exit). When the screen shows anything else, including a dialog the launcher does not recognise, the harness is treated as running and the workspace is focused. A running harness is never resumed over. If you know it has exited and `open` only focuses, use `resume --force`, which asks first (or takes `--yes`) because it starts a second process on the same conversation when the old one is in fact alive.

**Another adapter.** A session recorded by one terminal adapter is not looked at through another: `open --terminal mock` on a cmux session stops with `terminal_mismatch` and changes nothing.

**cmux errors are not "gone".** The terminal session counts as stale only when cmux says the workspace is not found, or lists the workspace without the recorded surface. Any other cmux failure is reported as an error and nothing is resumed.

## `resume <task> [--force]`

The same as `open` for a task that has a session (an unopened task is `no_session`). With `--force` it resumes in a new terminal session even though the old one looks alive.

Resuming starts the harness in a new workspace through the harness adapter's mechanism (Claude Code: `claude --resume <stored conversation ID>`), records the new workspace on the **same** session, and enters no prompt. The old workspace, if it still exists, is left alone for you to close.

**Restoration is only reported when the harness confirms it.** The result's `resume_state` is one of:

- `confirmed`: the harness's screen showed the resumed conversation.
- `failed`: the harness said it could not resume. Claude Code prints `No conversation found with session ID: …` (observed, 2.1.295), which is what happens when nothing was ever submitted in the conversation. The launcher closes the terminal session the resume just created and starts a fresh conversation on the same session (`action` is `created`, `resume_state` stays `failed`).
- `unconfirmed`: "resume attempted, not confirmed". Look at the terminal. Today this is what Claude Code gets, because the launcher has no observed pattern for a resumed screen and will not guess one (`ClaudeCodeAdapter.resume_check`).

A harness with no recorded conversation ID or no resume mechanism (Codex CLI, or a conversation whose ID was never recorded) can't be resumed. `open` and `resume` then start a **fresh conversation on the same session**: a new terminal session, a new conversation ID where the harness takes one at launch, and the Commodore invocation submitted at launch, in the repository's checkout. The task and session row stay (one session per task, ADR 0005); the old conversation is not deleted, and the old workspace, if it still exists, is left for you to close. The result's `action` is `created`. A launcher-created terminal session that has exited is closed first; one that can't be identified is left open and named in the `notice`. If the old harness is still running (`resume --force` on a Codex session), nothing starts: `harness_running` says to focus it, or close it and run `open`. The profile's instance `resume_args` (with `{id}` standing for the conversation ID) overrides the adapter's mechanism when set.

## Copied tasks

The first-run copy archives every task it brings from the old launcher, because their conversations were started in worktrees Commodore does not use. `open <url>` or `open <id>` of one of those tasks unarchives it and starts a fresh conversation on its session, once every check that can refuse the launch has passed (a refusal leaves it archived). `tasks unarchive` of one forgets the copied terminal session and conversation ID, so the next `open` does the same. A task you archived yourself stays refused (`task_archived`) until `tasks unarchive`, and keeps its conversation. A copied local task (one the old launcher made without a GitHub URL) stays listed and archived and can't be opened.

## JSON output

`open` and `resume` with `--json` print `action` (`created`, `focused` or `resumed`), `task`, `session`, `prompt`, `prompt_submitted`, `resume_state` and `notice`.

## Transactional launches

A first `open` is a recoverable transaction (ADR 0007). It holds a per-task lock (`<home>/locks/<task-id>.lock`, an OS `flock`, released automatically if the process dies) from resolving the task until the session is recorded, so two simultaneous opens of one task converge on one task, one session and one terminal session: the second waits (up to 60 s, then fails with `task_busy` and changes nothing), then focuses the session the first made. The lock is held while `open` asks you questions (harness picker, confirmations), so a prompt left unanswered for 60 s makes a concurrent `open` of that task fail with `task_busy`. Different tasks launch at the same time.

While a first launch is incomplete the task is `launching` (`launch_failed` after an error or Ctrl-C; `launching` also remains if the process was killed), never `active`, and `state.db` has a `launches` row recording the stage and the resources created so far. `commodore open <task>` again resumes:

- a terminal session recorded but never attached to a session is reused if it still exists, and replaced if not;
- if recording the session fails after the terminal session was created, the terminal session is closed when it is launcher-created, identifiable and unused, and otherwise left open and named in the error (`launch_incomplete`). The checkout is never touched.

Older databases may hold several sessions for one task; the latest one is the primary.

## What is not covered yet

`open` and `resume` start the harness in the task's repository checkout and never touch it (Commodore creates and owns every worktree). `restore_session` is not implemented for cmux: cmux IDs are not stable enough across restarts, so a stale ID is recovered from by resuming, not by finding the old workspace again. A session you started in a cmux workspace yourself can't be adopted: the launcher records only sessions it created.
