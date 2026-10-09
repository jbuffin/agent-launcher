# Agents

Agent *types* and agent *instances* are separate in `config.json`.

- **Agent types** (`agent_types`) are global: what kinds of agent exist, and the adapter and default executable for each. Built in: `claude` (adapter `claude-code`, executable `claude`), `codex` (`codex-cli`, `codex`) and `copilot` (`github-copilot-cli`, `copilot`). Defining `agent_types` in the file replaces this built-in set.
- **Agent instances** (`profiles.<name>.agents.<type>`) belong to one profile. The key must be an agent type. Nothing is shared between profiles.

```json
{
  "version": 2,
  "profiles": {
    "work": {
      "default_agent": "claude",
      "agents": {
        "claude": {
          "env": { "CLAUDE_CONFIG_DIR": "~/.claude-work" }
        },
        "codex": {
          "executable": "/Users/me/bin/codex-work",
          "args": ["--profile", "work"],
          "working_directory": "/Users/me/work"
        }
      }
    }
  }
}
```

## Instance fields

| Field | Default | Meaning |
| --- | --- | --- |
| `executable` | the type's executable | An absolute path, or a bare command name looked up on the instance's `PATH`. Relative paths such as `./x` are rejected. |
| `args` | `[]` | Arguments, as a list. Each element is one argument. |
| `env` | `{}` | Variables added to the inherited environment. A leading `~` in a value (and in each `PATH` entry) is expanded, using the instance's own `HOME` if it sets one. This happens before the executable lookup. |
| `working_directory` | none | Absolute path of an existing directory. |
| `resume_args` | `[]` | Arguments used to resume a session. |
| `prompt_mode` | `argument` | `argument`, `stdin` or `interactive`. |
| `skill_invocation` | `slash` | `slash`, `prompt` or `none`. |

`resume_args` applies only to an agent that records a conversation ID (Claude Code, Copilot). `prompt_mode` is recorded and validated but **unused**: the launcher always pastes the prompt into the input box, because every supported agent submits a prompt given as an argument. `skill_invocation` is recorded; the adapter decides the syntax.

CLI flags map to these fields: `--executable`, `--arg` (repeat; replaces the list), `--env NAME=VALUE` (repeat; merged), `--unset-env`, `--cwd`, `--clear-cwd`, `--resume-arg`, `--prompt-mode`, `--skill-invocation`. Pass an argument that starts with `-` as `--arg --model --arg opus`.

## Identity variables are not cleared

An instance inherits the caller's environment. If an instance does not set `CLAUDE_CONFIG_DIR`, `GH_TOKEN` or any other variable that selects an identity, it uses whatever the launching shell has. Set every identity variable explicitly on every instance that matters; nothing removes inherited ones.

## Misspelt fields are errors

Inside `profiles.<name>` and `profiles.<name>.agents.<type>` an unknown key is a validation error, not a warning, and resolution refuses the profile. Otherwise a typo such as `environment` (the field is `env`) or `command` (the field is `executable`) would be ignored and the agent would run with the wrong identity. The error suggests the likely name. Unknown top-level keys are still only warnings.

## Wrappers and no shell

An instance is launched as an argument list: the resolved executable followed by `args`. No shell is involved, so quotes, `;`, `$` and globs in arguments are passed literally. To use a wrapper, point `executable` at a script file (with the executable bit and a shebang line) that does what you need, e.g.

```sh
#!/bin/sh
exec codex --some-flag "$@"
```

Shell aliases and functions cannot be launched directly; put the logic in a script instead.

## Resolution

Resolving an agent for a profile (`profile check` shows the result):

1. The profile must exist. With no agent requested, its `default_agent` is used; if none is recorded, resolution fails rather than picking one.
2. The agent must be configured on that profile. Another profile's instance is never used.
3. The environment is the caller's environment plus the instance's `env`. A bare executable name is looked up on the `PATH` from that environment, so `env.PATH` in an instance controls which executable is found.
4. The executable must exist, be a file and be executable. A `working_directory`, if set, must be an existing directory.

Any failure gives an error naming the profile, the agent and the problem, and stops. There is no fallback to another agent, profile or executable. Resolution only checks the file; it does not run the agent, so it cannot confirm that the tool starts or is logged in.

## Agent adapters: Claude Code, Codex CLI, GitHub Copilot CLI

An agent adapter (`agent_adapters.py`) holds what is specific to one agent's terminal UI: how to tell its input box is ready, how it takes a conversation ID, how a skill is named. The adapter is the type's `adapter` value. Everything else (routing, launch, sessions) is the same for all three.

Verified read-only against Codex CLI 0.162.0, GitHub Copilot CLI 1.0.94 and Claude Code 2.1.295 (`--help`, subcommand help, `copilot help environment|commands`, and the vendors' skills documentation). No prompt was sent to any of them.

| | Claude Code | Codex CLI | Copilot CLI |
| --- | --- | --- | --- |
| Positional prompt | submits | submits (`codex [PROMPT]`) | `-i <prompt>` submits |
| Prepare (not submit) | paste into idle box | paste into idle box | paste into idle box |
| Fix conversation ID at launch | `--session-id <uuid>` | **no** | `--session-id <uuid>` |
| Resume | `--resume <id>` | `codex resume <id>` exists, but the ID only exists after launch; **not supported** | `--resume=<id>` |
| Skill in a prompt | `/skill arg` | `$skill arg` | `/skill arg` |
| Skills looked up by name in | `<CLAUDE_CONFIG_DIR>/skills`, `commands`, `<dir>/.claude/skills` | `<dir>/.agents/skills`, `~/.agents/skills`, `$CODEX_HOME/skills` | `<dir>/.github/skills`, `.agents/skills`, `.claude/skills`, `$COPILOT_HOME/skills` (default `~/.copilot/skills`), `~/.agents/skills` |
| Identity variable | `CLAUDE_CONFIG_DIR` | `CODEX_HOME` | `COPILOT_HOME` (see below) |

**Prepare never presses Enter.** All three agents submit a prompt given on the command line, so the launcher starts the agent bare, waits for its input box and pastes (bracketed paste) as in [ADR 4](adr/0004-prompt-by-bracketed-paste.md). Execute mode adds one Enter key after the same checks.

**Exited agents are not detected for Codex and Copilot.** Their adapters do not recognise an exit screen (`has_exited` is always false, the safe default), so `open` on a task whose agent quit focuses the old workspace rather than resuming. Use `resume --force` to start the stored conversation (Copilot) in a new workspace.

**Codex has no resume.** Its conversation ID is generated by Codex and appears only in its session files afterwards. The launcher does not scrape it ([ADR 5](adr/0005-one-session-per-task-conversation-id-at-launch.md)), so a Codex task stores no conversation ID, `open` on an exited agent says it cannot resume, and `restart` is the way forward.

**Skills** are looked up by directory name only (`<name>/SKILL.md`). Absence is never reported as `missing`: bundled, plugin and admin skills (`/etc/codex/skills`) are not found this way, so a skill not found is launched with a notice, as for Claude Code ([ADR 10](adr/0010-workflow-routing.md)). Names that are not a plain directory name (`plugin:skill`, `../x`) are not looked up. `$CODEX_HOME/skills` is checked although the current Codex documentation lists only `.agents/skills` locations; it costs nothing and covers older setups.

**Profile separation.** Codex reads its configuration and login from `CODEX_HOME`; set it per profile, as the setup wizard already requires. Copilot reads configuration and state from `COPILOT_HOME` (default `~/.copilot`), so set that per profile too. Its login may also come from `COPILOT_GITHUB_TOKEN`, `GH_TOKEN` or `GITHUB_TOKEN`, or from stored credentials; whether `COPILOT_HOME` alone separates stored logins was not verified (it was not run logged in). The setup wizard checks `COPILOT_HOME` like `CODEX_HOME` (two profiles may not share it), and nothing removes inherited variables (see above). When two profiles use Copilot, set `COPILOT_HOME` and, if you use a token, the token variable explicitly on each instance.

**Screen patterns are unobserved.** The ready, blocked and busy patterns for Codex and Copilot were written from the tools' documented UI text, not from a live terminal (cmux was not reachable). They are deliberately narrow: if they do not match, nothing is pasted and the prompt goes to the clipboard with a notice. Resume confirmation patterns are empty for Copilot, so a resume reports "attempted, not confirmed". In particular, Copilot's `blocked` list includes `/login`; if its idle screen mentions `/login`, prepare will always fall back to the clipboard, and the pattern needs narrowing. The live checks to run are listed in the ticket #16 report.
