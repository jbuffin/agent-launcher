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

Instance options (`--executable`, `--arg`, `--env`, `--unset-env`, `--cwd`, `--clear-cwd`, `--resume-arg`, `--prompt-mode`, `--skill-invocation`) are described in [agents.md](agents.md).

Editing changes only what you name. Other profiles, other keys in `config.json` and fields this version does not recognise are kept. A change that would make the file invalid is refused and nothing is written.

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
