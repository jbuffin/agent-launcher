# Configuration, profiles, agents

Files live in `~/.agent-launcher/` (or `$AGENT_LAUNCHER_HOME`): `config.json`, `workflows.json`, `templates/`, `state.db`, `logs/`, `mock-terminal.json`.

## config.json

```json
{
  "version": 2,
  "debug": false,
  "terminal": {"adapter": "cmux"},
  "repositories": {"search_roots": [], "clone_root": null, "auto_clone": false, "mappings": {}, "worktree_root": "~/.agent-launcher/worktrees", "base_branches": {}},
  "workflow_routing": {"selection_mode": "automatic", "require_verified_skills": false, "fallback": "default"},
  "prompt_execution": "prepare",
  "prompt_template": null,
  "agent_selection": "always_ask",
  "logs": {"max_bytes": 1000000, "backup_count": 5},
  "profiles": {}
}
```

- `version` is required (currently 2; 1 is accepted). A newer version than installed is rejected. `profile add|edit` and `setup` refuse a version 1 file (run `config migrate` first); nothing bumps the version implicitly.
- Unknown top-level keys are warnings in `config validate` (failures with `--strict`) and are preserved by programmatic edits. Unknown keys inside a profile or an agent instance are errors (a typo such as `environment` for `env` would otherwise run an agent with the wrong identity).
- One invalid profile makes the whole file unusable: every command that loads config refuses until fixed. `doctor` names the field.

| Key | Values |
| --- | --- |
| `terminal.adapter` | `cmux` (default). `--terminal mock` on a command records launches in `mock-terminal.json` instead of launching. |
| `repositories.search_roots` | directories searched (one and two levels deep) for a repository's checkout |
| `repositories.clone_root` | where clones go; `auto_clone: true` clones without asking |
| `repositories.mappings` | `{"owner/name": "/path"}` |
| `repositories.worktree_root` | task worktrees: `<root>/<repo>-<id>/<task-id>` |
| `repositories.base_branches` | `{"/abs/repo/path": "branch"}`; default is `origin/HEAD` |
| `workflow_routing.selection_mode` | `automatic`, `ask_on_multiple`, `always_ask` |
| `workflow_routing.require_verified_skills` | `true` refuses to launch when a skill cannot be found by name |
| `workflow_routing.fallback` | a workflow id; `default` is built in (no skill) |
| `prompt_execution` | `prepare` (default) or `execute` |
| `agent_selection` | `always_ask`, `use_default`, `ask_if_multiple` |

## Profiles and agent instances

```
agent-launcher profile list [--json]
agent-launcher profile add <name> --agent claude --env CLAUDE_CONFIG_DIR=~/.claude-work [--default-agent claude]
agent-launcher profile edit <name> --agent codex --executable /path/codex-work --default-agent codex
agent-launcher profile edit <name> --remove-agent codex
agent-launcher profile check <name> [--agent codex] [--json]
```

Profile names: letters, digits, `-`, `_`. Instance flags: `--executable`, `--arg` (repeat; replaces the list; write `--arg --model --arg opus` for dashed values), `--env NAME=VALUE` (repeat; merged), `--unset-env`, `--cwd`, `--clear-cwd`, `--resume-arg`, `--prompt-mode`, `--skill-invocation`.

Instance fields: `executable` (absolute path or bare name looked up on the instance's `PATH`), `args`, `env`, `working_directory`, `resume_args`, `prompt_mode` (recorded, unused), `skill_invocation` (`slash`, `prompt`, `none`). An instance is launched as an argument list with no shell; for a wrapper, point `executable` at a script. Resolution never falls back to another agent, profile or executable. `profile check` shows what would run without starting it.

A profile with several agents needs a `default_agent`; removing the default clears it.

## Setup wizard

`agent-launcher setup` asks about profiles, agents, terminal and defaults, shows a diff, and writes atomically after confirmation. It only adds or updates, and keeps unknown fields. Unattended: `setup --answers FILE --dry-run`, then `--yes` (see `agent-launcher setup --help`; answers file keys: `profiles`, `terminal_adapter`, `search_roots`, `worktree_root`, `workflow_selection`, `prompt_execution`, `agent_selection`). It never reads an agent's own config directory.

## Repository associations

```
agent-launcher profile which <repo> [--offline] [--json]
agent-launcher profile set <repo> <profile> [--archive-tasks | --keep-tasks | --cancel] [--offline] [--json]
```

`<repo>` is a path, `owner/name` or a GitHub URL. See hard rule 3 in SKILL.md. `set` to the current profile is a no-op. Reassignment never closes sessions, deletes worktrees or rewrites the profile stored on tasks.

## Migrations

```
agent-launcher config migrate --dry-run [--json]   # plan + diff for config.json and workflows.json; writes nothing
agent-launcher config migrate [--yes] [--json]     # back up to <home>/backups/<file>.<timestamp>.json, write, validate, restore on failure
agent-launcher config edit                         # $VISUAL/$EDITOR on a temp copy; saved only if it validates
```

- Always run `--dry-run` first and show the user the diff. A migration that changes more than `version` is "significant": it asks for confirmation, and without a terminal needs `--yes` (only pass it once the user has seen the diff).
- A file newer than this release is refused untouched; upgrade `agent-launcher` instead. Unknown fields are kept.
- `config edit` is interactive (it opens an editor), so do not run it yourself; edit the JSON directly or use the CLI commands, then `config validate`.
- If a custom config needs judgement a migration cannot make, hand it to `agent-launcher configure`.
- The state database upgrades itself on use (`doctor` warns about an older schema and fails on a newer one); `config migrate` never touches it, so repository-to-profile associations are unaffected.
