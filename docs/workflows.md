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
| `template` | The whole prompt, instead of the skill invocation: `{url}`, `{skill}`, `{repository}`, `{number}` are filled in. A GitHub issue's title and body are never available (untrusted text). For a local task, which has no URL, `{url}` is the task's own title and description, which you wrote. |
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

- No skill, no template: the task text (a GitHub task's URL; a local task's title and description).
- A skill and no template: the agent adapter's skill invocation of the task text. For Claude Code that is `/<skill> <url>`: `/code-review https://github.com/acme/widgets/pull/5`. Adapters for other agents come in #16 and define their own; an agent with no invocation syntax is an error (`skill_unsupported`), not a guess. The profile instance's `skill_invocation` setting can change this: `slash` (the adapter's syntax, default), `prompt` (a plain sentence naming the skill) or `none` (the agent cannot use skills: error).
- A template: the template.

**A skill is never substituted.** The launcher looks for the skill *by name only* where the agent keeps skills. For Claude Code that is `skills/<name>` and `commands/<name>.md` under the instance's `$CLAUDE_CONFIG_DIR` (default `~/.claude`) and under `.claude/` in the task's worktree (where the agent runs). Finding it proves it exists; not finding it proves nothing, because Claude Code also has bundled skills (`/code-review`), plugin skills and managed directories. So a skill that is not found is **launched anyway, with a notice** that lists where the launcher looked; the agent will say if it does not know it. Whatever happens, no other skill, workflow or plain prompt is used in its place.

To make that a hard refusal, set `workflow_routing.require_verified_skills` to `true` in `config.json` (default `false`). A skill that is not verified then stops the launch with `skill_missing` after the worktree is made (the retry reuses it). On a terminal you are first offered, defaulting to No, to continue with the fallback workflow instead; without a terminal it stops and names `--workflow`. Skills are never read or run by the launcher.

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
