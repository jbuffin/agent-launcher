"""Opt-in: `configure` end to end on the real session host, running real Claude Code in a pseudo-terminal. It submits one
prompt.

Run it from any terminal, with

    COMMODORE_LAUNCHER_LIVE=1 COMMODORE_LAUNCHER_LIVE_DIR=<repo> uv run pytest tests/test_live_host.py -s

`<repo>` is an existing git repository Claude Code already trusts (an untrusted one shows the folder-trust dialog, and
the prompt waits for it). The launcher home is a temp directory; the profile uses `CLAUDE_CONFIG_DIR` from your
environment, or `~/.claude`, so the management skill must be installed there. The test checks what the fakes cannot:
that Claude Code takes the prompt from its command line, saves the conversation where `has_saved_conversation` looks,
holds the session lock while it runs, and that the next open resumes that conversation.
"""

import json
import os
import pty
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from commodore_launcher import locks
from commodore_launcher.agent_adapters import ClaudeCodeAdapter

LIVE_DIR = os.environ.get("COMMODORE_LAUNCHER_LIVE_DIR")

pytestmark = pytest.mark.skipif(
    not (os.environ.get("COMMODORE_LAUNCHER_LIVE") and LIVE_DIR),
    reason="set COMMODORE_LAUNCHER_LIVE=1 and COMMODORE_LAUNCHER_LIVE_DIR=<repo> to run",
)


def commodore(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "COMMODORE_LAUNCHER_HOME": str(home)}
    return subprocess.run([sys.executable, "-m", "commodore_launcher", *args], capture_output=True, text=True, env=env, timeout=120)


def test_configure_runs_claude_code_here_and_the_next_open_resumes_it(tmp_path):
    claude = shutil.which("claude")
    assert claude, "Claude Code is not on PATH"
    home = tmp_path / "home"
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    instance = {"executable": claude, **({"env": {"CLAUDE_CONFIG_DIR": config_dir}} if config_dir else {})}
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "version": 4, "harness_selection": "use_default",
        "profiles": {"live": {"default_harness": "claude", "harnesses": {"claude": instance}}},
    }))
    request = "Reply with the single word ready and do nothing else."

    pid, fd = pty.fork()
    if pid == 0:  # the child: a terminal of its own, as when an engineer types the command
        os.environ["COMMODORE_LAUNCHER_HOME"] = str(home)
        os.execv(sys.executable, [sys.executable, "-m", "commodore_launcher", "configure", request, "--repo", LIVE_DIR, "--profile", "live"])
    try:
        env = {"CLAUDE_CONFIG_DIR": config_dir} if config_dir else {"HOME": os.environ["HOME"]}
        deadline = time.monotonic() + 180
        task_id = conversation = None
        while time.monotonic() < deadline:
            try:
                os.read(fd, 65536)  # keep the terminal drained so the harness never blocks on output
            except OSError:
                break
            if conversation is None and (home / "state.db").exists():
                with sqlite3.connect(home / "state.db") as db:
                    row = db.execute(
                        "SELECT s.task_id, c.conversation_id FROM sessions s JOIN agent_conversations c ON c.session_id = s.id"
                    ).fetchone()
                task_id, conversation = row if row else (None, None)
            if conversation and ClaudeCodeAdapter().has_saved_conversation(conversation, env):
                break
            time.sleep(1)
        assert task_id and conversation, "no session was recorded"
        os.environ["COMMODORE_LAUNCHER_HOME"] = str(home)
        holder = locks.session_holder(task_id)
        assert holder is not None and holder.pid != pid  # held by the harness itself
        assert ClaudeCodeAdapter().has_saved_conversation(conversation, env), "Claude Code did not save the conversation"
        os.kill(holder.pid, signal.SIGTERM)
        os.waitpid(pid, 0)
        deadline = time.monotonic() + 30
        while locks.session_holder(task_id) is not None and time.monotonic() < deadline:
            time.sleep(0.2)
        assert locks.session_holder(task_id) is None
    finally:
        os.environ.pop("COMMODORE_LAUNCHER_HOME", None)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.close(fd)
    again = commodore(home, "configure", "--repo", LIVE_DIR, "--profile", "live", "--dry-run", "--json")
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["command"][-2:] == ["--resume", conversation]
