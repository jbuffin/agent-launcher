# GitHub issues and pull requests

`agent-launcher open <issue-url>` runs Scenario A: from `https://github.com/<owner>/<repo>/issues/<N>` to an agent working in its own worktree.

```
agent-launcher open https://github.com/acme/widgets/issues/7 [--agent claude] [--terminal cmux] [--json] [--offline]
```

Only github.com issue and pull request URLs are accepted (`https://github.com/<owner>/<repo>/pull/<N>` is a pull request, see [Pull requests](#pull-requests)). Other hosts are `unsupported_url`.

## What happens

1. **Fetch** the issue and its repository with `gh api` (read-only). Failures are structured: `not_found`, `github_not_authenticated`, `github_forbidden`, `github_unreachable`, `gh_unavailable`, `github_error`.
2. **Find the task by GitHub's stable IDs** (issue node ID and database ID). If it exists, its stored title, state, labels and URL are refreshed and the task is opened as usual: a task that already has a session is focused (see [sessions.md](sessions.md)). Opening the same issue twice, or through a renamed repository's old or new URL, never makes a second task or session.
3. Otherwise **find the repository** (below), **ensure its profile association** (the stored one, or you choose once on a terminal; see [security.md](security.md)), create the task and continue as a normal `open`: worktree, agent choice, session.
4. The agent's **prompt is the issue URL**, exactly, unless a workflow rule matches: then it is that workflow's skill invocation of the URL, such as `/code-review <url>`. Routing, `--workflow` and `--ask-workflow` are in [workflows.md](workflows.md).

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

## Linking a local task

`agent-launcher tasks link <task> <github-url> [--json]` attaches an issue or pull request to a task created with `new`. One transaction, under the issue lock and then the task lock, inserts the `task_github` row and sets `tasks.source` to `github`. Nothing else changes: the task ID, title, worktree, sessions, agent conversation, profile and stored workflow stay, and no session is started. The local title is kept at link time; the next `open <url>` refreshes it to GitHub's. Afterwards `open <url>` finds this task (focuses its session if one is live, else starts the agent in the task's existing worktree), and `tasks show` and `workflows test <id>` show the GitHub identity. The stored workflow is not re-routed (see [workflows.md](workflows.md)). Linking again to the same item does nothing.

Refusals write nothing, and each has a stable code:

| Code | When |
| --- | --- |
| `github_repository_mismatch` | The item's repository is not the task's. Compared by GitHub ID, so a renamed repository still matches. If the task's repository was stored without an ID (for example a task made with `--offline`), `link` refuses and names `agent-launcher profile set <repo-path> <profile>`: run online with the repository's current profile, that command records the ID on the same repository and prints exactly what it recorded (`Recorded GitHub repository acme/widgets (ID 101) for <path>.`; `recorded_github_id` in `--json`). With a different profile and tasks present it refuses (`reassignment_requires_resolution`) and records nothing; with a resolution flag (`--archive-tasks` or `--keep-tasks`) it changes the profile and records the ID in the same step (see [ADR 0013](adr/0013-linking-local-tasks.md)); the ID is never recorded by anything else. |
| `github_profile_mismatch` | The repository's profile is not the task's profile; linking would cross profiles. |
| `github_identity_taken` | The item already belongs to another task. The message names it and offers choices (open that task, or keep this one local). Merging tasks is not supported. |
| `task_already_linked` | The task is linked to a different item. |

GitHub must be reachable: the item's IDs come from `gh`. See [ADR 0013](adr/0013-linking-local-tasks.md).

## Tests

`uv run pytest` uses a fake `gh` and real git against a local bare repository standing in for GitHub. `AGENT_LAUNCHER_LIVE=1 AGENT_LAUNCHER_SANDBOX_REPO=<owner>/<repo> uv run pytest tests/test_live_github.py` works in that throwaway repository (one you own; the tests open and close issues and PRs in it) with the mock terminal in a temporary launcher home (cloning the sandbox into a temporary `clone_root`):

- an issue: opened, IDs and worktree checked, then closed;
- a local task in a clone, linked to a new issue: `open <issue-url>` then focuses the same task with no second worktree or session; the issue is closed afterwards;
- an own PR (a branch with one commit, made through the API, and an open PR): the worktree is on the head branch tracking `origin/<head>`; the PR is closed and the branch deleted afterwards;
- the same kind of PR opened with the own-PR check forced off (a `GitHub` constructed with a fixed `viewer` who is not the author, a test seam): the worktree is the `review/pr-<N>` one.

The same variables on `uv run pytest tests/test_scenarios.py` add the live variants of scenarios A, D, E and G (a new issue with the clone, profile and agent questions; a PR whose head branch is already in a worktree, which is adopted; a labelled PR routed to a workflow skill; a local task linked to a new issue), all closed or deleted afterwards.

There is one GitHub account, so a real fork PR, or a PR by someone else, cannot be made live. Those paths are covered by the fakes in `tests/test_github_pulls.py` (a differing head repository ID, a deleted fork, a different author).

## Pull requests

`agent-launcher open https://github.com/<owner>/<repo>/pull/<N>` is the same pipeline as an issue (fetch, find the task by IDs, find the repository, ensure the profile association, create the task, launch). The agent's prompt is the PR URL. The PR is read with `gh api repos/o/n/pulls/N`; its body, diff and comments are not stored.

**Stored** in `task_github` (state migration 8; `tasks show --json` prints it under `github.pull`): the PR's node and database ID, `state` (`open`, `closed` or `merged`), author, and for pull requests the head repository (owner, name, ID, whether it is a fork), head ref and SHA, base ref, draft, review-requested and whether it is your own. Head SHA and state are refreshed each time you open it. A repository ID that differs from the base's, or a head repository that was deleted, counts as a fork.

**Your own PR.** "Own" means the PR's author is the account `gh` is logged in as (`gh api user`, asked once per run). If its head is in the same repository:

- the head branch is fetched from `origin` (a failed fetch is a notice, not an error) and checked out in the task worktree, tracking `origin/<head>`. A head branch that is not present locally is normal: it is created from `origin/<head>`. A local branch that exists but has no upstream gets `origin/<head>` as its upstream; a local branch that differs from `origin/<head>` is left as it is, with a notice;
- if the head branch is already checked out in another worktree, that worktree is offered for adoption (Scenario D): on a terminal you choose to adopt it or to create a review worktree instead; without one the command stops with `worktree_candidate_exists` and the `worktrees associate` command to run. It is never checked out a second time or forced. If the branch is the main checkout's (or another task's), a review worktree is used and a notice says why;
- if the head branch exists neither locally nor on `origin`, a review worktree is used.

**Someone else's PR, a fork, or an unknown viewer** (not logged in, or `gh` unreachable): an isolated review worktree on `review/pr-<N>`. `refs/pull/<N>/head` is fetched into `refs/agent-launcher/pr-<N>` (a ref only the launcher writes; GitHub keeps `refs/pull/N/head` for forks too) and the branch is cut from it with **no upstream**, so a `git push` there cannot go to the contributor's branch. The contributor's fork is never added as a remote, and nothing in the launcher pushes. If the head cannot be fetched and is not already local the error is `pr_head_unavailable`, and nothing is created. If `review/pr-<N>` already exists, `review/pr-<N>-2` and so on is used.

An existing worktree for the PR is discovered the same way as for any task (Scenario D); no worktree is created beside it without asking, and the association is stored.

**Limits.** Review-requested is true only when you are a directly requested reviewer, not through a team. An own PR from a fork of your own is checked out as a review worktree (the fork is not a remote of the checkout). Draft, state and the head are the values from when you last opened the task.
