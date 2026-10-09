"""Adopting external terminal sessions (ticket #19), through the CLI on the mock terminal."""

import json
import subprocess

import pytest

from test_session_lifecycle import configure, data, fake_agents, mock_calls, mock_file, repo, run, script  # noqa: F401

from agent_launcher import state
from agent_launcher.sessions import sessions_for_task


@pytest.fixture
def task(repo):
    return json.loads(run("new", "--title", "Fix login", "--repo", str(repo), "--offline", "--json").stdout)["task"]


def external(workspace="ext-1", cwd=None, title="claude", surface=True):
    return {"workspace_id": workspace, "surface_id": f"surf-{workspace}" if surface else None, "title": title, "cwd": cwd}


def seed(launcher_home, *sessions):
    file = mock_file(launcher_home)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps({"calls": [], "external": list(sessions)}))


def candidates(task):
    return data(run("sessions", "candidates", task["id"], "--json"))["candidates"]


def test_title_naming_the_task_is_evidence(task, launcher_home):
    seed(launcher_home, external(title=f"agent {task['id']}", cwd="/elsewhere"))
    (found,) = candidates(task)
    assert "title" in found["evidence"][0]


def test_a_session_in_the_repository_is_never_offered_without_a_worktree(task, repo, launcher_home):
    seed(launcher_home, external(cwd=str(repo), title="claude"))
    assert candidates(task) == []


def test_a_session_in_the_repository_is_never_offered_with_a_worktree(task, repo, launcher_home, tmp_path):
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "side", str(wt)], check=True)
    assert run("worktrees", "associate", task["id"], str(wt)).exit_code == 0
    seed(launcher_home, external(cwd=str(repo), title="claude"))
    assert candidates(task) == []


@pytest.mark.parametrize("title", ["Fix", "fix login", "pytest", "Fix login bug", "t-fix"])
def test_free_text_title_matches_are_not_evidence(task, launcher_home, title):
    seed(launcher_home, external(title=title, cwd="/elsewhere"))
    assert candidates(task) == []


def test_the_launchers_own_workspace_title_is_evidence(task, repo, launcher_home):
    seed(launcher_home, external(title="one — Fix login", cwd="/elsewhere"))
    (found,) = candidates(task)
    assert "launcher gives its own workspace" in found["evidence"][0]


def test_an_issue_reference_with_the_repository_name_is_evidence(task, repo, launcher_home):
    from agent_launcher.github_tasks import github_details  # noqa: F401  (the title check reads task_github)

    with state.open_state() as conn:
        conn.execute(
            "INSERT INTO task_github (task_id, kind, node_id, database_id, repository_github_id, repository_node_id, number, title, "
            "state, labels, author, assignees, url, fetched_at) VALUES (?, 'issue', 'n', 1, 1, 'r', 142, 'Fix', 'open', '[]', 'a', '[]', "
            "'https://github.com/o/one/issues/142', 'now')",
            (task["id"],),
        )
        conn.commit()
    seed(launcher_home, external("a", title="one #142"), external("b", title="#142"), external("c", title="one #1420"),
         external("d", title="one-two #142"), external("e", title="one.js #142"))
    assert [c["workspace_id"] for c in candidates(task)] == ["a"]


def test_control_characters_are_stripped_from_what_is_shown(task, launcher_home):
    seed(launcher_home, external(title=f"{task['id']}\x1b[31m red\x07", cwd=None))
    (found,) = candidates(task)
    assert "\x1b" not in found["title"] and "\x07" not in found["title"]


def test_an_agent_in_the_repo_alone_is_never_offered(task, launcher_home, tmp_path):
    seed(
        launcher_home,
        external("a", cwd=str(tmp_path / "other"), title="claude"),  # elsewhere, unrelated title
        external("b", cwd=None, title="claude — repo"),  # no cwd, no task in the title
    )
    assert candidates(task) == []
    assert "No session has evidence" in run("sessions", "candidates", task["id"]).output


def test_sessions_without_a_surface_are_not_offered(task, repo, launcher_home):
    seed(launcher_home, external(cwd=str(repo), surface=False))
    assert candidates(task) == []


def test_a_task_with_a_worktree_is_matched_by_it_not_by_the_repository(task, repo, launcher_home, tmp_path):
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "side", str(wt)], check=True)
    assert run("worktrees", "associate", task["id"], str(wt)).exit_code == 0
    seed(launcher_home, external("repo", cwd=str(repo)), external("tree", cwd=str(wt)))
    assert [c["workspace_id"] for c in candidates(task)] == ["tree"]


def test_adopt_records_an_unowned_session_and_persists_it(task, repo, launcher_home):
    seed(launcher_home, external(title=task["id"]))
    out = data(run("sessions", "adopt", task["id"], "ext-1", "--agent", "claude", "--json"))
    assert out["action"] == "adopted"
    with state.open_state() as conn:
        (only,) = sessions_for_task(conn, task["id"])
    assert only.terminal.workspace_id == "ext-1" and only.terminal.created_by_launcher is False
    assert only.agent == "claude"
    # Nothing was created, closed or typed into.
    assert not mock_calls(launcher_home, "create_session") and not mock_calls(launcher_home, "close_session")
    assert not mock_calls(launcher_home, "prepare_prompt")
    # An adopted session is no longer offered, to this task or another.
    assert candidates(task) == []


def test_adopt_refuses_what_is_not_a_candidate(task, launcher_home, tmp_path):
    seed(launcher_home, external(cwd=str(tmp_path)))
    refused = run("sessions", "adopt", task["id"], "ext-1", "--agent", "claude", "--json")
    assert refused.exit_code == 1 and json.loads(refused.stdout)["error"]["code"] == "not_a_candidate"
    with state.open_state() as conn:
        assert sessions_for_task(conn, task["id"]) == []


def test_a_task_with_a_session_cannot_adopt_a_second(task, repo, launcher_home):
    seed(launcher_home, external("one", title=task["id"]), external("two", title=task["id"]))
    assert run("sessions", "adopt", task["id"], "one", "--agent", "claude").exit_code == 0
    refused = run("sessions", "adopt", task["id"], "two", "--agent", "claude", "--json")
    assert refused.exit_code == 1 and json.loads(refused.stdout)["error"]["code"] == "session_exists"


@pytest.mark.parametrize("agent", ["claud", "codex"])
def test_adopt_refuses_an_agent_outside_the_tasks_profile_and_writes_nothing(
    task, configure, fake_agents, launcher_home, agent
):
    # `codex` exists, but only in another profile: the task's profile (`work`) has just `claude`.
    configure(
        profiles={
            "work": {"default_agent": "claude", "agents": {"claude": {"executable": str(fake_agents / "claude")}}},
            "other": {"default_agent": "codex", "agents": {"codex": {"executable": str(fake_agents / "codex")}}},
        }
    )
    seed(launcher_home, external(title=task["id"]))
    refused = run("sessions", "adopt", task["id"], "ext-1", "--agent", agent, "--json")
    assert refused.exit_code == 1 and json.loads(refused.stdout)["error"]["code"] == "agent_unresolved"
    with state.open_state() as conn:
        assert sessions_for_task(conn, task["id"]) == []
    assert len(candidates(task)) == 1


def test_adoption_is_refused_while_a_launch_is_unfinished(task, launcher_home):
    seed(launcher_home, external(title=task["id"]))
    with state.open_state() as conn:
        conn.execute("UPDATE tasks SET state = 'launch_failed' WHERE id = ?", (task["id"],))
        conn.commit()
    refused = run("sessions", "adopt", task["id"], "ext-1", "--agent", "claude", "--json")
    assert refused.exit_code == 1 and json.loads(refused.stdout)["error"]["code"] == "launch_incomplete"


def test_a_terminal_held_by_an_unfinished_launch_is_not_external(task, launcher_home):
    seed(launcher_home, external("held", title=task["id"]), external("free", title=task["id"]))
    with state.open_state() as conn:
        conn.execute(
            "INSERT INTO launches (task_id, stage, terminal_adapter, terminal_workspace_id, terminal_surface_id, "
            "terminal_created_by_launcher, started_at, updated_at) VALUES (?, 'terminal_created', 'mock', 'held', 'surf-held', 1, 'now', 'now')",
            (task["id"],),
        )
        conn.commit()
    assert [c["workspace_id"] for c in candidates(task)] == ["free"]


def test_adopt_needs_to_know_the_agent(task, repo, launcher_home):
    seed(launcher_home, external(title=task["id"]))
    refused = run("sessions", "adopt", task["id"], "ext-1", "--json")
    assert refused.exit_code == 1 and json.loads(refused.stdout)["error"]["code"] == "agent_required"


def test_open_focuses_an_adopted_session_and_restart_leaves_it_open(task, repo, launcher_home):
    seed(launcher_home, external(title=task["id"]))
    assert run("sessions", "adopt", task["id"], "ext-1", "--agent", "claude").exit_code == 0
    assert data(run("open", task["id"], "--offline", "--json"))["action"] == "focused"
    assert not mock_calls(launcher_home, "create_session")
    restarted = data(run("restart", task["id"], "--yes", "--offline", "--json"))
    assert restarted["action"] == "restarted" and "left open" in restarted["notice"]
    assert not mock_calls(launcher_home, "close_session")
