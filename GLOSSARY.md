# Commodore Launcher

Commodore Launcher starts a harness on a GitHub issue or pull request with Commodore running on its URL, in the right repository and under the right profile, and finds that session again when the same URL is opened.

## Language

**Commodore**:
The engineer's skill that coordinates a team of agents. Given a URL, it works out the mission and runs it.
_Avoid_: the agent, the workflow

**Task**:
One GitHub issue or pull request the launcher has opened, identified by GitHub's stable IDs so renames and transfers keep it.
_Avoid_: job, item, ticket, mission

**Profile**:
A named identity (such as `work` or `personal`) that decides which harnesses a repository's tasks may use and with which configuration directories and environment.
_Avoid_: account, identity

**Harness**:
The agent CLI a session runs in: Claude Code, Codex CLI or GitHub Copilot CLI. A profile lists the harnesses it may run and how each is configured.
_Avoid_: agent, agent instance, CLI

**Repository association**:
The one profile the engineer assigned to a repository. The launcher never infers it.
_Avoid_: mapping, binding

**Session**:
The single harness conversation running Commodore for a task, which opening the same URL again focuses or resumes.
_Avoid_: run, workspace

### Commodore's own words

Commodore defines these terms. The launcher never chooses or names them for its own concepts.

**Mission**:
What a Commodore run is for: build, review or pr-feedback. Commodore works it out from the URL; the launcher does not.
_Avoid_: using this word for anything the launcher does

**Target**:
Where the work runs for its users (a browser, an API). A task's URL is not its target.
_Avoid_: calling the issue or PR the target
