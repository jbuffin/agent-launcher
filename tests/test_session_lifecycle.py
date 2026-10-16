"""Reopen and resume (ticket #9, ADR 0022), through the CLI on the recording session host."""

import json
import os

import pytest
from typer.testing import CliRunner

from commodore_launcher import state
from commodore_launcher.cli import app
from commodore_launcher.errors import LauncherError
from commodore_launcher.sessions import record_session, sessions_for_task
from commodore_launcher.locks import claim_session
from conftest import launch_data, make_github_task

runner = CliRunner()


def run(*args):
    return runner.invoke(app, list(args))


def data(result):
    return launch_data(result)


def code(result):
    assert result.exit_code == 1, result.output
    return json.loads(result.stdout)["error"]["code"]


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
                "version": 4,
                "harness_selection": "use_default",
                "profiles": {"work": {"default_harness": "claude", "harnesses": agents}},
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
    """A GitHub issue task for the repository, as `open <url>` leaves it; `open <id> --offline` launches it."""
    return make_github_task(repo)


def open_(task, *extra):
    return data(run("open", task["id"], "--offline", *extra))


# --- reopen ---------------------------------------------------------------------------


def test_second_open_resumes_the_saved_conversation_here(configure, task, host):
    first = open_(task)
    conversation = first["session"]["harness_conversation_id"]
    # The picker would have to ask now; a reopen never asks.
    configure(harness_selection="always_ask")
    second = open_(task)
    assert second["action"] == "resumed" and second["notice"] is None
    assert second["session"]["id"] == first["session"]["id"]
    assert second["session"]["harness_conversation_id"] == conversation
    assert second["prompt"] is None  # a resume builds and enters no prompt
    assert host.runs[1]["argv"][-2:] == ["--resume", conversation]
    assert host.runs[1]["cwd"] == first["task"]["repo_path"]
    with state.open_state() as conn:
        assert len(sessions_for_task(conn, task["id"])) == 1


def test_reopen_with_a_different_agent_is_refused(configure, task):
    open_(task)
    refused = run("open", task["id"], "--harness", "codex", "--offline", "--dry-run", "--json")
    assert code(refused) == "harness_mismatch"


def test_a_running_harness_is_never_started_twice(configure, task, host):
    open_(task)
    held = claim_session(task["id"])  # the harness of another terminal, still running
    assert held is not None
    try:
        refused = run("open", task["id"], "--offline")
        assert refused.exit_code == 1 and "already running" in refused.output and len(host.runs) == 1
        error = json.loads(run("open", task["id"], "--offline", "--dry-run", "--json").stdout)["error"]
        assert error["code"] == "harness_running" and error["pid"] == os.getpid()
        assert f"resume {task['id']} --force" in error["message"]
        assert code(run("resume", task["id"], "--offline", "--dry-run", "--json")) == "harness_running"
    finally:
        held.release()
    assert open_(task)["action"] == "resumed"  # the lock is gone with the harness


def test_the_session_lock_is_held_while_the_harness_runs(configure, task, host):
    seen = []
    real = host.run

    def run_(claim, command):
        seen.append(host.running(task["id"]) is not None)
        return real(claim, command)

    host.run = run_
    open_(task)
    assert seen == [True] and host.running(task["id"]) is None


def test_a_conversation_the_harness_never_saved_starts_fresh_on_the_same_session(configure, task, host):
    host.saves = False  # it exited before anything was submitted
    first = open_(task)
    again = open_(task)
    assert again["action"] == "created" and again["session"]["id"] == first["session"]["id"]
    assert "no saved conversation" in again["notice"] and "fresh one was started" in again["notice"]
    old, new = first["session"]["harness_conversation_id"], again["session"]["harness_conversation_id"]
    assert new != old and host.runs[1]["argv"][-4:] == ["--session-id", new, "--", again["prompt"]]
    with state.open_state() as conn:
        (only,) = sessions_for_task(conn, task["id"])
        assert only.agent_conversation_id == new


def test_resume_force_runs_a_second_process_only_when_confirmed(configure, task, host):
    open_(task)
    held = claim_session(task["id"])
    try:
        # A second process on a live conversation needs an explicit yes.
        assert code(run("resume", task["id"], "--force", "--offline", "--dry-run", "--json")) == "confirmation_required"
        assert run("resume", task["id"], "--force", "--offline").exit_code == 1 and len(host.runs) == 1
        forced = data(run("resume", task["id"], "--force", "--yes", "--offline"))
        assert forced["action"] == "resumed" and "not tracked" in forced["notice"]
        assert host.runs[-1]["claimed"] is False  # the lock belongs to the other process
        assert host.running(task["id"]) is not None
    finally:
        held.release()


def test_resume_of_an_unopened_task_says_so(configure, task):
    assert code(run("resume", task["id"], "--offline", "--dry-run", "--json")) == "no_session"


def test_agent_without_a_conversation_id_starts_a_fresh_conversation_on_the_same_session(configure, task, host):
    first = open_(task, "--harness", "codex")
    assert first["session"]["harness_conversation_id"] is None
    again = open_(task)
    assert again["action"] == "created" and again["session"]["id"] == first["session"]["id"]
    assert "cannot resume" in again["notice"] and again["prompt"] == f"$commodore {first['task']['url']}"
    assert len(host.runs) == 2
    with state.open_state() as conn:
        assert len(sessions_for_task(conn, task["id"])) == 1


def test_a_second_session_for_a_task_is_refused_by_the_registry(configure, task):
    with state.open_state() as conn:
        record_session(conn, task["id"], "work", "claude")
        with pytest.raises(LauncherError) as err:
            record_session(conn, task["id"], "work", "claude")
        assert err.value.code == "session_exists"
        assert len(sessions_for_task(conn, task["id"])) == 1


def test_reopen_resolves_the_agent_first_and_starts_nothing_when_it_cannot(configure, task, host):
    open_(task)
    configure(profiles={"work": {"default_harness": "claude", "harnesses": {}}})
    assert code(run("open", task["id"], "--offline", "--dry-run", "--json")) == "config_invalid"  # a default with no harness
    assert run("open", task["id"], "--offline").exit_code == 1 and len(host.runs) == 1


def test_resume_args_without_the_id_placeholder_are_refused(write_config, fake_agents, make_repo, task, host):
    open_(task)
    write_config(
        {
            "version": 4,
            "harness_selection": "use_default",
            "profiles": {
                "work": {
                    "default_harness": "claude",
                    "harnesses": {"claude": {"executable": str(fake_agents / "claude"), "resume_args": ["--resume"]}},
                }
            },
        }
    )
    assert code(run("open", task["id"], "--offline", "--dry-run", "--json")) == "resume_args_invalid"
    assert len(host.runs) == 1


# --- the CLI around the harness ---------------------------------------------------------------


def test_json_needs_dry_run(configure, task, host):
    for argv in (("open", task["id"]), ("resume", task["id"]), ("configure",)):
        assert code(run(*argv, "--json")) == "json_needs_dry_run"
    assert host.runs == []


def test_dry_run_shows_the_launch_and_records_nothing(configure, task, host):
    plan = json.loads(run("open", task["id"], "--offline", "--dry-run", "--json").stdout)
    assert plan["action"] == "created" and plan["session"] is None
    assert plan["prompt"] == f"/commodore {task['url']}" and plan["command"][-2:] == ["--", plan["prompt"]]
    assert "--session-id" in plan["command"] and plan["working_directory"] == task["repo_path"]
    assert host.runs == []
    with state.open_state() as conn:
        assert sessions_for_task(conn, task["id"]) == []
        assert conn.execute("SELECT state FROM tasks WHERE id = ?", (task["id"],)).fetchone()[0] == "created"
        assert conn.execute("SELECT count(*) FROM launches").fetchone()[0] == 0
    human = run("open", task["id"], "--offline", "--dry-run")
    assert human.exit_code == 0 and "Would start task" in human.stdout and "Nothing was started" in human.stdout
    opened = open_(task)
    resume = json.loads(run("open", task["id"], "--offline", "--dry-run", "--json").stdout)
    assert resume["action"] == "resumed" and resume["session"]["id"] == opened["session"]["id"]
    assert len(host.runs) == 1


def test_the_harness_exit_status_is_the_commands(configure, task, host):
    host.status = 3
    done = run("open", task["id"], "--offline")
    assert done.exit_code == 3
    assert "Starting task" in done.output and f"commodore open {task['id']}` resumes it" in done.output


def test_a_harness_that_cannot_start_leaves_a_session_the_next_open_starts_fresh(configure, task, host):
    host.fail = "No such file or directory"
    failed = run("open", task["id"], "--offline")
    assert failed.exit_code == 1 and "No such file or directory" in failed.output
    assert host.running(task["id"]) is None  # the lock went with the failed start
    host.fail = None
    again = open_(task)
    assert again["action"] == "created" and "no saved conversation" in again["notice"]
