# Live validation checklist

Everything the automated suite can prove is proved by `uv run pytest` (no harness, no network) and, with `COMMODORE_LAUNCHER_LIVE=1`, by the GitHub tests against a throwaway sandbox repository. This file lists what only a person at a terminal with the real harnesses installed can check. These checks were written from `--help` output, vendor documentation and fakes, and they are the places where this release can still be wrong.

The list is ordered from quick to long. Each item gives the command, what to look for and how to fix the code if it is wrong. Tick them off as you go; every item says what to record if it fails.

Nothing here needs your real configuration. Use a scratch launcher home and a scratch repository (below), so a mistake costs a directory. Any terminal works; some items need two of them (two tabs or windows).

## 0. Set up once

```bash
export COMMODORE_LAUNCHER_HOME=$(mktemp -d)           # a scratch config root, state.db and logs
export SCRATCH=$(mktemp -d)                       # a scratch git repository for tasks to work in
git init -q -b main "$SCRATCH/demo" && git -C "$SCRATCH/demo" commit -q --allow-empty -m initial
cd <your clone of commodore> && uv sync
```

Harnesses need their trust dialog accepted once for the scratch directory; do that by running the harness in `$SCRATCH/demo` by hand first, or expect the dialog before the prompt is taken.

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

## 1. Health and packaging (2 minutes)

```bash
commodore doctor
commodore skill path
npx skills add jbuffin/commodore-launcher --skill commodore-launcher --list # or the path of a checkout
```

Look for: `doctor` finds git, gh, and each harness with its version; `skill path` prints a directory with `SKILL.md`; `npx skills` lists the one skill `commodore-launcher`.
If `npx skills add` from GitHub cannot see the skill: the repository must be readable by your GitHub login (a private repository needs access). The skill lives at `src/commodore_launcher/skills/commodore-launcher/`; a checkout path works with `--list` as a stand-in. Record the `skills` version that worked (it was `1.7.1` when written). Install with `-g -a claude-code -y` only if you want the skill in your real `~/.claude`; it writes there.

## 2. The real host with Claude Code (5 minutes)

```bash
COMMODORE_LAUNCHER_LIVE=1 COMMODORE_LAUNCHER_LIVE_DIR="$SCRATCH/demo" \
  uv run pytest tests/test_live_host.py -s
```

This runs `configure` in a pseudo-terminal: Claude Code starts on `/commodore-launcher <request>` (submitted at launch), the test waits until Claude Code has saved the conversation, checks that the harness itself holds the session lock, stops it, and checks that the next `configure` would resume the same conversation. It sends one short prompt to the model. It needs a repository Claude Code already trusts, so run `claude` in `$SCRATCH/demo` once first and accept the dialog.

Look for: the test passes.

If it fails:

- "Claude Code did not save the conversation": Claude Code no longer saves `<config>/projects/<directory>/<id>.jsonl`. Find where it keeps the conversation (`ls <config>/projects/*/`) and fix `ClaudeCodeAdapter.has_saved_conversation` (`agent_adapters.py`), with a unit test of the new shape in `tests/test_agent_adapters.py`.
- The lock is held by `commodore` and not the harness: the harness closed the inherited file. Check `ForegroundHost.run` (`session_host.py`).

## 3. Open, reopen and a harness running elsewhere (5 minutes)

In terminal 1:

```bash
commodore open <issue-url-in-the-demo-repository>   # prints the task ID; Claude Code runs here
```

Send a short message so the conversation exists (use something trivial). Then in terminal 2:

```bash
commodore open <task-id>
commodore tasks show <task-id>
```

Look for: terminal 2's `open` is refused with "already running (pid N) in another terminal" and starts nothing; `tasks show` says the session is running with that pid. Quit Claude in terminal 1 (`/exit`); `commodore` there prints how to resume and exits. Now `commodore open <task-id>` in terminal 2 resumes the conversation (`claude --resume <id>`) and shows your earlier message.

If terminal 2 starts a second Claude: the lock was not held. Record `ls -l $COMMODORE_LAUNCHER_HOME/locks` and check `locks.claim_session` and `ForegroundHost.run`.

## 4. Ctrl-C, closing the terminal, and a fresh conversation (5 minutes)

- In a running session, press Ctrl-C once: Claude handles it (it does not quit at the first one) and `commodore` keeps waiting. Quitting Claude returns you to the shell.
- Start a session, then close the terminal window. In another terminal, `commodore tasks show <task-id>` must say not running within a second or two (the harness ended with the terminal). If it still says running, a process Claude started kept the lock: record `lsof $COMMODORE_LAUNCHER_HOME/locks/<task-id>.session.lock`, and use `commodore resume <task-id> --force`.
- Open a new task and quit Claude before its prompt is submitted (Ctrl-C twice at the trust dialog of an untrusted directory). `commodore open <task-id>` must start a fresh conversation and say "no saved conversation", not fail with `No conversation found`.

## 5. Codex and Copilot launch argument and resume (10 minutes each)

Both take the prompt on the command line (`codex -- <prompt>`, `copilot --interactive=<prompt>`). Their documented skill syntax (`$commodore <url>`, `/commodore <url>`) was observed working as a launch argument on codex-cli 0.162.1 and Copilot CLI 1.0.98 with a probe skill ([harnesses.md](harnesses.md)); this checks it with Commodore on a real issue. Add the harness to a profile (item 0), then for each, with a GitHub issue URL of a repository you can open:

```bash
commodore open <issue-url> --harness codex        # and --harness copilot
```

Look for: the harness starts, shows the invocation as submitted and treats it as a skill invocation (it runs the `commodore` skill, or says it does not know it), not as plain text. If it is taken as plain text, the adapter's `skill_invocation` for that harness needs another form; record what you saw next to it in `agent_adapters.py`.

Resume: Copilot takes `--session-id` at launch and `--resume=<id>` later. After `open --harness copilot` and one message, quit it and run `commodore open <task-id>`: it should resume. Also quit one before any message is sent and check whether `<COPILOT_HOME>/session-state/<id>/` exists: if it does, Copilot saves a conversation before anything is submitted, and `CopilotCliAdapter.has_saved_conversation` needs to look inside it (record what is there). Codex has no resume by design: `open` on a Codex task whose harness is not running starts a fresh conversation on the same session.

Also confirm profile separation for each harness on your machine: the identity directory in the profile (`CODEX_HOME`, `COPILOT_HOME`) is the one the running harness reports (`/status` or its login screen), not your default one. Whether `COPILOT_HOME` alone separates stored logins was not verified.

## 6. The ten scenarios on the real tools (an hour or two)

`tests/test_scenarios.py` runs scenarios A to J on the recording host; A and E also run against the sandbox repository. Repeat them by hand with a real harness to see them end to end. Use a sandbox repository you own and a scratch home as above. Between scenarios run `commodore tasks list` and quit the harnesses.

| Scenario | Do | Expected |
| --- | --- | --- |
| A. Unknown repository | `commodore open <issue-url>` for a repository the launcher has not seen | Asks to clone (if needed), which profile, which harness; the harness runs in this terminal, in the checkout; Commodore's invocation of the URL is submitted at launch (nothing waiting in the input box) |
| B. Reopen | Quit the harness, then the same command again | Resumes the conversation; asks nothing; no prompt sent again |
| C. Isolation | Make a profile's `claude` executable unavailable (rename the wrapper) and open a task in a repository assigned to it | Refused with a message naming profile and executable; no other profile's Claude is used; `profile which` unchanged |
| D. Existing worktree | Removed: the launcher creates and adopts no worktrees | `open <pr-url>` starts the harness in the repository's checkout; no worktree, branch or ref is created |
| E. A review request | open a PR that requests your review | the prompt is the same `/commodore <url>` as for any PR |
| F. Local task | Removed: `open` takes a GitHub issue or PR URL only | `commodore open "fix the bug"` fails with `needs_issue_or_pr_url` and creates nothing |
| G. Removed commands | Nothing but `open <url>` starts a session | the commands listed in [Upgrading from Agent Launcher](config-migrations.md#upgrading-from-agent-launcher) are unknown commands |
| H. Concurrent | In two terminals run `commodore open <id>` at the same moment | One harness; the other terminal reports it already running |
| I. Partial failure | Point the profile's executable at a script whose first line names an interpreter that does not exist; `open <id>`; then fix it and `open <id>` again | A clear "Could not start" error; the retry starts the harness, no duplicates |
| J. `configure` | After `setup`, `commodore configure "add ~/src as a search root"` | A maintenance session, the management skill started on the request in this terminal |

Record any scenario that behaves differently as an issue with the command, the output and `commodore diagnostics export` (it redacts home paths and tokens; read it before sharing).

## Record of results

When you run a check, note the date, the tool versions (`commodore doctor`) and the result in the issue tracker, and fix any adapter form in `agent_adapters.py` with a unit test in `tests/test_agent_adapters.py`, using text you captured with every personal value replaced by a placeholder.
