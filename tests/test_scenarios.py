"""The ten end-to-end acceptance scenarios of SPEC §34 (A-J), one test each, driven through the CLI.

Each test runs the real `agent-launcher` commands against a temp launcher home, real git in temp repositories, the mock
terminal and a fake `gh` (the fakes of `test_github_issues` / `test_github_pulls`: an in-memory GitHub that answers
`gh api` and `gh repo clone`). Nothing reaches GitHub or cmux. The docstring of every test lists the scenario's expected
results, numbered as in the SPEC, and the assertions below it check each one. Interactive questions are answered by a
`ScriptedPrompter` standing in for the terminal picker; a prompter with no steps proves that nothing is asked.

Scenarios A, D, E and G also exist as opt-in live tests at the end of this file, against the throwaway
`owner/sandbox` repository (`AGENT_LAUNCHER_LIVE=1`, no cmux). What neither can show (the real cmux,
real agents' screens) is listed in docs/live-validation.md.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agent_launcher import cli, git, state
from agent_launcher.tasks import list_tasks
from agent_launcher.terminal_mock import CREATE_DELAY_ENV
from conftest import git_run
from scripted import ScriptedPrompter
from test_github_issues import URL, data, env, failure, mock_calls, run  # noqa: F401  (env is a fixture)
from test_github_issues import checkout  # noqa: F401  (fixture)
from test_github_pulls import PR_URL, gh, origin  # noqa: F401  (fixtures)
from test_workflows import FIXER, REVIEW, TRIAGE, review_pr, skills, write_flows  # noqa: F401  (skills is a fixture)


@pytest.fixture
def agent_bin(tmp_path) -> Path:
    bin_dir = tmp_path / "agents"
    bin_dir.mkdir()
    for name in ("claude", "codex"):
        exe = bin_dir / name
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    return bin_dir


@pytest.fixture
def two_profiles(env, agent_bin, write_config, tmp_path):
    """`work` and `personal`, each with Claude and Codex, asking which agent to run every time."""

    def _config(work_claude: Path | None = None, **extra) -> None:
        def agents(claude: Path):
            return {"claude": {"executable": str(claude)}, "codex": {"executable": str(agent_bin / "codex")}}

        write_config(
            {
                "version": 2,
                "terminal": {"adapter": "mock"},
                "agent_selection": "always_ask",
                "repositories": {
                    "worktree_root": str(tmp_path / "trees"),
                    "search_roots": [str(env.search)],
                    "clone_root": str(tmp_path / "clones"),
                },
                "profiles": {
                    "work": {"default_agent": "claude", "agents": agents(work_claude or agent_bin / "claude")},
                    "personal": {"default_agent": "claude", "agents": agents(agent_bin / "claude")},
                },
                **extra,
            }
        )

    _config()
    return _config


def ask(monkeypatch, prompter: ScriptedPrompter) -> ScriptedPrompter:
    """Make every command in this test interactive, answering from `prompter`."""
    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    monkeypatch.setattr(cli, "make_prompter", lambda: prompter)
    return prompter


def open_asking(monkeypatch, prompter: ScriptedPrompter, target: str, *extra: str) -> dict:
    """`open` on a terminal that answers from `prompter` (questions are only asked without `--json`), then the task as
    `tasks show --json` reports it: the task, its sessions and its worktree. Fails if a scripted answer is left over."""
    ask(monkeypatch, prompter)
    result = run("open", target, "--terminal", "mock", *extra)
    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    prompter.done()
    task_id = re.search(r"task (t-[0-9a-z]{8})", result.output).group(1)
    shown = data(run("tasks", "show", task_id, "--json"))
    shown["verb"] = result.output.split()[0]
    return shown


def unknown_checkout(env, make_repo) -> Path:
    """A local checkout of acme/widgets that the launcher has never seen: no profile association."""
    moved = env.search / "widgets"
    make_repo("widgets", remote="https://github.com/acme/widgets").rename(moved)
    return moved


def worktree_count(path: Path) -> int:
    return len(git.list_worktrees(str(path)))


def launcher_cli(*args: str) -> subprocess.CompletedProcess:
    """A separate `agent-launcher` process: what survives between invocations."""
    return subprocess.run([sys.executable, "-m", "agent_launcher", *args], capture_output=True, text=True, timeout=120)


# --- A ---------------------------------------------------------------------------------------------------------


def test_scenario_a_first_launch_from_an_unknown_repository(env, make_repo, gh, two_profiles, monkeypatch, launcher_home):
    """A GitHub issue is selected in a repository the launcher has never seen. Expected (SPEC §34 A):

    1. Repository identified.            2. Profile selection requested.      3. Association persisted.
    4. Agent picker displayed.           5. Workflow resolved.                6. Worktree established.
    7. cmux workspace created (mock).    8. Agent launched.                   9. Prompt prepared but not executed.
    """
    path = unknown_checkout(env, make_repo)
    assert run("profile", "which", str(path), "--offline", "--json").exit_code != 0  # unknown: nothing assigned
    prompter = ScriptedPrompter(("select", "Which profile", "personal"), ("select", "Which agent", "codex"))
    result = open_asking(monkeypatch, prompter, URL)
    # 2 and 4: both questions were asked, in that order, and nothing else (`done()` checked the script was used up).
    assert [kind for kind, _ in prompter.asked] == ["select", "select"]
    task, (session,) = result["task"], result["sessions"]
    # 1: the repository was identified through GitHub's stable ID, and is the local checkout.
    assert result["github"]["repository_github_id"] == 101 and result["github"]["node_id"] == "I_9007"
    assert task["repo_path"] == str(path.resolve())
    # 3: the association is in the database and survives a new process.
    assert task["profile"] == "personal"
    assert json.loads(launcher_cli("profile", "which", str(path), "--offline", "--json").stdout)["profile"] == "personal"
    # 5: no workflows.json, so the built-in default workflow.
    assert task["workflow"] == "default"
    # 6: a launcher-created worktree under the configured root, on a task branch.
    tree = result["worktree"]
    assert tree["ownership"] == "created" and Path(tree["path"]).is_dir() and str(launcher_home.parent / "trees") in tree["path"]
    assert tree["branch"].startswith("task/")
    # 7 and 8: one workspace, in the worktree, running the chosen agent of the chosen profile.
    (create,) = mock_calls(launcher_home)
    assert create["working_directory"] == tree["path"] and session["agent"] == "codex"
    assert Path(create["command"][0]).name == "codex"
    # 9: the prompt (the URL) is typed into the agent's input and not submitted.
    assert create["prompt"] == URL and create["submit_prompt"] is False


# --- B ---------------------------------------------------------------------------------------------------------


def test_scenario_b_reopening_an_existing_issue(env, checkout, gh, two_profiles, monkeypatch, launcher_home):
    """The same issue is selected again. Expected (SPEC §34 B):

    - Existing task found.  - Existing session focused.  - No duplicate worktree.  - No new agent selection.
    - No repeated prompt.
    """
    assert run("profile", "set", str(checkout), "personal", "--offline").exit_code == 0
    first = open_asking(monkeypatch, ScriptedPrompter(("select", "Which agent", "claude")), URL)
    trees = worktree_count(checkout)

    again = open_asking(monkeypatch, ScriptedPrompter(), URL)  # any question fails the test
    assert again["verb"] == "Focused" and again["task"]["id"] == first["task"]["id"]  # found, not created
    assert again["sessions"] == first["sessions"]  # the same session, untouched
    assert [c["session"]["workspace_id"] for c in mock_calls(launcher_home, "focus_session")] == [
        first["sessions"][0]["terminal"]["workspace_id"]
    ]
    assert worktree_count(checkout) == trees  # no duplicate worktree
    assert len(mock_calls(launcher_home)) == 1  # one create_session ever: no new agent, no new workspace, no prompt typed again
    with state.open_state() as conn:
        assert len(list_tasks(conn)) == 1


# --- C ---------------------------------------------------------------------------------------------------------


def test_scenario_c_work_personal_isolation(env, checkout, gh, two_profiles, agent_bin, tmp_path, launcher_home):
    """A repository assigned to `work` requests Claude, and the work Claude executable is missing. Expected (SPEC §34 C):

    - Launch rejected.  - No personal Claude fallback.  - No profile reassignment.  - Clear diagnostic error.
    """
    two_profiles(work_claude=tmp_path / "no-such-dir" / "claude-work")  # `personal` still has a working claude
    assert run("profile", "set", str(checkout), "work", "--offline").exit_code == 0
    task = data(run("new", "--title", "Isolated", "--repo", str(checkout), "--offline", "--json"))["task"]

    result = run("open", task["id"], "--agent", "claude", "--terminal", "mock", "--offline", "--json")
    error = failure(result)
    # Rejected, with a diagnostic that names the profile, the agent and the missing executable.
    assert error["code"] == "agent_unresolved"
    assert "work" in error["message"] and "claude" in error["message"] and "claude-work" in error["message"]
    # No personal fallback: nothing was launched, no worktree was cut, no session recorded.
    assert not (launcher_home / "mock-terminal.json").exists() or mock_calls(launcher_home) == []
    assert worktree_count(checkout) == 1
    with state.open_state() as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    # No reassignment: the repository still belongs to `work`, and so does the task.
    assert data(run("profile", "which", str(checkout), "--offline", "--json"))["profile"] == "work"
    assert data(run("tasks", "show", task["id"], "--json"))["task"]["profile"] == "work"
    # Fixing the executable (the work one) is the only way forward, and then it launches as `work`.
    two_profiles()
    ok = data(run("open", task["id"], "--agent", "claude", "--terminal", "mock", "--offline", "--json"))
    assert ok["session"]["profile"] == "work"


# --- D ---------------------------------------------------------------------------------------------------------


def test_scenario_d_existing_worktree(env, checkout, gh, origin, two_profiles, monkeypatch, tmp_path, launcher_home):
    """A PR already has an appropriate worktree. Expected (SPEC §34 D):

    - Existing worktree discovered.  - User offered adoption.  - No unnecessary worktree created.
    - Association persisted.
    """
    assert run("profile", "set", str(checkout), "personal", "--offline").exit_code == 0
    sha = origin.commit_on("feat/x", "x.txt")
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha)
    git_run(checkout, "fetch", "-q", origin.url, "+refs/heads/*:refs/remotes/origin/*")
    existing = tmp_path / "already-here"
    git_run(checkout, "worktree", "add", "-q", "--track", "-b", "feat/x", str(existing), "origin/feat/x")
    before = git_run(checkout, "worktree", "list")

    prompter = ScriptedPrompter(("select", "Which agent", "claude"), ("select", "no worktree yet", "wt:0"))
    result = open_asking(monkeypatch, prompter, PR_URL)
    offered = [m for _, m in prompter.asked if "no worktree yet" in m]
    assert len(offered) == 1  # 2: the existing tree was offered, and the user adopted it
    tree = result["worktree"]
    assert tree["ownership"] == "adopted" and Path(tree["path"]) == existing.resolve()  # 1: discovered
    assert git_run(checkout, "worktree", "list") == before  # 3: nothing new was created
    # 4: the association is stored, so a new process finds it, and the agent ran in it.
    shown = json.loads(launcher_cli("worktrees", "inspect", result["task"]["id"], "--json").stdout)
    assert shown["worktree"]["path"] == str(existing.resolve()) and shown["worktree"]["ownership"] == "adopted"
    (create,) = mock_calls(launcher_home)
    assert create["working_directory"] == str(existing.resolve())


# --- E ---------------------------------------------------------------------------------------------------------


def test_scenario_e_workflow_routing(env, checkout, gh, origin, skills, two_profiles, monkeypatch, launcher_home):
    """A PR requesting the user's review matches the code-review workflow. Expected (SPEC §34 E):

    - Correct rule selected.  - Appropriate agent preference highlighted.  - Skill invocation prepared.
    - User can edit prompt before submission.
    """
    assert run("profile", "set", str(checkout), "personal", "--offline").exit_code == 0
    write_flows(launcher_home, TRIAGE, FIXER, {**REVIEW, "preferred_agent": "codex"})
    review_pr(gh, origin)
    explained = data(run("workflows", "test", PR_URL, "--json"))
    assert explained["winner"]["id"] == "code-review", explained

    class Spy(ScriptedPrompter):
        highlighted = None

        def select(self, message, choices, default=None):
            if "Which agent" in message:
                Spy.highlighted = (default, [c.label for c in choices])
            return super().select(message, choices, default)

    result = open_asking(monkeypatch, Spy(("select", "Which agent", "codex")), PR_URL)
    assert result["task"]["workflow"] == "code-review"  # the rule, not the issue triage or the author's fixer
    assert Spy.highlighted[0] == "codex" and "codex (workflow preference)" in Spy.highlighted[1]  # preselected, still the user's choice
    (create,) = mock_calls(launcher_home)
    assert create["prompt"] == f"$code-review {PR_URL}"  # the skill invocation, in Codex's `$skill` syntax
    assert create["submit_prompt"] is False  # typed into the input, waiting for the user to edit and send


# --- F ---------------------------------------------------------------------------------------------------------


def test_scenario_f_local_task(env, checkout, two_profiles, monkeypatch, launcher_home):
    """The user creates a local task. Expected (SPEC §34 F):

    - Stable local task ID.  - Repository and profile resolved.  - Agent launched.
    - Task persists between application invocations.
    """
    ask(monkeypatch, ScriptedPrompter(("select", "Which profile", "work")))
    created = data(run("new", "--title", "Tidy the parser", "--repo", str(checkout), "--offline", "--json"))["task"]
    task_id = created["id"]
    assert re.fullmatch(r"t-[0-9a-z]{8}", task_id) and created["source"] == "local"  # stable local ID
    assert created["profile"] == "work" and created["repo_path"] == str(checkout.resolve())  # resolved

    opened = open_asking(monkeypatch, ScriptedPrompter(("select", "Which agent", "claude")), task_id, "--offline")
    assert opened["verb"] == "Opened" and opened["sessions"][0]["agent"] == "claude"
    (create,) = mock_calls(launcher_home)
    assert create["prompt"] == "Tidy the parser"

    # A separate process sees the same task, the same ID and its session.
    listed = json.loads(launcher_cli("tasks", "list", "--json").stdout)
    assert [t["id"] for t in listed["tasks"]] == [task_id]
    shown = json.loads(launcher_cli("tasks", "show", task_id, "--json").stdout)
    assert shown["task"]["id"] == task_id and shown["sessions"][0]["id"] == opened["sessions"][0]["id"]


# --- G ---------------------------------------------------------------------------------------------------------


def test_scenario_g_local_task_linked_to_github(env, checkout, gh, two_profiles, monkeypatch, launcher_home):
    """An existing local task is linked to a newly created GitHub issue. Expected (SPEC §34 G):

    - Internal identity preserved.  - Existing worktree preserved.  - Existing session preserved.
    - GitHub identity associated.  - No duplicate task.
    """
    assert run("profile", "set", str(checkout), "work", "--offline").exit_code == 0
    task = data(run("new", "--title", "Local first", "--repo", str(checkout), "--json"))["task"]
    opened = open_asking(monkeypatch, ScriptedPrompter(("select", "Which agent", "claude")), task["id"])
    with state.open_state() as conn:
        sessions = conn.execute("SELECT * FROM sessions").fetchall()
        trees = conn.execute("SELECT * FROM worktrees").fetchall()

    linked = data(run("tasks", "link", task["id"], URL, "--json"))
    assert linked["linked"] is True and linked["task"]["id"] == task["id"]  # internal identity
    assert linked["task"]["source"] == "github" and linked["task"]["url"] == URL
    assert linked["github"]["node_id"] == "I_9007" and linked["github"]["database_id"] == 9007  # GitHub identity
    with state.open_state() as conn:
        assert conn.execute("SELECT * FROM worktrees").fetchall() == trees  # worktree and session untouched
        assert conn.execute("SELECT * FROM sessions").fetchall() == sessions

    again = open_asking(monkeypatch, ScriptedPrompter(), URL)  # opening the issue now asks nothing
    assert again["task"]["id"] == task["id"] and again["verb"] == "Focused"
    assert again["sessions"] == opened["sessions"] and again["worktree"] == opened["worktree"]
    assert len(mock_calls(launcher_home)) == 1
    with state.open_state() as conn:
        assert len(list_tasks(conn)) == 1  # no duplicate task


# --- H ---------------------------------------------------------------------------------------------------------


def test_scenario_h_concurrent_launch(env, checkout, two_profiles, launcher_home):
    """Two processes open the same task at the same moment. Expected (SPEC §34 H):

    - One canonical task.  - One primary session.  - One associated worktree.  - No corrupted database state.
    """
    assert run("profile", "set", str(checkout), "work", "--offline").exit_code == 0
    task_id = data(run("new", "--title", "Race", "--repo", str(checkout), "--offline", "--json"))["task"]["id"]
    environment = {**os.environ, CREATE_DELAY_ENV: "1.0"}  # keeps both inside the launch window
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "agent_launcher", "open", task_id, "--agent", "claude", "--terminal", "mock", "--offline", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment,
        )
        for _ in range(2)
    ]
    outs = [p.communicate(timeout=120) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    assert sorted(json.loads(out)["action"] for out, _ in outs) == ["created", "focused"]
    assert len({json.loads(out)["session"]["id"] for out, _ in outs}) == 1
    assert len(mock_calls(launcher_home)) == 1  # one workspace
    with state.open_state() as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        counts = [conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("tasks", "sessions", "worktrees")]
        assert counts == [1, 1, 1]
        assert conn.execute("SELECT count(*) FROM launches").fetchone()[0] == 0  # nothing left half-done
    assert worktree_count(checkout) == 2  # the checkout and the task's one worktree


# --- I ---------------------------------------------------------------------------------------------------------


def test_scenario_i_partial_launch_failure(env, checkout, two_profiles, launcher_home):
    """The worktree is created, then the terminal fails. Expected (SPEC §34 I):

    - Recoverable task state.  - Worktree preserved.  - Clear error.  - Retry succeeds without creating duplicates.
    """
    assert run("profile", "set", str(checkout), "work", "--offline").exit_code == 0
    task_id = data(run("new", "--title", "Flaky", "--repo", str(checkout), "--offline", "--json"))["task"]["id"]
    launcher_home.mkdir(parents=True, exist_ok=True)
    (launcher_home / "mock-terminal.json").write_text(json.dumps({"fail_create": "cmux is not responding"}))

    failed = run("open", task_id, "--agent", "claude", "--terminal", "mock", "--offline", "--json")
    error = failure(failed)
    assert error["code"] == "terminal_create_failed" and "cmux is not responding" in error["message"]  # clear
    assert data(run("tasks", "show", task_id, "--json"))["task"]["state"] == "launch_failed"  # recoverable, not "active"
    tree = data(run("worktrees", "inspect", task_id, "--json"))["worktree"]
    assert Path(tree["path"]).is_dir() and tree["ownership"] == "created"  # preserved

    (launcher_home / "mock-terminal.json").write_text(json.dumps({}))  # the terminal is back
    retried = data(run("open", task_id, "--agent", "claude", "--terminal", "mock", "--offline", "--json"))
    assert retried["action"] == "created" and retried["worktree"]["path"] == tree["path"]  # the same worktree
    assert worktree_count(checkout) == 2 and len(mock_calls(launcher_home)) == 1
    with state.open_state() as conn:
        counts = [conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("tasks", "sessions", "worktrees")]
        assert counts == [1, 1, 1]  # no duplicates
    assert data(run("tasks", "show", task_id, "--json"))["task"]["state"] == "active"


# --- J ---------------------------------------------------------------------------------------------------------


def test_scenario_j_gh_dash_bootstrap(tmp_path, make_repo, agent_bin, fake_default_home, monkeypatch, launcher_home):
    """Initial setup completes, then the user chooses to configure gh-dash. Expected (SPEC §34 J):

    - Local maintenance task created.  - Management skill selected.  - Agent launched in cmux (mock here).
    - Integration prompt prepared.  - Existing gh-dash configuration preserved.
    """
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({
        "profiles": [{"name": "personal", "agents": {"claude": {"executable": str(agent_bin / "claude")}}, "default_agent": "claude"}],
        "agent_selection": "use_default",
        "worktree_root": str(tmp_path / "trees"),
    }))
    done = run("setup", "--answers", str(answers), "--yes")
    assert done.exit_code == 0, done.output  # initial setup completes
    gh_dash_config = fake_default_home / ".config" / "gh-dash" / "config.yml"
    gh_dash_config.parent.mkdir(parents=True)
    gh_dash_config.write_text("prSections:\n  - title: Mine\n    filters: is:open author:@me\nkeybindings:\n  prs:\n    - key: z\n      command: echo hi\n")
    before = gh_dash_config.read_bytes()
    maintenance_repo = make_repo("launcher-maintenance")
    monkeypatch.setattr(cli, "gh_dash_detected", lambda *a, **k: True)

    result = data(run("integrate", "gh-dash", "--repo", str(maintenance_repo), "--profile", "personal", "--terminal", "mock", "--json"))
    assert result["task"]["source"] == "local" and result["task"]["title"] == "Integrate Agent Launcher with gh-dash"
    assert result["workflow"] == "agent-launcher-configure"  # the management skill
    assert result["prompt"].startswith("/agent-launcher ")
    calls = [c for c in mock_calls(launcher_home) if "gh-dash" in (c["prompt"] or "")]
    assert len(calls) == 1 and calls[0]["prompt"] == result["prompt"]  # the agent was launched with the prompt
    assert calls[0]["submit_prompt"] is False and result["prompt_prepared"] and not result["prompt_submitted"]
    assert "Preserve all existing" in result["prompt"]
    assert gh_dash_config.read_bytes() == before  # Agent Launcher itself wrote nothing to gh-dash's config


# --- Live: A, D, E and G against the throwaway sandbox repository (opt-in, no cmux) -----------------------------
#
# `AGENT_LAUNCHER_LIVE=1 uv run pytest tests/test_scenarios.py`. Real `gh` (read for repository IDs and issue and PR
# data; it creates and closes issues and PRs in the sandbox only) and real git; the mock terminal; no agent is started.
# They show what the fakes above cannot: that GitHub's real responses are understood.

live = pytest.mark.skipif(not os.environ.get("AGENT_LAUNCHER_LIVE"), reason="set AGENT_LAUNCHER_LIVE=1 to run")


@pytest.fixture
def sandbox(monkeypatch, tmp_path, write_config):
    """Real gh, real HOME for gh's own login (read only), a config with one profile, temp roots, mock terminal."""
    import pwd

    from agent_launcher import repositories

    monkeypatch.setattr(repositories, "run_command", repositories._real_run_command)
    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (tmp_path / "src").mkdir()
    write_config(
        {
            "version": 2,
            "terminal": {"adapter": "mock"},
            "agent_selection": "always_ask",
            "repositories": {
                "worktree_root": str(tmp_path / "trees"),
                "search_roots": [str(tmp_path / "src")],
                "clone_root": str(tmp_path / "clones"),
            },
            "profiles": {"personal": {"default_agent": "claude", "agents": {"claude": {"executable": str(exe)}}}},
        }
    )
    return tmp_path


def _sandbox_clone(tmp_path: Path) -> Path:
    from test_live_github import SANDBOX

    clone = tmp_path / "src" / "sandbox"
    done = subprocess.run(["gh", "repo", "clone", SANDBOX, str(clone)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return clone


@live
def test_live_scenario_a_first_launch_from_an_unknown_issue(sandbox, launcher_home, monkeypatch):
    """Scenario A against a real new issue: the repository is unknown, so the clone, the profile and the agent are all
    asked; the stable GitHub IDs are stored; the worktree and the (mock) workspace exist; the prompt is not submitted."""
    from test_live_github import SANDBOX, _gh, subprocess_run_quiet

    url = _gh("issue", "create", "-R", SANDBOX, "--title", "agent-launcher scenario A (safe to close)", "--body", "Created by a test.").splitlines()[-1]
    try:
        prompter = ScriptedPrompter(("confirm", "Clone it into", True), ("select", "Which profile", "personal"), ("select", "Which agent", "claude"))
        shown = open_asking(monkeypatch, prompter, url)
        github = shown["github"]
        assert github["node_id"] and github["database_id"] > 0 and github["repository_github_id"] > 0
        assert shown["task"]["profile"] == "personal" and shown["task"]["workflow"] == "default"
        assert Path(shown["task"]["repo_path"]).parent == (sandbox / "clones").resolve()
        assert Path(shown["worktree"]["path"]).is_dir() and shown["worktree"]["ownership"] == "created"
        (create,) = mock_calls(launcher_home)
        assert create["prompt"] == url and create["submit_prompt"] is False
        assert json.loads(launcher_cli("profile", "which", shown["task"]["repo_path"], "--json").stdout)["profile"] == "personal"
    finally:
        subprocess_run_quiet(["gh", "issue", "close", url])


@live
def test_live_scenario_d_a_real_pr_with_an_existing_worktree(sandbox, launcher_home, monkeypatch):
    """Scenario D with a real PR whose head branch is already checked out in a worktree: it is offered, adopted and
    recorded, and no other worktree is created."""
    from test_live_github import _sandbox_pr

    clone = _sandbox_clone(sandbox)
    assert run("profile", "set", str(clone), "personal").exit_code == 0
    with _sandbox_pr() as (url, branch, head):
        git_run(clone, "fetch", "-q", "origin")
        existing = sandbox / "already-here"
        git_run(clone, "worktree", "add", "-q", "--track", "-b", branch, str(existing), f"origin/{branch}")
        before = git_run(clone, "worktree", "list")
        prompter = ScriptedPrompter(("select", "Which agent", "claude"), ("select", "no worktree yet", "wt:0"))
        shown = open_asking(monkeypatch, prompter, url)
        assert shown["worktree"]["ownership"] == "adopted" and Path(shown["worktree"]["path"]) == existing.resolve()
        assert git_run(clone, "worktree", "list") == before
        assert git.head_commit(shown["worktree"]["path"]) == head


@live
def test_live_scenario_e_a_labelled_pr_gets_its_workflow_skill(sandbox, launcher_home, monkeypatch):
    """Scenario E with a real PR. One account cannot be asked to review its own PR, so the rule matches a label that
    the test puts on the PR; the routing, the skill invocation and the unsubmitted prompt are the same code path."""
    from test_live_github import SANDBOX, _gh, _sandbox_pr

    config_dir = sandbox / "claude-config"
    (config_dir / "skills" / "code-review").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    label = "agent-launcher-live-review"
    _gh("label", "create", label, "-R", SANDBOX, "--force", "--color", "ededed")
    write_flows(
        launcher_home,
        {"id": "code-review", "priority": 80, "match": {"type": "pr", "labels_any": [label]}, "preferred_agent": "claude", "skill": "code-review"},
    )
    assert run("profile", "set", str(_sandbox_clone(sandbox)), "personal").exit_code == 0
    with _sandbox_pr() as (url, branch, head):
        _gh("pr", "edit", url, "--add-label", label)
        assert data(run("workflows", "test", url, "--json"))["winner"]["id"] == "code-review"
        shown = open_asking(monkeypatch, ScriptedPrompter(("select", "Which agent", "claude")), url)
        assert shown["task"]["workflow"] == "code-review"
        (create,) = mock_calls(launcher_home)
        assert create["prompt"] == f"/code-review {url}" and create["submit_prompt"] is False


@live
def test_live_scenario_g_link_a_local_task_to_a_new_issue(sandbox, launcher_home, monkeypatch):
    """Scenario G with a real new issue: the local task keeps its ID, worktree and session when linked, and opening the
    issue afterwards finds it."""
    from test_live_github import SANDBOX, _gh, subprocess_run_quiet

    clone = _sandbox_clone(sandbox)
    assert run("profile", "set", str(clone), "personal").exit_code == 0
    task = data(run("new", "--title", "Local first", "--repo", str(clone), "--json"))["task"]
    opened = open_asking(monkeypatch, ScriptedPrompter(("select", "Which agent", "claude")), task["id"])
    url = _gh("issue", "create", "-R", SANDBOX, "--title", "agent-launcher scenario G (safe to close)", "--body", "Created by a test.").splitlines()[-1]
    try:
        linked = data(run("tasks", "link", task["id"], url, "--json"))
        assert linked["linked"] and linked["task"]["id"] == task["id"] and linked["github"]["node_id"]
        again = open_asking(monkeypatch, ScriptedPrompter(), url)
        assert again["task"]["id"] == task["id"] and again["verb"] == "Focused"
        assert again["sessions"] == opened["sessions"] and again["worktree"] == opened["worktree"]
        assert len(mock_calls(launcher_home)) == 1
        with state.open_state() as conn:
            assert len(list_tasks(conn)) == 1
    finally:
        subprocess_run_quiet(["gh", "issue", "close", url])
