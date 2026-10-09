# 13. Linking a local task to a GitHub item

Status: accepted

## Decision

- **Linking is an `INSERT` into `task_github` plus `tasks.source = 'github'`**, as ADR 0008 planned. The task row, its ID, title, worktree, sessions, conversation, profile and workflow are untouched. The local title is kept at link time and replaced by GitHub's at the next `open <url>` refresh (`refresh_github`, ADR 0008); the GitHub title is in `task_github`.
- **One transaction under two locks**, taken issue lock first, then task lock, the same order `open <url>` uses, so the two cannot deadlock. The task is re-read under the locks. The table's `UNIQUE` constraints remain the last defence against a racing creator.
- **Refusals are checked before any write**, in this order: already linked (same item is a no-op, a different one is `task_already_linked`), identity taken by another task (`github_identity_taken`, naming the task and listing the choices), repository (`github_repository_mismatch`), profile (`github_profile_mismatch`).
- **Repository identity is the GitHub repository ID** of the task's stored repository against the item's. A task whose repository has no stored ID is never matched by name, and `link` never gives the row an ID (ADR 0003: a freed name may belong to someone else). It refuses and names `profile set <repo-path> <profile>`: run online on that exact path with the repository's current profile, that explicit command records the ID on the existing row (`associations._adopt_id`); with a different profile and tasks present it refuses (`reassignment_requires_resolution`, ADR 0014) and records nothing, because the refusal rolls the ID back; with `--archive-tasks` or `--keep-tasks` the profile change and the ID are recorded together (`--force` no longer exists). The command prints what it recorded (and `--json` has `recorded_github_id`), so the consent is informed.
- **Conflict resolution is the engineer's.** No merging, no reassigning the item to the new task. The error offers: open the task that owns the item, or keep this one local.
- **GitHub must be reachable**; there is no offline link, since the IDs are the point.

## Consequences

The stored workflow is not re-routed, so a task linked after its first open keeps the workflow it got (#14's reopen rule). A task that was never opened is routed on its first open, with the GitHub facts.
