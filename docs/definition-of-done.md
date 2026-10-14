# Definition of Done

The acceptance criteria of the Commodore Launcher rewrite ([#12](https://github.com/jbuffin/agent-launcher/issues/12), sub-issues #13 to #20, [ADR 0021](adr/0021-commodore-launcher.md)), plus the safety and release items that stayed. Each line says how the criterion is met and where the evidence is. Evidence is a test (written `tests/file.py::test_name`; `tests/test_docs.py` fails if a name here stops existing) or a command you can run. Where a criterion needs a person at a cmux terminal with the real harnesses, the gap is stated and points at [live-validation.md](live-validation.md). Nothing here is claimed on the strength of a fake alone without saying so.

Status at the release-readiness check: **automated evidence on fakes (real git, the mock terminal, a fake `gh`), with the live gaps listed** per criterion below. The live gaps need the real cmux and the harnesses' real screens and only the engineer can close them. No criterion is known to fail, and none is proved on real cmux yet.

Run all of the automated evidence: `uv run pytest -q` (no cmux, no network). With `COMMODORE_LAUNCHER_LIVE=1` it also runs the sandbox-repository tests; with `COMMODORE_LAUNCHER_PACKAGING=1` the pipx install and upgrade test.

## The ten acceptance scenarios

`tests/test_scenarios.py` runs each scenario through the CLI with a temporary launcher home, real Git, the mock terminal and a fake `gh`. A, D and E also run against the sandbox repository.

| Scenario | Test | Live variant |
| --- | --- | --- |
| A. First launch from an unknown repository | `tests/test_scenarios.py::test_scenario_a_first_launch_from_an_unknown_repository` | `test_live_scenario_a_first_launch_from_an_unknown_issue` |
| B. Reopen an existing issue | `tests/test_scenarios.py::test_scenario_b_reopening_an_existing_issue` | |
| C. Work/personal isolation | `tests/test_scenarios.py::test_scenario_c_work_personal_isolation` | |
| D. Existing worktree (removed: the launcher creates no worktrees; the harness starts in the checkout) | `tests/test_repository_checkout.py::test_the_session_starts_in_the_repositorys_checkout` | |
| E. A review request gets the same prompt (workflows are removed) | `tests/test_scenarios.py::test_scenario_e_a_review_request_gets_the_same_prompt` | `test_live_scenario_e_a_real_pr_gets_the_commodore_invocation` |
| F. Free-text input (removed as a feature: `open` takes a GitHub issue or PR URL only, anything else is refused) | `tests/test_url_only.py::test_open_of_anything_but_a_github_url_or_a_task_id_needs_an_issue_or_pr_url_and_creates_nothing` | |
| G. Removed commands (nothing but `open <url>` starts a session) | `tests/test_url_only.py::test_removed_commands_are_unknown` | |
| H. Concurrent launch | `tests/test_scenarios.py::test_scenario_h_concurrent_launch` | |
| I. Partial launch failure | `tests/test_scenarios.py::test_scenario_i_partial_launch_failure` | |
| J. gh-dash bootstrap | `tests/test_scenarios.py::test_scenario_j_gh_dash_bootstrap` | |

Gap: all ten use the mock terminal, so "cmux workspace created" and "harness launched" are proved against the adapter contract, not a real cmux. Repeat them by hand with [live-validation.md §10](live-validation.md#10-the-ten-scenarios-on-the-real-tools-an-hour-or-two). The one real-cmux check that exists is `tests/test_live_cmux.py` (items 2 to 5 there).

## Safety-critical tests

| # | Must hold | Evidence |
| --- | --- | --- |
| 1 | Unknown repositories never receive an automatic profile assignment | `tests/test_associations.py::test_unknown_repository_has_no_profile_and_nothing_is_assigned`, `tests/test_association_safety.py::test_a_configured_default_never_assigns_or_changes_a_profile`, `tests/test_associations.py::test_suggestion_is_reported_but_never_assigned` |
| 2 | A work task never falls back to a personal harness | `tests/test_profiles.py::test_safety_no_cross_profile_fallback`, `tests/test_scenarios.py::test_scenario_c_work_personal_isolation` |
| 3 | An unavailable harness causes a safe failure | `tests/test_profiles.py::test_safety_unavailable_agent_fails_safely_without_running_anything` |
| 4 | Profile associations survive restarts | `tests/test_associations.py::test_association_survives_a_restart`, `tests/test_scenarios.py::test_scenario_a_first_launch_from_an_unknown_repository` (read by a second process) |
| 5 | ... and configuration migration | `tests/test_migrations.py::test_repository_associations_survive_migration` |
| 6 | Existing task launches create no duplicate sessions | `tests/test_session_lifecycle.py::test_second_open_focuses_and_creates_nothing`, `tests/test_session_lifecycle.py::test_a_second_session_for_a_task_is_refused_by_the_registry`, `tests/test_scenarios.py::test_scenario_b_reopening_an_existing_issue` |
| 7 | The launcher never deletes or changes a worktree or checkout (it runs no `git worktree`) | `tests/test_repository_checkout.py::test_open_runs_no_git_worktree_and_no_git_write` |
| 8 | The checkout's branch and working tree are left as they were | `tests/test_repository_checkout.py::test_the_checkouts_branch_and_working_tree_are_unchanged_after_open_of_a_pr` |
| 9 | Failed launches remain recoverable | `tests/test_launch_transactions.py::test_a_terminal_failure_is_retried_by_open_with_no_duplicates`, `tests/test_repository_checkout.py::test_a_failed_launch_is_retried_by_open_with_no_duplicates`, `tests/test_scenarios.py::test_scenario_i_partial_launch_failure` |
| 10 | Concurrent requests for one task produce one primary session | `tests/test_launch_transactions.py::test_concurrent_opens_of_one_task_make_one_session`, `tests/test_scenarios.py::test_scenario_h_concurrent_launch` |

Also covered: terminal abstraction (`tests/test_tasks_flow.py::test_core_has_no_cmux_types`, and the whole suite running on the mock terminal with no cmux installed); every unit-test area has its own module under `tests/` (configuration, migrations, profiles, harness resolution, prompts, repository discovery, task identity, session transitions, concurrency, failure recovery, archiving).

## Acceptance criteria, by sub-issue

Each id is a row of the acceptance trace: the sub-issue number, then the criterion's number.

### 13. The rename and the one-time copy

- **13.1 `commodore version` works after `pipx install`, and the old command name is gone.** `tests/test_commodore_rename.py::test_distribution_and_console_script_in_pyproject`, `tests/test_commodore_rename.py::test_installed_metadata_has_one_console_script_named_commodore`, `tests/test_commodore_rename.py::test_version_prints_the_new_command_name`; the wheel install: `tests/test_packaging.py::test_pipx_install_from_wheel_and_upgrade_keeps_config_and_state` (opt-in, `COMMODORE_LAUNCHER_PACKAGING=1`). Command: `T=$(mktemp -d) && uv build -q --out-dir $T/dist && PIPX_HOME=$T/home PIPX_BIN_DIR=$T/bin PIPX_MAN_DIR=$T/man pipx install $T/dist/*.whl && $T/bin/commodore version`. Gap: installed on one macOS machine; Python 3.11 to 3.14 are declared, not all run.
- **13.2 With only `~/.agent-launcher` present, the first command copies it once, prints one line, and the old directory is byte for byte unchanged.** `tests/test_first_run_copy.py::test_first_command_copies_the_five_items_and_says_so_once_on_stderr`, `tests/test_first_run_copy.py::test_second_command_copies_nothing_and_prints_nothing_about_it`, `tests/test_first_run_copy.py::test_the_old_home_is_byte_for_byte_unchanged_including_its_wal`, `tests/test_first_run_copy.py::test_with_the_home_variable_set_no_copy_is_made`.
- **13.3 Profiles and repository associations from the old home are in effect after the copy.** `tests/test_first_run_copy.py::test_profiles_and_repository_associations_from_the_old_home_are_in_effect`.
- **13.4 `tests/test_no_private_data.py` and the full suite pass.** `uv run pytest -q`.

### 14. Harness, config version 3

- **14.1 `open <url> --harness codex` skips the picker; the old option is an unknown option.** `tests/test_harness.py::test_open_a_github_url_with_harness_skips_the_picker`, `tests/test_harness.py::test_the_old_agent_option_is_an_unknown_option_with_no_alias`.
- **14.2 A version 2 config migrates to version 3 with the keys renamed and the dropped keys removed, through `config migrate` and through the first-run copy.** `tests/test_harness.py::test_config_migrate_writes_a_backup_and_a_valid_v3_file`, `tests/test_harness.py::test_the_copy_of_a_v2_home_is_migrated_to_v3_and_the_old_home_is_unchanged`, `tests/test_harness.py::test_a_v1_file_chains_through_to_3`.
- **14.3 `config validate` on a version 3 file with a profile key from version 2 reports it as unknown, with a suggestion.** `tests/test_harness.py::test_config_validate_names_harnesses_for_a_profile_agents_key`.
- **14.4 No user-facing string uses the old word for the harness.** `tests/test_harness.py::test_no_command_help_says_agent_for_the_harness`, `tests/test_harness.py::test_doctor_check_ids_names_and_messages_say_harness`, `tests/test_harness.py::test_no_source_string_says_agent_for_the_harness`, `tests/test_harness.py::test_the_bundled_management_skill_says_harness`; the README and docs: `tests/test_docs_content.py::test_no_current_doc_says_agent_for_the_harness`.

### 15. The Commodore prompt, submitted at launch

- **15.1 `open <issue-url> --terminal mock` records a prompt argument that is exactly the harness's Commodore invocation of the URL, for each of the three harnesses.** `tests/test_commodore_prompt.py::test_an_issue_is_launched_with_the_harness_invocation_of_commodore_on_its_url`, `tests/test_commodore_prompt.py::test_a_pull_request_is_launched_the_same_way`, `tests/test_scenarios.py::test_scenario_e_a_review_request_gets_the_same_prompt`. `open` starts on Commodore; `configure` and `integrate gh-dash` start `commodore-launcher` on their request text, through the same path (rows 19.1 and 19.2). Gap: whether Codex and Copilot honour a skill invocation passed as the launch argument needs a live launch; the real check is `tests/test_live_cmux.py::test_a_new_session_submits_the_skill_invocation_at_launch` ([§5](live-validation.md#5-the-skill-invocation-at-launch-3-minutes)).
- **15.2 Nothing is pasted and no screen check runs for a new session.** `tests/test_terminal_cmux.py::test_execute_at_launch_hands_the_prompt_to_the_agent_without_typing_it`, `tests/test_commodore_prompt.py::test_a_new_session_records_one_create_and_no_prompt_call_of_any_other_kind`, `tests/test_tasks_flow.py::test_the_prompt_is_submitted_at_launch_on_the_command_line`.
- **15.3 The removed commands and flags are gone from `--help`.** `tests/test_commodore_prompt.py::test_the_removed_commands_are_not_in_help`, `tests/test_commodore_prompt.py::test_the_removed_options_are_not_in_help`.
- **15.4 Issue titles and bodies never reach the command or the prompt.** `tests/test_commodore_prompt.py::test_an_issue_title_and_body_never_reach_the_launch`, `tests/test_commodore_prompt.py::test_a_pull_request_title_and_body_never_reach_the_launch`.

### 16. No worktrees

- **16.1 The harness starts in the repository's checkout, for an issue, your own PR and someone else's PR.** `tests/test_repository_checkout.py::test_the_session_starts_in_the_repositorys_checkout`.
- **16.2 `open` runs no `git worktree` command.** `tests/test_repository_checkout.py::test_open_runs_no_git_worktree_and_no_git_write`, `tests/test_repository_checkout.py::test_open_creates_no_worktree_anywhere`.
- **16.3 Two tasks in one repository start in the same checkout.** `tests/test_repository_checkout.py::test_two_tasks_in_one_repository_start_in_the_same_checkout`.
- **16.4 The removed commands are gone from `--help`.** `tests/test_repository_checkout.py::test_the_removed_commands_are_not_registered`.
- Also: the checkout's branch and working tree are left as they were (`tests/test_repository_checkout.py::test_the_checkouts_branch_and_working_tree_are_unchanged_after_open_of_a_pr`); the first-run copy archives every copied task and lists old worktrees once (`tests/test_first_run_copy_worktrees.py::test_every_copied_task_is_archived_and_the_created_worktree_is_listed_once`).

### 17. URL only, a fresh conversation in place

- **17.1 `open <non-github-text>` fails saying it needs an issue or PR URL, and creates nothing.** `tests/test_url_only.py::test_open_of_anything_but_a_github_url_or_a_task_id_needs_an_issue_or_pr_url_and_creates_nothing`. Tasks copied from the old launcher stay listed and archived; one with no GitHub URL cannot be opened: `tests/test_url_only.py::test_copied_local_tasks_stay_archived_and_listed`, `tests/test_url_only.py::test_an_unarchived_local_task_is_still_refused_with_task_needs_url`.
- **17.2 Opening the same issue URL twice focuses the first session.** `tests/test_url_only.py::test_an_issue_url_twice_launches_once`, `tests/test_url_only.py::test_a_pr_url_twice_launches_once`, `tests/test_scenarios.py::test_scenario_b_reopening_an_existing_issue`.
- **17.3 `resume` works, and the commands that created or adopted other sessions are gone from `--help`.** `tests/test_url_only.py::test_removed_commands_are_unknown`, `tests/test_url_only.py::test_session_adoption_and_restart_are_removed_from_the_code`. A harness that cannot resume starts a fresh conversation on the same session: `tests/test_url_only.py::test_codex_after_exit_has_no_resume_so_it_starts_fresh_in_place`.

### 18. The skill check

- **18.1 `doctor` passes when Commodore's four skills are present for each profile's harness and fails naming the missing ones.** `tests/test_skill_check.py::test_doctor_fails_naming_the_missing_skills_and_an_install_hint`.
- **18.2 `open` with a missing skill exits non-zero, prints the hint, and leaves no new task or launch.** `tests/test_skill_check.py::test_open_of_a_new_url_with_a_missing_skill_refuses_and_creates_no_task_launch_or_terminal`.
- **18.3 A profile with its own `CLAUDE_CONFIG_DIR` is checked against that directory.** `tests/test_skill_check.py::test_open_checks_the_profiles_custom_claude_config_dir`. Gap: the places each harness loads skills from come from its documentation and `--help`, not from a live launch of each.

### 19. `configure` and `integrate gh-dash`

- **19.1 `configure "add a search root" --terminal mock` launches the management skill with the request.** `tests/test_u7_configure_integrate.py::test_guard_configure_prompt_is_the_harness_invocation_of_the_skill_with_the_request`, `tests/test_configure.py::test_skill_is_in_the_built_wheel`, `tests/test_configure.py::test_skill_frontmatter_is_valid_and_references_resolve`.
- **19.2 The prompt `integrate gh-dash --print-prompt` produces names the `commodore` command and no command under the old product name.** `tests/test_u7_configure_integrate.py::test_guard_print_prompt_names_commodore_open_for_issues_and_prs_and_no_agent_launcher`, `tests/test_integrate.py::test_commodore_launcher_never_writes_gh_dash_configuration`, `tests/test_scenarios.py::test_scenario_j_gh_dash_bootstrap`. Gap: the keybinding in a running gh-dash is checked against gh-dash's source and a fake cmux only ([§8](live-validation.md#8-gh-dash-keybinding-and-the-picker-surface-10-minutes)).

### 20. README and docs

- **20.1 No document describes a removed concept, except the upgrade notes and ADRs.** `tests/test_docs_content.py` (the concept scan, the old-word scan, the SPEC checks and the wording checks).
- **20.2 Every relative link in the README and `docs/` resolves.** `tests/test_docs.py::test_relative_links_resolve`.
- **20.3 `tests/test_no_private_data.py` passes.** `uv run pytest -q tests/test_no_private_data.py`.

## Safety and release items that stayed

- **Setup works.** `tests/test_wizard.py::test_answers_file_writes_config_and_is_idempotent`, `tests/test_setup_cli.py::test_setup_with_answers_and_yes`, `tests/test_wizard.py::test_rerun_never_removes_existing_config`. Gap: the interactive wizard runs on a scripted prompter; try `commodore setup --dry-run` in a terminal.
- **Profiles, harnesses and associations.** `tests/test_profiles.py::test_cli_add_list_edit_check`, `tests/test_profiles.py::test_wrapper_script_resolved_as_argv`, `tests/test_associations.py::test_association_survives_a_restart`, `tests/test_associations.py::test_rename_keeps_the_association_by_github_id`. Separation is by configuration, not an OS sandbox ([security.md](security.md)).
- **Harness selection.** `tests/test_tasks_flow.py::test_always_ask_asks_even_with_one_agent_and_highlights_last_used`, `tests/test_tasks_flow.py::test_use_default_never_asks`, `tests/test_tasks_flow.py::test_ask_if_multiple`. Gap: the real picker widget is not driven by a test.
- **GitHub issues and PRs can be opened.** `tests/test_github_issues.py::test_open_issue_end_to_end`; live: `tests/test_live_github.py::test_open_a_real_issue_end_to_end`, `tests/test_live_github.py::test_open_a_real_pr_stores_its_metadata_and_starts_in_the_clone`. Gap: a fork PR or another person's PR cannot be made with one account; those paths use fakes (`tests/test_github_pulls.py::test_pr_state_draft_and_review_requested_are_captured`).
- **cmux sessions are created and focused.** `tests/test_terminal_cmux.py` (whole module); real cmux: `tests/test_live_cmux.py::test_reopen_focuses_and_a_stale_workspace_resumes`. Gap: the live tests are opt-in and need a cmux terminal ([live-validation.md §2 and §3](live-validation.md#2-cmux-open-and-focus-5-minutes)).
- **Existing sessions are reused.** `tests/test_session_lifecycle.py::test_second_open_focuses_and_creates_nothing`, `tests/test_session_lifecycle.py::test_stale_workspace_recovers_through_resume_never_a_second_session`, `tests/test_session_lifecycle.py::test_running_agent_is_focused_not_resumed_even_if_unsure`.
- **Concurrent launches and partial failures are safe.** Safety tests 9 and 10; `tests/test_launch_transactions.py::test_different_tasks_launch_concurrently`, `tests/test_launch_transactions.py::test_a_lock_held_by_a_killed_process_is_recovered`, `tests/test_launch_transactions.py::test_record_failure_closes_the_terminal_it_created`, `tests/test_state.py::test_concurrent_writers_do_not_fail_with_locked`.
- **Configuration migrations are supported.** `tests/test_migrations.py::test_migrate_backs_up_then_writes_and_keeps_unknown_fields`, `tests/test_migrations.py::test_failed_validation_restores_the_original_bytes`, `tests/test_migrations.py::test_file_newer_than_the_code_is_refused_untouched`, `tests/test_state.py::test_failed_migration_leaves_the_previous_version`.
- **Diagnostics are available.** `tests/test_doctor.py::test_all_good`, `tests/test_doctor.py::test_only_read_only_commands_with_timeouts`, `tests/test_diagnostics.py::test_export_is_sanitised`, `tests/test_logs.py::test_debug_mode_writes_traces_to_file_and_stderr`, `tests/test_redact.py` (whole module).
- **Automated tests and documentation.** `uv run pytest -q`; `tests/test_docs.py` and `tests/test_docs_content.py` cover the documentation (every option appears, every link resolves, the README sections, the doc set, the evidence names in this file). Statements not yet observed on real tools are labelled as such where they appear ([harnesses.md](harnesses.md), [terminal-adapters.md](terminal-adapters.md), [gh-dash.md](gh-dash.md), [sessions.md](sessions.md)).

## The ultimate success criterion

A developer selects a GitHub issue or PR, and Commodore starts on it in a correctly configured, resumable session with minimal interaction. The path is `commodore open <url>` (or the gh-dash shortcut): proved end to end on the mock terminal by scenario A, and against real GitHub by the live tests. Doing it on real cmux with real harnesses is exactly what [live-validation.md](live-validation.md) is for; that is the one remaining acceptance step, and it belongs to the engineer.

## Known gaps, in one list

- Real cmux and the three harnesses' real screens: [live-validation.md](live-validation.md) §2 to §10.
- `npx skills add` from GitHub, and the gh-dash key in a running gh-dash.
- A fork PR or a PR by someone else on real GitHub (one account).
- Python 3.11 to 3.13 and Linux are declared (`requires-python`, classifiers) but not run.
