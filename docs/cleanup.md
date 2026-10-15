# Completion, archiving and cleanup

Removed ([ADR 0021](adr/0021-commodore-launcher.md)): `cleanup` and `tasks completed` are gone, because the launcher creates no worktrees to clean up and tracks no completion. Commodore owns worktrees. Archiving stays: `commodore tasks archive <task>` and `commodore tasks unarchive <task>` change only the task's state, and `open` and `resume` refuse an archived task.
