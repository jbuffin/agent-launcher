"""Opt-in: reads the repository ID of the throwaway sandbox repo through the real `gh`.

Run with `AGENT_LAUNCHER_LIVE=1 uv run pytest tests/test_live_github.py`. Read-only.
"""

import os
import pwd

import pytest

from agent_launcher import repositories

pytestmark = pytest.mark.skipif(not os.environ.get("AGENT_LAUNCHER_LIVE"), reason="set AGENT_LAUNCHER_LIVE=1 to run")


def test_sandbox_repository_id(monkeypatch):
    monkeypatch.setattr(repositories, "run_command", repositories._real_run_command)
    # Tests run with a fake HOME; the real `gh` needs the real one to find its (read-only) login.
    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)
    identity = repositories.identify_reference("owner/sandbox")
    assert isinstance(identity.github_id, int) and identity.github_id > 0
    assert identity.node_id and identity.full_name.lower() == "owner/sandbox"


SANDBOX = "owner/sandbox"


def test_open_a_real_issue_end_to_end(monkeypatch, tmp_path, write_config):
    """Scenario A against the throwaway sandbox repository: a new issue, a clone into a temp `clone_root`, the
    stable IDs, the association and the worktree. The issue is closed afterwards."""
    import json
    import subprocess
    from pathlib import Path

    from agent_launcher import state
    from agent_launcher.config import load_config
    from agent_launcher.github import GitHub
    from agent_launcher.github_tasks import open_issue
    from agent_launcher.terminal_mock import MockTerminalAdapter
    from agent_launcher.worktrees import get_worktree
    from scripted import ScriptedPrompter

    monkeypatch.setattr(repositories, "run_command", repositories._real_run_command)
    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    write_config(
        {
            "version": 2,
            "terminal": {"adapter": "mock"},
            "agent_selection": "use_default",
            "repositories": {"worktree_root": str(tmp_path / "trees"), "clone_root": str(tmp_path / "clones")},
            "profiles": {"personal": {"default_agent": "claude", "agents": {"claude": {"executable": str(exe)}}}},
        }
    )
    created = subprocess.run(
        ["gh", "issue", "create", "-R", SANDBOX, "--title", "agent-launcher live test (safe to close)", "--body", "Created by a test."],
        capture_output=True, text=True, check=True, timeout=60,
    )
    url = created.stdout.strip().splitlines()[-1]
    try:
        adapter = MockTerminalAdapter(tmp_path / "mock.json")
        prompter = ScriptedPrompter(("confirm", "Clone it into", True), ("select", "Which profile", "personal"))
        with state.open_state() as conn:
            first = open_issue(conn, url, config=load_config(), adapter=adapter, prompter=prompter, github=GitHub())
            second = open_issue(conn, url, config=load_config(), adapter=adapter, prompter=None, github=GitHub())
            tree = get_worktree(conn, first.task.id)
            ids = conn.execute("SELECT node_id, database_id, repository_github_id FROM task_github").fetchone()
        prompter.done()
        assert first.task.source == "github" and first.prompt == url
        assert second.task.id == first.task.id and second.action == "focused"
        assert ids[0] and ids[1] > 0 and ids[2] > 0
        assert tree is not None and Path(tree.path).is_dir() and str(tmp_path / "trees") in tree.path
        assert Path(first.task.repo_path).parent == (tmp_path / "clones").resolve()
        calls = json.loads((tmp_path / "mock.json").read_text())["calls"]
        assert [c["prompt"] for c in calls if c["op"] == "create_session"] == [url]
    finally:
        subprocess.run(["gh", "issue", "close", url], capture_output=True, text=True, timeout=60)
