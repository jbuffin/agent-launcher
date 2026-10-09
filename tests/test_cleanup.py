"""Completion detection, archiving and conservative cleanup (ticket #22).

Real git in temp repos for every refusal reason, the mock terminal for sessions, and the fake `gh` of the GitHub tests.
"""

import json
import subprocess
from pathlib import Path

import pytest

from agent_launcher import cleanup, cli, git, github, state
from agent_launcher.tasks import get_task
from conftest import git_run
from scripted import ScriptedPrompter
from test_github_issues import URL, checkout, data, env, failure, mock_calls, open_url, run  # noqa: F401  (fixtures)
from test_github_pulls import PR_URL, PullGh


@pytest.fixture
def gh(fake_github, monkeypatch) -> PullGh:
    fake = PullGh(fake_github)
    monkeypatch.setattr(github, "run_gh", fake)
    monkeypatch.setattr(git, "fetch_branch", lambda repo, branch: None)
    return fake


REAL_LSOF = cleanup.lsof_check


@pytest.fixture(autouse=True)
def no_process(monkeypatch):
    """By default nothing uses a worktree; tests override this."""
    monkeypatch.setattr(cleanup, "lsof_check", lambda path: False)


def close_issue(gh, state_="closed"):
    gh.issues[(101, 7)]["state"] = state_


def task_row(task_id):
    with state.open_state() as conn:
        return get_task(conn, task_id)


def opened(gh_url=URL):
    result = data(open_url(gh_url))
    return result["task"], result["worktree"]


def archive(task_id):
    return data(run("tasks", "archive", task_id, "--json"))


def cleanup_json(*args):
    return data(run("cleanup", "--terminal", "mock", "--json", *args))


def blockers(report, task_id):
    return {b["code"] for c in report["candidates"] if c["task"] == task_id for b in c["blockers"]}


def close_terminal(launcher_home, task_id):
    """The session's terminal is gone (the mock treats a workspace in `gone` as closed)."""
    with state.open_state() as conn:
        workspace = conn.execute(
            "SELECT t.workspace_id FROM terminal_sessions t JOIN sessions s ON s.id = t.session_id WHERE s.task_id = ?",
            (task_id,),
        ).fetchone()[0]
    record = launcher_home / "mock-terminal.json"
    body = json.loads(record.read_text())
    body["gone"] = [*body.get("gone", []), workspace]
    record.write_text(json.dumps(body))


# --- completion detection ---------------------------------------------------------------------------------


def test_closed_issue_marks_task_eligible_and_terminates_nothing(env, checkout, gh, launcher_home):
    task, tree = opened()
    assert task["eligible_for_cleanup"] is False
    close_issue(gh)

    listed = data(run("tasks", "completed", "--json"))
    assert [t["id"] for t in listed["tasks"]] == [task["id"]]
    assert listed["tasks"][0]["eligible_for_cleanup"] is True

    after = task_row(task["id"])
    assert after.state == "active" and after.cleanup_eligible_at  # lifecycle state untouched; a flag beside it
    assert Path(tree["path"]).is_dir()
    assert mock_calls(launcher_home, "close_session") == []
    with state.open_state() as conn:
        events = [e for (e,) in conn.execute("SELECT event FROM task_events WHERE task_id = ?", (task["id"],))]
    assert events == ["completed"]


def test_offline_does_not_fetch_and_reopened_item_clears_the_flag(env, checkout, gh):
    task, _ = opened()
    close_issue(gh)
    calls = len(gh.calls)
    assert data(run("tasks", "completed", "--offline", "--json"))["tasks"] == []
    assert len(gh.calls) == calls

    data(run("tasks", "completed", "--json"))
    assert task_row(task["id"]).cleanup_eligible_at
    close_issue(gh, "open")
    assert data(run("tasks", "completed", "--json"))["tasks"] == []
    with state.open_state() as conn:
        events = [e for (e,) in conn.execute("SELECT event FROM task_events WHERE task_id = ?", (task["id"],))]
    assert events == ["completed", "reopened"]


def test_merged_pull_request_is_completed(env, checkout, gh):
    gh.add_pull("acme/widgets", 5, db_id=9005, head_ref="feature", sha="a" * 40, user={"login": "other"})
    task = data(run("new", "--title", "x", "--repo", str(checkout), "--offline", "--json"))["task"]
    data(run("tasks", "link", task["id"], PR_URL, "--json"))
    gh.pulls[(101, 5)].update(state="closed", merged=True)
    listed = data(run("tasks", "completed", "--json"))
    assert [t["id"] for t in listed["tasks"]] == [task["id"]]
    assert task_row(task["id"]).cleanup_eligible_at


def test_closed_unmerged_pull_request_is_completed_too(env, checkout, gh):
    gh.add_pull("acme/widgets", 5, db_id=9005, head_ref="feature", sha="a" * 40)
    task = data(run("new", "--title", "x", "--repo", str(checkout), "--offline", "--json"))["task"]
    data(run("tasks", "link", task["id"], PR_URL, "--json"))
    gh.pulls[(101, 5)]["state"] = "closed"
    assert len(data(run("tasks", "completed", "--json"))["tasks"]) == 1


def test_unreachable_github_is_a_warning_not_a_failure(env, checkout, gh):
    task, _ = opened()
    gh.down = True
    listed = data(run("tasks", "completed", "--json"))
    assert listed["tasks"] == [] and [s["task"] for s in listed["skipped"]] == [task["id"]]


def test_open_by_task_id_notes_a_closed_issue_but_still_opens(env, checkout, gh):
    task, _ = opened()
    close_issue(gh)
    again = data(run("open", task["id"], "--terminal", "mock", "--json"))
    assert again["action"] == "focused"
    assert task_row(task["id"]).cleanup_eligible_at


def test_open_by_url_notes_a_closed_issue(env, checkout, gh):
    task, _ = opened()
    close_issue(gh)
    data(open_url())
    assert task_row(task["id"]).cleanup_eligible_at


def test_open_offline_does_not_fetch(env, checkout, gh):
    task, _ = opened()
    close_issue(gh)
    calls = len(gh.calls)
    data(run("open", task["id"], "--terminal", "mock", "--offline", "--json"))
    assert len(gh.calls) == calls and task_row(task["id"]).cleanup_eligible_at is None


# --- archive / unarchive ----------------------------------------------------------------------------------


def test_archive_is_metadata_only_and_unarchive_restores(env, checkout, gh, launcher_home):
    task, tree = opened()
    with state.open_state() as conn:
        sessions = conn.execute("SELECT * FROM sessions").fetchall()
        trees = conn.execute("SELECT * FROM worktrees").fetchall()
    archived = archive(task["id"])
    assert archived["changed"] is True and archived["task"]["state"] == "archived"
    assert Path(tree["path"]).is_dir() and mock_calls(launcher_home, "close_session") == []
    with state.open_state() as conn:
        assert conn.execute("SELECT * FROM sessions").fetchall() == sessions
        assert conn.execute("SELECT * FROM worktrees").fetchall() == trees

    refusal = failure(run("open", task["id"], "--terminal", "mock", "--json"))
    assert refusal["code"] == "task_archived" and f"tasks unarchive {task['id']}" in refusal["message"]
    assert archive(task["id"])["changed"] is False

    restored = data(run("tasks", "unarchive", task["id"], "--json"))
    assert restored["task"]["state"] == "active"
    assert data(run("open", task["id"], "--terminal", "mock", "--json"))["action"] == "focused"
    assert failure(run("tasks", "unarchive", task["id"], "--json"))["code"] == "task_not_archived"


def test_archived_github_task_can_be_worked_again_by_url(env, checkout, gh):
    task, _ = opened()
    archive(task["id"])
    assert failure(open_url())["code"] == "task_archived"
    data(run("tasks", "unarchive", task["id"], "--json"))
    assert data(open_url())["task"]["id"] == task["id"]


def test_unarchive_of_a_never_opened_task_returns_to_created(env, checkout, gh):
    task = data(run("new", "--title", "x", "--repo", str(checkout), "--offline", "--json"))["task"]
    archive(task["id"])
    assert data(run("tasks", "unarchive", task["id"], "--json"))["task"]["state"] == "created"


def test_archived_tasks_are_not_refreshed(env, checkout, gh):
    task, _ = opened()
    archive(task["id"])
    close_issue(gh)
    calls = len(gh.calls)
    data(run("tasks", "completed", "--json"))
    assert len(gh.calls) == calls


# --- cleanup ----------------------------------------------------------------------------------------------


def completed_and_archived(gh):
    task, tree = opened()
    close_issue(gh)
    data(run("tasks", "completed", "--json"))
    return task, Path(tree["path"])


def test_clean_completed_worktree_is_removed_with_explicit_list_and_records_survive(env, checkout, gh, launcher_home):
    task, path = completed_and_archived(gh)
    close_terminal(launcher_home, task["id"])
    branch = git_run(path, "branch", "--show-current").strip()

    dry = cleanup_json("--dry-run")
    assert [c["task"] for c in dry["candidates"]] == [task["id"]] and dry["candidates"][0]["removable"]
    assert path.is_dir() and dry["removed"] == []

    result = cleanup_json("--yes", task["id"])
    assert [r["task"] for r in result["removed"]] == [task["id"]] and result["removed"][0]["branch_deleted"] is True
    assert not path.exists()
    assert branch not in git_run(checkout, "branch", "--list")

    assert task_row(task["id"]).id == task["id"]
    with state.open_state() as conn:
        row = conn.execute("SELECT path, removed_at FROM worktrees WHERE task_id = ?", (task["id"],)).fetchone()
        assert row[0] == str(path) and row[1]
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        events = [e for (e,) in conn.execute("SELECT event FROM task_events WHERE task_id = ?", (task["id"],))]
    assert events == ["completed", "worktree_removed"]
    # A removed worktree is not listed again, and opening says why it is gone.
    assert cleanup_json()["candidates"] == []
    assert failure(run("open", task["id"], "--terminal", "mock", "--json"))["code"] == "worktree_removed"


def test_yes_without_a_task_list_is_refused(env, checkout, gh):
    completed_and_archived(gh)
    assert failure(run("cleanup", "--yes", "--terminal", "mock", "--json"))["code"] == "cleanup_requires_list"


def test_no_confirmation_means_no_removal(env, checkout, gh, launcher_home, monkeypatch):
    task, path = completed_and_archived(gh)
    close_terminal(launcher_home, task["id"])
    # Not interactive and no --yes: reported, never removed.
    assert cleanup_json()["removed"] == [] and path.is_dir()
    # Interactive, answered no.
    prompter = ScriptedPrompter(("confirm", "Remove worktree", False))
    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    monkeypatch.setattr(cli, "make_prompter", lambda: prompter)
    result = run("cleanup", "--terminal", "mock")
    assert result.exit_code == 0, result.output
    prompter.done()
    assert path.is_dir()


def test_interactive_confirmation_removes(env, checkout, gh, launcher_home, monkeypatch):
    task, path = completed_and_archived(gh)
    close_terminal(launcher_home, task["id"])
    prompter = ScriptedPrompter(("confirm", "Remove worktree", True))
    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    monkeypatch.setattr(cli, "make_prompter", lambda: prompter)
    assert run("cleanup", "--terminal", "mock").exit_code == 0
    prompter.done()
    assert not path.exists()


def test_uncommitted_change_is_never_removed(env, checkout, gh, launcher_home):
    task, path = completed_and_archived(gh)
    close_terminal(launcher_home, task["id"])
    (path / "README").write_text("work in progress")
    git_run(path, "add", "README")
    (path / "README").write_text("more work")
    report = cleanup_json("--yes", task["id"])
    assert "uncommitted_changes" in blockers(report, task["id"]) and report["removed"] == []
    assert path.is_dir() and (path / "README").read_text() == "more work"


def test_untracked_file_is_never_removed(env, checkout, gh, launcher_home):
    task, path = completed_and_archived(gh)
    close_terminal(launcher_home, task["id"])
    (path / "notes").mkdir()
    (path / "notes" / "deep.txt").write_text("x")
    report = cleanup_json("--yes", task["id"])
    assert "uncommitted_changes" in blockers(report, task["id"])
    assert path.is_dir()


def test_unpushed_commit_without_upstream_is_never_removed(env, checkout, gh, launcher_home):
    task, path = completed_and_archived(gh)
    close_terminal(launcher_home, task["id"])
    git_run(path, "commit", "-q", "--allow-empty", "-m", "local only")
    report = cleanup_json("--yes", task["id"])
    assert "unpushed_commits" in blockers(report, task["id"]) and path.is_dir()


def test_pushed_commit_is_not_unpushed_but_a_later_one_is(env, checkout, gh, launcher_home, tmp_path):
    task, path = completed_and_archived(gh)
    close_terminal(launcher_home, task["id"])
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git_run(checkout, "remote", "set-url", "--push", "origin", str(bare))
    git_run(path, "commit", "-q", "--allow-empty", "-m", "pushed")
    branch = git_run(path, "branch", "--show-current").strip()
    git_run(path, "push", "-q", str(bare), f"{branch}:refs/heads/{branch}")
    git_run(path, "fetch", "-q", str(bare), f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    assert "unpushed_commits" not in blockers(cleanup_json("--dry-run", task["id"]), task["id"])
    git_run(path, "commit", "-q", "--allow-empty", "-m", "after the push")
    assert "unpushed_commits" in blockers(cleanup_json("--dry-run", task["id"]), task["id"])


def test_live_terminal_session_blocks(env, checkout, gh):
    task, path = completed_and_archived(gh)
    report = cleanup_json("--yes", task["id"])
    assert "session_live" in blockers(report, task["id"]) and path.is_dir()


def test_session_that_cannot_be_checked_blocks(env, checkout, gh, monkeypatch):
    task, path = completed_and_archived(gh)
    monkeypatch.setattr(cleanup, "session_liveness", lambda adapter: (lambda session: None))
    assert "session_unknown" in blockers(cleanup_json("--dry-run", task["id"]), task["id"])


def test_running_process_blocks_and_unknown_counts_as_in_use(env, checkout, gh, launcher_home, monkeypatch):
    task, path = completed_and_archived(gh)
    close_terminal(launcher_home, task["id"])
    monkeypatch.setattr(cleanup, "lsof_check", lambda p: True)
    assert "process_running" in blockers(cleanup_json("--yes", task["id"]), task["id"]) and path.is_dir()
    monkeypatch.setattr(cleanup, "lsof_check", lambda p: None)
    assert "process_unknown" in blockers(cleanup_json("--yes", task["id"]), task["id"]) and path.is_dir()


def test_lsof_check_sees_a_real_process_in_the_directory(tmp_path):
    if REAL_LSOF(str(tmp_path)) is None:
        pytest.skip("lsof unavailable")
    assert REAL_LSOF(str(tmp_path)) is False
    proc = subprocess.Popen(["sleep", "30"], cwd=tmp_path)
    try:
        assert REAL_LSOF(str(tmp_path)) is True
    finally:
        proc.kill()
        proc.wait()


def test_active_task_is_not_cleaned_even_when_named(env, checkout, gh, launcher_home):
    task, tree = opened()  # not completed, not archived
    close_terminal(launcher_home, task["id"])
    report = cleanup_json("--yes", task["id"])
    assert "task_active" in blockers(report, task["id"]) and Path(tree["path"]).is_dir()
    assert cleanup_json()["candidates"] == []  # and it is not even offered


def test_adopted_worktree_is_never_removed(env, checkout, gh, launcher_home, tmp_path):
    task = data(run("new", "--title", "adopt", "--repo", str(checkout), "--offline", "--json"))["task"]
    external = tmp_path / "external"
    git_run(checkout, "worktree", "add", "-q", "-b", "mine", str(external))
    data(run("worktrees", "associate", task["id"], str(external), "--json"))
    archive(task["id"])
    with state.open_state() as conn:
        assert conn.execute("SELECT ownership FROM worktrees WHERE task_id = ?", (task["id"],)).fetchone()[0] == "adopted"
    assert cleanup_json()["candidates"] == []  # never offered
    report = cleanup_json("--yes", task["id"])
    assert "adopted" in blockers(report, task["id"]) and report["removed"] == []
    assert external.is_dir() and "mine" in git_run(checkout, "branch", "--list")


def test_unmerged_branch_is_kept_when_worktree_is_clean_and_pushed(env, checkout, gh, launcher_home, tmp_path):
    task, path = completed_and_archived(gh)
    close_terminal(launcher_home, task["id"])
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    branch = git_run(path, "branch", "--show-current").strip()
    git_run(path, "commit", "-q", "--allow-empty", "-m", "pushed work")
    git_run(path, "push", "-q", str(bare), f"{branch}:refs/heads/{branch}")
    git_run(path, "fetch", "-q", str(bare), f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    result = cleanup_json("--yes", task["id"])
    assert len(result["removed"]) == 1 and result["removed"][0]["branch_deleted"] is False
    assert "not merged" in result["removed"][0]["note"]
    assert not path.exists() and branch in git_run(checkout, "branch", "--list")  # `-d`, never `-D`


def test_missing_directory_is_reported_not_removed(env, checkout, gh, launcher_home):
    import shutil

    task, path = completed_and_archived(gh)
    close_terminal(launcher_home, task["id"])
    shutil.rmtree(path)
    assert "worktree_missing" in blockers(cleanup_json("--dry-run"), task["id"])


def test_archived_local_task_with_created_worktree_is_offered(env, checkout, gh, launcher_home):
    task = data(run("new", "--title", "local", "--repo", str(checkout), "--offline", "--json"))["task"]
    opened_local = data(run("open", task["id"], "--terminal", "mock", "--json"))
    close_terminal(launcher_home, task["id"])
    archive(task["id"])
    report = cleanup_json("--dry-run")
    assert [c["task"] for c in report["candidates"]] == [task["id"]] and report["candidates"][0]["removable"]
    assert Path(opened_local["worktree"]["path"]).is_dir()


def test_git_refuses_what_the_checks_missed(env, checkout, gh, launcher_home, monkeypatch):
    """Removal never passes --force: a dirty tree that slipped past the checks is still refused by git."""
    task, path = completed_and_archived(gh)
    close_terminal(launcher_home, task["id"])
    monkeypatch.setattr(git, "has_changes", lambda p: False)  # a check that was fooled
    (path / "late.txt").write_text("appeared after the check")
    result = run("cleanup", "--yes", task["id"], "--terminal", "mock", "--json")
    assert result.exit_code == 1
    assert (path / "late.txt").exists() and path.is_dir()
    assert task_row(task["id"]).id == task["id"]


def test_launch_failed_and_created_tasks_are_not_cleaned_even_when_named(env, checkout, gh, launcher_home):
    task, tree = opened()
    close_terminal(launcher_home, task["id"])
    for state_ in ("launch_failed", "created"):
        with state.open_state() as conn:
            conn.execute("UPDATE tasks SET state = ? WHERE id = ?", (state_, task["id"]))
        report = cleanup_json("--yes", task["id"])
        assert "task_active" in blockers(report, task["id"]) and report["removed"] == []
        assert Path(tree["path"]).is_dir()


def test_prompt_and_json_list_ignored_entries(env, checkout, gh, launcher_home, monkeypatch):
    task, path = completed_and_archived(gh)
    close_terminal(launcher_home, task["id"])
    (checkout / ".gitignore").write_text(".env\n")
    git_run(checkout, "add", ".gitignore")
    git_run(checkout, "commit", "-q", "-m", "ignore env")
    git_run(path, "merge", "-q", "main")
    (path / ".env").write_text("SECRET=1")
    assert cleanup_json("--dry-run")["candidates"][0]["ignored_entries"] == [".env"]
    prompter = ScriptedPrompter(("confirm", "Ignored files are deleted too: .env", False))
    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    monkeypatch.setattr(cli, "make_prompter", lambda: prompter)
    assert run("cleanup", "--terminal", "mock").exit_code == 0
    prompter.done()
    assert (path / ".env").exists()


def test_yes_with_dry_run_needs_no_list(env, checkout, gh):
    completed_and_archived(gh)
    assert data(run("cleanup", "--yes", "--dry-run", "--terminal", "mock", "--json"))["removed"] == []


def test_recovery_after_removal_by_associating_a_new_worktree(env, checkout, gh, launcher_home, tmp_path):
    task, path = completed_and_archived(gh)
    close_terminal(launcher_home, task["id"])
    archive(task["id"])
    cleanup_json("--yes", task["id"])
    assert not path.exists()

    unarchived = run("tasks", "unarchive", task["id"], "--json")
    assert data(unarchived)["warning"] and "worktrees associate" in data(unarchived)["warning"]
    refusal = failure(run("open", task["id"], "--terminal", "mock", "--json"))
    assert refusal["code"] == "worktree_removed" and f"worktrees associate {task['id']}" in refusal["message"]

    fresh = tmp_path / "fresh"
    git_run(checkout, "worktree", "add", "-q", "-b", "again", str(fresh))
    assert failure(run("worktrees", "associate", task["id"], str(fresh), "--json"))["code"] == "session_exists"
    adopted = data(run("worktrees", "associate", task["id"], str(fresh), "--force", "--json"))
    assert adopted["worktree"]["ownership"] == "adopted" and adopted["worktree"]["removed_at"] is None
    assert data(run("open", task["id"], "--terminal", "mock", "--json"))["action"] in ("focused", "resumed")
    with state.open_state() as conn:
        events = [e for (e,) in conn.execute("SELECT event FROM task_events WHERE task_id = ?", (task["id"],))]
    assert "worktree_replaced" in events and "worktree_removed" in events
    # an adopted worktree is never offered for cleanup
    archive(task["id"])
    assert cleanup_json()["candidates"] == []
