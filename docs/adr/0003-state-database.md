# 0003: `state.db`, the runtime state store

Status: accepted

- **Two files, two jobs.** `config.json` is what you edit. `state.db` (SQLite, under the launcher home) is what the launcher records as it runs: repository associations now; tasks, sessions, worktrees and locks in later tickets. They are never merged, and nothing is derived from `state.db` that the config could contradict.
- **One module owns the connection** (`agent_launcher.state`). Every connection sets `journal_mode=WAL`, `busy_timeout=5000` and `foreign_keys=ON`, and runs in autocommit mode; writers use `transaction()`, an explicit `BEGIN IMMEDIATE` ... `COMMIT` kept short, so concurrent launches queue on the lock instead of failing with "database is locked".
- **Schema versioning with `PRAGMA user_version`**, and an ordered list of migration functions in code. Migration N moves version N-1 to N inside one transaction that also bumps the version, so a failure leaves the previous version. The version is re-read under the write lock so two processes starting together migrate once. Shipped migrations are never edited. A database with a newer version is refused untouched.
- **Why not an ORM or a migration tool.** The schema is small and local; plain `sqlite3` from the standard library adds no dependency.
- **Repository identity** is stored as one `repositories` row (optional unique `github_id`, `node_id`, last known `full_name`) with its known paths and normalised remotes in side tables, and a separate `profile_associations` row only when the user chose a profile. Lookup order and the reused-name rule are in [security.md](../security.md).
- **`gh` is optional and fails soft.** Offline, missing or unauthenticated `gh` gives an identity without an ID; it never fails the command.
- **`profile set --force`** is a deliberate stopgap until ticket #18 adds safe reassignment.
- **Doctor** opens the database read-only and reports schema version, integrity (`quick_check`, `foreign_key_check`) and missing tables; it never creates or migrates.
