# Workflows, routing, templates

`workflows.json` sits next to `config.json`. A missing file is fine (every task gets the fallback). `agent-launcher workflows init` writes an example and refuses to overwrite.

```json
{"version": 1, "workflows": [
  {"id": "code-review", "priority": 80,
   "match": {"type": "pr", "review_requested": "self"},
   "preferred_agent": "claude", "skill": "code-review"}
]}
```

Fields: `id` (required, unique), `priority` (integer, higher wins; ties go to the earlier rule), `match`, `skill`, `template`, `prompt_execution`, `preferred_agent`, `description`. Unknown fields are errors with a suggestion; `profile`, `agents` and `env` are refused.

Conditions (all must hold): `type` (`issue`, `pr`, `local`), `repository`, `owner`, `labels_any`, `labels_all`, `author`, `assignee` (`"self"` allowed), `review_requested` (`"self"`), `draft`, `ci`, `state`. An inapplicable or unknown condition does not match.

```
agent-launcher workflows list [--json]
agent-launcher workflows test <issue-or-pr-url | task-id> [--json] [--offline]
```

`workflows test` is read only and explains every rule; run it after each edit. Then `config validate`.

A task's workflow is chosen once, at first open, and stored. `open --workflow ID` or `--ask-workflow` apply to a first open only (`workflow_fixed` otherwise). `prompt` and `restart` rebuild from the stored workflow and stop with `workflow_missing` if the file no longer defines it. Do not delete a workflow that tasks use.

## Prompt precedence

1. A template (workflow `template`, or global `prompt_template` for a workflow with neither skill nor template).
2. A skill: the agent's invocation of the task text (`/code-review <url>` for Claude Code, `$skill` for Codex, `/skill` for Copilot).
3. The task text: a GitHub URL, or a local task's title and description.

A skill is never substituted. If it is not found by name, it is launched anyway with a notice; `require_verified_skills: true` makes that a refusal.

## Templates

`templates/<name>.txt`, UTF-8, up to 64 KiB, names of letters, digits, `-`, `_`. Placeholders `$name` / `${name}`, `$$` for a literal dollar. Variables: `task_id`, `task_type`, `task_url` (GitHub tasks only), `task_title`, `repository`, `repository_path`, `worktree_path`, `profile`, `agent`, `workflow`, `skill_invocation` (workflows with a skill). Any error (missing file, unknown variable, unavailable variable) stops `open` before anything is created; fix the template and rerun.

## Execute mode

Order: `open --execute|--prepare`, then the workflow's `prompt_execution`, then global `prompt_execution` (default `prepare`). Prepare enters the prompt for the user to review; execute also presses Enter once, only after the launcher confirmed the prompt is in the input box. Tell the user before enabling `execute` in config.
