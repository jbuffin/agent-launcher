# Live validation checklist

Everything the automated suite can prove is proved by `uv run pytest` (no cmux, no network) and, with `COMMODORE_LAUNCHER_LIVE=1`, by the GitHub tests against a throwaway sandbox repository. This file lists what only a person at a cmux terminal with the real harnesses installed can check. They were written from `--help` output, vendor documentation and fakes, and they are the places where V1 can still be wrong.

The list is ordered from quick to long. Each item gives the command, what to look for and how to fix the code if it is wrong. Tick them off as you go; every item says what to record if it fails.

Nothing here needs your real configuration. Use a scratch launcher home and a scratch repository (below), so a mistake costs a directory.

## 0. Set up once

```bash
export COMMODORE_LAUNCHER_HOME=$(mktemp -d)           # a scratch config root, state.db and logs
export SCRATCH=$(mktemp -d)                       # a scratch git repository for tasks to work in
git init -q -b main "$SCRATCH/demo" && git -C "$SCRATCH/demo" commit -q --allow-empty -m initial
cd <your clone of commodore> && uv sync
```

Run every command from a terminal **inside cmux**: cmux only accepts commands from processes it started. Harnesses need their trust dialog accepted once for the scratch directory; do that by running the harness in `$SCRATCH/demo` by hand first, or expect (and check, item 2) the folder-trust fallback.

Write a scratch config that names the real harnesses and the identity directories you want to test with (replace the placeholders):

```bash
cat > "$SCRATCH/answers.json" <<'EOF'
{
  "profiles": [
    {"name": "personal",
     "harnesses": {"claude": {"executable": "claude", "env": {"CLAUDE_CONFIG_DIR": "<claude-config-dir>"}},
                "codex": {"executable": "codex", "env": {"CODEX_HOME": "<codex-home>"}},
                "copilot": {"executable": "copilot", "env": {"COPILOT_HOME": "<copilot-home>"}}},
     "default_harness": "claude"}
  ],
  "harness_selection": "use_default"
}
EOF
commodore setup --yes --answers "$SCRATCH/answers.json"
commodore profile set "$SCRATCH/demo" personal --offline
```

`doctor` should now pass or warn only about things you know (item 1).

To capture exactly what a harness shows (needed by items 3 to 7), read its screen while it is open:

```bash
cmux list-workspaces
cmux list-panels --workspace <workspace-ref>
cmux read-screen --workspace <workspace-ref> --surface <surface-id>
```

Close every workspace you open: `cmux workspace close <workspace-ref>` (add `--force` while a harness runs).

## 1. Health and packaging (2 minutes)

```bash
commodore doctor
commodore skill path
npx skills add <owner>/commodore-launcher --skill commodore-launcher --list     # or the path of a checkout
```

Look for: `doctor` finds cmux, git, gh, and each harness with its version; `skill path` prints a directory with `SKILL.md`; `npx skills` lists the one skill `commodore-launcher`.
If `npx skills add` from GitHub cannot see the skill: the repository must be readable by your GitHub login (a private repository needs access). The skill lives at `src/commodore_launcher/skills/commodore-launcher/`; a checkout path works with `--list` as a stand-in. Record the `skills` version that worked (it was `1.7.1` when written). Install with `-g -a claude-code -y` only if you want the skill in your real `~/.claude`; it writes there.

## 2. cmux open and focus (5 minutes)

```bash
COMMODORE_LAUNCHER_LIVE=1 COMMODORE_LAUNCHER_LIVE_DIR="$SCRATCH/demo" \
  uv run pytest tests/test_live_cmux.py -s -k "reopen"
```

This opens one cmux workspace running Claude Code on the maintenance task's prompt (`/commodore-launcher <title>`, submitted at launch), opens it again (must focus), closes the workspace and opens once more (must resume). It sends that one prompt to the model. It needs a repository Claude Code already trusts, so run `claude` in `$SCRATCH/demo` once first and accept the dialog.

Look for: the test passes; the printed screen shows the resumed conversation.

If it fails:

- `workspace_id` not found, or `cmux_failed`: the output of `cmux new-workspace` changed. Fix the parsing in `src/commodore_launcher/terminal_cmux.py` (`_handles`, `create_session`). The shape recorded for 0.65.0 is in [terminal-adapters.md](terminal-adapters.md).
- Claude asked about trust: expected for an untrusted directory. The prompt is on Claude Code's command line, so it takes it once you answer the dialog; nothing is typed into the dialog.

## 3. Reopen, stale workspace and resume (5 minutes)

```bash
COMMODORE_LAUNCHER_LIVE=1 COMMODORE_LAUNCHER_LIVE_DIR="$SCRATCH/demo" \
  uv run pytest tests/test_live_cmux.py -s -k "reopen_focuses"
```

This opens a task, opens it again (must focus, create nothing), closes its workspace, and opens it a third time (must report the stale workspace and resume in a new one). It prints the resumed screen.

Look for: the second open reports `focused` with the same workspace; the third reports `resumed` on the same session record with a different workspace.

If it fails: stale detection lives in `CmuxAdapter` (`_NOT_FOUND`, `_raise_unless_missing` in `terminal_cmux.py`); compare with the error text of `cmux workspace select <closed-ref>`.

**Claude Code's resume wording.** `ClaudeCodeAdapter.resume_check()` has an empty `confirmed` pattern on purpose, so a resume reports `unconfirmed` until you choose one. From the printed resumed screen pick a string that appears only when a conversation was restored (for example the previous message's text) and put it in `ResumeCheck(confirmed=(...))`. A conversation that never received a message cannot be resumed: `No conversation found with session ID` is expected then: the launcher forgets the ID, and the next `open` starts a fresh conversation.

## 4. Claude Code's exit wording (5 minutes)

```bash
commodore open <issue-url-in-the-demo-repository>   # prints the task ID
commodore open <task-id>
```

In the workspace type a short message so the conversation exists (this sends a prompt to the model; use something trivial), then exit Claude with `/exit` or Ctrl-D twice. The screen should end with text offering to resume. Then:

```bash
commodore open <task-id>        # must resume, not just focus
```

Look for: `open` resumes in a new workspace and says so. If it only focuses, the exit text is not recognised: read the screen and edit the regular expression in `ClaudeCodeAdapter.has_exited` (`agent_adapters.py`, currently `Resume this (conversation|session) with`). `open` never resumes over a running harness, so a wrong pattern errs towards focusing; `commodore resume <task> --force` is the manual override.

## 5. The skill invocation at launch (3 minutes)

```bash
COMMODORE_LAUNCHER_LIVE=1 COMMODORE_LAUNCHER_LIVE_EXECUTE=1 COMMODORE_LAUNCHER_LIVE_DIR="$SCRATCH/demo" \
  uv run pytest tests/test_live_cmux.py -s -k "submits_the_skill_invocation"
```

This starts Claude Code with `/commodore <issue-url>` on the command line (`claude ... -- "$(cat -- <file> ...)"`), waits, and checks that the screen shows it. It sends one prompt to the model.

Look for: Claude runs the invocation as a skill (if the skill is installed), and the typed shell line shows the `$(cat ...)` form, not the prompt. If Claude stops at its trust dialog, accept it: the prompt is submitted after. If it never arrives, check `create_session` in `terminal_cmux.py`.

## 6. (Removed) Adoption of a session you started yourself

`sessions candidates` and `sessions adopt` are gone: the launcher records only sessions it created.

## 7. Codex and Copilot launch argument and resume (10 minutes each)

Both take the prompt on the command line (`codex -- <prompt>`, `copilot --interactive=<prompt>`). Their documented skill syntax (`$commodore <url>`, `/commodore <url>`) has not been tried as a launch argument: that is what this checks. Add the harness to a profile (item 0), then for each, with a GitHub issue URL of a repository you can open:

```bash
commodore open <issue-url> --harness codex        # and --harness copilot
```

Look for: the harness starts, shows the invocation as submitted and treats it as a skill invocation (it runs the `commodore` skill, or says it does not know it), not as plain text. If it is taken as plain text, the adapter's `skill_invocation` for that harness needs another form; record what you saw next to it in `agent_adapters.py`.

Resume: Copilot takes `--session-id` at launch and `--resume=<id>` later, so after `open --harness copilot` and sending one message, close the workspace and run `commodore open <task-id>`: it should resume (only if an exit screen is recognised; otherwise `resume --force`). Copilot's resume confirmation pattern is empty, so expect `unconfirmed`. Codex has no resume by design: `open` on an exited Codex task starts a fresh conversation on the same session. If you observe Codex's or Copilot's exit text, add a `has_exited` override (see `ClaudeCodeAdapter.has_exited`).

Also confirm profile separation for each harness on your machine: the identity directory in the profile (`CODEX_HOME`, `COPILOT_HOME`) is the one the running harness reports (`/status` or its login screen), not your default one. Whether `COPILOT_HOME` alone separates stored logins was not verified.

## 8. gh-dash keybinding and the picker surface (10 minutes)

```bash
commodore integrate gh-dash --print-prompt        # the prompt a harness would follow
commodore integrate gh-dash --profile personal --repo "$SCRATCH/demo"
```

The second command launches a harness with the management skill and submits its request at launch (`--print-prompt` shows it first), so point it at a **copy** of your gh-dash config if you are cautious (it makes a dated backup itself). Then, in a cmux terminal, run `gh dash`, highlight an issue in a repository that has no profile yet, and press the key the harness added (`b` by default).

Look for:

- The picker (profile, harness, clone confirmation) appears inside gh-dash's suspended screen, and gh-dash returns afterwards. gh-dash runs custom commands with a real terminal, so this should work unchanged (checked against its source for 4.26.0, not driven).
- Your existing keybindings and sections are untouched and the new binding does not shadow one.

If no terminal is given: add `--picker-surface` to the command in gh-dash's `keybindings`. Press the key again: a temporary cmux workspace titled `Commodore Launcher: choose` should appear, show the picker, run the open, and **close itself** when the command ends (`run_interactive` types `<command>; exit`). If it stays open, cmux does not remove a surface whose shell exits; that is cosmetic, but record it. If no workspace appears, check `cmux new-workspace --help` against the call in `run_interactive` (`terminal_cmux.py`).

## 9. Cleanup against real sessions

Removed: the launcher creates and removes no worktrees, so there is nothing to check here.

## 10. The ten scenarios on the real tools (an hour or two)

`tests/test_scenarios.py` runs scenarios A to J (SPEC §34) on the mock terminal; A and E also run against the sandbox repository. Repeat them by hand with cmux and a real harness to see them end to end. Use a sandbox repository you own and a scratch home as above. Between scenarios run `commodore tasks list` and close the workspaces.

| Scenario | Do | Expected |
| --- | --- | --- |
| A. Unknown repository | `commodore open <issue-url>` for a repository the launcher has not seen | Asks to clone (if needed), which profile, which harness; a cmux workspace in the checkout; the URL waits in the input box unsubmitted |
| B. Reopen | The same command again | Focuses the workspace; asks nothing; nothing typed |
| C. Isolation | Make a profile's `claude` executable unavailable (rename the wrapper) and open a task in a repository assigned to it | Refused with a message naming profile and executable; no other profile's Claude is used; `profile which` unchanged |
| D. Existing worktree | Removed: the launcher creates and adopts no worktrees | `open <pr-url>` starts the harness in the repository's checkout; no worktree, branch or ref is created |
| E. A review request | open a PR that requests your review | the prompt is the same `/commodore <url>` as for any PR |
| F. Local task | Removed: `open` takes a GitHub issue or PR URL only | `commodore open "fix the bug"` fails with `needs_issue_or_pr_url` and creates nothing |
| G. Link | Removed: there are no local tasks to link | `commodore tasks link` is an unknown command |
| H. Concurrent | In two cmux panes run `commodore open <id>` at the same moment | One workspace; one of them reports focusing |
| I. Partial failure | Put a wrapper named `cmux` first on `PATH` that execs the real cmux for `ping` and exits 1 for `new-workspace`; `open <id>`; then remove the wrapper and `open <id>` again | A clear `terminal_create_failed` error and a `launch_failed` task; the retry creates the workspace, no duplicates |
| J. gh-dash | After `setup`, accept the gh-dash offer (or run `integrate gh-dash`) | A maintenance session, the management skill started on the integration request, your gh-dash config unchanged until the harness edits it |

Record any scenario that behaves differently as an issue with the command, the output and `commodore diagnostics export` (it redacts home paths and tokens; read it before sharing).

## Record of results

When you run a check, note the date, the tool versions (`commodore doctor`) and the result in the issue tracker, and fix any adapter form in `agent_adapters.py` with a unit test in `tests/test_agent_adapters.py`, using text you captured with every personal value replaced by a placeholder.
