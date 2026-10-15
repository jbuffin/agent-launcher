# Workflows (removed)

Workflows, `workflows.json`, prompt templates (`templates/`), `prompt_execution` and prepare mode were removed ([ADR 0021](adr/0021-commodore-launcher.md)).

Every new session now starts the harness on Commodore: the prompt is the harness's invocation of the `commodore` skill on the task's issue or PR URL (`/commodore <url>` for Claude Code), given on the harness's command line and submitted at launch ([ADR 0020](adr/0020-execute-mode-prompt-at-launch.md)). Commodore picks the mission, so nothing selects a skill or edits the prompt. Titles and bodies of issues are never part of it.

A `workflows.json` or `templates/` directory left in the home by an older release is ignored. `commodore doctor` lists it once as unused; delete it whenever you like. `config migrate` drops `prompt_execution`, `workflow_routing` and `prompt_template` from `config.json`, and the per-harness `prompt_mode` and `skill_invocation`.
