# 0015: Explicit adoption of external sessions

Status: accepted

- Discovery is an adapter capability (`discover_sessions`) returning `ExternalSession` (ref, title, reported cwd). The core never sees cmux types, and discovery never reads a screen.
- A candidate needs concrete evidence for one task: cwd equals the task's own worktree, or the title carries the task ID, the issue/PR number with the repository name, or the launcher's own `<repo> — <title>` form. The repository directory and free-text title matches are never evidence (SPEC §20).
- Adoption is a command (`sessions adopt`), never automatic, and records `created_by_launcher = false`. That flag alone keeps adapters from force-closing it and `restart` from closing it.
- The picked session is confirmed with a single `read_screen` after it is chosen; other workspaces' screens are never read.
- `open` does not offer adoption inline: `sessions candidates` prints the offer. That keeps `open` free of extra cmux calls.
