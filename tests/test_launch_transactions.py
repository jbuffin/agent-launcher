"""Transactional launches (ticket #11): per-task locks, stages, retry and rollback.

The concurrency tests run real `agent-launcher open` processes against the mock terminal adapter and its JSON
record file; the failure tests use the same adapter in-process.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_launcher import git, launches, locks, state
from agent_launcher.cli import app
from agent_launcher.config import load_config
from agent_launcher.errors import LauncherError
from agent_launcher.launch import open_task, restart_task
from agent_launcher.sessions import primary_session
from agent_launcher.tasks import get_task
from agent_launcher.terminal_mock import CREATE_DELAY_ENV, MockTerminalAdapter
from agent_launcher.terminals import TerminalError, TerminalSessionRef
from agent_launcher.worktrees import ensure_worktree, get_worktree, worktree_path_for

runner = CliRunner()


def run(*args):
    return runner.invoke(app, list(args))


@pytest.fixture
def configure(write_config, tmp_path):
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    write_config(
        {
            "version": 2,
            "terminal": {"adapter": "mock"},
            "agent_selection": "use_default",
            "repositories": {"worktree_root": str(tmp_path / "trees")},
            "profiles": {"work": {"default_agent": "claude", "agents": {"claude": {"executable": str(exe)}}}},
        }
    )


@pytest.fixture
def repo(configure, make_repo):
    path = make_repo("one")
    assert run("profile", "set", str(path), "work", "--offline").exit_code == 0
    return path


def new_task(repo: Path, title: str = "Fix login") -> str:
    result = run("new", "--title", title, "--repo", str(repo), "--offline", "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["task"]["id"]


def mock_file(launcher_home: Path) -> Path:
    return launcher_home / "mock-terminal.json"


def mock_calls(launcher_home: Path, op: str = "create_session") -> list[dict]:
    path = mock_file(launcher_home)
    if not path.exists():
        return []
    return [c for c in json.loads(path.read_text()).get("calls", []) if c["op"] == op]


def set_mock(launcher_home: Path, **keys) -> None:
    path = mock_file(launcher_home)
    data = json.loads(path.read_text()) if path.exists() else {}
    data.update(keys)
    for k in [k for k, v in data.items() if v is None]:
        del data[k]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def spawn_open(task_id: str, *extra: str, delay: float = 0.0, env: dict | None = None) -> subprocess.Popen:
    full = {**os.environ, CREATE_DELAY_ENV: str(delay), **(env or {})}
    return subprocess.Popen(
        [sys.executable, "-m", "agent_launcher", "open", task_id, "--terminal", "mock", "--offline", "--json", *extra],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=full,
    )


def counts() -> tuple[int, int, int]:
    with state.open_state() as conn:
        return tuple(  # type: ignore[return-value]
            conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("tasks", "sessions", "worktrees")
        )


def in_process_open(task_id: str, adapter: MockTerminalAdapter):
    with state.open_state() as conn:
        return open_task(
            conn, task_id, config=load_config(), adapter=adapter, prompter=None, agent="claude", offline=True
        )


# --- safety test 10: concurrent opens of one task ----------------------------------------------------------


def test_concurrent_opens_of_one_task_make_one_session(repo, launcher_home):
    task_id = new_task(repo)
    procs = [spawn_open(task_id, delay=1.0) for _ in range(2)]
    outs = [p.communicate(timeout=60) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    actions = sorted(json.loads(out)["action"] for out, _ in outs)
    assert actions == ["created", "focused"]
    assert len(mock_calls(launcher_home)) == 1
    tasks, sessions, worktrees = counts()
    assert (tasks, sessions, worktrees) == (1, 1, 1)
    assert len(git.list_worktrees(str(repo))) == 2  # the main checkout and the task's
    with state.open_state() as conn:
        assert get_task(conn, task_id).state == "active"
        assert conn.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == 0
    sessions_seen = {json.loads(out)["session"]["id"] for out, _ in outs}
    assert len(sessions_seen) == 1


def test_different_tasks_launch_concurrently(repo, launcher_home):
    first, second = new_task(repo, "One"), new_task(repo, "Two")
    # A launch of one task holds its lock; the other task is not held up by it.
    with locks.task_lock(first):
        started = time.monotonic()
        other = spawn_open(second)
        out, err = other.communicate(timeout=60)
        assert other.returncode == 0, err
        assert time.monotonic() - started < locks.LOCK_WAIT_SECONDS
    assert json.loads(out)["action"] == "created"
    # And two launches that overlap in time both complete, with their own terminals and worktrees.
    third, fourth = new_task(repo, "Three"), new_task(repo, "Four")
    procs = [spawn_open(t, delay=1.0) for t in (third, fourth)]
    assert [p.wait(timeout=60) for p in procs] == [0, 0]
    _, sessions, worktrees = counts()
    assert (sessions, worktrees) == (3, 3)
    names = {c["session"]["workspace_id"] for c in mock_calls(launcher_home)}
    assert len(names) == 3


# --- locks ---------------------------------------------------------------------------------------------------


def test_a_held_lock_makes_a_second_launch_wait_then_give_up(tmp_path):
    with locks.task_lock("t-aaaaaaaa"):
        with pytest.raises(LauncherError) as busy:
            with locks.task_lock("t-aaaaaaaa", wait=0.2):
                pass
        assert busy.value.code == "task_busy"
        with locks.task_lock("t-bbbbbbbb", wait=0.2):  # another task is free
            pass
    with locks.task_lock("t-aaaaaaaa", wait=0.2):  # and released afterwards
        pass


def test_a_lock_held_by_a_killed_process_is_recovered(tmp_path):
    script = textwrap.dedent(
        """
        import sys, time
        from agent_launcher.locks import task_lock
        with task_lock("t-crashed1"):
            print("held", flush=True)
            time.sleep(60)
        """
    )
    holder = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(LauncherError):
            with locks.task_lock("t-crashed1", wait=0.2):
                pass
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=10)
        with locks.task_lock("t-crashed1", wait=5):
            pass
    finally:
        holder.kill()
        holder.wait()


# --- safety test 9: terminal failure after the worktree exists ---------------------------------------------


def test_terminal_failure_keeps_the_worktree_and_retry_reuses_it(repo, launcher_home):
    task_id = new_task(repo)
    set_mock(launcher_home, fail_create="cmux is down")
    failed = run("open", task_id, "--terminal", "mock", "--offline", "--json")
    assert failed.exit_code != 0
    assert json.loads(failed.stdout)["error"]["code"] == "terminal_create_failed"
    with state.open_state() as conn:
        tree = get_worktree(conn, task_id)
        assert tree is not None and tree.ownership == "created" and Path(tree.path).is_dir()
        assert get_task(conn, task_id).state == "launch_failed"  # never "active" without a session
        assert primary_session(conn, task_id) is None
        launch = launches.get_launch(conn, task_id)
        assert launch is not None and launch.stage == launches.WORKTREE_READY
    assert counts()[1:] == (0, 1)

    set_mock(launcher_home, fail_create=None)
    ok = run("open", task_id, "--terminal", "mock", "--offline", "--json")
    assert ok.exit_code == 0, ok.output
    result = json.loads(ok.stdout)
    assert result["action"] == "created" and result["worktree"]["path"] == tree.path
    assert counts() == (1, 1, 1)
    assert len(mock_calls(launcher_home)) == 1
    assert len(git.list_worktrees(str(repo))) == 2
    with state.open_state() as conn:
        assert get_task(conn, task_id).state == "active"
        assert launches.get_launch(conn, task_id) is None


# --- orphaned terminal: record_session fails after create_session ------------------------------------------


def break_recording(monkeypatch, times=1):
    from agent_launcher import launch

    real = launch.record_session
    left = {"n": times}

    def flaky(*args, **kwargs):
        if left["n"] > 0:
            left["n"] -= 1
            raise RuntimeError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(launch, "record_session", flaky)


def test_record_failure_closes_the_terminal_it_created(repo, launcher_home, monkeypatch):
    task_id = new_task(repo)
    adapter = MockTerminalAdapter(mock_file(launcher_home))
    break_recording(monkeypatch)
    with pytest.raises(LauncherError) as err:
        in_process_open(task_id, adapter)
    assert err.value.code == "launch_failed"
    closed = mock_calls(launcher_home, "close_session")
    assert [c["session"]["workspace_id"] for c in closed] == ["mock-workspace-1"]
    with state.open_state() as conn:
        tree = get_worktree(conn, task_id)
        assert tree is not None and Path(tree.path).is_dir()  # the worktree is kept
        assert launches.get_launch(conn, task_id).terminal is None
    result = in_process_open(task_id, adapter)
    assert result.action == "created" and result.session.terminal.workspace_id == "mock-workspace-2"
    assert counts() == (1, 1, 1)


def test_record_failure_with_an_unclosable_terminal_keeps_it_and_retry_reuses_it(repo, launcher_home, monkeypatch):
    task_id = new_task(repo)

    class Stubborn(MockTerminalAdapter):
        def close_session(self, session):
            raise TerminalError("close_refused", "cannot close", adapter=self.name)

    adapter = Stubborn(mock_file(launcher_home))
    break_recording(monkeypatch)
    with pytest.raises(LauncherError) as err:
        in_process_open(task_id, adapter)
    assert err.value.code == "launch_incomplete"
    assert "mock-workspace-1" in err.value.message
    with state.open_state() as conn:
        launch = launches.get_launch(conn, task_id)
        assert launch.stage == launches.TERMINAL_CREATED and launch.terminal.workspace_id == "mock-workspace-1"
    result = in_process_open(task_id, MockTerminalAdapter(mock_file(launcher_home)))
    assert result.session.terminal.workspace_id == "mock-workspace-1"
    assert result.session.agent_conversation_id == launch.conversation_id
    assert len(mock_calls(launcher_home)) == 1  # no second terminal session
    assert counts() == (1, 1, 1)


def test_rollback_never_closes_a_terminal_it_did_not_create(repo, launcher_home):
    from agent_launcher.launch import _roll_back_terminal

    task_id = new_task(repo)
    adapter = MockTerminalAdapter(mock_file(launcher_home))
    foreign = TerminalSessionRef("mock", "someone-elses", "surface", created_by_launcher=False)
    with state.open_state() as conn:
        error = _roll_back_terminal(conn, adapter, get_task(conn, task_id), foreign, RuntimeError("x"))
    assert error.code == "launch_incomplete"
    assert mock_calls(launcher_home, "close_session") == []


def test_a_vanished_recorded_terminal_is_replaced_not_reused(repo, launcher_home):
    task_id = new_task(repo)
    with state.open_state() as conn:
        task = get_task(conn, task_id)
        launches.begin(conn, task_id)
        ensure_worktree(conn, task, load_config(), None, offline=True)
        launches.note_worktree_ready(conn, task_id)
        launches.note_agent(conn, task_id, "claude", "old-conversation")
        launches.note_terminal(conn, task_id, TerminalSessionRef("mock", "gone-ws", "s", True))
    set_mock(launcher_home, gone=["gone-ws"])
    result = in_process_open(task_id, MockTerminalAdapter(mock_file(launcher_home)))
    assert result.session.terminal.workspace_id == "mock-workspace-1"
    assert result.session.agent_conversation_id != "old-conversation"


# --- crash orphan: worktree added, never recorded ------------------------------------------------------------


def test_worktree_left_by_a_crashed_launch_is_recovered_not_duplicated(repo, launcher_home):
    task_id = new_task(repo)
    config = load_config()
    with state.open_state() as conn:
        task = get_task(conn, task_id)
        launches.begin(conn, task_id)
        path = worktree_path_for(task, config)
        branch = f"task/{task_id}-fix-login"
        launches.note_worktree_intent(conn, task_id, str(path), branch, "refs/heads/main")
    path.parent.mkdir(parents=True, exist_ok=True)
    git.add_worktree(str(repo), path, branch, "refs/heads/main")  # ... and the process dies here
    with state.open_state() as conn:
        assert get_worktree(conn, task_id) is None

    result = in_process_open(task_id, MockTerminalAdapter(mock_file(launcher_home)))
    assert result.worktree is not None and result.worktree.ownership == "created"
    assert result.worktree.path == git.canonical(path)
    assert "Recovered" in (result.notice or "")
    assert len(git.list_worktrees(str(repo))) == 2


def test_an_unrelated_worktree_at_the_path_is_left_alone(repo, launcher_home):
    task_id = new_task(repo)
    config = load_config()
    with state.open_state() as conn:
        task = get_task(conn, task_id)
        launches.begin(conn, task_id)
        path = worktree_path_for(task, config)
        launches.note_worktree_intent(conn, task_id, str(path), f"task/{task_id}-fix-login", "refs/heads/main")
    path.mkdir(parents=True)  # something else is there, and git knows nothing of it
    (path / "mine.txt").write_text("keep")
    with pytest.raises(LauncherError) as err:
        in_process_open(task_id, MockTerminalAdapter(mock_file(launcher_home)))
    assert err.value.code == "worktree_path_exists"
    assert (path / "mine.txt").read_text() == "keep"
    assert mock_calls(launcher_home) == []


# --- leftover from #10 -----------------------------------------------------------------------------------------


def test_restart_reports_a_missing_worktree_before_asking_for_confirmation(repo, launcher_home):
    task_id = new_task(repo)
    result = in_process_open(task_id, MockTerminalAdapter(mock_file(launcher_home)))
    shutil.rmtree(result.worktree.path)
    with state.open_state() as conn:
        with pytest.raises(LauncherError) as err:
            restart_task(
                conn, task_id, config=load_config(), adapter=MockTerminalAdapter(mock_file(launcher_home)),
                prompter=None, confirmed=False, offline=True,
            )
    assert err.value.code == "worktree_missing"


def test_forced_resume_reports_a_missing_worktree_before_asking_for_confirmation(repo, launcher_home):
    task_id = new_task(repo)
    adapter = MockTerminalAdapter(mock_file(launcher_home))
    shutil.rmtree(in_process_open(task_id, adapter).worktree.path)
    with state.open_state() as conn:
        with pytest.raises(LauncherError) as err:
            open_task(
                conn, task_id, config=load_config(), adapter=adapter, prompter=None, offline=True,
                force_resume=True, confirmed=False,
            )
    assert err.value.code == "worktree_missing"


# --- review follow-ups -------------------------------------------------------------------------------------------


def test_task_is_launching_not_active_while_the_terminal_session_is_created(repo, launcher_home):
    task_id = new_task(repo)
    seen = {}

    class Peeking(MockTerminalAdapter):
        def create_session(self, request):
            with state.open_state() as conn:
                seen["state"] = get_task(conn, task_id).state
                seen["sessions"] = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                seen["stage"] = launches.get_launch(conn, task_id).stage
            return super().create_session(request)

    in_process_open(task_id, Peeking(mock_file(launcher_home)))
    assert seen == {"state": "launching", "sessions": 0, "stage": launches.WORKTREE_READY}


def test_ctrl_c_during_a_launch_marks_it_failed(repo, launcher_home):
    task_id = new_task(repo)

    class Interrupted(MockTerminalAdapter):
        def create_session(self, request):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        in_process_open(task_id, Interrupted(mock_file(launcher_home)))
    with state.open_state() as conn:
        assert get_task(conn, task_id).state == "launch_failed"


def prepare_recorded_terminal(task_id: str, ref: TerminalSessionRef) -> None:
    with state.open_state() as conn:
        launches.begin(conn, task_id)
        ensure_worktree(conn, get_task(conn, task_id), load_config(), None, offline=True)
        launches.note_worktree_ready(conn, task_id)
        launches.note_agent(conn, task_id, "claude", "old-conversation")
        launches.note_terminal(conn, task_id, ref)


def test_a_recorded_terminal_of_another_adapter_is_named_not_silently_forgotten(repo, launcher_home):
    task_id = new_task(repo)
    prepare_recorded_terminal(task_id, TerminalSessionRef("cmux", "workspace:7", "surface:9", True))
    result = in_process_open(task_id, MockTerminalAdapter(mock_file(launcher_home)))
    assert "cmux workspace workspace:7" in result.notice and "close it yourself" in result.notice
    assert mock_calls(launcher_home, "close_session") == []


def test_a_recorded_terminal_that_cannot_be_checked_stops_the_launch_and_keeps_the_record(repo, launcher_home):
    task_id = new_task(repo)
    prepare_recorded_terminal(task_id, TerminalSessionRef("mock", "odd-ws", "surface", True))

    class Unreadable(MockTerminalAdapter):
        def read_screen(self, session):
            raise TerminalError("cmux_failed", "timed out", adapter=self.name)

    with pytest.raises(LauncherError) as err:
        in_process_open(task_id, Unreadable(mock_file(launcher_home)))
    assert err.value.code == "terminal_check_failed"
    assert "mock workspace odd-ws" in err.value.message and "may still be running" in err.value.message
    assert mock_calls(launcher_home) == []  # no second agent
    with state.open_state() as conn:
        assert launches.get_launch(conn, task_id).terminal.workspace_id == "odd-ws"
        assert get_task(conn, task_id).state == "launch_failed"
    # Once the terminal can be checked again, a definitive "gone" lets the retry proceed.
    set_mock(launcher_home, gone=["odd-ws"])
    result = in_process_open(task_id, MockTerminalAdapter(mock_file(launcher_home)))
    assert result.session.terminal.workspace_id == "mock-workspace-1"


def test_rollback_messages_tell_closed_from_kept_and_recorded_from_unrecorded(repo, launcher_home):
    from agent_launcher.launch import _roll_back_terminal

    task_id = new_task(repo)
    ours = TerminalSessionRef("mock", "ws", "surface", True)

    class Stubborn(MockTerminalAdapter):
        def close_session(self, session):
            raise TerminalError("close_refused", "no", adapter=self.name)

    with state.open_state() as conn:
        task = get_task(conn, task_id)
        launches.begin(conn, task_id)
        kept = _roll_back_terminal(conn, Stubborn(), task, ours, RuntimeError("x"), recorded=True)
        loose = _roll_back_terminal(conn, Stubborn(), task, ours, RuntimeError("x"), recorded=False)
        conn.execute("DROP TABLE launches")  # recording the forgotten terminal now fails after a good close
        closed = _roll_back_terminal(conn, MockTerminalAdapter(mock_file(launcher_home)), task, ours, RuntimeError("x"))
    assert kept.code == loose.code == "launch_incomplete"
    assert "could not be closed" in kept.message and "reuses it" in kept.message
    assert "creates another" in loose.message and "reuses" not in loose.message
    assert closed.code == "launch_failed" and "was closed" in closed.message
