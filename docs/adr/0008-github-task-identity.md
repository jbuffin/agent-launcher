# 8. GitHub task identity and repository resolution

Status: accepted

## Decision

- **A GitHub task is found by GitHub's IDs**, not by `owner/repo#N`: the issue's node ID and database ID (both `UNIQUE` in `task_github`) and the repository's database and node ID. A rename or transfer of the repository changes the URL and name but not these, so opening the old or the new URL finds the same task. The URL is stored (it is the prompt) and refreshed from GitHub each time; it is used as a key only when GitHub cannot be reached.
- **Tasks are source-independent.** `tasks.source` (`local` | `github`) plus a side table `task_github` (state migration 6), keyed by the internal task ID. Linking a local task to an issue (#17) is an `INSERT` into that table; `tasks.id` does not change. Only what later tickets need is stored: kind, number, title, state, labels, author, assignees, URL and the repository IDs. Bodies and comments are never stored.
- **One module talks to GitHub:** `github.py` runs `gh api` and `gh repo clone` as argv lists with timeouts and returns `GitHubError` with a stable code. Authentication is behind `GitHubAuth` (shared by all profiles today); nothing runs `gh auth`. Two REST reads per issue, no GraphQL: the plain responses carry every field needed.
- **Resolution order** is SPEC §11: mapping, remembered path, configured roots (two levels deep, `origin` only, `.git` directories only), then remote inspection (checkouts in those roots with another GitHub name, asked about by ID, at most 25 calls; the answer for a remote URL is cached for 7 days in `github_remote_ids`, state migration 7), then clone. A candidate counts only if its primary remote is the repository and GitHub's ID agrees when GitHub can be asked. A bad explicit mapping is an error, not a fall-through. Only directories the user configured are searched (SPEC §11): `clone_root` is scanned only if set explicitly, not for its `~/Projects` default. The exact path a clone would go to is always checked, so a repository cloned by a launch that then stopped (for example at the profile question) is found, not cloned again.
- **One issue is set up at a time:** `flock` on `<home>/locks/issue-<database id>.lock` around find, locate, associate and create. Two simultaneous opens create one task and clone once; the launch itself is serialised by the task lock of ADR 0007.

## Consequences

Offline, an issue never opened before cannot be opened; a known one is found by URL. A freed-and-reused repository name is told apart from the old repository by ID only while GitHub is reachable.
