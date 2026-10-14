# Task worktrees

Removed ([ADR 0021](adr/0021-commodore-launcher.md)): the launcher creates no worktrees, adopts none and removes none. The harness starts in the repository's own checkout, and Commodore owns worktrees. The `worktrees` command is gone. A worktree Agent Launcher made earlier stays on disk where it was; the first run's copy of the old home lists them once.
