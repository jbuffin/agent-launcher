# 11. Prompt templates are files rendered by `string.Template`; execute mode is paste, verify, one Enter

Status: accepted (execute mode superseded by ADR 0020 for agents that take a prompt at launch)

## Context

SPEC §17 asks for optional templates under `~/.agent-launcher/templates/` with documented variables, validated, with render failures reported and never replaced. SPEC §18 asks for an execute mode, global and per workflow. #14 had an inline `template` string with four variables.

## Decision

- **A template is a file, referenced by name.** `workflows.json` `template` (and the global `prompt_template`) hold a name; the text is `templates/<name>.txt`. Inline strings were dropped: nothing had been released with them, and one mechanism is easier to validate.
- **Engine: stdlib `string.Template`** (`$var`, `${var}`, `$$`). No new dependency, no logic, single-pass substitution, so task titles are inserted as text and never evaluated or expanded again.
- **The ten variables of the spec, and nothing else.** Issue/PR bodies, comments and diffs are not variables. A variable with no value for the task (`task_url` on a local task, `worktree_path` where none exists yet) is an error naming the variable, never an empty string.
- **Validation happens early and again at render.** `config validate`, `doctor` and every load of `workflows.json` check that each referenced file exists, is UTF-8 and at most 64 KiB, parses, and uses only known variables; a workflow that only matches local tasks cannot use `task_url`. `open` and `restart` render the prompt once as a dry run (worktree path stubbed) before anything is created, asked or closed, so a variable the task lacks cannot leave a worktree, a launch record or a closed session behind. `--execute` is checked against the adapter before the pickers. A render failure aborts; no other prompt takes its place.
- **Precedence is template, skill, URL, local reference.** The template is the workflow's own, or the global `prompt_template` only for a workflow with neither a template nor a skill: the global one never silently replaces a skill invocation. `$skill_invocation` (a skill workflow only) lets a template name the skill explicitly. A task with no workflow (opened before #14) keeps its plain prompt.
- **Execute mode resolves as `--execute/--prepare`, then the workflow's `prompt_execution`, then the global one.** The mode is resolved once the workflow is known, so the adapter capability (`submit_prompt` or `prepare_prompt`) is checked after routing and before anything is created.
- **cmux `submit_prompt`: the same paste and screen checks as prepare, then one `cmux send-key enter`.** Not `paste --submit`, which would submit without the check. If any step before the key failed, nothing is sent and the prepare fallback applies, with a notice saying the prompt was not submitted. A refused Enter leaves the prompt in the box and says so.

## Consequences

Editing a template takes effect on the next launch (and `workflows test`); validation covers files, not whether a prompt is a good one. The Enter is not verified afterwards. The live check against a real Claude Code is the engineer's (see the ticket #15 report).
