"""Archiving and unarchiving a task (ADR 0018, kept by ADR 0021): metadata only.

Real git in temp repos, the recording session host and the fake `gh` of the GitHub tests. Completion detection and cleanup are
gone (issue #16): a closed item changes nothing but the stored copy of its GitHub state.
"""

import pytest

from commodore_launcher import github, state
from commodore_launcher.tasks import get_task
from conftest import make_github_task
from test_github_issues import URL, checkout, data, env, failure, open_url, refuse_url, run  # noqa: F401  (fixtures)
from test_github_pulls import PullGh


@pytest.fixture
def gh(fake_github, monkeypatch) -> PullGh:
    fake = PullGh(fake_github)
    monkeypatch.setattr(github, "run_gh", fake)
    return fake


def opened() -> dict:
    return data(open_url())["task"]


def archive(task_id):
    return data(run("tasks", "archive", task_id, "--json"))


def test_archive_is_metadata_only_and_unarchive_restores(env, checkout, gh, launcher_home):
    task = opened()
    with state.open_state() as conn:
        sessions = conn.execute("SELECT * FROM sessions").fetchall()
    archived = archive(task["id"])
    assert archived["changed"] is True and archived["task"]["state"] == "archived"
    assert checkout.is_dir()
    with state.open_state() as conn:
        assert conn.execute("SELECT * FROM sessions").fetchall() == sessions

    refusal = failure(run("open", task["id"], "--dry-run", "--json"))
    assert refusal["code"] == "task_archived" and f"tasks unarchive {task['id']}" in refusal["message"]
    assert archive(task["id"])["changed"] is False

    restored = data(run("tasks", "unarchive", task["id"], "--json"))
    assert restored["task"]["state"] == "active"
    assert data(run("open", task["id"]))["action"] == "resumed"
    assert failure(run("tasks", "unarchive", task["id"], "--json"))["code"] == "task_not_archived"


def test_archived_github_task_can_be_worked_again_by_url(env, checkout, gh):
    task = opened()
    archive(task["id"])
    assert failure(refuse_url())["code"] == "task_archived"
    data(run("tasks", "unarchive", task["id"], "--json"))
    assert data(open_url())["task"]["id"] == task["id"]


def test_unarchive_of_a_never_opened_task_returns_to_created(env, checkout, gh):
    task = make_github_task(checkout, "x")
    archive(task["id"])
    assert data(run("tasks", "unarchive", task["id"], "--json"))["task"]["state"] == "created"


def test_archived_tasks_are_not_refreshed(env, checkout, gh):
    task = opened()
    archive(task["id"])
    gh.issues[(101, 7)]["state"] = "closed"
    calls = len(gh.calls)
    assert failure(run("open", task["id"], "--dry-run", "--json"))["code"] == "task_archived"
    assert len(gh.calls) == calls


def test_open_offline_does_not_ask_github(env, checkout, gh):
    task = opened()
    calls = len(gh.calls)
    data(run("open", task["id"], "--offline"))
    assert len(gh.calls) == calls


def test_a_closed_issue_changes_nothing_but_the_stored_state(env, checkout, gh, launcher_home):
    task = opened()
    gh.issues[(101, 7)]["state"] = "closed"
    again = data(run("open", task["id"]))
    assert again["action"] == "resumed" and again["task"]["state"] == "active"
    assert "eligible_for_cleanup" not in again["task"] and "cleanup_eligible_at" not in again["task"]
    assert data(run("tasks", "show", task["id"], "--json"))["github"]["state"] == "closed"
    with state.open_state() as conn:
        assert get_task(conn, task["id"]).state == "active"
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id = ?", (task["id"],)).fetchone()[0] == 0
