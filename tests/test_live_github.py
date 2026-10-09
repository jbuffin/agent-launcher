"""Opt-in: reads the repository ID of the throwaway sandbox repo through the real `gh`.

Run with `AGENT_LAUNCHER_LIVE=1 uv run pytest tests/test_live_github.py`. Read-only.
"""

import os
import pwd
from contextlib import contextmanager

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


# --- Pull requests (ticket #13) ---------------------------------------------------------------
#
# There is one GitHub account here, so a real fork PR or a PR by someone else cannot be created. The review
# checkout is exercised by giving `GitHub` a fixed viewer who is not the author; forks are covered by the
# fakes in tests/test_github_pulls.py.


def _gh(*args: str, stdin: str | None = None) -> str:
    import subprocess

    done = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=True, timeout=60, input=stdin
    )
    return done.stdout.strip()


@contextmanager
def _sandbox_pr():
    """A branch with one commit and an open PR in the sandbox, made through the API (no push). Closed and deleted after."""
    import base64
    import uuid

    branch = f"agent-launcher-live-{uuid.uuid4().hex[:8]}"
    default = _gh("api", f"repos/{SANDBOX}", "--jq", ".default_branch")
    sha = _gh("api", f"repos/{SANDBOX}/git/ref/heads/{default}", "--jq", ".object.sha")
    _gh("api", f"repos/{SANDBOX}/git/refs", "-f", f"ref=refs/heads/{branch}", "-f", f"sha={sha}")
    try:
        _gh(
            "api", "-X", "PUT", f"repos/{SANDBOX}/contents/live-test-{branch}.txt",
            "-f", "message=agent-launcher live test", "-f", f"branch={branch}",
            "-f", "content=" + base64.b64encode(b"live test\n").decode(),
        )
        url = _gh(
            "pr", "create", "-R", SANDBOX, "--head", branch, "--base", default,
            "--title", "agent-launcher live test (safe to close)", "--body", "Created by a test.",
        ).splitlines()[-1]
        try:
            head = _gh("api", f"repos/{SANDBOX}/git/ref/heads/{branch}", "--jq", ".object.sha")
            yield url, branch, head
        finally:
            subprocess_run_quiet(["gh", "pr", "close", url])
    finally:
        subprocess_run_quiet(["gh", "api", "-X", "DELETE", f"repos/{SANDBOX}/git/refs/heads/{branch}"])


def subprocess_run_quiet(argv):
    import subprocess

    subprocess.run(argv, capture_output=True, text=True, timeout=60)


def _pr_config(monkeypatch, tmp_path, write_config):
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


def _open_pr(url, tmp_path, **kwargs):
    from agent_launcher import state
    from agent_launcher.config import load_config
    from agent_launcher.github import GitHub
    from agent_launcher.github_tasks import open_pull_request
    from agent_launcher.terminal_mock import MockTerminalAdapter
    from agent_launcher.worktrees import get_worktree
    from scripted import ScriptedPrompter

    prompter = ScriptedPrompter(("confirm", "Clone it into", True), ("select", "Which profile", "personal"))
    with state.open_state() as conn:
        result = open_pull_request(
            conn, url, config=load_config(), adapter=MockTerminalAdapter(tmp_path / "mock.json"), prompter=prompter,
            github=GitHub(**kwargs),
        )
        record = get_worktree(conn, result.task.id)
        row = conn.execute(
            "SELECT kind, state, pr_head_ref, pr_head_sha, pr_own, pr_head_fork FROM task_github"
        ).fetchone()
    prompter.done()
    return result, record, row


def test_open_own_pr_checks_out_its_head_branch(monkeypatch, tmp_path, write_config):
    from pathlib import Path

    from agent_launcher import git

    _pr_config(monkeypatch, tmp_path, write_config)
    with _sandbox_pr() as (url, branch, head):
        result, tree, row = _open_pr(url, tmp_path)
        assert result.task.source == "github" and result.prompt == url
        assert tree is not None and tree.branch == branch and Path(tree.path).is_dir()
        assert git.current_branch(tree.path) == branch and git.head_commit(tree.path) == head
        assert git.branch_upstream(tree.path, branch) == f"refs/remotes/origin/{branch}"
        assert row == ("pull_request", "open", branch, head, 1, 0)


def test_open_pr_as_a_review_checkout(monkeypatch, tmp_path, write_config):
    from pathlib import Path

    from agent_launcher import git

    _pr_config(monkeypatch, tmp_path, write_config)
    with _sandbox_pr() as (url, branch, head):
        result, tree, row = _open_pr(url, tmp_path, viewer="not-the-author")
        number = int(url.rsplit("/", 1)[1])
        assert tree is not None and tree.branch == f"review/pr-{number}" and Path(tree.path).is_dir()
        assert git.head_commit(tree.path) == head
        assert git.branch_upstream(tree.path, tree.branch) is None
        assert not git.branch_exists(result.task.repo_path, branch)  # the contributor's branch is not checked out
        assert row[0] == "pull_request" and row[4] == 0


def test_link_a_local_task_to_a_real_issue(monkeypatch, tmp_path, write_config):
    """Ticket #17 against the sandbox: a local task in a clone, a new issue, `link`, then `open <issue-url>` finds the
    same task and creates no second worktree or session. The issue is closed afterwards."""
    import subprocess

    from agent_launcher import state
    from agent_launcher.associations import ensure_profile
    from agent_launcher.config import load_config
    from agent_launcher.github import GitHub
    from agent_launcher.github_tasks import link_task, open_issue
    from agent_launcher.launch import open_task
    from agent_launcher.tasks import create_task, list_tasks
    from agent_launcher.terminal_mock import MockTerminalAdapter
    from scripted import ScriptedPrompter

    _pr_config(monkeypatch, tmp_path, write_config)
    clone = tmp_path / "clone"
    subprocess.run(["gh", "repo", "clone", SANDBOX, str(clone)], capture_output=True, text=True, check=True, timeout=120)
    url = _gh("issue", "create", "-R", SANDBOX, "--title", "agent-launcher link test (safe to close)", "--body", "Created by a test.").splitlines()[-1]
    try:
        adapter = MockTerminalAdapter(tmp_path / "mock.json")
        identity = repositories.identify_reference(str(clone))
        with state.open_state() as conn:
            prompter = ScriptedPrompter(("select", "Which profile", "personal"))
            resolved = ensure_profile(conn, identity, ["personal"], prompter)
            prompter.done()
            task = create_task(conn, "Local first", "", resolved.repository_id, identity.path, "personal")
            opened = open_task(conn, task.id, config=load_config(), adapter=adapter, prompter=None)
            linked = link_task(conn, task.id, url, github=GitHub())
            again = open_issue(conn, url, config=load_config(), adapter=adapter, prompter=None, github=GitHub())
            trees = conn.execute("SELECT count(*) FROM worktrees").fetchone()[0]
            sessions = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
            assert len(list_tasks(conn)) == 1
        assert linked.linked and linked.task.id == task.id and linked.task.source == "github"
        assert again.task.id == task.id and again.action == "focused"
        assert again.session.id == opened.session.id and trees == 1 and sessions == 1
    finally:
        subprocess_run_quiet(["gh", "issue", "close", url])
