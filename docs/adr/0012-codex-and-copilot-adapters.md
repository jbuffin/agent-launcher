# 12. Codex and Copilot adapters are peers of the Claude Code adapter

Status: accepted

## Context

Ticket #16 adds `codex-cli` and `github-copilot-cli`. The adapter seam from #9/#13/#14 (`prompt_input`, `new_conversation_id`, `conversation_args`, `resume_args`, `skill_invocation`, `check_skill`) was meant to take them without touching routing, launch or sessions.

## Decision

- Both are new classes in `agent_adapters.py`, registered next to `ClaudeCodeAdapter`. `launch.py`, `routing.py`, `workflows.py` and the session code are unchanged, which is the evidence the seam holds.
- **Codex has no conversation ID at launch**, so it returns `None` and `resume_args` is `None`. The ID could only be found by reading Codex's session files, which ADR 5 rules out. Copilot has `--session-id <uuid>` and `--resume=<id>` and uses them like Claude Code.
- **Prompts are pasted, never given as an argument**, for both: a positional prompt (`codex [PROMPT]`, `copilot -i`) submits.
- **Skill names are looked up as `<root>/<name>/SKILL.md`, only for plain names.** Skill names reach the filesystem, so a name with a separator or a leading dot is never joined to a path.
- **Screen patterns are narrow and unobserved**, so a wrong guess fails into the clipboard fallback and never into a stray submission.

## Consequences

The patterns need a live check against both TUIs. Setup's identity check and the config-export allowlist know `COPILOT_HOME` as well as `CODEX_HOME`. `has_exited` is not overridden for Codex or Copilot, so an exited agent is focused, not resumed, until an exit screen is observed.
