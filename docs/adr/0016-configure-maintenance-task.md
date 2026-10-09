# 16. `configure` is a local maintenance task with a built-in workflow

Status: accepted

## Decision

`agent-launcher configure` (SPEC §24) goes through the normal launch path (`open_task`). Nothing about it bypasses profile safety.

- **A local task.** Title `Configure Agent Launcher`, description the user's request (or a default asking for a `doctor` review). It has a worktree, a session and a conversation like any task.
- **Repository.** `--repo PATH` names one. Otherwise the launcher owns `<home>/maintenance`, made on first use by `git init` plus one empty commit, because a task needs a base branch to cut its worktree from. The commit ignores the user's git configuration (no signing, no hooks, `--no-verify`; an identity is supplied for that command only when none resolves, and no config file is written). A repository with commits is left alone; one without (an earlier attempt failed after `git init`) gets its commit on the next run. The directory has no remote, so it never has a GitHub ID, and an existing repository there is left alone. If git fails, the error says to use `--repo`.
- **Profile.** Always the repository's stored association. A repository with none gets one only by the user's explicit choice: `--profile NAME`, or the prompter on a terminal. Without either, `unknown_repository_profile`, with nothing created. A `--profile` that differs from an existing association is refused (`configure_profile_mismatch`); changing an association is `profile set`'s, with its task resolution (ADR 0014).
- **Reuse.** The task is found by its stored workflow (`agent-launcher-configure`, set when the task is created, so a user's own task with the same title is never taken), repository, profile and `local` source (archived ones are skipped), so repeating `configure` focuses the open session instead of making a second task. A new request cannot reach an already opened task (reopening never sends a prompt), nor change one not yet opened (its prompt comes from the stored description), so a different request is refused (`configure_request_ignored`) with the way forward (`restart`).
- **Workflow.** A built-in workflow `agent-launcher-configure` (skill `agent-launcher`) is found by id like `default`, passed as the first open's `--workflow`, and stored on the task. No rule can route to it, and `prompt`/`restart` find it again without `workflows.json`. A `workflows.json` that defines the same id replaces it, as for `default`.
- **Skill not installed.** The existing rule applies: the skill is looked up by name, and if it is not found the launch goes ahead with a notice (ADR 0010). `configure` does not install anything into an agent's directories. `require_verified_skills` still turns that into a refusal.

## The skill ships twice, in one place

The skill lives once, as package data in `src/agent_launcher/skills/agent-launcher/` (so a pipx install has it and needs no Node). `npx skills add <repo>` discovers it there (checked against skills 1.7.1 with a temporary `HOME`: it lists and installs the one skill to `~/.claude/skills` and `~/.agents/skills`), so there is no second copy to drift. `agent-launcher skill path` prints the installed directory for a manual copy, which is also how a custom `CLAUDE_CONFIG_DIR` is served: `npx skills` installs to the default `~/.claude`, and the launcher does nothing special about other directories.

## Consequences

Each profile that runs `configure` has its own maintenance task in the same directory (the key is repository plus profile; a repository has one profile, so in practice one task per `--repo`). The maintenance worktree accumulates like any task's; archiving is ticket #22's.
