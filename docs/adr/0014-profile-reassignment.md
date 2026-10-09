# 0014: Profile reassignment

Ticket #18. Replaces the `profile set --force` stopgap from ADR 0003.

- **One explicit resolution for the whole reassignment**, chosen when the repository has tasks in any state: `--archive-tasks`, `--keep-tasks` or `--cancel`. Interactive mode lists the affected tasks and asks (Prompter); non-interactive mode without a flag fails with `reassignment_requires_resolution` and the same list. Two flags are `conflicting_resolution`.
- **`--force` is removed**, not aliased: nothing outside the tests used it, and an alias would reintroduce a silent change. A repository with nothing to resolve is changed by the plain command, because naming a new profile is already the explicit act.
- **Affected tasks** are the repository's tasks not archived and not already on the new profile. That last rule is what lets `--keep-tasks` be undone by setting the old profile back with no further prompt.
- **`archived` is a minimal task state** (`tasks.TASK_ARCHIVED`). `open`, `resume`, `prompt` and `restart` refuse it (`task_archived`). Ticket #22 owns archiving and builds on it. The previous state is not recorded.
- **Nothing live is touched.** No session is closed, no worktree removed, `tasks.profile` and `sessions.profile` are never rewritten, so an old session can only ever be reopened under its original profile, and `profile_mismatch` keeps `--keep-tasks` tasks shut.
- **Atomic:** the plan (with the only terminal probe) is read first and the prompt happens outside any transaction. Then the association change and the archiving commit in one transaction that re-reads task IDs only, never probing. If the set differs from the plan it writes nothing and raises `reassignment_changed`.
- **Liveness is a best-effort read** of the session's terminal through the configured adapter (`read_screen`): live, not live, or unknown (another adapter, an unsupported capability, or an error). It informs the listing only.
