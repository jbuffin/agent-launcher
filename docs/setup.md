# Setup wizard

```bash
agent-launcher                 # with no config.json on a terminal, offers to run setup
agent-launcher setup           # interactive wizard, any time
agent-launcher setup --answers answers.json --yes   # unattended
agent-launcher setup --answers answers.json --dry-run
```

Setup detects what it can (OS, Python, git, gh and its auth, cmux, gh-dash, Claude Code, Codex, Copilot, wrapper executables on PATH such as `claude-work`, and the *names* of `~/.claude*` and `~/.codex*` directories), shows it, and asks you to confirm each choice. Detection is a guide, never a decision: it never picks an identity for you. The tool probes are the same ones `doctor` uses.

## What it asks

1. A profile (at least one on a first run): name, which agents it uses, each agent's executable (the detected one, or a wrapper or path you give), the directory for `CLAUDE_CONFIG_DIR` / `CODEX_HOME` (a detected directory, a path you type, or **don't set**, which is the default), extra `NAME=VALUE` variables, and a default agent when there are several.
2. Terminal adapter, repository search roots (optional), worktree root, workflow selection, prompt execution default, agent selection.

Repositories are not configured here; they get a profile association when first encountered.

Before anything is written you see a unified diff of `config.json` and confirm it. The write is one atomic `update_config`, and a result that does not validate is refused.

## Re-running is safe

Setup only adds or updates. It never removes a key, profile, agent or search root, and keeps unknown fields. Existing profiles are left alone (use `profile edit`). With nothing to change it says so and does not touch the file. A config that cannot be loaded is not modified. A version 1 file is bumped to 2 only when setup actually writes.

Setup never reads, copies or writes anything inside an agent's own configuration directory (`~/.claude*`, `~/.codex*`); it only lists their names.

## Identity ambiguity

If two profiles use the same agent, each must set a different `CLAUDE_CONFIG_DIR` / `CODEX_HOME` (or `HOME`). Interactively, setup warns and asks whether to keep the profile; unattended it fails with `ambiguous_identity`.

## Unattended: the answers file

Nothing is detected or guessed. Every field is optional; omitted ones are left unchanged.

```json
{
  "profiles": [
    {
      "name": "personal",
      "agents": {
        "claude": {"executable": "claude", "env": {"CLAUDE_CONFIG_DIR": "~/.claude-personal"}}
      },
      "default_agent": "claude"
    }
  ],
  "terminal_adapter": "cmux",
  "search_roots": ["~/Projects"],
  "worktree_root": "~/.agent-launcher/worktrees",
  "workflow_selection": "automatic",
  "prompt_execution": "prepare",
  "agent_selection": "always_ask"
}
```

Agent fields are those of [agents.md](agents.md) (`executable`, `args`, `env`, `working_directory`, `resume_args`, `prompt_mode`, `skill_invocation`). `search_roots` are added to existing ones. A profile with several agents needs `default_agent`; an executable (the default one if omitted) must exist.

Without a terminal, `--yes` is required to write (`--dry-run` shows the diff). With `--json`, failures print `{"error": {"code", "message", "field"}}` and exit 1. Codes: `invalid_answers`, `executable_not_found`, `ambiguous_default`, `ambiguous_identity`, `config_invalid`, `invalid_config`, `needs_confirmation`, `needs_input`. Ctrl-C in the wizard exits 130 with nothing written.

## Settings it stores

Defaults apply when a key is absent. `terminal.adapter`, `repositories.worktree_root`, `repositories.base_branches`, `repositories.search_roots`, `repositories.clone_root`, `repositories.auto_clone`, `repositories.mappings`, `prompt_execution` and `agent_selection` are used today (see [worktrees.md](worktrees.md) and [github-integration.md](github-integration.md) for the repository keys); the others are validated only.

| Key | Values | Default |
| --- | --- | --- |
| `terminal.adapter` | `cmux` | `cmux` |
| `repositories.search_roots` | list of absolute or `~/` paths | `[]` |
| `repositories.clone_root` | absolute or `~/` path | unset (clones go to `~/Projects`) |
| `repositories.auto_clone` | `true`, `false` | `false` |
| `repositories.mappings` | map of `owner/name` to checkout path | `{}` |
| `repositories.worktree_root` | absolute or `~/` path | `~/.agent-launcher/worktrees` |
| `repositories.base_branches` | map of repository path to branch name | `{}` (use `origin/HEAD`) |
| `workflow_routing.selection_mode` | `automatic`, `ask_on_multiple`, `always_ask` | `automatic` |
| `workflow_routing.require_verified_skills` | `true`, `false` | `false` (a skill not found by name is launched with a notice) |
| `workflow_routing.fallback` | a workflow id; `default` is built in (no skill) | `default` |
| `prompt_execution` | `prepare`, `execute` (a workflow's own `prompt_execution` and `open --execute/--prepare` override it) | `prepare` |
| `prompt_template` | a template name in `~/.agent-launcher/templates/`, used by workflows with neither a template nor a skill; or absent | none |
| `agent_selection` | `always_ask`, `use_default`, `ask_if_multiple` | `always_ask` |
