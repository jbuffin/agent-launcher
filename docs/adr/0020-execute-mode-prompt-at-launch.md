# 20. Execute mode passes the prompt on the agent's command line

Status: accepted (supersedes the execute-mode part of ADR 0011)

## Context

Execute mode pasted the prompt like prepare mode, checked the screen, then sent one Enter. Every new task worktree is a new folder to the agent, so its folder-trust dialog came first, and the screen check (ADR 0004) was the step that failed. All three agents already submit a prompt given at launch, and do so once their dialog is answered: `claude [prompt]`, `codex [PROMPT]`, `copilot -i <prompt>`.

## Decision

- An agent adapter's `launch_prompt_args()` gives the arguments, with `PROMPT_MARKER` where the prompt goes: `-- <prompt>` for Claude Code and Codex (`--` so a prompt starting with `-` is not an option), `--interactive=<prompt>` for Copilot (not `-p`, which exits when done). In execute mode a new session (`open`, `restart`) appends them to the command and nothing is pasted. An agent without them keeps paste, check, Enter.
- cmux types the command into the workspace's shell, which echoes it and may keep it in its history. So the prompt goes to a private temp file (0600) and the typed line holds `"$(cat -- <file> && rm -f -- <file>)"`: one argument, read and removed by the shell. Exactly one argument may hold the marker. The line assumes a POSIX shell (zsh, bash).
- Prepare mode is unchanged: no agent has a pre-fill flag, so it still pastes after the screen check.

## Consequences

No screen check before submission: `prompt_submitted` means the command was handed to cmux. The agent's own dialogs still come first, and the prompt is submitted once they are answered. Trailing newlines of the prompt are dropped by the command substitution. Each form was checked on a live launch (Claude Code 2.1.295, Codex 0.162.0, Copilot 1.0.94) with a two-line prompt starting with `-`, after the folder-trust dialog.
