"""`tasks link <task> <github-url>`: attach a GitHub issue or PR to an existing local task (ticket #17).

Same fakes as `test_github_issues`: a fake `gh`, real git in temp repos, the mock terminal.
"""

import json

import pytest

from agent_launcher import state
from agent_launcher.tasks import list_tasks
from test_github_issues import (  # noqa: F401  (fixtures)
    URL,
    FakeGh,
    checkout,
    data,
    env,
    failure,
    gh,
    mock_calls,
    open_url,
    run,
)


def new_task(checkout, title="Local work"):
    return data(run("new", "--title", title, "--repo", str(checkout), "--offline", "--json"))["task"]


def new_identified_task(checkout, title="Local work"):
    # Online (fake gh), so the repository has its GitHub ID.
    return data(run("new", "--title", title, "--repo", str(checkout), "--json"))["task"]


def link(task_id, url=URL):
    return run("tasks", "link", task_id, url, "--json")


def start(task_id):
    return data(run("open", task_id, "--terminal", "mock", "--json"))


def test_link_keeps_identity_worktree_session_and_conversation(env, checkout, gh, launcher_home):
    task = new_identified_task(checkout)
    opened = start(task["id"])
    with state.open_state() as conn:
        before_sessions = conn.execute("SELECT * FROM sessions").fetchall()
        before_trees = conn.execute("SELECT * FROM worktrees").fetchall()

    result = data(link(task["id"]))
    assert result["linked"] is True
    linked = result["task"]
    assert linked["id"] == task["id"] and linked["source"] == "github" and linked["url"] == URL
    assert linked["profile"] == task["profile"] and linked["workflow"] == opened["task"]["workflow"]
    assert linked["title"] == task["title"]  # the local title is the engineer's; not overwritten
    assert result["github"]["node_id"] == "I_9007" and result["github"]["repository_github_id"] == 101

    with state.open_state() as conn:
        assert conn.execute("SELECT * FROM sessions").fetchall() == before_sessions
        assert conn.execute("SELECT * FROM worktrees").fetchall() == before_trees
        assert len(list_tasks(conn)) == 1
    assert len(mock_calls(launcher_home)) == 1  # linking launches nothing

    shown = data(run("tasks", "show", task["id"], "--json"))
    assert shown["github"]["number"] == 7 and shown["task"]["source"] == "github"
    assert shown["worktree"] == opened["worktree"]


def test_open_url_after_link_focuses_the_linked_task(env, checkout, gh, launcher_home):
    task = new_identified_task(checkout)
    first = start(task["id"])
    data(link(task["id"]))
    again = data(open_url())
    assert again["task"]["id"] == task["id"] and again["action"] == "focused"
    assert again["session"]["id"] == first["session"]["id"]
    assert len(mock_calls(launcher_home)) == 1
    with state.open_state() as conn:
        assert len(list_tasks(conn)) == 1
        assert conn.execute("SELECT count(*) FROM worktrees").fetchone()[0] == 1


def test_workflows_test_reflects_the_github_identity_without_rerouting(env, checkout, gh):
    task = new_identified_task(checkout)
    start(task["id"])
    with state.open_state() as conn:
        stored = conn.execute("SELECT workflow FROM tasks WHERE id = ?", (task["id"],)).fetchone()[0]
    data(link(task["id"]))
    explained = data(run("workflows", "test", task["id"], "--json"))
    assert explained["task"]["number"] == 7 and explained["task"]["repository"] == "acme/widgets"
    with state.open_state() as conn:
        assert conn.execute("SELECT workflow FROM tasks WHERE id = ?", (task["id"],)).fetchone()[0] == stored


def test_relinking_the_same_item_is_a_noop(env, checkout, gh):
    task = new_identified_task(checkout)
    data(link(task["id"]))
    with state.open_state() as conn:
        before = conn.execute("SELECT * FROM task_github").fetchall()
        updated = conn.execute("SELECT updated_at FROM tasks").fetchone()
    again = data(link(task["id"]))
    assert again["linked"] is False
    with state.open_state() as conn:
        assert conn.execute("SELECT * FROM task_github").fetchall() == before
        assert conn.execute("SELECT updated_at FROM tasks").fetchone() == updated


def test_identity_taken_by_another_task_names_it_and_offers_options(env, checkout, gh):
    owner = data(open_url())["task"]
    local = new_identified_task(checkout)
    error = failure(link(local["id"]))
    assert error["code"] == "github_identity_taken" and error["owner_task"] == owner["id"]
    assert owner["id"] in error["message"] and "Merging" in error["message"]
    assert error["options"]
    with state.open_state() as conn:
        assert conn.execute("SELECT count(*) FROM task_github").fetchone()[0] == 1
        assert conn.execute("SELECT source FROM tasks WHERE id = ?", (local["id"],)).fetchone()[0] == "local"


def test_other_repository_is_refused_by_id(env, checkout, gh, make_repo, tmp_path):
    gh.add_repo("acme/other", 202)
    gh.add_issue("acme/other", 3, db_id=9100)
    task = new_identified_task(checkout)
    error = failure(link(task["id"], "https://github.com/acme/other/issues/3"))
    assert error["code"] == "github_repository_mismatch"
    with state.open_state() as conn:
        assert conn.execute("SELECT count(*) FROM task_github").fetchone()[0] == 0


def test_a_renamed_repository_is_still_the_same_repository(env, checkout, gh):
    task = new_identified_task(checkout)
    gh.rename("acme/widgets", "acme/gadgets")
    gh.add_issue("acme/gadgets", 8, db_id=9008)
    assert data(link(task["id"], "https://github.com/acme/gadgets/issues/8"))["linked"] is True


def _forget_repository_id():
    with state.open_state() as conn:  # a repository stored before its GitHub ID was known
        conn.execute("UPDATE repositories SET github_id = NULL")
        conn.commit()


def test_offline_task_is_refused_with_a_hint_and_linked_after_profile_set_online(env, checkout, gh):
    task = new_task(checkout)
    _forget_repository_id()
    error = failure(link(task["id"]))
    assert error["code"] == "github_repository_mismatch" and "no GitHub repository ID" in error["message"]
    assert f"agent-launcher profile set {checkout.resolve()} work" in error["message"]
    # Nothing recorded the ID by itself: not even the failed link.
    with state.open_state() as conn:
        assert conn.execute("SELECT github_id FROM repositories").fetchall() == [(None,)]
    # The explicit command, online, with the same profile, records it on the same row.
    assert run("profile", "set", str(checkout.resolve()), "work").exit_code == 0
    with state.open_state() as conn:
        assert conn.execute("SELECT id, github_id FROM repositories").fetchall() == [(1, 101)]
        assert conn.execute("SELECT repository_id FROM tasks").fetchall() == [(1,)]
    assert data(link(task["id"]))["linked"] is True


def _add_second_profile(launcher_home):
    path = launcher_home / "config.json"
    config = json.loads(path.read_text())
    config["profiles"]["personal"] = config["profiles"]["work"]
    path.write_text(json.dumps(config))


def test_profile_set_with_another_profile_refuses_and_records_nothing(env, checkout, gh, launcher_home):
    new_task(checkout)
    _forget_repository_id()
    _add_second_profile(launcher_home)  # so the command gets as far as set_profile
    error = failure(run("profile", "set", str(checkout.resolve()), "personal", "--json"))
    assert error["code"] == "association_exists"
    with state.open_state() as conn:
        assert conn.execute("SELECT github_id FROM repositories").fetchall() == [(None,)]


def test_profile_set_says_exactly_what_it_recorded(env, checkout, gh):
    new_task(checkout)
    _forget_repository_id()
    path = str(checkout.resolve())
    out = run("profile", "set", path, "work")
    assert f"Recorded GitHub repository acme/widgets (ID 101) for {path}." in out.output
    _forget_repository_id()
    as_json = data(run("profile", "set", path, "work", "--json"))
    assert as_json["recorded_github_id"] == {"full_name": "acme/widgets", "id": 101, "path": path}
    assert data(run("profile", "set", path, "work", "--json"))["recorded_github_id"] is None  # already has it


def test_force_changes_the_profile_and_records_the_id_together(env, checkout, gh, launcher_home):
    new_task(checkout)
    _forget_repository_id()
    _add_second_profile(launcher_home)
    out = data(run("profile", "set", str(checkout.resolve()), "personal", "--force", "--json"))
    assert out["recorded_github_id"]["id"] == 101 and out["previous_profile"] == "work"


def test_local_title_is_kept_at_link_time_and_replaced_by_githubs_on_the_next_open(env, checkout, gh):
    task = new_identified_task(checkout, "My own words")
    assert data(link(task["id"]))["task"]["title"] == "My own words"
    assert data(open_url())["task"]["title"] != "My own words"  # the refresh on `open <url>` takes GitHub's title


def test_profile_mismatch_is_refused(env, checkout, gh):
    # Forced by hand: no command moves a task to another profile, but a repository reassigned after the task was
    # made (ticket #18) would look exactly like this. Linking must not paper over it.
    task = new_identified_task(checkout)
    with state.open_state() as conn:
        conn.execute("UPDATE tasks SET profile = 'personal' WHERE id = ?", (task["id"],))
        conn.commit()
    error = failure(link(task["id"]))
    assert error["code"] == "github_profile_mismatch"
    assert error["task_profile"] == "personal" and error["repository_profile"] == "work"
    with state.open_state() as conn:
        assert conn.execute("SELECT count(*) FROM task_github").fetchone()[0] == 0
        row = conn.execute("SELECT profile, agent, source FROM tasks WHERE id = ?", (task["id"],)).fetchone()
        assert row == ("personal", task["agent"], "local")  # neither profile nor agent was touched


def test_task_linked_to_a_different_item_is_refused(env, checkout, gh):
    gh.add_issue("acme/widgets", 8, db_id=9008)
    task = new_identified_task(checkout)
    data(link(task["id"]))
    error = failure(link(task["id"], "https://github.com/acme/widgets/issues/8"))
    assert error["code"] == "task_already_linked" and error["linked_url"] == URL
    with state.open_state() as conn:
        assert conn.execute("SELECT number FROM task_github").fetchall() == [(7,)]


def test_unknown_task_and_missing_item(env, checkout, gh):
    assert failure(link("t-nope"))["code"] == "task_not_found"
    task = new_identified_task(checkout)
    assert failure(link(task["id"], "https://github.com/acme/widgets/issues/99"))["code"] == "not_found"


def test_busy_issue_lock_changes_nothing(env, checkout, gh, monkeypatch):
    from agent_launcher import locks

    monkeypatch.setattr(locks, "LOCK_WAIT_SECONDS", 0.1)
    task = new_identified_task(checkout)
    with locks.task_lock("issue-9007"):
        error = failure(link(task["id"]))
    assert error["code"] == "task_busy"
    with state.open_state() as conn:
        assert conn.execute("SELECT count(*) FROM task_github").fetchone()[0] == 0


def test_text_output(env, checkout, gh):
    task = new_identified_task(checkout)
    out = run("tasks", "link", task["id"], URL)
    assert out.exit_code == 0 and f"Linked task {task['id']} to {URL}" in out.output
