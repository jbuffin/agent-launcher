"""Safe reassignment of a repository's profile (ticket #18, SPEC §6)."""

import json
import os

import pytest
from typer.testing import CliRunner

from agent_launcher import cli, state
from agent_launcher.cli import app
from scripted import CANCEL, ScriptedPrompter

runner = CliRunner()


def run(*args):
    return runner.invoke(app, list(args))


def data(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def error_of(result):
    assert result.exit_code == 1, result.output
    return json.loads(result.stdout)["error"]


@pytest.fixture
def fake_agents(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "codex"):
        exe = bin_dir / name
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    return bin_dir


@pytest.fixture(autouse=True)
def configure(write_config, fake_agents):
    agents = {n: {"executable": str(fake_agents / n)} for n in ("claude", "codex")}
    write_config(
        {
            "version": 2,
            "terminal": {"adapter": "mock"},
            "agent_selection": "use_default",
            "profiles": {
                "work": {"default_agent": "claude", "agents": agents},
                "personal": {"default_agent": "codex", "agents": {"codex": agents["codex"]}},
            },
        }
    )


@pytest.fixture
def repo(make_repo):
    path = make_repo("one")
    assert run("profile", "set", str(path), "work", "--offline").exit_code == 0
    return path


def new_task(repo, title="T"):
    return data(run("new", "--title", title, "--repo", str(repo), "--offline", "--json"))["task"]


def open_task(task_id):
    return data(run("open", task_id, "--offline", "--json"))


def rows(sql, *args):
    with state.open_state() as conn:
        return conn.execute(sql, args).fetchall()


@pytest.fixture
def opened(repo):
    """One task that was opened (session + worktree + conversation) and one that was only created."""
    first = new_task(repo, "opened")
    result = open_task(first["id"])
    return first, new_task(repo, "created only"), result


def test_non_interactive_without_a_resolution_lists_what_is_affected(repo, opened):
    first, second, result = opened
    error = error_of(run("profile", "set", str(repo), "personal", "--offline", "--json"))
    assert error["code"] == "reassignment_requires_resolution"
    assert error["current_profile"] == "work" and error["requested_profile"] == "personal"
    by_id = {t["task"]: t for t in error["affected_tasks"]}
    assert set(by_id) == {first["id"], second["id"]}
    session = by_id[first["id"]]["session"]
    assert session["id"] == result["session"]["id"] and session["profile"] == "work" and session["agent"] == "claude"
    assert "live" in session and session["terminal"]["adapter"] == "mock"
    assert by_id[first["id"]]["worktree"]["ownership"] == "created"
    assert by_id[first["id"]]["conversation_id"] == result["session"]["agent_conversation_id"]
    assert by_id[second["id"]]["session"] is None and by_id[second["id"]]["worktree"] is None
    # Nothing changed.
    assert rows("SELECT profile FROM profile_associations") == [("work",)]
    assert {r[0] for r in rows("SELECT state FROM tasks")} <= {"active", "created"}


def test_archive_tasks_archives_and_keeps_everything_else(repo, opened):
    first, second, result = opened
    worktree = rows("SELECT path FROM worktrees WHERE task_id = ?", first["id"])[0][0]
    sessions_before = rows("SELECT id, profile, state FROM sessions")
    out = data(run("profile", "set", str(repo), "personal", "--archive-tasks", "--offline", "--json"))
    assert out["changed"] and out["reassignment"] == "archive-tasks"
    assert set(out["affected_tasks"]) == {first["id"], second["id"]}
    assert rows("SELECT profile FROM profile_associations") == [("personal",)]
    assert {r[0] for r in rows("SELECT state FROM tasks")} == {"archived"}
    # Historical profiles are not rewritten, sessions and worktrees are not touched.
    assert {r[0] for r in rows("SELECT profile FROM tasks")} == {"work"}
    assert rows("SELECT id, profile, state FROM sessions") == sessions_before
    assert rows("SELECT path FROM worktrees WHERE task_id = ?", first["id"]) == [(worktree,)]
    assert os.path.isdir(worktree)
    # An archived task is not opened, resumed, prompted or restarted.
    for command in ("open", "resume", "prompt", "restart"):
        assert error_of(run(command, first["id"], "--offline", "--json"))["code"] == "task_archived"
    assert error_of(run("open", second["id"], "--offline", "--json"))["code"] == "task_archived"


def test_keep_tasks_leaves_them_unopenable_until_the_repository_goes_back(repo, opened):
    first, second, _ = opened
    out = data(run("profile", "set", str(repo), "personal", "--keep-tasks", "--offline", "--json"))
    assert out["reassignment"] == "keep-tasks"
    assert {r[0] for r in rows("SELECT profile FROM tasks")} == {"work"}
    assert {r[0] for r in rows("SELECT state FROM tasks")} != {"archived"}
    assert error_of(run("open", first["id"], "--offline", "--json"))["code"] == "profile_mismatch"
    assert error_of(run("open", second["id"], "--offline", "--json"))["code"] == "profile_mismatch"
    # Back to the original profile: nothing is affected any more, and the task opens again.
    back = data(run("profile", "set", str(repo), "work", "--offline", "--json"))
    assert back["changed"] and back["affected_tasks"] == []
    assert run("open", second["id"], "--offline", "--json").exit_code == 0


def test_cancel_changes_nothing(repo, opened):
    out = data(run("profile", "set", str(repo), "personal", "--cancel", "--offline", "--json"))
    assert out["cancelled"] and not out["changed"]
    assert rows("SELECT profile FROM profile_associations") == [("work",)]


def test_two_resolutions_are_a_conflict(repo, opened):
    error = error_of(run("profile", "set", str(repo), "personal", "--archive-tasks", "--keep-tasks", "--json"))
    assert error["code"] == "conflicting_resolution"
    assert rows("SELECT profile FROM profile_associations") == [("work",)]


def test_a_repository_without_tasks_needs_no_resolution(repo):
    out = data(run("profile", "set", str(repo), "personal", "--offline", "--json"))
    assert out["changed"] and out["affected_tasks"] == []


def test_archived_tasks_are_not_listed_again(repo, opened):
    run("profile", "set", str(repo), "personal", "--archive-tasks", "--offline")
    out = data(run("profile", "set", str(repo), "work", "--offline", "--json"))  # nothing left to resolve
    assert out["changed"] and out["affected_tasks"] == []


def test_the_old_session_is_never_reused_under_the_new_profile(repo, opened):
    first, _, result = opened
    run("profile", "set", str(repo), "personal", "--archive-tasks", "--offline")
    fresh = new_task(repo, "after")
    assert fresh["profile"] == "personal"
    data(run("open", fresh["id"], "--offline", "--json"))
    sessions = rows("SELECT id, task_id, profile, agent FROM sessions ORDER BY created_at, rowid")
    assert [(s[1], s[2]) for s in sessions] == [(first["id"], "work"), (fresh["id"], "personal")]
    assert sessions[0][0] == result["session"]["id"] and sessions[0][3] == "claude"


def test_interactive_lists_then_asks_once(repo, opened, monkeypatch):
    first, second, _ = opened
    prompter = ScriptedPrompter(("select", "What should happen", "archive-tasks"))
    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    monkeypatch.setattr(cli, "make_prompter", lambda: prompter)
    result = run("profile", "set", str(repo), "personal", "--offline")
    assert result.exit_code == 0, result.output
    prompter.done()
    listing = "\n".join(prompter.said)
    assert first["id"] in listing and second["id"] in listing and "worktree" in listing and "session" in listing
    assert {r[0] for r in rows("SELECT state FROM tasks")} == {"archived"}


def test_interactive_cancel_and_ctrl_c_change_nothing(repo, opened, monkeypatch):
    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    monkeypatch.setattr(cli, "make_prompter", lambda: ScriptedPrompter(("select", "What should happen", "cancel")))
    assert run("profile", "set", str(repo), "personal", "--offline").exit_code == 0
    monkeypatch.setattr(cli, "make_prompter", lambda: ScriptedPrompter(("select", "What should happen", CANCEL)))
    assert run("profile", "set", str(repo), "personal", "--offline").exit_code == 130
    assert rows("SELECT profile FROM profile_associations") == [("work",)]
    assert {r[0] for r in rows("SELECT state FROM tasks")} != {"archived"}


def test_same_profile_still_records_the_id_without_a_resolution(repo, opened):
    out = data(run("profile", "set", str(repo), "work", "--offline", "--json"))
    assert out["changed"] is False and out["affected_tasks"] == []


def test_force_is_gone(repo):
    assert run("profile", "set", str(repo), "personal", "--force", "--offline").exit_code == 2


def test_a_task_added_after_the_plan_refuses_with_reassignment_changed(repo, opened):
    from agent_launcher.associations import AssociationError, plan_reassignment, set_profile
    from agent_launcher.repositories import identify_reference

    identity = identify_reference(str(repo), fetch_github=False)
    with state.open_state() as conn:
        _, affected = plan_reassignment(conn, identity, "personal")
    planned = [t.task_id for t in affected]
    late = new_task(repo, "late")
    with state.open_state() as conn:
        with pytest.raises(AssociationError) as exc:
            set_profile(conn, identity, "personal", reassignment="archive-tasks", planned=planned)
    assert exc.value.code == "reassignment_changed" and late["id"] in {t["task"] for t in exc.value.details["affected_tasks"]}
    assert rows("SELECT profile FROM profile_associations") == [("work",)]
    assert "archived" not in {r[0] for r in rows("SELECT state FROM tasks")}


def test_no_terminal_is_probed_inside_the_transaction(repo, opened, monkeypatch):
    from agent_launcher import reassignment

    calls = []
    real = reassignment.affected_tasks

    def spy(conn, repository_id, new_profile, liveness=None):
        calls.append((conn.in_transaction, liveness is not None))
        return real(conn, repository_id, new_profile, liveness)

    monkeypatch.setattr(reassignment, "affected_tasks", spy)
    import agent_launcher.associations as associations

    monkeypatch.setattr(associations, "affected_tasks", spy)
    data(run("profile", "set", str(repo), "personal", "--archive-tasks", "--offline", "--json"))
    assert (False, True) in calls  # the plan probes, outside any transaction
    assert all(not probed for in_tx, probed in calls if in_tx)


def test_archived_message_says_what_to_do_next(repo, opened):
    first, _, _ = opened
    run("profile", "set", str(repo), "personal", "--archive-tasks", "--offline")
    message = error_of(run("open", first["id"], "--offline", "--json"))["message"]
    assert "no unarchive" in message and f"tasks show {first['id']}" in message
