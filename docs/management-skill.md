# Management skill and `configure`

Agent Launcher ships an agent skill, `agent-launcher`, that teaches an agent the config schema, profiles and agent instances, repository associations, workflow routing and templates, tasks, sessions, terminal adapters and diagnostics. Its hard rules: change configuration through the CLI or a validated edit and then run `config validate`; never write `state.db`; never bypass profile safety (no hand-edited associations, no `--keep-tasks` or `--archive-tasks` without the user's choice); never touch another agent's config directory. It describes only commands that exist; `tests/test_configure.py` checks that every `agent-launcher …` command it names is real.

## Install the skill

The skill is package data, so a pipx install already contains it. Print where:

```bash
agent-launcher skill path          # the directory holding SKILL.md (and reference/); --json for scripts
```

To install it where your agents look, use [`npx skills`](https://github.com/vercel-labs/skills) (Node is needed for that command only, never to run Agent Launcher):

```bash
npx skills add jbuffin/agent-launcher --skill agent-launcher -g          # asks which agents
npx skills add jbuffin/agent-launcher --skill agent-launcher -g -a claude-code -y
npx skills add ./path/to/checkout --list                                  # check what it finds
```

`-g` installs for you (`~/.claude/skills`, and `~/.agents/skills` for Codex and Copilot), without it into the current project. Checked against `skills` 1.7.1 with a temporary `HOME`: it finds the skill inside the package (`src/agent_launcher/skills/agent-launcher`) and installs it. It installs to the default directories and does not read `CLAUDE_CONFIG_DIR`; for a profile with its own config directory, copy the directory `skill path` prints into that directory's `skills/`.

`npx skills` can also install from a local checkout, as above.

## `configure`

```bash
agent-launcher configure ["what you want done"] [--repo PATH] [--profile NAME] [--agent A] [--terminal mock] [--json]
```

Creates a local maintenance task (`Configure Agent Launcher`) and launches it the way `open` does, with the built-in workflow `agent-launcher-configure`, whose skill is `agent-launcher`: for Claude Code the prompt is `/agent-launcher <your request>`, prepared for you to review (or submitted if `prompt_execution` is `execute`).

- **Repository.** `--repo`, or a launcher-owned git repository at `<home>/maintenance` (created on first use, with one empty commit so a worktree can be cut).
- **Profile.** The repository's associated profile. If it has none, you choose one (on a terminal) or pass `--profile`; nothing is inferred. A different `--profile` for a repository that already has one is refused (`configure_profile_mismatch`): use `profile set`.
- **Agent.** One of that profile's agents, chosen as for `open` (`--agent`, `agent_selection`).
- **Reuse.** Running it again focuses the same task's session. A different request for an existing task is refused (`configure_request_ignored`): tell the agent in its terminal, or `restart` the task.
- If the skill is not installed for that agent, the launch still happens with a notice naming where the launcher looked. Install it as above.

See [ADR 0016](adr/0016-configure-maintenance-task.md).

The gh-dash bootstrap is a specialised `configure`: see [gh-dash.md](gh-dash.md).
