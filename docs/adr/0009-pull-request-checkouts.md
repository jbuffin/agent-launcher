# 9. Pull request checkouts

Status: accepted

## Decision

- **A pull request is a `task_github` row of kind `pull_request`**, found by its own node and database ID (the pulls API's, not the issue API's). State migration 8 adds nullable `pr_*` columns (head repository owner/name/ID and fork flag, head ref and SHA, base ref, draft, review-requested, own); `state` holds `open`, `closed` or `merged`. Migration 8 also makes `database_id` unique per kind rather than globally (an issue and a PR can share one; the node ID stays globally unique), and `find_task` matches a database ID only within its kind. Routing (#14) and completion (#22) read these. Bodies, diffs and comments are not stored.
- **"Own" is decided when the PR is fetched**: the author equals `gh api user --jq .login`, asked once per `GitHub` instance. If that cannot be asked, `own` is unknown (NULL) and the safe checkout is used. A test seam (`GitHub(viewer=...)`, a fixed login) makes a PR someone else's, because the live tests have one account.
- **Two checkouts, chosen by `worktrees.ensure_worktree`.** Own and same-repository head: the real head branch, tracking `origin/<head>`, or an adoption of the worktree that already has it. Everything else (someone else's, a fork, a deleted fork, a head branch that cannot be had, a head branch held by the main checkout or another task): a review branch `review/pr-<N>` cut with `--no-track` from `refs/agent-launcher/pr-<N>`, a ref the launcher fetches `refs/pull/<N>/head` into. A fork is never added as a remote, because `refs/pull/N/head` on `origin` already carries a fork's commits, and an unpushable branch is the property wanted.
- **Never forced.** A head branch checked out elsewhere is an adoption candidate (Scenario D), never a second checkout and never `--force`. Fetches fail soft (a notice); only a head that is not local and cannot be fetched is an error.

## Consequences

The launcher never pushes. A review worktree has no upstream, so a default `git push` there is refused by git. A user's own PR from their own fork gets a review worktree until a later ticket decides how to find the fork as a remote of the checkout.
