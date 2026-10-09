"""Reopen, resume, prompt and restart (ticket #9), through the CLI on the mock terminal and the cmux adapter on a fake."""

import json

import pytest
from typer.testing import CliRunner

from agent_launcher import cli, state
from agent_launcher.cli import app
from agent_launcher.errors import LauncherError
from agent_launcher.sessions import record_session, sessions_for_task
from agent_launcher.terminals import TerminalSessionRef
from scripted import ScriptedPrompter

runner = CliRunner()
EXITED = "Resume this conversation with:\n  claude --resume abc\n$ "


def run(*args):
    return runner.invoke(app, list(args))


def data(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


@pytest.fixture
def fake_agents(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "codex"):
        exe = bin_dir / name
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    return bin_dir


@pytest.fixture
def configure(write_config, fake_agents):
    def _configure(**extra):
        agents = {n: {"executable": str(fake_agents / n)} for n in ("claude", "codex")}
        write_config(
            {
                "version": 2,
                "terminal": {"adapter": "mock"},
                "agent_selection": "use_default",
                "profiles": {"work": {"default_agent": "claude", "agents": agents}},
                **extra,
            }
        )

    _configure()
    return _configure


@pytest.fixture
def repo(make_repo, configure):
    path = make_repo("one")
    assert run("profile", "set", str(path), "work", "--offline").exit_code == 0
    return path


@pytest.fixture
def task(repo):
    return json.loads(run("new", "--title", "Fix", "--repo", str(repo), "--offline", "--json").stdout)["task"]


def mock_file(launcher_home):
    return launcher_home / "mock-terminal.json"


def mock_calls(launcher_home, op=None):
    calls = json.loads(mock_file(launcher_home).read_text())["calls"]
    return [c for c in calls if op is None or c["op"] == op]


def script(launcher_home, **keys):
    """Set what the mock terminal reports (`screens`, `gone`, `resume_state`) between invocations."""
    file = mock_file(launcher_home)
    current = json.loads(file.read_text())
    file.write_text(json.dumps({**current, **keys}))


def open_(task, *extra):
    return data(run("open", task["id"], "--offline", "--json", *extra))


# --- reopen ---------------------------------------------------------------------------


def test_second_open_focuses_and_creates_nothing(configure, task, launcher_home):
    first = open_(task)
    # The picker would have to ask now; a reopen never asks.
    configure(agent_selection="always_ask")
    second = open_(task)
    assert second["action"] == "focused"
    assert second["session"]["id"] == first["session"]["id"]
    assert second["session"]["terminal"] == first["session"]["terminal"]
    assert second["prompt_prepared"] is False and second["notice"] is None
    assert second["prompt"] is None  # a focus builds and returns no prompt
    assert len(mock_calls(launcher_home, "create_session")) == 1
    (focus,) = mock_calls(launcher_home, "focus_session")
    assert focus["session"]["workspace_id"] == first["session"]["terminal"]["workspace_id"]
    with state.open_state() as conn:
        assert len(sessions_for_task(conn, task["id"])) == 1


def test_reopen_with_a_different_agent_is_refused(configure, task):
    open_(task)
    refused = run("open", task["id"], "--agent", "codex", "--offline", "--json")
    assert refused.exit_code == 1
    assert json.loads(refused.stdout)["error"]["code"] == "agent_mismatch"


def test_exited_agent_resumes_the_stored_conversation_in_a_new_workspace(configure, task, launcher_home):
    first = open_(task)
    conversation = first["session"]["agent_conversation_id"]
    script(launcher_home, screens={"mock-workspace-1": EXITED})
    again = open_(task)
    assert again["action"] == "resumed"
    resumed = mock_calls(launcher_home, "create_session")[-1]
    assert resumed["command"][-2:] == ["--resume", conversation]
    assert resumed["prompt"] is None  # the prompt is not entered again
    assert again["session"]["id"] == first["session"]["id"]
    assert again["session"]["agent_conversation_id"] == conversation
    assert again["session"]["terminal"]["workspace_id"] == "mock-workspace-2"
    with state.open_state() as conn:
        (only,) = sessions_for_task(conn, task["id"])
        assert only.terminal.workspace_id == "mock-workspace-2"
        assert only.agent_conversation_id == conversation


def test_resume_is_only_claimed_when_the_agent_confirms(configure, task, launcher_home):
    open_(task)
    script(launcher_home, gone=["mock-workspace-1"])
    unconfirmed = open_(task)
    assert unconfirmed["resume_state"] == "unconfirmed"
    assert "not confirmed" in unconfirmed["notice"] and "was restored" not in unconfirmed["notice"]
    script(launcher_home, gone=["mock-workspace-2"], resume_state="confirmed")
    confirmed = open_(task)
    assert confirmed["resume_state"] == "confirmed" and "was restored" in confirmed["notice"]
    script(launcher_home, gone=["mock-workspace-3"], resume_state="failed")
    failed = open_(task)
    assert failed["resume_state"] == "failed" and "restart" in failed["notice"]


def test_stale_workspace_recovers_through_resume_never_a_second_session(configure, task, launcher_home):
    open_(task)
    script(launcher_home, gone=["mock-workspace-1"])
    recovered = open_(task)
    assert recovered["action"] == "resumed" and "no longer exists" in recovered["notice"]
    assert not mock_calls(launcher_home, "focus_session")
    with state.open_state() as conn:
        assert len(sessions_for_task(conn, task["id"])) == 1


def test_running_agent_is_focused_not_resumed_even_if_unsure(configure, task, launcher_home):
    open_(task)
    script(launcher_home, screens={"mock-workspace-1": "some permission dialog nobody taught us\n"})
    assert open_(task)["action"] == "focused"
    assert len(mock_calls(launcher_home, "create_session")) == 1


def test_resume_command_forces_a_new_workspace_only_with_force(configure, task, launcher_home):
    open_(task)
    assert data(run("resume", task["id"], "--offline", "--json"))["action"] == "focused"
    # A second process on a live conversation needs an explicit yes.
    unconfirmed = run("resume", task["id"], "--force", "--offline", "--json")
    assert unconfirmed.exit_code == 1 and json.loads(unconfirmed.stdout)["error"]["code"] == "confirmation_required"
    assert len(mock_calls(launcher_home, "create_session")) == 1
    forced = data(run("resume", task["id"], "--force", "--yes", "--offline", "--json"))
    assert forced["action"] == "resumed"


def test_resume_of_an_unopened_task_says_so(configure, task):
    refused = run("resume", task["id"], "--offline", "--json")
    assert refused.exit_code == 1 and json.loads(refused.stdout)["error"]["code"] == "no_session"


def test_agent_without_a_conversation_id_cannot_resume_and_says_what_to_do(configure, task, launcher_home):
    first = open_(task, "--agent", "codex")
    assert first["session"]["agent_conversation_id"] is None
    script(launcher_home, gone=["mock-workspace-1"])
    refused = run("open", task["id"], "--offline", "--json")
    error = json.loads(refused.stdout)["error"]
    assert refused.exit_code == 1 and error["code"] == "resume_unsupported" and "restart" in error["message"]
    assert len(mock_calls(launcher_home, "create_session")) == 1


def test_a_second_session_for_a_task_is_refused_by_the_registry(configure, task):
    ref = TerminalSessionRef("mock", "w", "s")
    with state.open_state() as conn:
        record_session(conn, task["id"], "work", "claude", ref)
        with pytest.raises(LauncherError) as err:
            record_session(conn, task["id"], "work", "claude", ref)
        assert err.value.code == "session_exists"
        assert len(sessions_for_task(conn, task["id"])) == 1


def test_reopen_resolves_the_agent_first_and_focuses_nothing_when_it_cannot(configure, task, launcher_home):
    open_(task)
    configure(profiles={"work": {"default_agent": "claude", "agents": {}}})
    refused = run("open", task["id"], "--offline", "--json")
    assert refused.exit_code == 1  # agent unresolved: no launch, no focus under a different setup
    assert not mock_calls(launcher_home, "focus_session")


# --- prompt ---------------------------------------------------------------------------


def test_prompt_prepares_the_prompt_again_in_the_existing_session(configure, task, launcher_home):
    first = open_(task)
    again = data(run("prompt", task["id"], "--offline", "--json"))
    assert again["action"] == "prompted" and again["prompt_prepared"] is True and again["prompt_submitted"] is False
    (call,) = mock_calls(launcher_home, "prepare_prompt")
    assert call["prompt"] == "Fix" and call["session"] == first["session"]["terminal"]
    assert len(mock_calls(launcher_home, "create_session")) == 1


def test_prompt_needs_a_running_agent(configure, task, launcher_home):
    open_(task)
    script(launcher_home, screens={"mock-workspace-1": EXITED})
    refused = run("prompt", task["id"], "--offline", "--json")
    assert refused.exit_code == 1 and json.loads(refused.stdout)["error"]["code"] == "no_agent_running"
    assert not mock_calls(launcher_home, "prepare_prompt")


# --- restart --------------------------------------------------------------------------


def test_restart_needs_confirmation_and_changes_nothing_without_it(configure, task, launcher_home):
    open_(task)
    refused = run("restart", task["id"], "--offline", "--json")
    assert refused.exit_code == 1 and json.loads(refused.stdout)["error"]["code"] == "confirmation_required"
    assert not mock_calls(launcher_home, "close_session")
    assert len(mock_calls(launcher_home, "create_session")) == 1


def test_restart_declined_at_the_prompt_changes_nothing(configure, task, launcher_home, monkeypatch):
    open_(task)
    prompter = ScriptedPrompter(("confirm", "Restart task", False))
    monkeypatch.setattr(cli, "make_prompter", lambda: prompter)
    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    declined = run("restart", task["id"], "--offline")
    prompter.done()
    assert declined.exit_code == 1
    assert not mock_calls(launcher_home, "close_session")


def _git(repo, *args):
    import subprocess

    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return done.stdout + done.stderr


def test_restart_closes_the_old_workspace_keeps_task_and_repository(configure, task, repo, launcher_home):
    first = open_(task)
    before = sorted(p.name for p in repo.iterdir())
    status, head = _git(repo, "status", "--porcelain"), _git(repo, "rev-parse", "HEAD")
    restarted = data(run("restart", task["id"], "--yes", "--offline", "--json"))
    assert restarted["action"] == "restarted" and restarted["prompt_prepared"] is True
    (closed,) = mock_calls(launcher_home, "close_session")
    assert closed["session"]["workspace_id"] == "mock-workspace-1"
    assert restarted["session"]["id"] == first["session"]["id"]
    assert restarted["session"]["terminal"]["workspace_id"] == "mock-workspace-2"
    assert restarted["session"]["agent_conversation_id"] != first["session"]["agent_conversation_id"]
    assert restarted["task"]["id"] == task["id"]
    assert sorted(p.name for p in repo.iterdir()) == before
    assert _git(repo, "status", "--porcelain") == status and _git(repo, "rev-parse", "HEAD") == head
    with state.open_state() as conn:
        assert len(sessions_for_task(conn, task["id"])) == 1


def test_restart_does_not_close_a_workspace_that_is_already_gone(configure, task, launcher_home):
    open_(task)
    script(launcher_home, gone=["mock-workspace-1"])
    data(run("restart", task["id"], "--yes", "--offline", "--json"))
    assert not mock_calls(launcher_home, "close_session")


def test_ownership_is_stored_and_old_rows_default_to_not_owned(configure, task, launcher_home):
    open_(task)
    with state.open_state() as conn:
        (only,) = sessions_for_task(conn, task["id"])
        assert only.terminal.created_by_launcher is True
        column = next(c for c in conn.execute("PRAGMA table_info(terminal_sessions)") if c[1] == "created_by_launcher")
        assert column[4] == "0" and column[3] == 1  # existing rows migrate to "not owned"
        conn.execute("UPDATE terminal_sessions SET created_by_launcher = 0")
        (legacy,) = sessions_for_task(conn, task["id"])
        assert legacy.terminal.created_by_launcher is False


def test_resume_args_without_the_id_placeholder_are_refused(write_config, fake_agents, make_repo, task, launcher_home):
    open_(task)
    script(launcher_home, gone=["mock-workspace-1"])
    write_config(
        {
            "version": 2,
            "terminal": {"adapter": "mock"},
            "agent_selection": "use_default",
            "profiles": {
                "work": {
                    "default_agent": "claude",
                    "agents": {"claude": {"executable": str(fake_agents / "claude"), "resume_args": ["--resume"]}},
                }
            },
        }
    )
    refused = run("open", task["id"], "--offline", "--json")
    assert refused.exit_code == 1 and json.loads(refused.stdout)["error"]["code"] == "resume_args_invalid"
    assert len(mock_calls(launcher_home, "create_session")) == 1


def test_a_session_recorded_in_another_adapter_is_never_resumed_over(configure, task, launcher_home, monkeypatch):
    from agent_launcher.sessions import replace_terminal

    open_(task)
    with state.open_state() as conn:
        (only,) = sessions_for_task(conn, task["id"])
        replace_terminal(conn, only.id, TerminalSessionRef("cmux", "W", "S", True))
    script(launcher_home, gone=["mock-workspace-1"])  # even if the mock knew nothing of it
    refused = run("open", task["id"], "--offline", "--json", "--terminal", "mock")
    assert refused.exit_code == 1
    error = json.loads(refused.stdout)["error"]
    assert error["code"] == "terminal_mismatch" and "--terminal cmux" in error["message"]
    assert len(mock_calls(launcher_home, "create_session")) == 1
    with state.open_state() as conn:
        (still,) = sessions_for_task(conn, task["id"])
        assert still.terminal.adapter == "cmux" and still.terminal.workspace_id == "W"
    for command in ("resume", "prompt", "restart"):
        extra = ["--yes"] if command == "restart" else []
        out = run(command, task["id"], "--offline", "--json", "--terminal", "mock", *extra)
        assert json.loads(out.stdout)["error"]["code"] == "terminal_mismatch"


def test_restart_leaves_a_workspace_open_when_it_cannot_be_identified(configure, task, launcher_home):
    from agent_launcher.sessions import replace_terminal

    open_(task)
    for ref in (
        TerminalSessionRef("mock", "mock-workspace-1", None, True),  # no surface recorded
        TerminalSessionRef("mock", "mock-workspace-1", "mock-surface-1", False),  # ownership unrecorded
    ):
        with state.open_state() as conn:
            (only,) = sessions_for_task(conn, task["id"])
            replace_terminal(conn, only.id, ref)
        restarted = data(run("restart", task["id"], "--yes", "--offline", "--json"))
        assert "was left open" in restarted["notice"]
        assert not mock_calls(launcher_home, "close_session")
        script(launcher_home, gone=[])


def test_migration_3_keeps_a_populated_legacy_terminal_row_as_not_owned(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "legacy.db"
    monkeypatch.setattr(state, "MIGRATIONS", state.MIGRATIONS[:2])
    monkeypatch.setattr(state, "SCHEMA_VERSION", 2)
    conn = state.connect(path)
    conn.execute("INSERT INTO repositories (id, created_at, updated_at) VALUES (1, 'x', 'x')")
    conn.execute(
        "INSERT INTO tasks (id, title, repository_id, repo_path, profile, state, created_at, updated_at) "
        "VALUES ('t-legacy01', 'T', 1, '/r', 'work', 'active', 'x', 'x')"
    )
    conn.execute("INSERT INTO sessions VALUES ('s-legacy01', 't-legacy01', 'work', 'claude', 'launched', 'x')")
    conn.execute("INSERT INTO agent_conversations VALUES ('s-legacy01', 'claude', NULL)")
    conn.execute("INSERT INTO terminal_sessions VALUES ('s-legacy01', 'cmux', 'workspace:7', 'surface:9')")
    conn.close()
    monkeypatch.undo()  # back to the real migrations
    conn = state.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == state.SCHEMA_VERSION
    (legacy,) = sessions_for_task(conn, "t-legacy01")
    assert legacy.terminal == TerminalSessionRef("cmux", "workspace:7", "surface:9", False)
    assert legacy.agent_conversation_id is None
