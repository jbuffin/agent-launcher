# Session lifecycle

A task has **one primary session** for its whole life. `open`, `resume`, `prompt` and `restart` all act on that one session; none of them ever creates a second. The registry enforces it: recording a second session for a task fails with `session_exists`.

## The three identities

- **Task** (`t-xxxxxxxx`): never changes.
- **Agent conversation** (`agent_conversations.conversation_id`): the agent's own ID. For Claude Code the launcher generates a UUID at launch, starts the agent with `--session-id <uuid>` and stores it. Nothing is discovered afterwards. Agents that cannot be given an ID at launch (Codex and Copilot for now) store none, and so cannot be resumed.
- **Terminal session** (`terminal_sessions`): the workspace and surface. It can vanish; the task, session and conversation stay.

## `open <task>`

| The task has... | What happens |
| --- | --- |
| no session | The first-open flow: pick an agent, build the prompt, create the terminal session, store the conversation ID, prepare the prompt unsubmitted. |
| a session whose terminal session is there | **Focus it.** No agent picker, no regenerated prompt, nothing created. `--agent` is only accepted if it names the session's agent (`agent_mismatch` otherwise). |
| a session whose agent has exited | **Resume** (below) in a new terminal session. |
| a session whose workspace or surface is gone (stale IDs) | **Resume** in a new terminal session. |

The profile, repository and agent checks of the first open still run before anything is focused or resumed. The agent of the existing session is used; it is not picked again.

Before focusing, the cmux adapter checks that the recorded workspace (and surface, if one was recorded) still exists (`cmux list-panels`). A missing one is `terminal_session_stale`, and `open` then recovers by resuming.

**How "exited" is decided.** Only on positive evidence: the agent adapter must recognise its own exit text on the screen. Claude Code's is `Resume this conversation with …` (the wording is unobserved, so check it against a live exit). When the screen shows anything else, including a dialog the launcher does not recognise, the agent is treated as running and the workspace is focused. A running agent is never resumed over. If you know it has exited and `open` only focuses, use `resume --force`, which asks first (or takes `--yes`) because it starts a second process on the same conversation when the old one is in fact alive.

**Another adapter.** A session recorded by one terminal adapter is not looked at through another: `open --terminal mock` on a cmux session stops with `terminal_mismatch` and changes nothing.

**cmux errors are not "gone".** The terminal session counts as stale only when cmux says the workspace is not found, or lists the workspace without the recorded surface. Any other cmux failure is reported as an error and nothing is resumed.

## `resume <task> [--force]`

The same as `open` for a task that has a session (an unopened task is `no_session`). With `--force` it resumes in a new terminal session even though the old one looks alive.

Resuming starts the agent in a new workspace through the agent adapter's mechanism (Claude Code: `claude --resume <stored conversation ID>`), records the new workspace on the **same** session, and enters no prompt. The old workspace, if it still exists, is left alone for you to close.

**Restoration is only reported when the agent confirms it.** The result's `resume_state` is one of:

- `confirmed`: the agent's screen showed the resumed conversation.
- `failed`: the agent said it could not resume. Claude Code prints `No conversation found with session ID: …` (observed, 2.1.295), which is what happens when nothing was ever submitted in the conversation. Run `restart`.
- `unconfirmed`: "resume attempted, not confirmed". Look at the terminal. Today this is what Claude Code gets, because the launcher has no observed pattern for a resumed screen and will not guess one (`ClaudeCodeAdapter.resume_check`).

An agent with no recorded conversation ID or no resume mechanism fails with `resume_unsupported`, which points at `restart`. The profile's instance `resume_args` (with `{id}` standing for the conversation ID) overrides the adapter's mechanism when set.

## `prompt <task>`

Prepares the task's prompt again, **unsubmitted**, in the existing session, by the same path as `open` (wait for the input box, one bracketed paste, check the screen; never `--submit`). If the agent is not running, it stops with `no_agent_running`.

This is the recovery after Claude Code's **folder-trust dialog**. Every new worktree triggers that dialog. The launcher will not type into a dialog, so the prompt is left on your clipboard and `open` says so; accept the dialog, then run `agent-launcher prompt <task>`. If the box already holds a draft, cmux refuses the paste and you are told.

## `restart <task> [--yes]`

Starts the agent afresh: a new conversation ID, the prompt prepared, in a new terminal session recorded on the same session. It needs confirmation: `--yes`, or an answer at the prompt (default no). With neither (non-interactive, no `--yes`) it stops with `confirmation_required` and changes nothing.

It closes the old terminal session first, unless that is already gone. If the old session cannot be identified (not recorded as launcher-created, or no surface recorded) it is left open and the notice says so. Otherwise it force-closes only a workspace recorded as created by the launcher whose recorded surface is still listed in it; otherwise it asks cmux to close it normally, and if cmux refuses it stops (`close_refused`) before starting anything. The task and its worktree are kept, and nothing under the repository is touched. The old conversation is not deleted; it remains in the agent's own history.

## JSON output

`open`, `resume`, `prompt` and `restart` with `--json` print `action` (`created`, `focused`, `resumed`, `prompted`, `restarted`), `task`, `session`, `prompt`, `prompt_prepared`, `prompt_submitted`, `resume_state` and `notice`.

## Known gap: concurrent first `open`

Two `open`s of a never-opened task at the same moment can each create a terminal session before either records its session. The registry refuses the second record (`session_exists`), so there is still one primary session, but the second workspace (and its agent) is orphaned. Ticket #11's state machine fixes this. Legacy databases from before this ticket may hold several sessions for one task; the latest one is the primary.

## What is not covered yet

`restart` and `resume` start the agent in the task's recorded worktree and never touch it (see [worktrees.md](worktrees.md)). `discover_sessions` and `restore_session` are not implemented for cmux: cmux IDs are not stable enough across restarts, so a stale ID is recovered from by resuming, not by finding the old workspace again.
