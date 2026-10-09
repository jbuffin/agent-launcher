# Configuration migrations and `config edit`

## Versioned files

`config.json` (currently version 2) and `workflows.json` (version 1) carry a `version`. When a release changes the schema, the version goes up and an ordered chain of Python functions (`vN -> vN+1`, in `migrations.py`) upgrades older files. Each step takes the raw JSON object and returns the new one; keys it does not mean to change, including unknown ones, stay as they are. `state.db` has its own migrations and is never touched here, so repository-to-profile associations are unaffected.

The only migration so far is `config.json` version 1 to 2 (adds the optional `agent_types` and `profiles`), which changes just the number.

## `config migrate`

```
agent-launcher config migrate --dry-run [--json]
agent-launcher config migrate [--yes] [--json]
```

- `--dry-run` lists each file's plan and a unified diff, and writes nothing (no backup either).
- A real run, per file: copy it to `<home>/backups/<name>.<UTC timestamp>.json` (never overwriting an earlier backup), write the migrated object atomically, re-read and validate the file on disk, and put the backup back byte for byte if validation fails.
- If the migrated object would not validate, nothing is written at all.
- A migration is **significant** when it changes any top-level key other than `version`. Significant migrations ask for confirmation; without a terminal (or with `--json`) they need `--yes`. A version-only migration (1 to 2) just runs.
- Several files are migrated one after another, each with its own backup. If one fails (and is restored), the run stops with exit 1 and the files already migrated stay migrated; the output names them. Run it again after fixing the cause.
- A file that changes between the plan and the write (bytes differ) is not written: `config_changed`.
- If the backup cannot be made, nothing is written (`backup_failed`).
- A file whose version is newer than this release, missing, or not a whole number, or that is not a JSON object, is refused untouched with exit 1. A missing file is skipped. Up-to-date files are left alone and get no backup.

`agent-launcher doctor` warns when `config.json` is at an older version and points here. `profile add`, `profile edit` and `setup` (all with the error code `config_outdated`) no longer bump an old file's version as a side effect: they refuse it and point here ([ADR 0019](adr/0019-config-migrations.md)).

For a custom config that needs interpretation, `agent-launcher configure` starts the agent-assisted maintenance task.

## `config edit`

Opens `$VISUAL`, else `$EDITOR`, else `vi` (arguments allowed, e.g. `code --wait`) on a temporary copy. When the editor exits successfully the text must parse as a JSON object and pass the checks of `config validate`; unknown fields are only warned about. If it is invalid you can edit again or discard (without a terminal it is discarded and the command fails). A valid result replaces `config.json` atomically. The command also writes nothing when the text is unchanged, when the editor fails, or when `config.json` changed on disk while you were editing. A missing config starts from `{"version": 2}`; leaving it untouched creates nothing. Editor output that is not UTF-8 text is treated as invalid.
