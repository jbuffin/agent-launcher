"""Opt-in: `open` end to end in one real cmux workspace running real Claude Code. Never submits.

Run from a cmux terminal (cmux only accepts commands from processes it started), with
AGENT_LAUNCHER_LIVE_DIR set to an existing git repository Claude Code already trusts (otherwise it
shows its folder-trust dialog, the adapter correctly pastes nothing, and this test fails):

    AGENT_LAUNCHER_LIVE=1 AGENT_LAUNCHER_LIVE_DIR=<repo> uv run pytest tests/test_live_cmux.py -s

Uses a temp AGENT_LAUNCHER_HOME and a profile whose claude instance sets
CLAUDE_CONFIG_DIR=~/.claude-personal. The directory is used as is (nothing nested is created).
Sends no prompt to the model, and closes the workspace it opened.
"""

import json
import os
import pwd
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_launcher import cli
from agent_launcher.config import load_config
from agent_launcher.launch import open_task
from agent_launcher.state import open_state
from agent_launcher.terminal_cmux import CmuxAdapter, find_cli, run_process
from agent_launcher.terminals import TerminalSessionRef

pytestmark = pytest.mark.skipif(not os.environ.get("AGENT_LAUNCHER_LIVE"), reason="set AGENT_LAUNCHER_LIVE=1 to run")

REAL_FIND_CLI = find_cli  # imported before the default-suite fixture replaces it
REAL_RUN = run_process


def test_open_prepares_prompt_without_submitting(write_config):
    repo = os.environ.get("AGENT_LAUNCHER_LIVE_DIR")
    if not repo:
        pytest.skip("set AGENT_LAUNCHER_LIVE_DIR to a git repository Claude Code trusts")
    repo = str(Path(repo).resolve())
    if not (Path(repo) / ".git").exists():
        pytest.skip(f"{repo} is not a git repository")
    claude = shutil.which("claude")
    assert claude, "claude is not on PATH"
    real_home = pwd.getpwuid(os.getuid()).pw_dir

    adapter = CmuxAdapter(lambda argv, timeout, stdin=None: REAL_RUN(argv, timeout, stdin), cli=REAL_FIND_CLI() or None)
    reason = adapter.unavailable_reason()
    if reason:
        pytest.skip(reason)

    write_config(
        {
            "version": 2,
            "terminal": {"adapter": "cmux"},
            "agent_selection": "use_default",
            "profiles": {
                "personal": {
                    "default_agent": "claude",
                    "agents": {"claude": {"executable": claude, "env": {"CLAUDE_CONFIG_DIR": f"{real_home}/.claude-personal"}}},
                }
            },
        }
    )
    runner = CliRunner()
    assert runner.invoke(cli.app, ["profile", "set", repo, "personal", "--offline"]).exit_code == 0
    made = runner.invoke(cli.app, ["new", "--title", "cmux adapter live check", "--repo", repo, "--offline", "--json"])
    task = json.loads(made.stdout)["task"]

    with open_state() as conn:
        result = open_task(conn, task["id"], config=load_config(), adapter=adapter, prompter=None, offline=True)
    # Closing needs `--force` while Claude runs, and only a workspace this test made may be forced.
    mine = TerminalSessionRef(adapter.name, result.session.terminal.workspace_id, result.session.terminal.surface_id, True)
    try:
        screen = adapter._screen(mine)
        print(screen)
        assert result.prompt_prepared, result.notice
        assert not result.prompt_submitted
        assert "cmux adapter live check" in screen or "[Pasted text" in screen
        assert "esc to interrupt" not in screen
    finally:
        adapter.close_session(mine)


def test_reopen_focuses_and_a_stale_workspace_resumes(write_config):
    """Opt-in, from a cmux terminal, same setup as above. Opens a task, opens it again (must focus and
    create nothing), then closes its workspace and opens it once more (must report the stale workspace and
    resume in a new one). Prints the resumed screen so the `ResumeCheck.confirmed` pattern for Claude Code can be
    chosen from what it really shows. Nothing is submitted; every workspace it opens is closed."""
    repo = os.environ.get("AGENT_LAUNCHER_LIVE_DIR")
    if not repo:
        pytest.skip("set AGENT_LAUNCHER_LIVE_DIR to a git repository Claude Code trusts")
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
            "version": 2,
            "terminal": {"adapter": "cmux"},
            "agent_selection": "use_default",
            "profiles": {
                "personal": {
                    "default_agent": "claude",
                    "agents": {"claude": {"executable": claude, "env": {"CLAUDE_CONFIG_DIR": f"{real_home}/.claude-personal"}}},
                }
            },
        }
    )
    runner = CliRunner()
    assert runner.invoke(cli.app, ["profile", "set", repo, "personal", "--offline"]).exit_code == 0
    task = json.loads(runner.invoke(cli.app, ["new", "--title", "cmux reopen live check", "--repo", repo, "--offline", "--json"]).stdout)["task"]
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
