# 0002: Setup wizard answers and merge rules

Status: accepted

- **One path to disk.** Interactive and unattended setup both produce a `SetupAnswers` object; `apply_answers` turns it into a proposed config, the diff is shown, and one `update_config` writes it. All prompts go through a `Prompter` interface (questionary in production, a script in tests).
- **Unattended means an answers file plus `--yes`**, not a long flag list. Nothing is detected there, so nothing is guessed; missing or ambiguous values fail with a structured error.
- **Additive merge.** Setup never deletes. Search roots are unioned; scalar settings are overwritten only when answered (and shown in the diff). Existing profiles are extended, not replaced.
- **Schema stays at version 2.** The new keys (`terminal`, `repositories`, `workflow_routing`, `prompt_execution`, `agent_selection`) are optional with defaults. A v1 file is bumped when setup writes, as with profiles.
- **Shared identity is an error, not a guess.** Two profiles using the same agent must point it at different configuration directories.
