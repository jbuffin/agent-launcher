# Completion, archiving and cleanup

SPEC §25, [ADR 0018](adr/0018-completion-archive-cleanup.md).

## Detection

`agent-launcher tasks completed [--offline] [--json]` re-reads the issue or pull request of every GitHub task that is not archived and lists the tasks whose item is **closed** (issue), or **closed or merged** (pull request). `open` does the same for the one task it opens (best effort, skipped with `--offline`, never blocks the open). A task found closed gets `cleanup_eligible_at` (`eligible_for_cleanup` in `--json`) and a `completed` row in its history; if GitHub later shows the item open again the flag is cleared (`reopened`). That flag is all detection does: no session is closed, no file or worktree is touched, and the task still opens. Items GitHub cannot be asked about are reported as warnings.

## Archiving

`tasks archive <task>` sets the state to `archived` and remembers the previous one. Files, the worktree and the session record stay. `open`, `resume`, `prompt` and `restart` refuse an archived task (`task_archived`, naming the way back). `tasks unarchive <task>` restores the remembered state (for tasks archived by a profile reassignment before it was remembered: `active` if there is a session, else `created`). A task archived by a reassignment still needs its repository back on the task's profile to open.

## Cleanup

`agent-launcher cleanup [<task>...] [--dry-run] [--yes] [--json] [--terminal T]` considers the worktrees that the launcher **created** for completed or archived tasks (or the named tasks). Each is removed only if all of these hold:

- no uncommitted changes and no untracked files (`git status --porcelain --untracked-files=all`);
- no commits that are on no remote-tracking branch, no fetched pull-request ref and not in the base branch (a branch without an upstream counts as unpushed unless all its commits are in the base);
- no live terminal session recorded for the task, and `lsof +D` finds no process in the directory. A session or process that cannot be checked counts as in use;
- the task is not active (it is completed, or archived).

Adopted worktrees are never removed and never offered. On a terminal, each removable worktree needs a yes (default no). Without a terminal (or with `--json`) nothing is removed unless `--yes` is given together with the task IDs to clean; `--yes` alone is refused (`cleanup_requires_list`). `--dry-run` shows the plan and removes nothing.

Ignored files (a local `.env`, `node_modules/`) are not "changes", so they do not block removal, and removal **deletes them too**. The confirmation prompt lists the top-level ignored entries (`ignored_entries` in `--json`); copy out anything you need first.

Removal is `git worktree remove` without `--force` (git itself refuses a dirty tree), then `git branch -d` for the task's branch, which only succeeds when merged; an unmerged branch is kept and said so. Every check runs again under the task's lock right before removal. The task, session and conversation records stay; the worktree row gets `removed_at`, a `worktree_removed` event is added to the task's history, and `open` then stops with `worktree_removed`.

A task that is neither completed nor archived is active, whatever its state (created, launch_failed, active): it is never cleaned, even when named. `--yes --dry-run` needs no task list.

**Getting a removed worktree back.** `open` stops with `worktree_removed`, and `tasks unarchive` warns about it. Add a worktree yourself (`git worktree add <path> <branch>`; the branch is kept when unmerged) and run `agent-launcher worktrees associate <task> <path> --force`: the removed row goes into the task's history and the new worktree is adopted (so never removed by the launcher).

Run `tasks completed` first so the stored GitHub state is current: `cleanup` does not fetch.
