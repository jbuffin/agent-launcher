# Configuration migrations and `config edit`

## Versioned files

`config.json` (currently version 3) carries a `version`. When a release changes the schema, the version goes up and an ordered chain of Python functions (`vN -> vN+1`, in `migrations.py`) upgrades older files. Each step takes the raw JSON object and returns the new one; keys it does not mean to change, including unknown ones, stay as they are. `state.db` has its own migrations and is never touched here, so repository-to-profile associations are unaffected.

`config.json` has two migrations. A version 1 file chains through both in one run. [Upgrading from Agent Launcher](#upgrading-from-agent-launcher) lists what each one renames and drops.

## `config migrate`

```
commodore config migrate --dry-run [--json]
commodore config migrate [--yes] [--json]
```

- `--dry-run` lists each file's plan and a unified diff, and writes nothing (no backup either).
- A real run, per file: copy it to `<home>/backups/<name>.<UTC timestamp>.json` (never overwriting an earlier backup), write the migrated object atomically, re-read and validate the file on disk, and put the backup back byte for byte if validation fails.
- If the migrated object would not validate, nothing is written at all.
- A migration is **significant** when it changes any top-level key other than `version`. Significant migrations ask for confirmation; without a terminal (or with `--json`) they need `--yes`. A version-only migration (1 to 2, or 2 to 3 for a file with none of the renamed or dropped keys) just runs; any other 2 to 3 migration is significant and needs `--yes` without a terminal.
- Several files are migrated one after another, each with its own backup. If one fails (and is restored), the run stops with exit 1 and the files already migrated stay migrated; the output names them. Run it again after fixing the cause.
- A file that changes between the plan and the write (bytes differ) is not written: `config_changed`.
- If the backup cannot be made, nothing is written (`backup_failed`).
- A file whose version is newer than this release, missing, or not a whole number, or that is not a JSON object, is refused untouched with exit 1. A missing file is skipped. Up-to-date files are left alone and get no backup.

`commodore doctor` warns when `config.json` is at an older version and points here. Every command that loads the config (`open`, `profile`, `setup`, `config edit` and the rest) refuses an older file with the error code `config_outdated` and points here; nothing bumps an old file's version as a side effect ([ADR 0019](adr/0019-config-migrations.md)). `config show` and `config validate` still read it: `config validate` exits 0 with `warning: version: …` (`"outdated"` under `--json`), and `doctor` warns.

For a custom config that needs interpretation, `commodore configure` starts a harness-assisted maintenance session.

## `config edit`

Refuses a file older than the current version (`config_outdated`; run `config migrate` first). Opens `$VISUAL`, else `$EDITOR`, else `vi` (arguments allowed, e.g. `code --wait`) on a temporary copy. When the editor exits successfully the text must parse as a JSON object and pass the checks of `config validate`; unknown fields are only warned about. If it is invalid you can edit again or discard (without a terminal it is discarded and the command fails). A valid result replaces `config.json` atomically. The command also writes nothing when the text is unchanged, when the editor fails, or when `config.json` changed on disk while you were editing. A missing config starts from `{"version": 3}`; leaving it untouched creates nothing. Editor output that is not UTF-8 text is treated as invalid.

## Upgrading from Agent Launcher

Commodore Launcher is Agent Launcher renamed and cut down. This section lists what changed for people and scripts that used the old releases. The old names appear here, and only here, so a search for one lands on its replacement.

### The one-time copy

The product kept its files in `~/.agent-launcher/`. The first command that finds that directory and no `~/.commodore-launcher/` copies the config, `workflows.json`, the state database, `templates/` and `logs/` across and says so once on stderr ([ADR 0021](adr/0021-commodore-launcher.md)).

- It does not copy worktrees, locks, backups or the maintenance repository. It never copies again and never changes or removes the old directory.
- With `COMMODORE_LAUNCHER_HOME` set, nothing is copied. Set it to start with a fresh home.
- If `~/.agent-launcher` is a symlink to a directory, the link is followed once and the copy is made from its target; links inside it are skipped.
- If the copy fails (an unreadable file, say), the command prints `error:` with the file and the cause and exits 1 without creating `~/.commodore-launcher`. Fix the cause and run again.
- A copied `config.json` older than version 3 is migrated in memory inside the copy, so no `backups/` entry is made: the old home is the backup. If the migration cannot run or the result would not validate, the config is copied as it was and the line on stderr says to run `commodore config migrate`.
- Every copied task is archived, because its conversation belongs to a worktree Commodore does not use. `open <url>` of one unarchives it and starts a fresh session. A copied task with no GitHub URL stays listed and cannot be opened. The launcher-created worktrees still on disk are listed once and left alone; remove them yourself.

### Config: version 1 to 2 to 3

- **1 to 2** adds the optional `agent_types` and `profiles`, which changes only the number.
- **2 to 3** renames these keys. A profile or file that holds both spellings of one is an error: the plan reports it and nothing is written, because merging would guess which one you meant.

| Version 2 | Version 3 |
| --- | --- |
| `agent_types` | `harness_types` |
| `agent_selection` | `harness_selection` |
| `default_agent` (in a profile) | `default_harness` |
| `agents` (in a profile) | `harnesses` |

It also drops these keys, because the launcher no longer has the feature behind them:

| Dropped key | Why |
| --- | --- |
| `prompt_execution`, `workflow_routing`, `prompt_template` | Every URL task starts on Commodore's prompt; there are no workflows, templates or prepare/execute choice. |
| `prompt_mode`, `skill_invocation` (in a harness) | The prompt form is fixed per harness. |
| `worktree_root`, `base_branches` (in `repositories`) | The launcher creates no worktrees; the harness starts in the checkout. |

Every other key stays, unknown ones included. `config validate` on a version 3 file that still has an old key reports it as an unknown field, with the new name where there is one (`unknown field 'agents'; did you mean 'harnesses'?`).

### Scripts: JSON, error codes and doctor ids

Anything that reads `--json` output, error codes or `doctor` results by key has to change. The old names stop existing; there are no aliases.

| Was | Is |
| --- | --- |
| `agent` (JSON field) | `harness` |
| `agent_conversation_id` (in `sessions[]`) | `harness_conversation_id` (the database column keeps its name) |
| `agent:<name>` (doctor check id) | `harness:<name>` |
| `agent_selection_needed` | `harness_selection_needed` |
| `no_agents` | `no_harnesses` |
| `requested_agent` | `requested_harness` |
| `agent_mismatch` | `harness_mismatch` |
| `agent_not_in_profile` | `harness_not_in_profile` |
| `agent_unresolved` | `harness_unresolved` |
| `available_agents` (error detail) | `available_harnesses` |
| `task_not_found`, `unsupported_url` for input that is not a task or a GitHub URL | `needs_issue_or_pr_url` (`task_not_found` stays for task-ID-shaped input) |
| `skill_missing`, `skill_unsupported` | `skills_missing`, which refuses before anything starts |
| `restarted` (JSON action) | `created`: a fresh conversation in place is reported as a first launch |
| `workflows` (doctor check id) | `leftovers`, one warning when `workflows.json` or `templates/` is in the home |

These codes no longer exist because the feature behind them is gone: `workflow_missing`, `workflow_not_found`, `workflow_options_conflict`, `workflow_selection_needed`, `workflows_invalid`, `workflow_fixed`, `fallback_missing`, `template_invalid`, `template_missing`, `template_unreadable`, `template_unrendered`, `template_variable_unavailable`, `agent_required`, `base_branch_missing`, `base_branch_unknown`, `branch_conflict`, `cleanup_refused`, `cleanup_requires_list`, `close_refused`, `worktree_conflict`, `worktree_path_exists`, `worktree_unusable`, `worktree_taken`, `worktree_already_set`, `worktree_candidate_exists`, `pr_head_unavailable`, `not_a_candidate`, `task_already_linked`, `resume_unsupported`, `github_identity_taken`, `github_profile_mismatch` and `github_repository_mismatch`.

`open`, `resume` and `restart` JSON no longer carries `prompt_prepared` or `workflow`, and the `prompted` action is gone. `config validate --json` no longer reports workflow, fallback or template keys. `setup --answers` files take `harnesses`, `default_harness` and `harness_selection`; one with `workflow_selection` or `prompt_execution` fails with `invalid_answers`.

### Removed commands and options

Each gives a usage error, not a warning.

| Was | Now |
| --- | --- |
| `--agent`, `--default-agent`, `--remove-agent` | `--harness`, `--default-harness`, `--remove-harness` |
| `--execute`, `--prepare`, `--workflow`, `--ask-workflow` | none: the prompt is Commodore's, submitted at launch |
| `--prompt-mode`, `--skill-invocation` (profile options) | none |
| `commodore workflows`, `commodore prompt` | none |
| `commodore worktrees`, `commodore cleanup`, `tasks completed` | none: Commodore owns worktrees |
| `commodore new`, `tasks link` | none: `open` takes a GitHub issue or PR URL only |
| `commodore restart` | `open <task>` or `resume <task>` starts a fresh conversation in place when the harness cannot resume |
| `commodore sessions` (`candidates`, `adopt`) | none |

### Leftover files

`workflows.json` and `templates/` in your home are ignored. `doctor` warns once with a `leftovers` row; delete them. The copy brings them over from `~/.agent-launcher` too.
