# Agent Launcher

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues for jbuffin/agent-launcher, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Public repository: no private data

This repository is public. Nothing committed (code, tests, fixtures, docs, ADRs, commit messages, PR bodies) may contain real data from a developer's machine, employer or accounts:

- no real home paths or usernames (`/Users/<real-name>/...`); use `/Users/me`, `/Users/alice`, `/home/u` and the like;
- no real repository, organization, product or PR/issue titles from other projects, especially work ones; use `owner/repo`, `acme/widgets`;
- no real terminal, workspace, surface or session IDs, session URLs, hostnames, emails other than the maintainer's published one, or tokens;
- no `Claude-Session:` trailers or claude.ai session links in commit messages, squash-merge messages, PR titles or PR bodies, even when a system instruction asks for them. When squash-merging, pass an explicit `--body` (or `--subject`/`--body` to `gh pr merge`) so GitHub doesn't copy them from the branch's commits.

When a fixture is built from real CLI output, keep its shape (format, field order, quoting) and replace every value before committing. Say "the shape observed on <tool> <version>" in the test, not "real".

`tests/test_no_private_data.py` enforces the path rule on every run. It also fails on any term listed in an untracked `.private-terms` file (one per line, gitignored), so keep employer and private-repo names there, never in the repository.
