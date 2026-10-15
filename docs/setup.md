# Setup wizard

```bash
commodore                 # with no config.json on a terminal, offers to run setup
commodore setup           # interactive wizard, any time
commodore setup --answers answers.json --yes   # unattended
commodore setup --answers answers.json --dry-run
```

Setup detects what it can (OS, Python, git, gh and its auth, cmux, gh-dash, Claude Code, Codex, Copilot, wrapper executables on PATH such as `claude-work`, and the *names* of `~/.claude*` and `~/.codex*` directories), shows it, and asks you to confirm each choice. Detection is a guide, never a decision: it never picks an identity for you. The tool probes are the same ones `doctor` uses.

## What it asks

1. A profile (at least one on a first run): name, which harnesses it uses, each harness's executable (the detected one, or a wrapper or path you give), the directory for `CLAUDE_CONFIG_DIR` / `CODEX_HOME` (a detected directory, a path you type, or **don't set**, which is the default), extra `NAME=VALUE` variables, and a default harness when there are several.
2. Terminal adapter, repository search roots (optional), harness selection.

Repositories are not configured here; they get a profile association when first encountered.

Before anything is written you see a unified diff of `config.json` and confirm it. The write is one atomic `update_config`, and a result that does not validate is refused.

## Re-running is safe

Setup only adds or updates. It never removes a key, profile, harness or search root, and keeps unknown fields. Existing profiles are left alone (use `profile edit`). With nothing to change it says so and does not touch the file. A config that cannot be loaded is not modified. A version 1 or 2 file is not bumped: when setup would write to it, it fails with `config_outdated` and asks you to run `commodore config migrate` first (nothing to change still writes nothing and succeeds).

Setup never reads, copies or writes anything inside a harness's own configuration directory (`~/.claude*`, `~/.codex*`); it only lists their names.

## Identity ambiguity

If two profiles use the same harness, each must set a different `CLAUDE_CONFIG_DIR` / `CODEX_HOME` (or `HOME`). Interactively, setup warns and asks whether to keep the profile; unattended it fails with `ambiguous_identity`.

## Unattended: the answers file

Nothing is detected or guessed. Every field is optional; omitted ones are left unchanged.

```json
{
  "profiles": [
    {
      "name": "personal",
      "harnesses": {
        "claude": {"executable": "claude", "env": {"CLAUDE_CONFIG_DIR": "~/.claude-personal"}}
      },
      "default_harness": "claude"
    }
  ],
  "terminal_adapter": "cmux",
  "search_roots": ["~/Projects"],
  "harness_selection": "always_ask"
}
```

Harness fields are those of [harnesses.md](harnesses.md) (`executable`, `args`, `env`, `working_directory`, `resume_args`). `search_roots` are added to existing ones. A profile with several harnesses needs `default_harness`; an executable (the default one if omitted) must exist.

Without a terminal, `--yes` is required to write (`--dry-run` shows the diff). With `--json`, failures print `{"error": {"code", "message", "field"}}` and exit 1. Codes: `invalid_answers`, `executable_not_found`, `ambiguous_default`, `ambiguous_identity`, `config_invalid`, `config_outdated`, `invalid_config`, `needs_confirmation`, `needs_input`. Ctrl-C in the wizard exits 130 with nothing written.

## Settings it stores

Defaults apply when a key is absent. `terminal.adapter`, `repositories.search_roots`, `repositories.clone_root`, `repositories.auto_clone`, `repositories.mappings` and `harness_selection` are used today (see [github-integration.md](github-integration.md) for the repository keys); the others are validated only.

| Key | Values | Default |
| --- | --- | --- |
| `terminal.adapter` | `cmux` (`mock` records launches instead of starting them; set it in `config.json` or with `--terminal mock`, the answers file takes only `cmux`) | `cmux` |
| `repositories.search_roots` | list of absolute or `~/` paths | `[]` |
| `repositories.clone_root` | absolute or `~/` path | unset (clones go to `~/Projects`) |
| `repositories.auto_clone` | `true`, `false` | `false` |
| `repositories.mappings` | map of `owner/name` to checkout path | `{}` |
| `harness_selection` | `always_ask`, `use_default`, `ask_if_multiple` | `always_ask` |

At the end of an interactive setup, if gh-dash is detected, you are offered a harness-assisted gh-dash integration (default No; never required). See [gh-dash.md](gh-dash.md).
