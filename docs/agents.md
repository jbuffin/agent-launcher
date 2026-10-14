# Harnesses

Harness *types* and harnesses are separate in `config.json`.

- **Harness types** (`harness_types`) are global: what kinds of harness exist, and the adapter and default executable for each. Built in: `claude` (adapter `claude-code`, executable `claude`), `codex` (`codex-cli`, `codex`) and `copilot` (`github-copilot-cli`, `copilot`). Defining `harness_types` in the file replaces this built-in set.
- **Harnesses** (`profiles.<name>.harnesses.<type>`) belong to one profile. The key must be a harness type. Nothing is shared between profiles.

```json
{
  "version": 3,
  "profiles": {
    "work": {
      "default_harness": "claude",
      "harnesses": {
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

`resume_args` applies only to a harness that records a conversation ID (Claude Code, Copilot).

CLI flags map to these fields: `--executable`, `--arg` (repeat; replaces the list), `--env NAME=VALUE` (repeat; merged), `--unset-env`, `--cwd`, `--clear-cwd`, `--resume-arg`. Pass an argument that starts with `-` as `--arg --model --arg opus`.

## Identity variables are not cleared

An instance inherits the caller's environment. If an instance does not set `CLAUDE_CONFIG_DIR`, `GH_TOKEN` or any other variable that selects an identity, it uses whatever the launching shell has. Set every identity variable explicitly on every instance that matters; nothing removes inherited ones.

## Misspelt fields are errors

Inside `profiles.<name>` and `profiles.<name>.harnesses.<type>` an unknown key is a validation error, not a warning, and resolution refuses the profile. Otherwise a typo such as `environment` (the field is `env`) or `command` (the field is `executable`) would be ignored and the harness would run with the wrong identity. The error suggests the likely name. Unknown top-level keys are still only warnings.

## Wrappers and no shell

An instance is launched as an argument list: the resolved executable followed by `args`. No shell is involved, so quotes, `;`, `$` and globs in arguments are passed literally. To use a wrapper, point `executable` at a script file (with the executable bit and a shebang line) that does what you need, e.g.

```sh
#!/bin/sh
exec codex --some-flag "$@"
```

Shell aliases and functions cannot be launched directly; put the logic in a script instead.

## Resolution

Resolving a harness for a profile (`profile check` shows the result):

1. The profile must exist. With no harness requested, its `default_harness` is used; if none is recorded, resolution fails rather than picking one.
2. The harness must be configured on that profile. Another profile's instance is never used.
3. The environment is the caller's environment plus the instance's `env`. A bare executable name is looked up on the `PATH` from that environment, so `env.PATH` in an instance controls which executable is found.
4. The executable must exist, be a file and be executable. A `working_directory`, if set, must be an existing directory.

Any failure gives an error naming the profile, the harness and the problem, and stops. There is no fallback to another harness, profile or executable. Resolution only checks the file; it does not run the harness, so it cannot confirm that the tool starts or is logged in.

## Harness adapters: Claude Code, Codex CLI, GitHub Copilot CLI

A harness adapter (`agent_adapters.py`) holds what is specific to one harness's terminal UI: how to tell its input box is ready, how it takes a conversation ID, how a skill is named. The adapter is the type's `adapter` value. Everything else (routing, launch, sessions) is the same for all three.

Verified read-only against Codex CLI 0.162.0, GitHub Copilot CLI 1.0.94 and Claude Code 2.1.295 (`--help`, subcommand help, `copilot help environment|commands`, and the vendors' skills documentation). No prompt was sent to any of them.

| | Claude Code | Codex CLI | Copilot CLI |
| --- | --- | --- | --- |
| Positional prompt | submits | submits (`codex [PROMPT]`) | `-i <prompt>` submits |
| Fix conversation ID at launch | `--session-id <uuid>` | **no** | `--session-id <uuid>` |
| Resume | `--resume <id>` | `codex resume <id>` exists, but the ID only exists after launch; **not supported** | `--resume=<id>` |
| Skill in a prompt | `/skill arg` | `$skill arg` | `/skill arg` |
| Prompt at launch | `claude -- <prompt>` | `codex -- <prompt>` | `copilot --interactive=<prompt>` |
| Skills looked up by name in | `<CLAUDE_CONFIG_DIR>/skills`, `commands`, `<dir>/.claude/skills` | `<dir>/.agents/skills`, `~/.agents/skills`, `$CODEX_HOME/skills` | `<dir>/.github/skills`, `.agents/skills`, `.claude/skills`, `$COPILOT_HOME/skills` (default `~/.copilot/skills`), `~/.agents/skills` |
| Identity variable | `CLAUDE_CONFIG_DIR` | `CODEX_HOME` | `COPILOT_HOME` (see below) |

**The prompt is given at launch.** All three harnesses submit a prompt given on the command line, so the launcher starts the harness with the prompt (`claude -- <prompt>`, `codex -- <prompt>`, `copilot --interactive=<prompt>`) and the harness submits it once any dialog is answered ([ADR 20](adr/0020-execute-mode-prompt-at-launch.md)). Nothing is pasted and no screen is read. A harness type whose adapter is none of these three has no such form, and `open` refuses it (`harness: <name> cannot be given a prompt at launch`).

**The skill syntax is the documented one, not tried as a launch argument.** Codex: type `$` to mention a skill (<https://developers.openai.com/codex/skills>, which redirects to <https://learn.chatgpt.com/docs/build-skills>, checked against `codex-cli 0.162.1`, whose `--help` has `[PROMPT]` and no skill syntax). Copilot: name it as `/skill-name` in the prompt (<https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-skills>, checked against `GitHub Copilot CLI 1.0.97`, whose `--help` has `-i, --interactive <prompt>` and no skill syntax). Whether a skill invocation passed as the launch argument is honoured needs a live launch of each; that has not been done.

**Exited harnesses are not detected for Codex and Copilot.** Their adapters do not recognise an exit screen (`has_exited` is always false, the safe default), so `open` on a task whose harness quit focuses the old workspace rather than resuming. Use `resume --force` to start the stored conversation (Copilot) in a new workspace.

**Codex has no resume.** Its conversation ID is generated by Codex and appears only in its session files afterwards. The launcher does not scrape it ([ADR 5](adr/0005-one-session-per-task-conversation-id-at-launch.md)), so a Codex task stores no conversation ID, `open` on an exited harness starts a fresh conversation on the same session.

**Skills** are looked up by directory name only (`<name>/SKILL.md`). Absence is never reported as `missing`: bundled, plugin and admin skills (`/etc/codex/skills`) are not found this way. No launch checks for the skill until #18 (`AgentAdapter.check_skill` is kept for it). Names that are not a plain directory name (`plugin:skill`, `../x`) are not looked up. `$CODEX_HOME/skills` is checked although the current Codex documentation lists only `.agents/skills` locations; it costs nothing and covers older setups.

**Profile separation.** Codex reads its configuration and login from `CODEX_HOME`; set it per profile, as the setup wizard already requires. Copilot reads configuration and state from `COPILOT_HOME` (default `~/.copilot`), so set that per profile too. Its login may also come from `COPILOT_GITHUB_TOKEN`, `GH_TOKEN` or `GITHUB_TOKEN`, or from stored credentials; whether `COPILOT_HOME` alone separates stored logins was not verified (it was not run logged in). The setup wizard checks `COPILOT_HOME` like `CODEX_HOME` (two profiles may not share it), and nothing removes inherited variables (see above). When two profiles use Copilot, set `COPILOT_HOME` and, if you use a token, the token variable explicitly on each instance.

**Resume confirmation is unobserved.** The resume confirmation patterns are empty for Copilot, so a resume reports "attempted, not confirmed".
