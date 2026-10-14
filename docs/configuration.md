# Configuration

Everything Commodore Launcher stores lives under one directory, `~/.commodore-launcher/`. Set `COMMODORE_LAUNCHER_HOME` to use another root (tests and trial runs always do). Nothing is ever written into the installed package, so `pipx upgrade` and `pipx uninstall` leave your configuration and state alone.

If you used Agent Launcher, the first command copies its home once; [Upgrading from Agent Launcher](config-migrations.md#upgrading-from-agent-launcher) says what is copied and what is archived.

| Path (under the root) | What | Edited by |
| --- | --- | --- |
| `config.json` | Settings, harness types, profiles. Plain JSON; you may edit it by hand. | you, `setup`, `profile`, `config edit`, `config migrate` |
| `workflows.json`, `templates/` | Left by an older release (the first-run copy brings them over). Unused. `doctor` lists them once. | you, to delete |
| `state.db` | SQLite: tasks, repository associations, sessions, launches. Migrated in place. | the launcher only |
| `locks/` | Per-task lock files that serialise launches. | the launcher only |
| `logs/` | JSON-lines log, rotated ([diagnostics.md](diagnostics.md)). Created on the first record. | the launcher only |
| `backups/` | Copies made by `config migrate` before it writes. | `config migrate` |
| `maintenance/` | The directory `configure` and `integrate` use when you name no repository. | the launcher only |
| `mock-terminal.json` | What the `mock` terminal adapter recorded (tests, trial runs). | the launcher only |

A missing `config.json` is fine: defaults apply and nothing is created. Unknown keys are never dropped: `config validate` reports them as warnings (`--strict` fails on them) and every write keeps them. Do not put tokens or credentials in config: harnesses bring their own login through the environment variables you set per profile.

`commodore config show` prints the effective configuration (defaults overlaid with the file). `config validate` checks the file and names the failing field. `config migrate` upgrades an older `version` with a backup, `config edit` edits a copy in `$VISUAL`/`$EDITOR` and saves it only if it validates ([config-migrations.md](config-migrations.md)).

## Keys

`version` is required: the schema version (currently `3`; older files still load for `config show` and `config validate`, and `config migrate` upgrades them). A file newer than the installed release is refused.

| Key | Values | Default | Used by |
| --- | --- | --- | --- |
| `debug` | `true`, `false` | `false` | Same as always passing `--debug` |
| `logs.max_bytes`, `logs.backup_count` | integers (at least 1024; 0 to 1000) | `1000000`, `5` | Log rotation |
| `harness_types` | map of name to `{adapter, executable}` | `claude`, `codex`, `copilot` | Which harnesses exist ([harnesses.md](harnesses.md)) |
| `profiles` | map of name to `{default_harness, harnesses}` | none | Named profiles ([profiles.md](profiles.md)) |
| `terminal.adapter` | `cmux`, or `mock` (records instead of launching) | `cmux` | [terminal-adapters.md](terminal-adapters.md); `--terminal` overrides it per command |
| `harness_selection` | `always_ask`, `use_default`, `ask_if_multiple` | `always_ask` | The harness picker ([architecture.md](architecture.md#harness-selection)) |
| `repositories.search_roots` | list of absolute or `~/` paths | `[]` | Where checkouts are looked for (two levels deep) |
| `repositories.clone_root` | path | `~/Projects` when unset | Where a missing repository is cloned, after asking |
| `repositories.auto_clone` | `true`, `false` | `false` | Clone without asking |
| `repositories.mappings` | `owner/name` to checkout path | `{}` | Wins over every other way of finding a repository |

Paths must be absolute or start with `~/`. Profile and harness type names use letters, digits, `-` and `_`. A profile harness has `executable`, `args`, `env`, `working_directory` and `resume_args`; a misspelt field there is an error, not a warning, because a dropped `env` would run the harness as the wrong identity.

## Environment variables

| Variable | Effect |
| --- | --- |
| `COMMODORE_LAUNCHER_HOME` | Replaces `~/.commodore-launcher/` |
| `VISUAL`, `EDITOR` | The editor for `config edit` |
| `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `COPILOT_HOME` | Set per profile in the harness's `env`, not globally; `doctor` and `setup` warn when two profiles would share one |
| `COMMODORE_LAUNCHER_LIVE`, `COMMODORE_LAUNCHER_SANDBOX_REPO`, `COMMODORE_LAUNCHER_PACKAGING`, `COMMODORE_LAUNCHER_LIVE_DIR`, `COMMODORE_LAUNCHER_LIVE_EXECUTE`, `COMMODORE_LAUNCHER_MOCK_CREATE_DELAY` | Test switches only ([development.md](development.md)); only the mock terminal reads the last one |
