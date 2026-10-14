# Configuration migrations and `config edit`

## Versioned files

`config.json` (currently version 3) carries a `version`. When a release changes the schema, the version goes up and an ordered chain of Python functions (`vN -> vN+1`, in `migrations.py`) upgrades older files. Each step takes the raw JSON object and returns the new one; keys it does not mean to change, including unknown ones, stay as they are. `state.db` has its own migrations and is never touched here, so repository-to-profile associations are unaffected.

`config.json` has two migrations:

- **1 to 2** adds the optional `agent_types` and `profiles`, which changes just the number.
- **2 to 3** renames the agent keys to harness: `agent_types` to `harness_types`, `agent_selection` to `harness_selection`, and in each profile `default_agent` to `default_harness` and `agents` to `harnesses`. Nothing else changes, unknown keys included. A profile that holds both spellings of one key is an error: the plan reports it and nothing is written, because merging would guess which one you meant.

A version 1 file chains through both steps in one run.

## `config migrate`

```
commodore config migrate --dry-run [--json]
commodore config migrate [--yes] [--json]
```

- `--dry-run` lists each file's plan and a unified diff, and writes nothing (no backup either).
- A real run, per file: copy it to `<home>/backups/<name>.<UTC timestamp>.json` (never overwriting an earlier backup), write the migrated object atomically, re-read and validate the file on disk, and put the backup back byte for byte if validation fails.
- If the migrated object would not validate, nothing is written at all.
- A migration is **significant** when it changes any top-level key other than `version`. Significant migrations ask for confirmation; without a terminal (or with `--json`) they need `--yes`. A version-only migration (1 to 2, or 2 to 3 for a file with none of the renamed keys) just runs; any other 2 to 3 migration is significant and needs `--yes` without a terminal.
- Several files are migrated one after another, each with its own backup. If one fails (and is restored), the run stops with exit 1 and the files already migrated stay migrated; the output names them. Run it again after fixing the cause.
- A file that changes between the plan and the write (bytes differ) is not written: `config_changed`.
- If the backup cannot be made, nothing is written (`backup_failed`).
- A file whose version is newer than this release, missing, or not a whole number, or that is not a JSON object, is refused untouched with exit 1. A missing file is skipped. Up-to-date files are left alone and get no backup.

`commodore doctor` warns when `config.json` is at an older version and points here. Every command that loads the config (`open`, `profile`, `setup`, `config edit` and the rest) refuses an older file with the error code `config_outdated` and points here; nothing bumps an old file's version as a side effect ([ADR 0019](adr/0019-config-migrations.md)). `config show` and `config validate` still read it: `config validate` (including its workflow-fallback and template checks) and `doctor`'s workflow check read the migrated result in memory, and `config validate` exits 0 with `warning: version: …` (`"outdated"` under `--json`), and `doctor` warns.

For a custom config that needs interpretation, `commodore configure` starts the harness-assisted maintenance task.

### The first-run copy

The first run after the rename copies `~/.agent-launcher` to `~/.commodore-launcher` ([ADR 0021](adr/0021-commodore-launcher.md)). A copied `config.json` older than version 3 is migrated in memory inside the copy, so no `backups/` entry is made: the old home is the backup. The copy's one line on stderr says so. If the migration cannot run or the result would not validate, the config is copied as it was and the line says to run `commodore config migrate`. Even then a `repositories.worktree_root` inside the old home is pointed at the new default, and nothing else in the file changes.

## `config edit`

Refuses a file older than the current version (`config_outdated`; run `config migrate` first). Opens `$VISUAL`, else `$EDITOR`, else `vi` (arguments allowed, e.g. `code --wait`) on a temporary copy. When the editor exits successfully the text must parse as a JSON object and pass the checks of `config validate`; unknown fields are only warned about. If it is invalid you can edit again or discard (without a terminal it is discarded and the command fails). A valid result replaces `config.json` atomically. The command also writes nothing when the text is unchanged, when the editor fails, or when `config.json` changed on disk while you were editing. A missing config starts from `{"version": 3}`; leaving it untouched creates nothing. Editor output that is not UTF-8 text is treated as invalid.
