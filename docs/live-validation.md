# Live validation checklist

Everything the automated suite can prove is proved by `uv run pytest` (no cmux, no network) and, with `AGENT_LAUNCHER_LIVE=1`, by the GitHub tests against a throwaway sandbox repository. This file lists what only a person at a cmux terminal with the real agents installed can check. They were written from `--help` output, vendor documentation and fakes, and they are the places where V1 can still be wrong.

The list is ordered from quick to long. Each item gives the command, what to look for and how to fix the code if it is wrong. Tick them off as you go; every item says what to record if it fails.

Nothing here needs your real configuration. Use a scratch launcher home and a scratch repository (below), so a mistake costs a directory.

## 0. Set up once

```bash
export AGENT_LAUNCHER_HOME=$(mktemp -d)           # a scratch config root, state.db and logs
export SCRATCH=$(mktemp -d)                       # a scratch git repository for tasks to work in
git init -q -b main "$SCRATCH/demo" && git -C "$SCRATCH/demo" commit -q --allow-empty -m initial
cd <your clone of agent-launcher> && uv sync
```

Run every command from a terminal **inside cmux**: cmux only accepts commands from processes it started. Agents need their trust dialog accepted once for the scratch directory; do that by running the agent in `$SCRATCH/demo` by hand first, or expect (and check, item 2) the folder-trust fallback.

Write a scratch config that names the real agents and the identity directories you want to test with (replace the placeholders):

```bash
cat > "$SCRATCH/answers.json" <<'EOF'
{
  "profiles": [
    {"name": "personal",
     "agents": {"claude": {"executable": "claude", "env": {"CLAUDE_CONFIG_DIR": "<claude-config-dir>"}},
                "codex": {"executable": "codex", "env": {"CODEX_HOME": "<codex-home>"}},
                "copilot": {"executable": "copilot", "env": {"COPILOT_HOME": "<copilot-home>"}}},
     "default_agent": "claude"}
  ],
  "worktree_root": "<scratch>/trees",
  "agent_selection": "use_default"
}
EOF
agent-launcher setup --yes --answers "$SCRATCH/answers.json"
agent-launcher profile set "$SCRATCH/demo" personal --offline
```

`doctor` should now pass or warn only about things you know (item 1).

To capture exactly what an agent shows (needed by items 3 to 7), read its screen while it is open:

```bash
cmux list-workspaces
cmux list-panels --workspace <workspace-ref>
cmux read-screen --workspace <workspace-ref> --surface <surface-id>
```

Close every workspace you open: `cmux workspace close <workspace-ref>` (add `--force` while an agent runs).

## 1. Health and packaging (2 minutes)

```bash
agent-launcher doctor
agent-launcher skill path
npx skills add <owner>/agent-launcher --skill agent-launcher --list     # or the path of a checkout
```

Look for: `doctor` finds cmux, git, gh, and each agent with its version; `skill path` prints a directory with `SKILL.md`; `npx skills` lists the one skill `agent-launcher`.
If `npx skills add` from GitHub cannot see the skill: the repository must be readable by your GitHub login (a private repository needs access). The skill lives at `src/agent_launcher/skills/agent-launcher/`; a checkout path works with `--list` as a stand-in. Record the `skills` version that worked (it was `1.7.1` when written). Install with `-g -a claude-code -y` only if you want the skill in your real `~/.claude`; it writes there.

## 2. cmux open, prepare, focus (5 minutes)

```bash
AGENT_LAUNCHER_LIVE=1 AGENT_LAUNCHER_LIVE_DIR="$SCRATCH/demo" \
  uv run pytest tests/test_live_cmux.py -s -k "prepares_prompt"
```

This opens one cmux workspace running Claude Code, pastes the prompt without submitting it, prints the screen, and closes the workspace. It needs a repository Claude Code already trusts, so run `claude` in `$SCRATCH/demo` once first and accept the dialog.

Look for: the test passes; the printed screen shows the prompt (or `[Pasted text ...]`) in the input box with nothing running.

If it fails:

- `workspace_id` not found, or `cmux_failed`: the output of `cmux new-workspace` changed. Fix the parsing in `src/agent_launcher/terminal_cmux.py` (`_handles`, `create_session`). The shape recorded for 0.65.0 is in [terminal-adapters.md](terminal-adapters.md).
- The prompt went to the clipboard with a notice (`prompt_prepared: false`): the ready pattern did not match. Read the screen (above) while Claude is idle and edit `ClaudeCodeAdapter.prompt_input().ready` in `src/agent_launcher/agent_adapters.py` to a stable string from its footer. Patterns are regular expressions, matched against the whole screen.
- The prompt is visible but the test says it was not: edit `collapsed` (the placeholder Claude shows for a pasted block).
- Claude asked about trust: expected for an untrusted directory; the adapter pastes nothing while the dialog is up. Accept it within 2 minutes and the prompt is pasted; otherwise run `agent-launcher prompt <task>` (the recovery).

Then the folder-trust fallback by hand, to see it behave: `agent-launcher new --title "trust check" --repo <an untrusted git repo> ` after `profile set`, `open`, and confirm nothing was typed while the dialog was up. Accept it within 2 minutes and confirm the prompt is pasted, unsubmitted. Repeat with a new task, leave the dialog open, and confirm the notice after 2 minutes names `agent-launcher prompt <task>`.

## 3. Reopen, stale workspace and resume (5 minutes)

```bash
AGENT_LAUNCHER_LIVE=1 AGENT_LAUNCHER_LIVE_DIR="$SCRATCH/demo" \
  uv run pytest tests/test_live_cmux.py -s -k "reopen_focuses"
```

This opens a task, opens it again (must focus, create nothing), closes its workspace, and opens it a third time (must report the stale workspace and resume in a new one). It prints the resumed screen.

Look for: the second open reports `focused` with the same workspace; the third reports `resumed` on the same session record with a different workspace.

If it fails: stale detection lives in `CmuxAdapter` (`_NOT_FOUND`, `_raise_unless_missing` in `terminal_cmux.py`); compare with the error text of `cmux workspace select <closed-ref>`.

**Claude Code's resume wording.** `ClaudeCodeAdapter.resume_check()` has an empty `confirmed` pattern on purpose, so a resume reports `unconfirmed` until you choose one. From the printed resumed screen pick a string that appears only when a conversation was restored (for example the previous message's text) and put it in `ResumeCheck(confirmed=(...))`. A conversation that never received a message cannot be resumed: `No conversation found with session ID` is expected then, and the answer is `restart`.

## 4. Claude Code's exit wording (5 minutes)

```bash
agent-launcher new --title "exit check" --repo "$SCRATCH/demo"
agent-launcher open <task-id>
```

In the workspace type a short message so the conversation exists (this sends a prompt to the model; use something trivial), then exit Claude with `/exit` or Ctrl-D twice. The screen should end with text offering to resume. Then:

```bash
agent-launcher open <task-id>        # must resume, not just focus
```

Look for: `open` resumes in a new workspace and says so. If it only focuses, the exit text is not recognised: read the screen and edit the regular expression in `ClaudeCodeAdapter.has_exited` (`agent_adapters.py`, currently `Resume this (conversation|session) with`). `open` never resumes over a running agent, so a wrong pattern errs towards focusing; `agent-launcher resume <task> --force` is the manual override.

## 5. Execute mode, the one `say hi` check (3 minutes)

```bash
AGENT_LAUNCHER_LIVE=1 AGENT_LAUNCHER_LIVE_EXECUTE=1 AGENT_LAUNCHER_LIVE_DIR="$SCRATCH/demo" \
  uv run pytest tests/test_live_cmux.py -s -k "execute_renders"
```

This is the only automated test that sends a prompt to the model. It renders a one-line template (`say hi`), submits it with exactly one Enter key, waits, and checks that the screen shows the prompt.

Look for: the agent answers. If the prompt was pasted but not submitted, `cmux send-key ... enter` did not register: check `cmux send-key --help` and `_prepare` in `terminal_cmux.py`. If it was never pasted, see item 2.

## 6. Adoption of a session you started yourself (5 minutes)

1. In cmux, open a workspace by hand with its working directory set to a task's worktree (`agent-launcher worktrees inspect <task>` prints it) and run any agent or just a shell there.
2. In another workspace:

```bash
agent-launcher sessions candidates <task-id> --json
agent-launcher sessions adopt <task-id> <workspace-ref> --agent claude
agent-launcher open <task-id>          # focuses the adopted workspace
```

Look for: the hand-made workspace is listed with `evidence` naming its working directory, workspaces unrelated to the task are not listed, `adopt` records it, and `open` focuses it. Closing the task's workspace later by `restart` must leave the adopted one open.

If candidates are missing: discovery parses `cmux list-workspaces` and `cmux list-panels` (`discover_sessions` in `terminal_cmux.py`). A workspace without a single terminal surface is deliberately not offered. If the listing format differs, fix `_handles` and the line parsing there.

## 7. Codex and Copilot screen patterns and resume (10 minutes each)

The ready, blocked and busy patterns for both were written from documentation, not from a live terminal. Wrong patterns fail safe: the prompt goes to the clipboard with a notice and nothing is pasted.

Add the agent to a profile (item 0), then for each:

```bash
agent-launcher new --title "codex check" --repo "$SCRATCH/demo"
agent-launcher open <task-id> --agent codex        # and --agent copilot
```

Look for: no clipboard notice, the prompt visible in the agent's input box, not submitted. If it fell back to the clipboard:

1. Run the agent by hand in a workspace; capture the idle screen (`cmux read-screen`).
2. Edit `CodexCliAdapter.prompt_input()` or `CopilotCliAdapter.prompt_input()` in `agent_adapters.py`: `ready` must match the idle footer; `blocked` must match trust and login dialogs and must not match the idle screen (Copilot's `blocked` list contains `/login`; if its idle footer mentions `/login`, narrow it); `busy` must match only while the agent is working; `collapsed` is the placeholder shown for a pasted block.
3. Re-run `open` on a new task. Then check that a trust or login dialog (a directory the agent has not seen) still stops the paste.

Resume: Copilot takes `--session-id` at launch and `--resume=<id>` later, so after `open --agent copilot` and sending one message, close the workspace and run `agent-launcher open <task-id>`: it should resume (only if an exit screen is recognised; otherwise `resume --force`). Copilot's resume confirmation pattern is empty, so expect `unconfirmed`. Codex has no resume by design: `open` on an exited Codex task says so and `restart` is the way forward. If you observe Codex's or Copilot's exit text, add a `has_exited` override (see `ClaudeCodeAdapter.has_exited`).

Also confirm profile separation for each agent on your machine: the identity directory in the profile (`CODEX_HOME`, `COPILOT_HOME`) is the one the running agent reports (`/status` or its login screen), not your default one. Whether `COPILOT_HOME` alone separates stored logins was not verified.

## 8. gh-dash keybinding and the picker surface (10 minutes)

```bash
agent-launcher integrate gh-dash --print-prompt        # the prompt an agent would follow
agent-launcher integrate gh-dash --profile personal --repo "$SCRATCH/demo"
```

The second command launches an agent with the management skill and the prompt prepared but not submitted; review it before sending, and point it at a **copy** of your gh-dash config if you are cautious (it makes a dated backup itself). Then, in a cmux terminal, run `gh dash`, highlight an issue in a repository that has no profile yet, and press the key the agent added (`b` by default).

Look for:

- The picker (profile, agent, clone confirmation) appears inside gh-dash's suspended screen, and gh-dash returns afterwards. gh-dash runs custom commands with a real terminal, so this should work unchanged (checked against its source for 4.26.0, not driven).
- Your existing keybindings and sections are untouched and the new binding does not shadow one.

If no terminal is given: add `--picker-surface` to the command in gh-dash's `keybindings`. Press the key again: a temporary cmux workspace titled `Agent Launcher: choose` should appear, show the picker, run the open, and **close itself** when the command ends (`run_interactive` types `<command>; exit`). If it stays open, cmux does not remove a surface whose shell exits; that is cosmetic, but record it. If no workspace appears, check `cmux new-workspace --help` against the call in `run_interactive` (`terminal_cmux.py`).

## 9. Cleanup against real sessions (10 minutes)

```bash
agent-launcher cleanup --dry-run
```

With a task whose issue is closed (or one you archived) and whose workspace is open, a worktree is only offered when nothing uses it. Look for: an open workspace or a process in the directory (`lsof +D <worktree>`) keeps it from being offered; a dirty worktree, an adopted one and one with unpushed commits are never offered; after you close the workspace and confirm, the worktree and its branch go. Everything not removed is listed with a reason.

## 10. The ten scenarios on the real tools (an hour or two)

`tests/test_scenarios.py` runs scenarios A to J (SPEC §34) on the mock terminal; A, D, E and G also run against the sandbox repository. Repeat them by hand with cmux and a real agent to see them end to end. Use a sandbox repository you own and a scratch home as above. Between scenarios run `agent-launcher tasks list` and close the workspaces.

| Scenario | Do | Expected |
| --- | --- | --- |
| A. Unknown repository | `agent-launcher open <issue-url>` for a repository the launcher has not seen | Asks to clone (if needed), which profile, which agent; a cmux workspace in a new worktree; the URL waits in the input box unsubmitted |
| B. Reopen | The same command again | Focuses the workspace; asks nothing; no second worktree; nothing typed |
| C. Isolation | Make a profile's `claude` executable unavailable (rename the wrapper) and open a task in a repository assigned to it | Refused with a message naming profile and executable; no other profile's Claude is used; `profile which` unchanged |
| D. Existing worktree | Check out a PR's head branch in a worktree yourself, then `open <pr-url>` | The tree is offered; adopt it; no other worktree is created; `worktrees inspect` shows `adopted` |
| E. Routing | `workflows init`, edit the rules, open a PR that requests your review | `workflows test <pr-url>` names the code-review rule; the agent preference is highlighted; `/code-review <url>` is in the input box; edit it before sending |
| F. Local task | `agent-launcher new --title ... --repo ...`, then `open <id>` | Stable `t-xxxxxxxx` ID; the agent runs in the repository's worktree; the task is there in a new shell |
| G. Link | Create a GitHub issue, `tasks link <id> <issue-url>`, `open <issue-url>` | Same task, same session focused, no second worktree |
| H. Concurrent | In two cmux panes run `agent-launcher open <id>` at the same moment | One workspace; one of them reports focusing |
| I. Partial failure | Put a wrapper named `cmux` first on `PATH` that execs the real cmux for `ping` and exits 1 for `new-workspace`; `open <id>`; then remove the wrapper and `open <id>` again | A clear `terminal_create_failed` error and a `launch_failed` task with its worktree intact; the retry creates the workspace in the same worktree, no duplicates |
| J. gh-dash | After `setup`, accept the gh-dash offer (or run `integrate gh-dash`) | A maintenance task, the management skill's prompt prepared, your gh-dash config unchanged until you let the agent edit it |

Record any scenario that behaves differently as an issue with the command, the output and `agent-launcher diagnostics export` (it redacts home paths and tokens; read it before sharing).

## Record of results

When you run a check, note the date, the tool versions (`agent-launcher doctor`) and the result in the issue tracker, and fix any pattern in `agent_adapters.py` with a unit test in `tests/test_agent_prepare.py` that uses the screen text you captured with every personal value replaced by a placeholder.
