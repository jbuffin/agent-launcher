"""Transactional launches (ticket #11, ADR 0022): per-task locks, the session lock, retries.

The concurrency tests run real `commodore open` processes on the real session host, with a fake harness that saves its
conversation, logs its argv and sleeps; the failure tests run in-process.
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from commodore_launcher import locks, state
from commodore_launcher.cli import app
from commodore_launcher.config import load_config
from commodore_launcher.errors import LauncherError
from commodore_launcher.launch import open_task
from commodore_launcher.session_host import ForegroundHost, HarnessCommand
from commodore_launcher.sessions import primary_session
from commodore_launcher.tasks import get_task
from conftest import RecordingHost, launched, make_github_task

runner = CliRunner()

FAKE_HARNESS = """#!{python}
import json, os, pathlib, sys, time
args = sys.argv[1:]
if "--session-id" in args:  # saved where Claude Code saves it, as once its prompt is submitted
    saved = pathlib.Path(os.environ["HOME"]) / ".claude" / "projects" / "-fake"
    saved.mkdir(parents=True, exist_ok=True)
    (saved / (args[args.index("--session-id") + 1] + ".jsonl")).write_text("{{}}\\n")
with open(os.environ["FAKE_HARNESS_LOG"], "a") as log:
    log.write(json.dumps({{"pid": os.getpid(), "argv": args, "cwd": os.getcwd()}}) + "\\n")
time.sleep(float(os.environ.get("FAKE_HARNESS_SECONDS", "0")))
sys.exit(int(os.environ.get("FAKE_HARNESS_STATUS", "0")))
"""


def run(*args):
    return runner.invoke(app, list(args))


@pytest.fixture
def harness_log(tmp_path, monkeypatch) -> Path:
    log = tmp_path / "harness.log"
    monkeypatch.setenv("FAKE_HARNESS_LOG", str(log))
    return log


@pytest.fixture
def configure(write_config, tmp_path, harness_log):
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text(FAKE_HARNESS.format(python=sys.executable))
    exe.chmod(0o755)
    write_config(
        {
            "version": 4,
            "harness_selection": "use_default",
            "profiles": {"work": {"default_harness": "claude", "harnesses": {"claude": {"executable": str(exe)}}}},
        }
    )


@pytest.fixture
def repo(configure, make_repo):
    path = make_repo("one")
    assert run("profile", "set", str(path), "work", "--offline").exit_code == 0
    return path


def new_task(repo: Path, title: str = "Fix login") -> str:
    return make_github_task(repo, title)["id"]


def harness_runs(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def spawn_open(task_id: str, *extra: str, seconds: float = 0.0) -> subprocess.Popen:
    env = {**os.environ, "FAKE_HARNESS_SECONDS": str(seconds)}
    return subprocess.Popen(
        [sys.executable, "-m", "commodore_launcher", "open", task_id, "--offline", *extra],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, env=env,
    )


def wait_for(predicate, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def counts() -> tuple[int, int]:
    with state.open_state() as conn:
        return tuple(  # type: ignore[return-value]
            conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("tasks", "sessions")
        )


def in_process_open(task_id: str, host=None):
    host = host or RecordingHost()
    with state.open_state() as conn:
        result = open_task(conn, task_id, config=load_config(), host=host, prompter=None, agent="claude", offline=True)
    return launched(host, result)


# --- safety test 10: concurrent opens of one task ----------------------------------------------------------


def test_concurrent_opens_of_one_task_make_one_session_and_one_harness(repo, harness_log):
    task_id = new_task(repo)
    procs = [spawn_open(task_id, seconds=5) for _ in range(2)]
    outs = [p.communicate(timeout=60) for p in procs]
    assert sorted(p.returncode for p in procs) == [0, 1], outs
    refused = next(err for p, (_, err) in zip(procs, outs) if p.returncode == 1)
    assert "already running" in refused
    assert len(harness_runs(harness_log)) == 1
    assert counts() == (1, 1)
    with state.open_state() as conn:
        assert get_task(conn, task_id).state == "active"
        assert conn.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == 0


def test_different_tasks_launch_concurrently(repo, harness_log):
    first, second = new_task(repo, "One"), new_task(repo, "Two")
    # A launch of one task holds its lock; the other task is not held up by it.
    with locks.task_lock(first):
        started = time.monotonic()
        other = spawn_open(second)
        out, err = other.communicate(timeout=60)
        assert other.returncode == 0, err
        assert time.monotonic() - started < locks.LOCK_WAIT_SECONDS
    # And two harnesses of different tasks run at the same time.
    third, fourth = new_task(repo, "Three"), new_task(repo, "Four")
    procs = [spawn_open(t, seconds=2) for t in (third, fourth)]
    wait_for(lambda: len(harness_runs(harness_log)) == 3)
    assert locks.session_holder(third) is not None and locks.session_holder(fourth) is not None
    assert [p.wait(timeout=60) for p in procs] == [0, 0]
    assert counts()[1] == 3


# --- the session lock, on the real host ----------------------------------------------------------------------


def test_the_harness_keeps_the_session_lock_when_commodore_dies_first(repo, harness_log):
    task_id = new_task(repo)
    launcher = spawn_open(task_id, seconds=3)
    wait_for(lambda: harness_runs(harness_log))
    (harness,) = harness_runs(harness_log)
    assert locks.session_holder(task_id) == locks.SessionHolder(harness["pid"])
    launcher.send_signal(signal.SIGKILL)
    launcher.wait(timeout=10)
    held = locks.session_holder(task_id)
    assert held is not None and held.pid == harness["pid"]  # still running, in a terminal commodore left
    with state.open_state() as conn:
        with pytest.raises(LauncherError) as err:
            open_task(conn, task_id, config=load_config(), host=ForegroundHost(), prompter=None, offline=True)
    assert err.value.code == "harness_running" and str(harness["pid"]) in err.value.message
    wait_for(lambda: locks.session_holder(task_id) is None)


def test_ctrl_c_reaches_the_harness_not_commodore(repo, harness_log):
    task_id = new_task(repo)
    launcher = spawn_open(task_id, seconds=2)
    wait_for(lambda: harness_runs(harness_log))
    launcher.send_signal(signal.SIGINT)  # sent to commodore alone: it must keep waiting for its harness
    out, err = launcher.communicate(timeout=30)
    assert launcher.returncode == 0, err
    assert "The harness exited" in err


def test_the_harness_runs_in_the_checkout_with_the_prompt_on_its_command_line(repo, harness_log):
    task_id = new_task(repo)
    done = spawn_open(task_id)
    _, err = done.communicate(timeout=60)
    assert done.returncode == 0, err
    (harness,) = harness_runs(harness_log)
    assert Path(harness["cwd"]).resolve() == repo.resolve()
    assert harness["argv"][0] == "--session-id" and harness["argv"][-2] == "--"
    assert harness["argv"][-1].startswith("/commodore https://github.com/acme/widgets/issues/")
    again = spawn_open(task_id)
    assert again.wait(timeout=60) == 0
    assert harness_runs(harness_log)[1]["argv"] == ["--resume", harness["argv"][1]]


def test_a_harness_that_cannot_start_is_an_error_and_releases_the_lock(tmp_path):
    claim = locks.claim_session("t-nostart1")
    with pytest.raises(LauncherError) as err:
        ForegroundHost().run(claim, HarnessCommand((str(tmp_path / "missing"),), str(tmp_path), dict(os.environ)))
    assert err.value.code == "harness_start_failed"
    assert locks.session_holder("t-nostart1") is None


def test_the_exit_status_of_a_killed_harness_is_128_plus_the_signal(tmp_path):
    status = ForegroundHost().run(None, HarnessCommand(("/bin/sh", "-c", "kill -TERM $$"), str(tmp_path), dict(os.environ)))
    assert status == 128 + signal.SIGTERM


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
        from commodore_launcher.locks import task_lock
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


# --- a failure before the harness starts, then a retry ------------------------------------------------------


def break_recording(monkeypatch, error=RuntimeError("disk full"), times=1):
    from commodore_launcher import launch

    real = launch.record_session
    left = {"n": times}

    def flaky(*args, **kwargs):
        if left["n"] > 0:
            left["n"] -= 1
            raise error
        return real(*args, **kwargs)

    monkeypatch.setattr(launch, "record_session", flaky)


def test_a_failure_before_the_session_is_recorded_starts_nothing_and_a_retry_launches(repo, monkeypatch):
    task_id = new_task(repo)
    host = RecordingHost()
    break_recording(monkeypatch)
    with pytest.raises(RuntimeError):
        in_process_open(task_id, host)
    assert host.runs == []
    with state.open_state() as conn:
        assert get_task(conn, task_id).state == "launch_failed"  # never "active" without a session
        assert primary_session(conn, task_id) is None
        assert conn.execute("SELECT agent FROM launches WHERE task_id = ?", (task_id,)).fetchone() == ("claude",)
    assert locks.session_holder(task_id) is None  # the session lock was let go
    result = in_process_open(task_id, host)
    assert result.action == "created" and counts() == (1, 1) and len(host.runs) == 1
    with state.open_state() as conn:
        assert get_task(conn, task_id).state == "active"
        assert conn.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == 0


def test_task_is_launching_not_active_until_the_session_is_recorded(repo, monkeypatch):
    from commodore_launcher import launch

    task_id = new_task(repo)
    seen = {}
    real = launch.record_session

    def peek(conn, *args, **kwargs):
        seen["state"] = get_task(conn, task_id).state
        seen["sessions"] = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        seen["held"] = locks.session_holder(task_id) is not None
        return real(conn, *args, **kwargs)

    monkeypatch.setattr(launch, "record_session", peek)
    in_process_open(task_id)
    assert seen == {"state": "launching", "sessions": 0, "held": True}


def test_ctrl_c_during_a_launch_marks_it_failed(repo, monkeypatch):
    task_id = new_task(repo)
    break_recording(monkeypatch, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        in_process_open(task_id)
    with state.open_state() as conn:
        assert get_task(conn, task_id).state == "launch_failed"
    assert locks.session_holder(task_id) is None
