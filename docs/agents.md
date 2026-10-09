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

`resume_args`, `prompt_mode` and `skill_invocation` are recorded and validated now. They have no effect until the launch tickets use them.

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
