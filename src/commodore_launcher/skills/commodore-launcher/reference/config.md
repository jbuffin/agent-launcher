# Configuration, profiles, harnesses

Files live in `~/.commodore-launcher/` (or `$COMMODORE_LAUNCHER_HOME`): `config.json`, `workflows.json`, `templates/`, `state.db`, `logs/`, `mock-terminal.json`.

## config.json

```json
{
  "version": 3,
  "debug": false,
  "terminal": {"adapter": "cmux"},
  "repositories": {"search_roots": [], "clone_root": null, "auto_clone": false, "mappings": {}, "worktree_root": "~/.commodore-launcher/worktrees", "base_branches": {}},
  "workflow_routing": {"selection_mode": "automatic", "require_verified_skills": false, "fallback": "default"},
  "prompt_execution": "prepare",
  "prompt_template": null,
  "harness_selection": "always_ask",
  "logs": {"max_bytes": 1000000, "backup_count": 5},
  "profiles": {}
}
```

- `version` is required (currently 3). A newer version than installed is rejected. `config show` and `config validate` still read an older file, but every other command refuses it (`config_outdated`) until `config migrate` has run; nothing bumps the version implicitly.
- Unknown top-level keys are warnings in `config validate` (failures with `--strict`) and are preserved by programmatic edits. Unknown keys inside a profile or a harness are errors (a typo such as `environment` for `env` would otherwise run a harness with the wrong identity).
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
| `harness_selection` | `always_ask`, `use_default`, `ask_if_multiple` |

## Profiles and harnesses

```
commodore profile list [--json]
commodore profile add <name> --harness claude --env CLAUDE_CONFIG_DIR=~/.claude-work [--default-harness claude]
commodore profile edit <name> --harness codex --executable /path/codex-work --default-harness codex
commodore profile edit <name> --remove-harness codex
commodore profile check <name> [--harness codex] [--json]
```

Profile names: letters, digits, `-`, `_`. Instance flags: `--executable`, `--arg` (repeat; replaces the list; write `--arg --model --arg opus` for dashed values), `--env NAME=VALUE` (repeat; merged), `--unset-env`, `--cwd`, `--clear-cwd`, `--resume-arg`, `--prompt-mode`, `--skill-invocation`.

Instance fields: `executable` (absolute path or bare name looked up on the instance's `PATH`), `args`, `env`, `working_directory`, `resume_args`, `prompt_mode` (recorded, unused), `skill_invocation` (`slash`, `prompt`, `none`). An instance is launched as an argument list with no shell; for a wrapper, point `executable` at a script. Resolution never falls back to another harness, profile or executable. `profile check` shows what would run without starting it.

A profile with several harnesses needs a `default_harness`; removing the default clears it.

## Setup wizard

`commodore setup` asks about profiles, harnesses, terminal and defaults, shows a diff, and writes atomically after confirmation. It only adds or updates, and keeps unknown fields. Unattended: `setup --answers FILE --dry-run`, then `--yes` (see `commodore setup --help`; answers file keys: `profiles`, `terminal_adapter`, `search_roots`, `worktree_root`, `workflow_selection`, `prompt_execution`, `harness_selection`). It never reads a harness's own config directory.

## Repository associations

```
commodore profile which <repo> [--offline] [--json]
commodore profile set <repo> <profile> [--archive-tasks | --keep-tasks | --cancel] [--offline] [--json]
```

`<repo>` is a path, `owner/name` or a GitHub URL. See hard rule 3 in SKILL.md. `set` to the current profile is a no-op. Reassignment never closes sessions, deletes worktrees or rewrites the profile stored on tasks.

## Migrations

```
commodore config migrate --dry-run [--json]   # plan + diff for config.json and workflows.json; writes nothing
commodore config migrate [--yes] [--json]     # back up to <home>/backups/<file>.<timestamp>.json, write, validate, restore on failure
commodore config edit                         # $VISUAL/$EDITOR on a temp copy; saved only if it validates
```

- Always run `--dry-run` first and show the user the diff. A migration that changes more than `version` is "significant": it asks for confirmation, and without a terminal needs `--yes` (only pass it once the user has seen the diff).
- A file newer than this release is refused untouched; upgrade `commodore` instead. Unknown fields are kept.
- `config edit` is interactive (it opens an editor), so do not run it yourself; edit the JSON directly or use the CLI commands, then `config validate`.
- If a custom config needs judgement a migration cannot make, hand it to `commodore configure`.
- The state database upgrades itself on use (`doctor` warns about an older schema and fails on a newer one); `config migrate` never touches it, so repository-to-profile associations are unaffected.
