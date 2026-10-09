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
