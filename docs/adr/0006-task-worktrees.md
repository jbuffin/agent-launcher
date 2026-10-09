# 6. Task worktrees: layout, base branch, ownership

Status: accepted

## Context

Each task needs its own branch and worktree (SPEC §12). The repository's base branch must not be assumed to be `main`, a worktree the launcher did not create must never be mistaken for the task's, and nothing in this ticket may destroy work.

## Decision

- **One worktree per task**, in table `worktrees` (state migration 4), with `ownership` of `created` or `adopted` enforced by a CHECK constraint. Adoption only happens through the Prompter at first `open`, or `worktrees associate`. A matching branch name only orders the choices.
- **Layout:** `<repositories.worktree_root>/<repo-name>-<repository-id>/<task-id>`. The task ID is opaque and the repository name is slugified, so the path is safe for any title and unique across repositories. The branch is `task/<task-id>-<title-slug>`.
- **Base branch:** `repositories.base_branches[<repo path>]`, then `origin/HEAD`, then the local default (`init.defaultBranch`, then the checked-out branch). The branch is fetched from `origin` unless `--offline`; a failed fetch is a notice, never an error. New branches are cut from `refs/remotes/origin/<base>` when it exists (with `--no-track`), else the local branch.
- **No destructive git.** The only writes are `git worktree add` and `git fetch`. All git calls are argv lists with a timeout, values after `--`, and machine-readable output (`worktree list --porcelain -z`, `status --porcelain=v1 -z`).
- Tasks whose session predates worktrees keep running in their repository directory, because their conversation lives there.

## Consequences

Removing worktrees, cleaning up branches and pull-request checkouts are later tickets. A recorded worktree that disappears is reported (`worktree_missing`), not recreated. Two concurrent first opens of one task are not yet serialised (ticket #11).
