# Configuration

Everything Commodore Launcher stores lives under one directory, `~/.commodore-launcher/`. Set `COMMODORE_LAUNCHER_HOME` to use another root (tests and trial runs always do). Nothing is ever written into the installed package, so `pipx upgrade` and `pipx uninstall` leave your configuration and state alone.

The product was called Agent Launcher until the rename, and kept its files in `~/.agent-launcher/`. The first command that finds that directory and no `~/.commodore-launcher/` copies the config, `workflows.json`, the state database, `templates/` and `logs/` across and says so once on stderr. It does not copy worktrees, locks, backups or the maintenance repository, never copies again, and never changes or removes the old directory. With `COMMODORE_LAUNCHER_HOME` set, nothing is copied. If `~/.agent-launcher` is itself a symlink to a directory, the link is followed once and the copy is made from its target; links inside it are skipped. If the copy fails (an unreadable file, say), the command prints `error:` with the file and the cause and exits 1 without creating `~/.commodore-launcher`, so fixing the cause and running again copies; set `COMMODORE_LAUNCHER_HOME` instead to start with a fresh home. A `repositories.worktree_root` in the copied config that points inside `~/.agent-launcher` is changed to `~/.commodore-launcher/worktrees` in the copy only, so new worktrees never land in the old home.

| Path (under the root) | What | Edited by |
| --- | --- | --- |
| `config.json` | Settings, harness types, profiles. Plain JSON; you may edit it by hand. | you, `setup`, `profile`, `config edit`, `config migrate` |
| `workflows.json` | Workflow rules ([workflows.md](workflows.md)). Optional. | you, `workflows init` |
| `templates/` | Prompt templates, `<name>.txt` ([workflows.md](workflows.md#prompt-templates)). Optional. | you |
| `state.db` | SQLite: tasks, repository associations, sessions, worktrees, launches. Migrated in place. | the launcher only |
| `locks/` | Per-task lock files that serialise launches. | the launcher only |
| `logs/` | JSON-lines log, rotated ([diagnostics.md](diagnostics.md)). Created on the first record. | the launcher only |
| `backups/` | Copies made by `config migrate` before it writes. | `config migrate` |
| `worktrees/` | Default `repositories.worktree_root`: one Git worktree per task. | the launcher only |
| `maintenance/` | The directory `configure` and `integrate` use when you name no repository. | the launcher only |
| `mock-terminal.json` | What the `mock` terminal adapter recorded (tests, trial runs). | the launcher only |

A missing `config.json` is fine: defaults apply and nothing is created. Unknown keys are never dropped: `config validate` reports them as warnings (`--strict` fails on them) and every write keeps them. Do not put tokens or credentials in config: harnesses bring their own login through the environment variables you set per profile.

`commodore config show` prints the effective configuration (defaults overlaid with the file). `config validate` checks the file and names the failing field. `config migrate` upgrades an older `version` with a backup, `config edit` edits a copy in `$VISUAL`/`$EDITOR` and saves it only if it validates ([config-migrations.md](config-migrations.md)).

## Keys

`version` is required: the schema version (currently `2`; version 1 files still load and `config migrate` upgrades them). A file newer than the installed release is refused.

| Key | Values | Default | Used by |
| --- | --- | --- | --- |
| `debug` | `true`, `false` | `false` | Same as always passing `--debug` |
| `logs.max_bytes`, `logs.backup_count` | integers (at least 1024; 0 to 1000) | `1000000`, `5` | Log rotation |
| `harness_types` | map of name to `{adapter, executable}` | `claude`, `codex`, `copilot` | Which harnesses exist ([agents.md](agents.md)) |
| `profiles` | map of name to `{default_harness, harnesses}` | none | Identities ([profiles.md](profiles.md)) |
| `terminal.adapter` | `cmux`, or `mock` (records instead of launching) | `cmux` | [terminal-adapters.md](terminal-adapters.md); `--terminal` overrides it per command |
| `harness_selection` | `always_ask`, `use_default`, `ask_if_multiple` | `always_ask` | The harness picker ([architecture.md](architecture.md#harness-selection)) |
| `prompt_execution` | `prepare`, `execute` | `prepare` | Whether a prompt is only typed or also submitted; a workflow's own setting and `--execute`/`--prepare` override it |
| `prompt_template` | a template name, or absent | none | Template for workflows with neither a template nor a skill |
| `workflow_routing.selection_mode` | `automatic`, `ask_on_multiple`, `always_ask` | `automatic` | [workflows.md](workflows.md) |
| `workflow_routing.require_verified_skills` | `true`, `false` | `false` | A skill not found by name is launched with a notice, or refused |
| `workflow_routing.fallback` | a workflow id | `default` | The workflow when no rule matches |
| `repositories.search_roots` | list of absolute or `~/` paths | `[]` | Where checkouts are looked for (two levels deep) |
| `repositories.clone_root` | path | `~/Projects` when unset | Where a missing repository is cloned, after asking |
| `repositories.auto_clone` | `true`, `false` | `false` | Clone without asking |
| `repositories.mappings` | `owner/name` to checkout path | `{}` | Wins over every other way of finding a repository |
| `repositories.worktree_root` | path | `~/.commodore-launcher/worktrees` | Where task worktrees go ([worktrees.md](worktrees.md)) |
| `repositories.base_branches` | checkout path to branch | `{}` | The branch worktrees are cut from; absent means the repository's `origin/HEAD` |

Paths must be absolute or start with `~/`. Profile and harness type names use letters, digits, `-` and `_`. A profile harness has `executable`, `args`, `env`, `working_directory`, `resume_args`, `prompt_mode` and `skill_invocation`; a misspelt field there is an error, not a warning, because a dropped `env` would run the harness as the wrong identity.

## Environment variables

| Variable | Effect |
| --- | --- |
| `COMMODORE_LAUNCHER_HOME` | Replaces `~/.commodore-launcher/` |
| `VISUAL`, `EDITOR` | The editor for `config edit` |
| `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `COPILOT_HOME` | Set per profile in the harness's `env`, not globally; `doctor` and `setup` warn when two profiles would share one |
| `COMMODORE_LAUNCHER_LIVE`, `COMMODORE_LAUNCHER_SANDBOX_REPO`, `COMMODORE_LAUNCHER_PACKAGING`, `COMMODORE_LAUNCHER_LIVE_DIR`, `COMMODORE_LAUNCHER_LIVE_EXECUTE`, `COMMODORE_LAUNCHER_MOCK_CREATE_DELAY` | Test switches only ([development.md](development.md)); only the mock terminal reads the last one |
