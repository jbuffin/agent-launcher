# 0018: Completion detection, archiving and cleanup

Ticket #22 (SPEC §25).

- **Eligibility is a flag, not a state.** `tasks.cleanup_eligible_at` is set when GitHub shows the issue closed or the PR closed or merged, and cleared if the item is seen open again. The lifecycle state (`active`, `archived`, ...) is untouched, so detection can never stop a session or change what `open` does. Changes are appended to `task_events` (migration 10), the lifecycle history.
- **Detection runs on `tasks completed`** (unless `--offline`) **and opportunistically on `open`** of a GitHub task. `refresh_github` applies it, so every existing refresh path does too. Fetch failures are warnings. Archived tasks are not refreshed.
- **One archive mechanism.** `archived` is #18's state. `tasks archive` and reassignment both go through `tasks.archive_tasks`, which now records `archived_state`; `tasks unarchive` restores it (derived for older rows). `task_archived` names the way back.
- **A task is "active"** for cleanup unless it is archived or completed, so created, launching, launch_failed and active are all protected. A task that was merely opened and is still open on GitHub is never cleaned, even if named.
- **Unpushed** is `git rev-list HEAD <branch> --not --remotes --glob=refs/agent-launcher/* <base>`. That makes "no upstream" unpushed unless every commit is in the base or on some remote-tracking ref.
- **Unknown means in use.** A recorded terminal on another adapter, an adapter that cannot read its screen, a missing or slow `lsof`: all block removal. This makes `cleanup` need `lsof` (present on macOS and most Linux).
- **No force, and rechecked.** `git worktree remove` without `--force`, `git branch -d` without `-D`, all checks again under the task lock. The worktree row is kept with `removed_at`; `open` reports `worktree_removed` rather than recreating it.
- **Confirmation.** Per worktree on a terminal (default no). Non-interactive removal needs `--yes` plus named tasks.
