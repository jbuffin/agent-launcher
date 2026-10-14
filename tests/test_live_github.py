"""Opt-in: reads the repository ID of a throwaway sandbox repo through the real `gh`.

Run with `COMMODORE_LAUNCHER_LIVE=1 COMMODORE_LAUNCHER_SANDBOX_REPO=<owner>/<repo> uv run pytest tests/test_live_github.py`,
naming a repository you own and do not mind tests opening and closing issues and PRs in.
"""

import os
import pwd
from contextlib import contextmanager

import pytest

from commodore_launcher import repositories

SANDBOX = os.environ.get("COMMODORE_LAUNCHER_SANDBOX_REPO", "")
pytestmark = pytest.mark.skipif(
    not (os.environ.get("COMMODORE_LAUNCHER_LIVE") and SANDBOX),
    reason="set COMMODORE_LAUNCHER_LIVE=1 and COMMODORE_LAUNCHER_SANDBOX_REPO=<owner>/<repo> to run",
)


def test_sandbox_repository_id(monkeypatch):
    monkeypatch.setattr(repositories, "run_command", repositories._real_run_command)
    # Tests run with a fake HOME; the real `gh` needs the real one to find its (read-only) login.
    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)
    identity = repositories.identify_reference(SANDBOX)
    assert isinstance(identity.github_id, int) and identity.github_id > 0
    assert identity.node_id and identity.full_name.lower() == SANDBOX.lower()



def test_open_a_real_issue_end_to_end(monkeypatch, tmp_path, write_config):
    """Scenario A against the throwaway sandbox repository: a new issue, a clone into a temp `clone_root`, the
    stable IDs, the association and the session started in the clone. The issue is closed afterwards."""
    import json
    import subprocess
    from pathlib import Path

    from commodore_launcher import state
    from commodore_launcher.config import load_config
    from commodore_launcher.github import GitHub
    from commodore_launcher.github_tasks import open_issue
    from commodore_launcher.terminal_mock import MockTerminalAdapter
    from scripted import ScriptedPrompter

    monkeypatch.setattr(repositories, "run_command", repositories._real_run_command)
    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    write_config(
        {
            "version": 3,
            "terminal": {"adapter": "mock"},
            "harness_selection": "use_default",
            "repositories": {"clone_root": str(tmp_path / "clones")},
            "profiles": {"personal": {"default_harness": "claude", "harnesses": {"claude": {"executable": str(exe)}}}},
        }
    )
    created = subprocess.run(
        ["gh", "issue", "create", "-R", SANDBOX, "--title", "commodore live test (safe to close)", "--body", "Created by a test."],
        capture_output=True, text=True, check=True, timeout=60,
    )
    url = created.stdout.strip().splitlines()[-1]
    try:
        adapter = MockTerminalAdapter(tmp_path / "mock.json")
        prompter = ScriptedPrompter(("confirm", "Clone it into", True), ("select", "Which profile", "personal"))
        with state.open_state() as conn:
            first = open_issue(conn, url, config=load_config(), adapter=adapter, prompter=prompter, github=GitHub())
            second = open_issue(conn, url, config=load_config(), adapter=adapter, prompter=None, github=GitHub())
            ids = conn.execute("SELECT node_id, database_id, repository_github_id FROM task_github").fetchone()
        prompter.done()
        assert first.task.source == "github" and first.prompt == f"/commodore {url}"
        assert second.task.id == first.task.id and second.action == "focused"
        assert ids[0] and ids[1] > 0 and ids[2] > 0
        assert Path(first.task.repo_path).parent == (tmp_path / "clones").resolve()
        calls = json.loads((tmp_path / "mock.json").read_text())["calls"]
        assert [c["prompt"] for c in calls if c["op"] == "create_session"] == [f"/commodore {url}"]
        assert [c["working_directory"] for c in calls if c["op"] == "create_session"] == [first.task.repo_path]
    finally:
        subprocess.run(["gh", "issue", "close", url], capture_output=True, text=True, timeout=60)


# --- Pull requests (ticket #13) ---------------------------------------------------------------
#
# There is one GitHub account here, so a real fork PR or a PR by someone else cannot be created. A fork
# is covered by the fakes in tests/test_github_pulls.py.


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

    branch = f"commodore-launcher-live-{uuid.uuid4().hex[:8]}"
    default = _gh("api", f"repos/{SANDBOX}", "--jq", ".default_branch")
    sha = _gh("api", f"repos/{SANDBOX}/git/ref/heads/{default}", "--jq", ".object.sha")
    _gh("api", f"repos/{SANDBOX}/git/refs", "-f", f"ref=refs/heads/{branch}", "-f", f"sha={sha}")
    try:
        _gh(
            "api", "-X", "PUT", f"repos/{SANDBOX}/contents/live-test-{branch}.txt",
            "-f", "message=commodore live test", "-f", f"branch={branch}",
            "-f", "content=" + base64.b64encode(b"live test\n").decode(),
        )
        url = _gh(
            "pr", "create", "-R", SANDBOX, "--head", branch, "--base", default,
            "--title", "commodore live test (safe to close)", "--body", "Created by a test.",
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
            "version": 3,
            "terminal": {"adapter": "mock"},
            "harness_selection": "use_default",
            "repositories": {"clone_root": str(tmp_path / "clones")},
            "profiles": {"personal": {"default_harness": "claude", "harnesses": {"claude": {"executable": str(exe)}}}},
        }
    )


def _open_pr(url, tmp_path, **kwargs):
    from commodore_launcher import state
    from commodore_launcher.config import load_config
    from commodore_launcher.github import GitHub
    from commodore_launcher.github_tasks import open_pull_request
    from commodore_launcher.terminal_mock import MockTerminalAdapter
    from scripted import ScriptedPrompter

    prompter = ScriptedPrompter(("confirm", "Clone it into", True), ("select", "Which profile", "personal"))
    with state.open_state() as conn:
        result = open_pull_request(
            conn, url, config=load_config(), adapter=MockTerminalAdapter(tmp_path / "mock.json"), prompter=prompter,
            github=GitHub(**kwargs),
        )
        row = conn.execute(
            "SELECT kind, state, pr_head_ref, pr_head_sha, pr_own, pr_head_fork FROM task_github"
        ).fetchone()
    prompter.done()
    return result, row


def test_open_a_real_pr_stores_its_metadata_and_starts_in_the_clone(monkeypatch, tmp_path, write_config):
    import json

    _pr_config(monkeypatch, tmp_path, write_config)
    with _sandbox_pr() as (url, branch, head):
        result, row = _open_pr(url, tmp_path)
        assert result.task.source == "github" and result.prompt == f"/commodore {url}"
        assert row == ("pull_request", "open", branch, head, 1, 0)
        calls = json.loads((tmp_path / "mock.json").read_text())["calls"]
        assert [c["working_directory"] for c in calls if c["op"] == "create_session"] == [result.task.repo_path]
