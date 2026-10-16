# 21. Commodore Launcher: every task runs Commodore, in the repository's checkout

Status: accepted (supersedes ADRs 0004, 0006, 0009, 0010, 0011, 0013 and 0015, and the completion and cleanup parts of 0018); focusing the session is superseded by ADR 0022

## Context

The launcher prepared a prompt for a general agent: workflow rules picked a skill, templates shaped the prompt, prepare mode pasted it for review, and each task got its own worktree. In practice every task is handed to Commodore, a skill that coordinates a team of agents. Commodore already works out what a URL is for (its mission), confirms it with the engineer, and creates one worktree per unit of work, refusing to start a teammate in the checkout it was given. The launcher's routing, prompt preparation and worktrees did the same jobs a second time, or went unused.

## Decision

- The tool is renamed Commodore Launcher (command `commodore`, home `~/.commodore-launcher`). Its one job is to start a harness on a GitHub issue or pull request URL with Commodore invoked on that URL, submitted at launch (ADR 0020's mechanism). There are no workflows, templates or prepare mode.
- The harness starts in the repository's own checkout. The launcher creates, adopts and cleans up no worktrees; Commodore owns them.
- Input is a GitHub issue or PR URL only. Local tasks and linking them are gone.
- The launcher knows nothing about Commodore's run: Commodore picks the mission and the run name. So there is no `restart`; `resume` continues the conversation, and Commodore continues from its own state.
- Kept: repository lookup and cloning, profiles and repository associations, task identity by GitHub's stable IDs (ADR 0008), one session per task (ADR 0005), focusing it on reopen, archiving.
- "Harness" replaces "agent" in the CLI, the docs and the config keys.

## Considered options

- **A new, thin tool.** Rejected: profiles, repository associations and reopen-focuses-the-session are the parts worth keeping, and they are already built and tested.
- **Keep a launcher worktree per task as the coordinator's home.** Rejected: Commodore never builds in it, so it would stay empty, and every new worktree is a new folder that brings back the harness's folder-trust dialog.
- **The launcher names Commodore's run**, so `restart` could resume the same run in a fresh conversation. Rejected for now: it couples the launcher to Commodore's run layout and needs a change to Commodore.

## Consequences

Several tasks in one repository share its checkout. Commodore writes only to its own worktrees and run directory, so the launcher does not guard the checkout's branch or state; a pr-feedback run stops on its own if the PR's branch is checked out there. `doctor` and `open` check that Commodore and the skills it needs are installed for the profile's harness, because a profile with its own configuration directory does not get skills installed by `npx skills`.
