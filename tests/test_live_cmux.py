"""Opt-in: `open` end to end in one real cmux workspace running real Claude Code. Each launch submits one prompt.

Run from a cmux terminal (cmux only accepts commands from processes it started), with
COMMODORE_LAUNCHER_LIVE_DIR set to an existing git repository Claude Code already trusts (otherwise it
shows its folder-trust dialog, the adapter waits up to 2 minutes for it to be answered by hand, and the
test fails if it is not):

    COMMODORE_LAUNCHER_LIVE=1 COMMODORE_LAUNCHER_LIVE_DIR=<repo> uv run pytest tests/test_live_cmux.py -s

Uses a temp COMMODORE_LAUNCHER_HOME and a profile whose claude instance sets
CLAUDE_CONFIG_DIR=~/.claude-personal. The directory is used as is (nothing nested is created).
Every launch starts Claude Code on a submitted prompt (`/commodore <url>`), so the
tests that open a workspace send that one prompt to the model. They close the workspace they opened.
"""

import json
import os
import pwd
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from commodore_launcher import cli
from conftest import make_github_task
from commodore_launcher.config import load_config
from commodore_launcher.launch import open_task
from commodore_launcher.state import open_state
from commodore_launcher.terminal_cmux import CmuxAdapter, find_cli, run_process
from commodore_launcher.terminals import TerminalSessionRef

pytestmark = pytest.mark.skipif(not os.environ.get("COMMODORE_LAUNCHER_LIVE"), reason="set COMMODORE_LAUNCHER_LIVE=1 to run")

REAL_FIND_CLI = find_cli  # imported before the default-suite fixture replaces it
REAL_RUN = run_process


def test_reopen_focuses_and_a_stale_workspace_resumes(write_config):
    """Opt-in, from a cmux terminal, same setup as above. Opens a task, opens it again (must focus and
    create nothing), then closes its workspace and opens it once more (must report the stale workspace and
    resume in a new one). Prints the resumed screen so the `ResumeCheck.confirmed` pattern for Claude Code can be
    chosen from what it really shows. The first launch submits its prompt; every workspace it opens is closed."""
    repo = os.environ.get("COMMODORE_LAUNCHER_LIVE_DIR")
    if not repo:
        pytest.skip("set COMMODORE_LAUNCHER_LIVE_DIR to a git repository Claude Code trusts")
    repo = str(Path(repo).resolve())
    claude = shutil.which("claude")
    assert claude, "claude is not on PATH"
    real_home = pwd.getpwuid(os.getuid()).pw_dir
    adapter = CmuxAdapter(lambda argv, timeout, stdin=None: REAL_RUN(argv, timeout, stdin), cli=REAL_FIND_CLI() or None)
    reason = adapter.unavailable_reason()
    if reason:
        pytest.skip(reason)
    write_config(
        {
            "version": 3,
            "terminal": {"adapter": "cmux"},
            "harness_selection": "use_default",
            "profiles": {
                "personal": {
                    "default_harness": "claude",
                    "harnesses": {"claude": {"executable": claude, "env": {"CLAUDE_CONFIG_DIR": f"{real_home}/.claude-personal"}}},
                }
            },
        }
    )
    runner = CliRunner()
    assert runner.invoke(cli.app, ["profile", "set", repo, "personal", "--offline"]).exit_code == 0
    task = make_github_task(Path(repo), "cmux reopen live check")
    opened = []

    def mine(ref):
        return TerminalSessionRef(adapter.name, ref.workspace_id, ref.surface_id, True)

    try:
        with open_state() as conn:
            first = open_task(conn, task["id"], config=load_config(), adapter=adapter, prompter=None, offline=True)
            opened.append(first.session.terminal)
            assert first.session.agent_conversation_id
            again = open_task(conn, task["id"], config=load_config(), adapter=adapter, prompter=None, offline=True)
            assert again.action == "focused" and again.session.id == first.session.id
            assert again.session.terminal == first.session.terminal
            # Make the workspace stale, then reopen: the recovery path, never a second session.
            adapter.close_session(mine(first.session.terminal))
            opened.clear()
            recovered = open_task(conn, task["id"], config=load_config(), adapter=adapter, prompter=None, offline=True)
            opened.append(recovered.session.terminal)
            print(recovered.notice)
            print(adapter._screen(mine(recovered.session.terminal)))
            assert recovered.action == "resumed" and recovered.session.id == first.session.id
            assert recovered.session.terminal.workspace_id != first.session.terminal.workspace_id
            assert recovered.resume_state in {"confirmed", "unconfirmed", "failed"}
    finally:
        for ref in opened:
            adapter.close_session(mine(ref))


@pytest.mark.skipif(not os.environ.get("COMMODORE_LAUNCHER_LIVE_EXECUTE"), reason="set COMMODORE_LAUNCHER_LIVE_EXECUTE=1: this one submits")
def test_a_new_session_submits_the_skill_invocation_at_launch(write_config, launcher_home):
    """Opt-in, from a cmux terminal, with COMMODORE_LAUNCHER_LIVE_DIR set as for the test above, plus
    COMMODORE_LAUNCHER_LIVE_EXECUTE=1. Checks the launch-argument form for real: Claude Code takes `/commodore
    <url>` from its command line and runs it as a skill invocation (the screen shows it submitted). Opens one
    workspace and closes it."""
    repo = os.environ.get("COMMODORE_LAUNCHER_LIVE_DIR")
    if not repo:
        pytest.skip("set COMMODORE_LAUNCHER_LIVE_DIR to a git repository Claude Code trusts")
    repo = str(Path(repo).resolve())
    claude = shutil.which("claude")
    assert claude, "claude is not on PATH"
    real_home = pwd.getpwuid(os.getuid()).pw_dir
    adapter = CmuxAdapter(lambda argv, timeout, stdin=None: REAL_RUN(argv, timeout, stdin), cli=REAL_FIND_CLI() or None)
    reason = adapter.unavailable_reason()
    if reason:
        pytest.skip(reason)
    write_config(
        {
            "version": 3,
            "terminal": {"adapter": "cmux"},
            "harness_selection": "use_default",
            "profiles": {
                "personal": {
                    "default_harness": "claude",
                    "harnesses": {"claude": {"executable": claude, "env": {"CLAUDE_CONFIG_DIR": f"{real_home}/.claude-personal"}}},
                }
            },
        }
    )
    runner = CliRunner()
    assert runner.invoke(cli.app, ["profile", "set", repo, "personal", "--offline"]).exit_code == 0
    task = make_github_task(Path(repo), "cmux execute live check")
    with open_state() as conn:
        result = open_task(conn, task["id"], config=load_config(), adapter=adapter, prompter=None, offline=True)
    mine = TerminalSessionRef(adapter.name, result.session.terminal.workspace_id, result.session.terminal.surface_id, True)
    try:
        assert result.prompt == f"/commodore {task['url']}"
        assert result.prompt_submitted, result.notice
        import time

        time.sleep(8)  # let the agent answer so the screen shows it took the prompt
        screen = adapter._screen(mine)
        print(screen)
        assert "commodore" in screen
    finally:
        adapter.close_session(mine)
