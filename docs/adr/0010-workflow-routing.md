# 10. Workflow routing

Status: accepted

## Decision

- **`workflows.json` is a separate file** from `config.json` (its own `version`, Pydantic schema and atomic writes), because it is edited far more often and its mistakes should not make the whole configuration unusable. `config validate` and `doctor` check both. The fallback and the selection mode stay in `config.json` (`workflow_routing.fallback`, `selection_mode`).
- **Total, documented order**: priority descending, then position in the file. There is no other tie-breaker (ids, names and timestamps do not count), so reordering the file is the way to change a tie.
- **A workflow has no profile, agent list or environment**, and the schema refuses them. That makes "the fallback cannot change the profile or grant agents" a property of the types, not a check. `preferred_agent` is filtered against the repository's profile by the picker.
- **The built-in fallback is a workflow called `default`** with no skill and no template; `workflow_routing.fallback` names any workflow in the file instead. A workflow defined with the id `default` replaces the built-in one.
- **The chosen workflow id is stored on the task** (`tasks.workflow`, migration 9) at first open, before the launch begins. Reopen never routes; `prompt` and `restart`, which do regenerate the prompt, look the stored id up. If it has gone from the file they stop; they do not route again or use another workflow.
- **Lazy, soft inputs.** Routing reads stored task metadata. The `gh` user and CI status are fetched only when a rule uses them; a failure is "unknown", which never matches. CI is `gh api` check runs plus the combined status, read only.
- **A skill not found by name is launched with a notice, not refused.** A lookup by name proves a skill is there, never that it is absent: Claude Code also has bundled skills (`/code-review`), plugin skills under their bare names and managed directories. So `check_skill` returns `available` or `not_verified` (listing where it looked), the launch goes ahead, and the agent says if it does not know the skill. The launcher never substitutes another skill or the plain URL prompt. `missing` is for a case an adapter can be certain of. `workflow_routing.require_verified_skills` (default false) turns every unverified skill into a refusal (`skill_missing`); on a terminal the user is then offered, defaulting to No, to continue with the fallback workflow, and without one the launch stops. The skill is looked for in the task's worktree, where the agent runs. Skill invocation syntax belongs to the agent adapter (`skill_invocation`, `check_skill`).
- **`workflows test` is read only**: it creates no task, worktree or association, and the database is opened `mode=ro` (never created or migrated). SQLite may still create its `-wal`/`-shm` scratch files when reading a WAL database; the database file itself is not changed.
- **After a launch has started the agent, the workflow is fixed**: `--workflow` and `--ask-workflow` are refused (`workflow_fixed`) both on reopen and on a retry that finds a terminal session an earlier attempt created, because the prompt was already sent.

## Consequences

An invalid `workflows.json` stops `open` until fixed (`workflows_invalid`); `doctor` flags it first. A task opened before this release has `workflow` NULL and keeps its plain prompt on `prompt` and `restart`. The next agent adapters (#16) implement `skill_invocation` and `check_skill` for their agents.
