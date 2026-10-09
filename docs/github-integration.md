# GitHub issues

`agent-launcher open <issue-url>` runs Scenario A: from `https://github.com/<owner>/<repo>/issues/<N>` to an agent working in its own worktree.

```
agent-launcher open https://github.com/acme/widgets/issues/7 [--agent claude] [--terminal cmux] [--json] [--offline]
```

Only github.com issue URLs are accepted. Pull requests (`unsupported_url`) and other hosts are not supported yet.

## What happens

1. **Fetch** the issue and its repository with `gh api` (read-only). Failures are structured: `not_found`, `github_not_authenticated`, `github_forbidden`, `github_unreachable`, `gh_unavailable`, `github_error`.
2. **Find the task by GitHub's stable IDs** (issue node ID and database ID). If it exists, its stored title, state, labels and URL are refreshed and the task is opened as usual: a task that already has a session is focused (see [sessions.md](sessions.md)). Opening the same issue twice, or through a renamed repository's old or new URL, never makes a second task or session.
3. Otherwise **find the repository** (below), **ensure its profile association** (the stored one, or you choose once on a terminal; see [security.md](security.md)), create the task and continue as a normal `open`: worktree, agent choice, session.
4. The agent's **prompt is the issue URL**, exactly. Workflow and skill text come in a later ticket.

## Identity

`tasks.source` is `github`; `task_github` holds the issue node ID and database ID, the repository's database and node ID, number, kind, title, state, labels, author, assignees and URL. Issue bodies and comments are not stored. `tasks show <id>` prints it (`github` in `--json`). The task keeps an ordinary `t-xxxxxxxx` ID. See [ADR 0008](adr/0008-github-task-identity.md).

## Finding the repository

In this order, stopping at the first checkout whose `origin` is the repository (and, online, whose GitHub ID agrees):

1. `repositories.mappings`: `{"acme/widgets": "~/src/widgets"}`. A subdirectory or linked worktree is resolved to the main checkout. If the mapped path is not that repository you get `mapping_invalid`; nothing else is tried.
2. A path remembered from an earlier launch. It is checked again each time; a path that is now another repository, or gone, is ignored.
3. `repositories.search_roots`, and `repositories.clone_root` if you set it (the built-in `~/Projects` default is not searched): the directories directly inside them, and the ones inside those (`~/Projects/widgets`, `~/Projects/acme/widgets`). Never recursive, never anywhere else; linked worktrees are skipped. The exact place a clone would go is also checked, so a clone left by an interrupted launch is reused. Several matches: you choose on a terminal, otherwise `ambiguous_repository_location` (add a mapping).
4. Remote inspection: checkouts in those roots whose `origin` has a different GitHub name are asked about by repository ID (at most 25 calls; answers are cached for 7 days in `state.db`), which finds a repository that was renamed or transferred after you cloned it. Not done with `--offline`.
5. Not found: on a terminal you are asked `Clone it into <clone_root>/<name>?` (default **No**). Without a terminal the error is `repository_not_found`, naming the target; nothing is cloned. `repositories.auto_clone: true` clones without asking. The clone is `gh repo clone owner/name <absolute path>`; if the directory is taken it uses `owner-name`, and if that is taken too, `clone_target_exists`.

A freshly cloned repository is associated with a profile (you are asked, as for any new repository) before the task is created, so no agent launches in a repository without one. With no terminal and `auto_clone` on, the clone happens and then `unknown_repository_profile` stops the launch; run `agent-launcher profile set <path> <profile>` and open again (the clone is found, not repeated).

## Accounts and untrusted text

- `gh` is used as logged in. The launcher never runs `gh auth`, never switches the active account, and does not use GitHub identity to choose a profile. All profiles share the login today; `GitHubAuth` in `github.py` is where a per-profile login can be added.
- Titles, labels and names come from GitHub and are untrusted. They never reach a command: the argv holds only a validated `owner`, `name` and number. A stored title has control characters removed and is cut at 200 characters; the branch and worktree names use a letters-digits-dashes slug of it; the prompt does not contain it.

## Offline

`--offline` (or an unreachable GitHub) cannot open an issue for the first time (`github_required`). A task already created is found by its URL (query strings, fragments and a trailing slash are ignored) and opened with a notice that it was not refreshed.

## Config

`repositories.search_roots`, `clone_root` (clone target, default `~/Projects`; searched only if you set it), `auto_clone` (default `false`), `mappings`. See [setup.md](setup.md).

## Tests

`uv run pytest` uses a fake `gh`. `AGENT_LAUNCHER_LIVE=1 uv run pytest tests/test_live_github.py` creates one issue in `owner/sandbox`, opens it with the mock terminal in a temporary launcher home (cloning the sandbox into a temporary `clone_root`), checks the IDs and the worktree, and closes the issue.
