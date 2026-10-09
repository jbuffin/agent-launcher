# 4. Prepare prompts by bracketed paste after a screen-readiness check

Status: accepted

## Context

SPEC §18 requires the prompt to sit unsubmitted in the agent's input. Claude Code's `claude [prompt]` submits its argument, and it has no pre-fill flag. cmux offers `send` (keystrokes, Enter at every newline) and `paste` (bracketed paste; `--submit` optional).

## Decision

After starting the agent, wait for its input box on `cmux read-screen`, then `cmux paste` the prompt from stdin without `--submit` or `--force`, then read the screen to confirm. On any doubt, keep the workspace, type nothing, put the prompt on the clipboard and say so. Readiness patterns belong to the agent adapter; cmux commands belong to the cmux adapter.

## Consequences

Multi-line prompts cannot submit early. A slow or dialog-blocked start costs the user one paste, not a stray submission. The patterns depend on Claude Code's UI text and need re-checking when it changes.
