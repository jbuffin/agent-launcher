# 7. Transactional launches: flock per task, persisted stages

Status: accepted

## Context

A first `open` creates a worktree, then a terminal session (and an agent), then records the session. Two simultaneous opens, a failure between the steps, or a crash could create a second terminal session, orphan a worktree or leave a terminal nothing tracks.

## Decision

- **Lock:** `fcntl.flock` on `<home>/locks/<task-id>.lock`, held from task resolution until the session is recorded (`open`, and `restart`). The OS releases it when the process dies, so there are no stale locks to detect and no PID or expiry logic; a DB lease row would need exactly that. A waiting launch polls for up to 60 s, then fails with `task_busy` and changes nothing. Different tasks use different files and never wait for each other. No daemon.
- **Stages:** table `launches` (state migration 5), one row per task while its first launch is incomplete: `started` → `worktree_pending` → `worktree_ready` → `terminal_created`. "Ready" is the transaction that inserts the session, marks the task `active` and deletes the row. Before that the task is `launching` (or `launch_failed` after an error), never `active`.
- **What is persisted:** only stages that create something: the worktree intent and path, the agent and conversation ID, and the terminal reference. Resolving the task, repository, profile, workflow and agent create nothing, so they are re-checked on every attempt rather than stored; a stored answer could only go stale (a profile association can change between attempts). A task in `launching` may be in progress or interrupted (a killed process cannot update it); Ctrl-C sets `launch_failed`.
- **Resume, don't redo:** a recorded worktree is reused. A terminal session recorded in `launches` that the adapter still finds is adopted instead of creating another; one that has vanished is forgotten and replaced. One that belongs to another adapter is forgotten too, but named in the notice and never closed. One the adapter fails to check is not gone: the launch stops with `terminal_check_failed`, the record is kept and no second agent starts; the user verifies or closes it and retries.
- **Crash between `git worktree add` and recording it:** the launcher writes the intended path and branch to `launches` before `git worktree add`. On retry, a tree git lists at exactly that path on exactly that branch, with no owner, is recorded as `created`. Anything else at the path is left alone and reported (`worktree_path_exists`).
- **Rollback removes only what this attempt created and can verify:** if recording the session fails after the terminal session was created, the terminal session is closed only when the adapter marked it launcher-created with an identifiable surface and no session record uses it. Otherwise it is kept, recorded in `launches` for the retry, and named in the error (`launch_incomplete`). Worktrees are never removed (no destructive git, ADR 0006): the retry reuses them. Pre-existing and adopted worktrees are never touched.

## Consequences

A terminal session created and then lost to a crash *before* `launches` records it (a window of a few instructions inside the adapter's return) can still be orphaned; the launcher cannot name what it was never told. The lock is held while `open` prompts the user, so an unanswered prompt makes a concurrent `open` fail with `task_busy` after 60 s (safe, nothing changes). Resumes and prompts do not take the lock (they do not create sessions); `restart` does.
