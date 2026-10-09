# Task worktrees

Each task runs in its own Git worktree, and the agent's working directory is that worktree. The first `agent-launcher open <task>` creates it (or lets you adopt one); every later `open`, `resume` and `restart` reuses it.

## Where and what

- Path: `<repositories.worktree_root>/<repo-name>-<repository-id>/<task-id>`. The default root is `~/.agent-launcher/worktrees`. The task ID is not derived from the title, so any title is safe.
- Branch: `task/<task-id>-<title-slug>` (the slug is lower-case ASCII, at most 40 characters; dropped if empty).
- Base branch, in order: `repositories.base_branches` in `config.json` (keyed by the repository's absolute path), the branch `origin/HEAD` points at, then the local default. `main` is never assumed. The base is fetched from `origin` first (not with `--offline`); if the fetch fails you get a notice and the last fetched state is used. When `origin` has the base branch, `origin/<base>` is used even if `origin/HEAD` was never set; when the base is only a guess (the local default or checked-out branch), `open` says which branch it cut from. A repository with no commits has no base and is refused.

```json
{ "repositories": { "base_branches": { "/Users/me/src/service": "develop" } } }
```

## Ownership and adoption

Every recorded worktree is `created` (the launcher made it) or `adopted` (you said it is this task's). Before creating one, `open` lists the repository's other worktrees (`git worktree list --porcelain -z`) and, on a terminal, asks whether to adopt one or create a new one. The answer is remembered and never asked again. A branch whose name looks like the task's is listed first but is not proof of anything. Without a terminal (or with `--json`) nothing is adopted; if a worktree has a `task/<id>*` branch, `open` refuses (`worktree_candidate_exists`) rather than create a second one beside it.

`agent-launcher worktrees associate <task> <path>` adopts explicitly. The path must be a linked worktree of the task's repository, not the main checkout, and not another task's. A task keeps one worktree; a different one is refused. A task that already has a session is refused (`session_exists`) unless you pass `--force`: its agent conversation is tied to the directory it ran in, so resuming from another one would not find it.

## Never destructive

The launcher never resets, stashes, cleans, force-checks-out, pushes or removes. Uncommitted changes in a worktree you adopt are reported and left alone (the Prompter asks you to confirm). If the task's branch already exists, then on a terminal you can adopt the worktree holding it or create one on a suffixed branch (`...-2`); otherwise nothing is created and you get `branch_conflict`, naming the worktree that holds it and the `associate` command. If the target path exists, `worktree_path_exists` (the message names `associate`, in case it is the task's own worktree from an interrupted launch). If a recorded worktree disappears from disk, `open`, `resume` and `restart` all stop with `worktree_missing` before closing or starting anything; it is not recreated.

## Commands

| Command | |
| --- | --- |
| `worktrees list [--json]` | Recorded worktrees, owner, and whether each still exists. |
| `worktrees inspect <task> [--json]` | What was recorded next to what git says now: branch, HEAD, uncommitted changes, locked/prunable. |
| `worktrees associate <task> <path> [--force] [--json]` | Adopt an existing worktree. |

## Claude Code's folder-trust dialog

Every new worktree is a new folder to Claude Code, so its first launch there shows the "do you trust this folder" dialog. The launcher never types into it. The prompt is left on the clipboard and `open` says so; accept the dialog, then run `agent-launcher prompt <task>`. Adopted worktrees may already be trusted.

## Tasks from before worktrees

A task whose session was started before this feature keeps running in its repository directory, because its agent conversation is stored for that directory.
