# Workflows

A **workflow** says what an agent should be asked to do with a task: which skill to run, optionally which prompt, and which of the repository's agents to highlight. `workflows.json` holds the rules that pick one. Routing is plain data matching: no model is involved, and the same task metadata always gives the same answer.

A workflow never changes the repository's profile and never grants an agent. The profile comes from the repository's association ([security.md](security.md)); a workflow only shapes the prompt and the picker's highlight inside it.

## `workflows.json`

It lives next to `config.json` (`~/.agent-launcher/workflows.json`, or under `AGENT_LAUNCHER_HOME`). A missing file is fine: every task then gets the fallback. `agent-launcher workflows init` writes the example below (and refuses to overwrite a file); writes are atomic.

```json
{
  "version": 1,
  "workflows": [
    { "id": "issue-triage", "priority": 100,
      "match": { "type": "issue", "labels_any": ["needs-triage"] },
      "skill": "issue-triage" },
    { "id": "code-review", "priority": 80,
      "match": { "type": "pr", "review_requested": "self" },
      "preferred_agent": "claude", "skill": "code-review" },
    { "id": "pr-review-fixer", "priority": 60,
      "match": { "type": "pr", "author": "self" },
      "skill": "pr-review-fixer" }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `id` | Required. Letters, digits, `-` and `_`; unique in the file. |
| `priority` | Integer, default `0`. Higher wins. |
| `match` | Conditions, all of which must hold (see below). Empty or missing: matches every task. |
| `skill` | Name of the agent skill to run. Letters, digits, `. _ - :`; no slashes or spaces. |
| `template` | The name of a prompt template (a file in `templates/`, see [Prompt templates](#prompt-templates)), used as the whole prompt instead of the skill invocation. |
| `prompt_execution` | `prepare` or `execute` for tasks this workflow handles, instead of the global setting (see [Execute mode](#execute-mode)). |
| `preferred_agent` | An agent to highlight first in the picker. Advisory (see below). |
| `description` | Free text for you. |

Unknown fields are errors with a suggestion (`unknown condition 'label'; did you mean 'labels_any'?`), because a misspelt condition would otherwise never match. A `profile`, `agents` or `env` field is refused with an explanation. `version` is required; a newer one than this release supports is an error. `agent-launcher config validate` and `doctor` check the file; an invalid file stops `open` before anything is created.

### Conditions

| Condition | Matches when |
| --- | --- |
| `type` | `issue`, `pr` or `local`. |
| `repository` | `owner/name`, case-insensitive. |
| `owner` | The repository owner. |
| `labels_any` / `labels_all` | The issue or PR has at least one / all of these labels (case-insensitive). |
| `author` | The author is this login, or one of a list. `"self"` is the authenticated `gh` user. |
| `assignee` | One of the assignees is this login (or a list), or `"self"`. |
| `review_requested` | `"self"`: your review is directly requested on the PR. |
| `draft` | `true` or `false`. PRs only. |
| `ci` | The PR head commit's checks are `success`, `failure`, `pending` or `unknown` (or a list). |
| `state` | `open`, `closed` or `merged` (or a list). GitHub tasks only. |

Rules read only what the launcher already stored when it fetched the issue or PR (#7, #12, #13), refreshed on every `open`. A condition that does not apply (`draft` on an issue, `labels_any` on a local task) or whose answer is unknown (`self` when `gh` cannot say who you are) does not match. **CI status** is not stored: it is fetched (`gh api repos/o/n/commits/<sha>/check-runs` and `.../status`, read only) only when a rule with `ci` has matched on every other condition, once per routing, and it is `unknown` if anything fails, so a `ci: success` rule simply does not match then. A check run counts as success when it completed with `success`, `neutral` or `skipped`; any other conclusion is a failure; a run still going is `pending`; nothing reporting is `unknown`.

Local tasks route too: `type: local` and `repository` (the repository's last known GitHub name, if it has one).

## Which rule wins

1. Every rule is tested. Those that match are ranked by **priority, highest first**.
2. **Ties go to the rule earlier in the file.** Rules with the same priority are tried in file order, always.
3. If nothing matches, the **fallback** applies: `workflow_routing.fallback` in `config.json` (default `"default"`). `default` is built in and has no skill, so the prompt is the task's URL, as before workflows existed. Set `fallback` to any workflow id in the file to use that instead; defining a workflow called `default` replaces the built-in one. A fallback naming a workflow that does not exist is an error in `config validate` and `doctor`.

## Selection modes

`workflow_routing.selection_mode` in `config.json`:

- `automatic` (default): take the winner.
- `ask_on_multiple`: ask only when more than one rule matches (the winner is offered first).
- `always_ask`: always ask; every workflow in the file is offered, matching ones first and marked `matches`.

Asking goes through the same prompter as the agent picker, so without a terminal (or with `--json`) it is a `workflow_selection_needed` error that lists the ids to pass.

`agent-launcher open <url-or-task> --workflow <id>` uses that workflow (any id in the file, or `default`) and skips routing; `--ask-workflow` asks regardless of the mode. They cannot be combined. A misspelt id is `workflow_not_found` with a suggestion.

**The workflow is chosen once, at the task's first open, and stored on the task.** Reopening focuses the session and never routes again (it does not regenerate the prompt either), so `--workflow` and `--ask-workflow` on a task that already has a session, or whose earlier launch already started an agent terminal, are refused (`workflow_fixed`). `prompt` and `restart` rebuild the prompt from the *stored* workflow, looked up by id; if the file no longer defines it, they stop (`workflow_missing`) rather than use another. A first open that failed part-way keeps the workflow it chose.

## The prompt and skills

The prompt follows this precedence exactly (SPEC §17):

1. **A template**: the workflow's `template`; or, for a workflow with no skill and no template of its own, the global `prompt_template` in `config.json`. The global template never replaces a skill invocation; a skill workflow that wants a template names its own and can write the skill with `$skill_invocation`.
2. **A skill**, no template: the skill invocation and the task URL, nothing else.
3. **Neither**: the task URL; for a local task, its title and description as a minimal reference.

No GitHub body, diff or comment history is ever appended; the agent's skill reads what it needs through the URL. A task opened before workflows existed has no workflow and keeps its plain prompt (the global template does not apply to it). Because the global template reaches every local task, it should use only variables every task has (`doctor` warns about `$task_url`).

- No skill, no template: the task text (a GitHub task's URL; a local task's title and description).
- A skill and no template: the agent adapter's skill invocation of the task text. For Claude Code that is `/<skill> <url>`: `/code-review https://github.com/acme/widgets/pull/5`. Adapters for other agents come in #16 and define their own; an agent with no invocation syntax is an error (`skill_unsupported`), not a guess. The profile instance's `skill_invocation` setting can change this: `slash` (the adapter's syntax, default), `prompt` (a plain sentence naming the skill) or `none` (the agent cannot use skills: error).
- A template: the rendered template.

**A skill is never substituted.** The launcher looks for the skill *by name only* where the agent keeps skills. For Claude Code that is `skills/<name>` and `commands/<name>.md` under the instance's `$CLAUDE_CONFIG_DIR` (default `~/.claude`) and under `.claude/` in the task's worktree (where the agent runs). Finding it proves it exists; not finding it proves nothing, because Claude Code also has bundled skills (`/code-review`), plugin skills and managed directories. So a skill that is not found is **launched anyway, with a notice** that lists where the launcher looked; the agent will say if it does not know it. Whatever happens, no other skill, workflow or plain prompt is used in its place.

To make that a hard refusal, set `workflow_routing.require_verified_skills` to `true` in `config.json` (default `false`). A skill that is not verified then stops the launch with `skill_missing` after the worktree is made (the retry reuses it). On a terminal you are first offered, defaulting to No, to continue with the fallback workflow instead; without a terminal it stops and names `--workflow`. Skills are never read or run by the launcher.

## Prompt templates

A template named `review` is the file `~/.agent-launcher/templates/review.txt` (under `AGENT_LAUNCHER_HOME` if set): UTF-8 text up to 64 KiB. A workflow names it (`"template": "review"`); `"prompt_template": "review"` in `config.json` makes it the default for workflows that have neither a template nor a skill. Names are letters, digits, `-` and `_`.

```
Review $task_type $repository ($task_url) from $worktree_path.
Focus on the changes a reviewer would miss. Costs $$5 to run.
```

Placeholders are `$name` or `${name}` (use the braces when text follows directly: `${task_id}_notes`); `$$` is a literal `$`. This is Python's `string.Template`, nothing more: no conditionals, no code. Rendering is one pass, so a task's title is inserted as text and never evaluated or expanded (a title containing `$(rm -rf x)` or `${task_id}` appears as written).

| Variable | Value | Available |
| --- | --- | --- |
| `task_id` | the launcher's task ID (`t-xxxxxxxx`) | always |
| `task_type` | `issue`, `pr` or `local` (the kind stored with the task) | always |
| `task_url` | the GitHub URL | issues and PRs only; **a template that uses it on a local task is an error** |
| `task_title` | the task's title, verbatim | always |
| `repository` | `owner/name` | issues and PRs; a local task only if its repository has a known GitHub name, else an error |
| `repository_path` | the repository's local checkout | always |
| `worktree_path` | the task's worktree | at `open`, `prompt` and `restart`; not in `workflows test`, which shows `<worktree_path>` |
| `profile` | the repository's profile | always |
| `agent` | the agent that runs | at `open`, `prompt` and `restart`; in `workflows test` once an agent is settled, else `<agent>` |
| `workflow` | the id of the workflow that chose it | always |
| `skill_invocation` | how the workflow's skill is named to the agent, without the task: `/code-review` for Claude Code | only for a workflow with a skill (an error otherwise, found at validation) |

Issue and PR bodies, comments and diffs are not variables.

**Everything is an error, never a silent blank or another prompt.** `config validate`, `doctor` and every command that loads `workflows.json` check each referenced template: a missing or unreadable file, invalid UTF-8, a file over 64 KiB, a malformed placeholder (a lone `$`, `${x` without `}`), and an unknown variable are errors (`workflows_invalid`, or `template_invalid` for the global template at `open`), naming the template and file. A workflow that matches only `type: local` cannot use `$task_url`, and that is caught at validation. Whatever depends on the task (`task_url` or `repository` on a task that has none) fails with `template_variable_unavailable` before anything is changed: `open` makes no worktree and records no launch, and `restart` fails before it asks for confirmation or closes the old session. (The prompt is rendered once as a dry run with a stand-in for the worktree path, which does not exist yet at `open`.) An old inline `template` string from before templates were files is rejected with a hint to move the text into `templates/<name>.txt`. A failed render does not fall back to the skill or the URL; fix the template and open again.

`workflows test` shows the rendered prompt, or `(not possible: …)` with the reason.

## Execute mode

By default the launcher **prepares** the prompt: it is entered into the agent's input box and left for you to review and send. **Execute** mode also submits it.

The mode is the first of these that is set:

1. `open --execute` or `open --prepare` (also `restart`), for that launch.
2. The workflow's `prompt_execution`.
3. `prompt_execution` in `config.json` (default `prepare`).

`--execute` and `--prepare` cannot be combined. A terminal adapter that cannot submit is an explicit error (`unsupported capability: submit_prompt`), not a silent fallback to prepare. `agent-launcher prompt` always prepares. Execute applies to a new agent session only (`open`, `restart`), never to a resume or a reopen.

With cmux, execute pastes the prompt exactly as prepare does, confirms on screen that it is in the input box and the agent is not already working, then sends one Enter (`cmux send-key enter`). If the paste or that check failed, **no Enter is sent**: you get the prepare fallback (prompt on the clipboard, a notice that begins "Execute mode: the prompt was not submitted"). See [terminal-adapters.md](terminal-adapters.md).

## The preferred agent

`preferred_agent` only decides which agent the picker highlights. The picker's order is: the workflow's preferred agent, then the last agent used in this profile, then the profile's default. It is offered only if the repository's profile already has that agent; otherwise it is ignored, `open` says so in its notice, and `workflows test` shows it as `(ignored)`. With `agent_selection: use_default`, or a profile with one agent and `ask_if_multiple`, nothing is asked, so nothing is highlighted.

## `workflows list` and `workflows test`

```
agent-launcher workflows list [--json]
agent-launcher workflows test <issue-or-pr-url | task-id> [--json] [--offline]
```

`list` shows the rules in the order they are tried. `test` explains one task, rule by rule, and is **read only**: it creates no task, worktree, association or database, and changes nothing in the state database (SQLite may create its `-wal`/`-shm` scratch files when reading it). It reads the issue or PR from GitHub (or, if GitHub cannot be reached, the stored copy of an existing task), or a task from the state database opened read-only, and shows for every rule its position, priority, whether it matched and its rank, the reason for each condition, any tie, the winner, whether `open` would ask, the skill, the preferred agent (when the repository's profile is known) and the prompt it would produce.

```
pr acme/widgets#5 [open]   (from: github)
profile: work   selection mode: automatic

#1 issue-triage  priority 100  not matched
     no  type: the task is pr, not issue
#2 code-review  priority 80  MATCHED, rank 1
     yes  type: the task is pr
     yes  review_requested: your review is requested

winner: code-review - priority 80, best of 1 matching
skill: code-review
preferred agent: claude (highlighted first)
prompt: /code-review https://github.com/acme/widgets/pull/5
```

See [ADR 0010](adr/0010-workflow-routing.md) for the decisions behind this.
