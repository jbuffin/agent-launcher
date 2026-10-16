# Management skill and `configure`

Commodore Launcher ships an agent skill, `commodore-launcher`, that teaches a harness the config schema, profiles and harnesses, repository associations, tasks, sessions and diagnostics. Its hard rules: change configuration through the CLI or a validated edit and then run `config validate`; never write `state.db`; never bypass profile safety (no hand-edited associations, no `--keep-tasks` or `--archive-tasks` without the user's choice); never touch another harness's config directory. It describes only commands that exist; `tests/test_configure.py` checks that every `commodore …` command it names is real.

## Install the skill

The skill is package data, so a pipx install already contains it. Print where:

```bash
commodore skill path          # the directory holding SKILL.md (and reference/); --json for scripts
```

To install it where your harnesses look, use [`npx skills`](https://github.com/vercel-labs/skills) (Node is needed for that command only, never to run Commodore Launcher):

```bash
npx skills add jbuffin/commodore-launcher --skill commodore-launcher -g          # asks which harnesses
npx skills add jbuffin/commodore-launcher --skill commodore-launcher -g -a claude-code -y
npx skills add ./path/to/checkout --list                                  # check what it finds
```

`-g` installs for you (`~/.claude/skills`, and `~/.agents/skills` for Codex and Copilot), without it into the current project. Checked against `skills` 1.7.1 with a temporary `HOME`: it finds the skill inside the package (`src/commodore_launcher/skills/commodore-launcher`) and installs it. It installs to the default directories and does not read `CLAUDE_CONFIG_DIR`; for a profile with its own config directory, create that directory's `skills/` if needed (`mkdir -p <dir>/skills`) and copy the directory `skill path` prints into it.

`npx skills` can also install from a local checkout, as above.

## `configure`

```bash
commodore configure ["what you want done"] [--repo PATH] [--profile NAME] [--harness A] [--dry-run [--json]]
```

Starts a maintenance session (stored as a hidden internal task, `Configure Commodore Launcher`: it isn't listed by `tasks list`, and `open`, `resume` and `tasks archive` refuse it) and runs its harness in this terminal the way `open` does, through the shared path (a harness started on a skill with an argument): the skill is `commodore-launcher`, and for Claude Code the prompt is `/commodore-launcher <your request>`, submitted at launch. The row is marked with `commodore-launcher-configure` in its `workflow` column, so `configure` finds it again.

- **Repository.** `--repo`, or a launcher-owned git repository at `<home>/maintenance` (created on first use, with one empty commit).
- **Profile.** The repository's associated profile. If it has none, you choose one (on a terminal) or pass `--profile`; nothing is inferred. A different `--profile` for a repository that already has one is refused (`configure_profile_mismatch`): use `profile set`.
- **Harness.** One of that profile's harnesses, chosen as for `open` (`--harness`, `harness_selection`).
- **Reuse.** Running it again resumes the same session (or says it is running in another terminal). A different request while one exists is refused (`configure_request_ignored`): tell the harness.
- **Dry run.** `--dry-run` shows the command it would run, records nothing and starts nothing.
- If the skill is not installed for that harness, the launch is refused (`skills_missing`) before anything is started; the message names where the launcher looked. Install it as above.

See [ADR 0016](adr/0016-configure-maintenance-task.md).
