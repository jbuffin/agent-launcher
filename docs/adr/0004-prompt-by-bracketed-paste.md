# 4. Prepare prompts by bracketed paste after a screen-readiness check

Status: accepted

## Context

SPEC §18 requires the prompt to sit unsubmitted in the agent's input. Claude Code's `claude [prompt]` submits its argument, and it has no pre-fill flag. cmux offers `send` (keystrokes, Enter at every newline) and `paste` (bracketed paste; `--submit` optional).

## Decision

After starting the agent, wait for its input box on `cmux read-screen`, then `cmux paste` the prompt from stdin without `--submit` or `--force`, then read the screen to confirm. On any doubt, keep the workspace, type nothing, put the prompt on the clipboard and say so. Readiness patterns belong to the agent adapter; cmux commands belong to the cmux adapter.

## Consequences

Multi-line prompts cannot submit early. A slow or dialog-blocked start costs the user one paste, not a stray submission. The patterns depend on Claude Code's UI text and need re-checking when it changes.

## Addendum: wait out a dialog

Every new task worktree is a new folder to Claude Code, so the first launch in it shows the folder-trust dialog. Stopping at once left the user, now looking at the new workspace, with an empty input box and a notice in the terminal they had left. Now, while a *blocked* pattern shows, nothing is typed and the wait is extended to `PromptInput.dialog_timeout` (120 s) from when the dialog was first seen. Once the user answers it, the input box must still match *ready* on two equal reads before the paste. A dialog still up at the end, or no input box after it, takes the clipboard fallback as before. The cost is that `open` (and a gh-dash shortcut) blocks while the dialog is open.
