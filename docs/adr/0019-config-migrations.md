# 0019: Configuration migrations

Ticket #23.

- **Per-file ordered steps**, raw dict in and out (`migrations.MigrationTarget`). One framework serves `config.json` and `workflows.json`; a new versioned file adds a target. Steps are deterministic Python, never an agent.
- **Backup, write, validate, restore.** The backup is a byte copy, and so is the restore, so a failed migration leaves the file exactly as it was. A result that fails validation before writing is not written at all.
- **Significant** means a top-level key other than `version` changes. Only those ask for confirmation, so the current 1-to-2 migration runs unprompted while a future one that rewrites settings does not slip through under `--yes`-less automation.
- **`profile add/edit` and `setup` no longer bump the version.** They refuse an old file with a hint to run `config migrate`. Why: the bump was a silent, unbacked-up write of a schema change that an older release then rejects; one explicit command with a backup and dry-run is easier to reason about. The cost is one extra command on a version 1 file. `setup` that would write nothing still succeeds.
- **`config edit` runs the editor with no timeout** (it is interactive) and as an argv list, never through a shell.
- Agent-assisted interpretation of custom config is the existing `configure` task; no new mechanism.
