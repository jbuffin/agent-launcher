# Profiles

A profile is a named development identity such as `work`, `personal` or `opensource`. Names are yours to choose (letters, digits, `-` and `_`; nothing is hard-coded). Each profile has its own agent instances and a default agent. See [agents.md](agents.md) for what an instance can configure and how it is resolved.

## Commands

```bash
agent-launcher profile list [--json]
agent-launcher profile add <name> [--agent claude] [instance options] [--default-agent claude]
agent-launcher profile edit <name> [--agent claude] [instance options] [--default-agent codex] [--remove-agent codex]
agent-launcher profile check <name> [--agent claude] [--json]
```

- `add` creates a profile and fails if the name exists. With `--agent` it also configures that agent's instance. If the profile ends up with exactly one agent, that agent becomes its default.
- `edit` changes an existing profile. With `--agent` it creates or updates that agent's instance. `--default-agent` records the profile's default (it must be configured for the profile). Removing the default agent clears the default; it is never replaced by another.
- `list` shows each profile, its agents and its default.
- `check` resolves an agent for the profile (the default if `--agent` is omitted) and prints the executable it would run, or a diagnostic and exit code 1. It does not start the agent.

## Repository associations

```bash
agent-launcher profile set <repo> <profile> [--archive-tasks | --keep-tasks | --cancel] [--offline] [--json]
agent-launcher profile which <repo> [--offline] [--json]
```

`<repo>` is a local path, `owner/name` or a GitHub URL. Each repository has one explicit profile, kept in `state.db` and keyed by GitHub's immutable repository ID when `gh` can supply it, so renames and transfers keep it.

- `set` is the only command that changes an association. Setting the profile it already has is a no-op (it still records the GitHub ID of an ID-less checkout you named by path). `--offline` skips the GitHub ID lookup. `--force` no longer exists.
- **Reassigning a repository that has tasks** (any state, except ones already archived, and except tasks already on the new profile) needs one explicit resolution for all of them. The launcher first lists each affected task with its profile, session (agent, and whether its terminal is live, not live, or unknown), worktree (path, `created` or `adopted`) and agent conversation ID. Then:
  - `--archive-tasks`: mark them `archived`. `open`, `resume`, `prompt` and `restart` refuse an archived task (`task_archived`). Files, worktrees, sessions and conversations are kept.
  - `--keep-tasks`: they keep their original profile and stay un-openable (`profile_mismatch`) until the repository is set back to it. Setting it back needs no resolution for them.
  - `--cancel`: change nothing.
  
  On a terminal, without a flag, the list is printed and you choose one of the three (Ctrl-C changes nothing, exit 130). Without a terminal, or with `--json`, and without a flag, nothing is changed and the command exits 1 with `reassignment_requires_resolution`, whose `affected_tasks` carries the same list. Two flags together are `conflicting_resolution`. The list is read once, before any transaction (the only terminal probe); the change itself re-reads task IDs only, and if the set differs from what was planned it writes nothing and fails with `reassignment_changed`. A repository with no tasks to resolve is simply changed.
- **What reassignment never does:** close a session, delete a worktree, or rewrite `tasks.profile` / `sessions.profile`. Old sessions stay under their original profile and are never reused by the new one; new tasks get new sessions under the new profile. The association and the archiving commit in one transaction.
- `which` prints the profile. For a repository with none it asks you to choose on a terminal and saves the choice; without a terminal, or with `--json`, it exits 1 with `{"error": {"code": "unknown_repository_profile", "available_profiles": [...], "suggested_profile": null, ...}}`. Other codes: `unknown_profile`, `reassignment_requires_resolution`, `conflicting_resolution`, `reassignment_changed`, `task_archived`, `associated_profile_missing`, `no_profiles`, `ambiguous_repository`, `not_a_repository`, `invalid_repository`.

How repositories are recognised and what is guaranteed: [security.md](security.md).

Instance options (`--executable`, `--arg`, `--env`, `--unset-env`, `--cwd`, `--clear-cwd`, `--resume-arg`, `--prompt-mode`, `--skill-invocation`) are described in [agents.md](agents.md).

Editing changes only what you name. Other profiles and other keys in `config.json` are kept, including top-level and `agent_types` keys this version does not recognise. Unknown keys *inside* a profile or one of its agent instances are not kept: they are validation errors (see [agents.md](agents.md)). Because the whole file must validate, one bad profile makes the whole config unusable: every command that loads the config (including launching with other, healthy profiles) refuses until it is fixed. `agent-launcher doctor` names the offending field. A change that would make the file invalid is refused and nothing is written.

## Schema version

Writing a profile into a version 1 `config.json` also changes `"version"` to 2, silently. This is intentional until `config migrate` (with backup and dry-run) exists. Version 2 is additive, so nothing else in the file changes, but an older release will refuse the file afterwards.

## Example

```bash
agent-launcher profile add work --agent claude --env CLAUDE_CONFIG_DIR=~/.claude-work
agent-launcher profile edit work --agent codex --executable /usr/local/bin/codex-work
agent-launcher profile edit work --default-agent codex
```

Quote or escape `~` if your shell would expand it before agent-launcher sees it; either way the stored value works.

## Safety

A profile never borrows another profile's agent. If `work`'s agent is not configured, or its executable is missing or not executable, resolution fails with a message and nothing else is used (see [agents.md](agents.md)).

**Profiles are not OS-level sandboxing.** Agent Launcher selects which executable, arguments, environment and working directory an agent starts with. It does not isolate processes running as your user: an agent started under one profile can still read files and use credentials that your account can reach, including those of other profiles. Do not rely on profiles to keep work and personal data apart from a misbehaving agent.
