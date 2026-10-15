"""The ten end-to-end acceptance scenarios of SPEC §34 (A-J), one test each, driven through the CLI.

Each test runs the real `commodore` commands against a temp launcher home, real git in temp repositories, the mock
terminal and a fake `gh` (the fakes of `test_github_issues` / `test_github_pulls`: an in-memory GitHub that answers
`gh api` and `gh repo clone`). Nothing reaches GitHub or cmux. The docstring of every test lists the scenario's expected
results, numbered as in the SPEC, and the assertions below it check each one. Interactive questions are answered by a
`ScriptedPrompter` standing in for the terminal picker; a prompter with no steps proves that nothing is asked.

Scenarios A, E and G also exist as opt-in live tests at the end of this file, against a throwaway sandbox
repository you name (`COMMODORE_LAUNCHER_LIVE=1 COMMODORE_LAUNCHER_SANDBOX_REPO=<owner>/<repo>`, no cmux). What neither can show (the real cmux,
real agents' screens) is listed in docs/live-validation.md.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from commodore_launcher import cli, state
from commodore_launcher.tasks import list_tasks
from commodore_launcher.terminal_mock import CREATE_DELAY_ENV
from conftest import make_github_task
from scripted import ScriptedPrompter
from test_github_issues import URL, data, env, failure, mock_calls, run  # noqa: F401  (env is a fixture)
from test_github_issues import checkout  # noqa: F401  (fixture)
from test_github_pulls import PR_URL, gh, origin  # noqa: F401  (fixtures)


@pytest.fixture
def agent_bin(tmp_path) -> Path:
    bin_dir = tmp_path / "harnesses"
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
                "version": 3,
                "terminal": {"adapter": "mock"},
                "harness_selection": "always_ask",
                "repositories": {
                    "search_roots": [str(env.search)],
                    "clone_root": str(tmp_path / "clones"),
                },
                "profiles": {
                    "work": {"default_harness": "claude", "harnesses": agents(work_claude or agent_bin / "claude")},
                    "personal": {"default_harness": "claude", "harnesses": agents(agent_bin / "claude")},
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
    `tasks show --json` reports it: the task and its sessions. Fails if a scripted answer is left over."""
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
    """Worktrees git lists for the checkout: the launcher adds none, so it stays 1."""
    return len(subprocess.run(["git", "-C", str(path), "worktree", "list"], capture_output=True, text=True).stdout.splitlines())


def launcher_cli(*args: str) -> subprocess.CompletedProcess:
    """A separate `commodore` process: what survives between invocations."""
    return subprocess.run([sys.executable, "-m", "commodore_launcher", *args], capture_output=True, text=True, timeout=120)


# --- A ---------------------------------------------------------------------------------------------------------


def test_scenario_a_first_launch_from_an_unknown_repository(env, make_repo, gh, two_profiles, monkeypatch, launcher_home):
    """A GitHub issue is selected in a repository the launcher has never seen. Expected (SPEC §34 A):

    1. Repository identified.            2. Profile selection requested.      3. Association persisted.
    4. Agent picker displayed.           5. (no workflow: removed)           6. Started in the checkout (no worktree).
    7. cmux workspace created (mock).    8. Agent launched.                   9. Prompt submitted at launch.
    """
    path = unknown_checkout(env, make_repo)
    assert run("profile", "which", str(path), "--offline", "--json").exit_code != 0  # unknown: nothing assigned
    prompter = ScriptedPrompter(("select", "Which profile", "personal"), ("select", "Which harness", "codex"))
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
    # 5: nothing routes the task: it stores no workflow.
    assert task["workflow"] is None
    # 6: no worktree: the harness starts in the repository's own checkout, which is unchanged.
    assert "worktree" not in result and worktree_count(path) == 1
    # 7 and 8: one workspace, in the checkout, running the chosen agent of the chosen profile.
    (create,) = mock_calls(launcher_home)
    assert create["working_directory"] == str(path.resolve()) and session["harness"] == "codex"
    assert Path(create["command"][0]).name == "codex"
    # 9: the prompt is Codex's invocation of Commodore on the URL, passed on its command line and submitted.
    assert create["prompt"] == f"$commodore {URL}" and create["submit_prompt"] is True


# --- B ---------------------------------------------------------------------------------------------------------


def test_scenario_b_reopening_an_existing_issue(env, checkout, gh, two_profiles, monkeypatch, launcher_home):
    """The same issue is selected again. Expected (SPEC §34 B):

    - Existing task found.  - Existing session focused.  - No new agent selection.
    - No repeated prompt.
    """
    assert run("profile", "set", str(checkout), "personal", "--offline").exit_code == 0
    first = open_asking(monkeypatch, ScriptedPrompter(("select", "Which harness", "claude")), URL)

    again = open_asking(monkeypatch, ScriptedPrompter(), URL)  # any question fails the test
    assert again["verb"] == "Focused" and again["task"]["id"] == first["task"]["id"]  # found, not created
    assert again["sessions"] == first["sessions"]  # the same session, untouched
    assert [c["session"]["workspace_id"] for c in mock_calls(launcher_home, "focus_session")] == [
        first["sessions"][0]["terminal"]["workspace_id"]
    ]
    assert worktree_count(checkout) == 1  # and no worktree
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
    task = make_github_task(checkout, "Isolated")

    result = run("open", task["id"], "--harness", "claude", "--terminal", "mock", "--offline", "--json")
    error = failure(result)
    # Rejected, with a diagnostic that names the profile, the agent and the missing executable.
    assert error["code"] == "harness_unresolved"
    assert "work" in error["message"] and "claude" in error["message"] and "claude-work" in error["message"]
    # No personal fallback: nothing was launched, no session recorded.
    assert not (launcher_home / "mock-terminal.json").exists() or mock_calls(launcher_home) == []
    assert worktree_count(checkout) == 1
    with state.open_state() as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    # No reassignment: the repository still belongs to `work`, and so does the task.
    assert data(run("profile", "which", str(checkout), "--offline", "--json"))["profile"] == "work"
    assert data(run("tasks", "show", task["id"], "--json"))["task"]["profile"] == "work"
    # Fixing the executable (the work one) is the only way forward, and then it launches as `work`.
    two_profiles()
    ok = data(run("open", task["id"], "--harness", "claude", "--terminal", "mock", "--offline", "--json"))
    assert ok["session"]["profile"] == "work"


# --- E ---------------------------------------------------------------------------------------------------------


def test_scenario_e_a_review_request_gets_the_same_prompt(env, checkout, gh, origin, two_profiles, monkeypatch, launcher_home):
    """A PR requesting the user's review is launched like any other (workflows are gone, so no rule picks a skill).
    Expected:

    - The prompt is Commodore's invocation of the PR URL.  - Submitted at launch.  - No harness is highlighted by a rule.
    """
    assert run("profile", "set", str(checkout), "personal", "--offline").exit_code == 0
    sha = origin.commit_on("feat/x", "x.txt")
    origin.pull_head(5, sha)
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha, user={"login": "octo"},
                requested_reviewers=[{"login": "me"}])

    class Spy(ScriptedPrompter):
        labels = None

        def select(self, message, choices, default=None):
            if "Which harness" in message:
                Spy.labels = [c.label for c in choices]
            return super().select(message, choices, default)

    result = open_asking(monkeypatch, Spy(("select", "Which harness", "codex")), PR_URL)
    assert result["task"]["workflow"] is None
    assert not any("workflow" in label for label in Spy.labels)
    (create,) = mock_calls(launcher_home)
    assert create["prompt"] == f"$commodore {PR_URL}"  # Codex's `$skill` syntax
    assert create["submit_prompt"] is True


# --- H ---------------------------------------------------------------------------------------------------------


def test_scenario_h_concurrent_launch(env, checkout, two_profiles, launcher_home):
    """Two processes open the same task at the same moment. Expected (SPEC §34 H):

    - One canonical task.  - One primary session.  - No worktree.  - No corrupted database state.
    """
    assert run("profile", "set", str(checkout), "work", "--offline").exit_code == 0
    task_id = make_github_task(checkout, "Race")["id"]
    environment = {**os.environ, CREATE_DELAY_ENV: "1.0"}  # keeps both inside the launch window
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "commodore_launcher", "open", task_id, "--harness", "claude", "--terminal", "mock", "--offline", "--json"],
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
        counts = [conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("tasks", "sessions")]
        assert counts == [1, 1]
        assert conn.execute("SELECT count(*) FROM launches").fetchone()[0] == 0  # nothing left half-done
    assert worktree_count(checkout) == 1


# --- I ---------------------------------------------------------------------------------------------------------


def test_scenario_i_partial_launch_failure(env, checkout, two_profiles, launcher_home):
    """The terminal fails. Expected (SPEC §34 I):

    - Recoverable task state.  - Clear error.  - Retry succeeds without creating duplicates.
    """
    assert run("profile", "set", str(checkout), "work", "--offline").exit_code == 0
    task_id = make_github_task(checkout, "Flaky")["id"]
    launcher_home.mkdir(parents=True, exist_ok=True)
    (launcher_home / "mock-terminal.json").write_text(json.dumps({"fail_create": "cmux is not responding"}))

    failed = run("open", task_id, "--harness", "claude", "--terminal", "mock", "--offline", "--json")
    error = failure(failed)
    assert error["code"] == "terminal_create_failed" and "cmux is not responding" in error["message"]  # clear
    assert data(run("tasks", "show", task_id, "--json"))["task"]["state"] == "launch_failed"  # recoverable, not "active"

    (launcher_home / "mock-terminal.json").write_text(json.dumps({}))  # the terminal is back
    retried = data(run("open", task_id, "--harness", "claude", "--terminal", "mock", "--offline", "--json"))
    assert retried["action"] == "created"
    assert worktree_count(checkout) == 1 and len(mock_calls(launcher_home)) == 1
    with state.open_state() as conn:
        counts = [conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("tasks", "sessions")]
        assert counts == [1, 1]  # no duplicates
    assert data(run("tasks", "show", task_id, "--json"))["task"]["state"] == "active"


# --- J ---------------------------------------------------------------------------------------------------------


def test_scenario_j_gh_dash_bootstrap(tmp_path, make_repo, agent_bin, fake_default_home, monkeypatch, launcher_home):
    """Initial setup completes, then the user chooses to configure gh-dash. Expected (SPEC §34 J):

    - Local maintenance task created.  - Management skill selected.  - Agent launched in cmux (mock here).
    - Integration request submitted at launch.  - Existing gh-dash configuration preserved.
    """
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({
        "profiles": [{"name": "personal", "harnesses": {"claude": {"executable": str(agent_bin / "claude")}}, "default_harness": "claude"}],
        "harness_selection": "use_default",
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
    assert result["task"]["source"] == "local" and result["task"]["title"] == "Integrate Commodore Launcher with gh-dash"
    assert result["task"]["workflow"] == "commodore-launcher-configure"  # the management skill
    assert result["prompt"].startswith("/commodore-launcher ")
    calls = [c for c in mock_calls(launcher_home) if "gh-dash" in (c["prompt"] or "")]
    assert len(calls) == 1 and calls[0]["prompt"] == result["prompt"]  # the agent was launched with the prompt
    assert calls[0]["submit_prompt"] is True and result["prompt_submitted"]
    assert "Preserve all existing" in result["prompt"]
    assert gh_dash_config.read_bytes() == before  # Commodore Launcher itself wrote nothing to gh-dash's config


# --- Live: A, D, E and G against the throwaway sandbox repository (opt-in, no cmux) -----------------------------
#
# `COMMODORE_LAUNCHER_LIVE=1 COMMODORE_LAUNCHER_SANDBOX_REPO=<owner>/<repo> uv run pytest tests/test_scenarios.py`. Real `gh` (read for repository IDs and issue and PR
# data; it creates and closes issues and PRs in the sandbox only) and real git; the mock terminal; no agent is started.
# They show what the fakes above cannot: that GitHub's real responses are understood.

live = pytest.mark.skipif(
    not (os.environ.get("COMMODORE_LAUNCHER_LIVE") and os.environ.get("COMMODORE_LAUNCHER_SANDBOX_REPO")),
    reason="set COMMODORE_LAUNCHER_LIVE=1 and COMMODORE_LAUNCHER_SANDBOX_REPO=<owner>/<repo> to run",
)


@pytest.fixture
def sandbox(monkeypatch, tmp_path, write_config):
    """Real gh, real HOME for gh's own login (read only), a config with one profile, temp roots, mock terminal."""
    import pwd

    from commodore_launcher import repositories

    monkeypatch.setattr(repositories, "run_command", repositories._real_run_command)
    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (tmp_path / "src").mkdir()
    write_config(
        {
            "version": 3,
            "terminal": {"adapter": "mock"},
            "harness_selection": "always_ask",
            "repositories": {
                "search_roots": [str(tmp_path / "src")],
                "clone_root": str(tmp_path / "clones"),
            },
            "profiles": {"personal": {"default_harness": "claude", "harnesses": {"claude": {"executable": str(exe)}}}},
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
    asked; the stable GitHub IDs are stored; the (mock) workspace starts in the clone; the prompt is Commodore's invocation, submitted at launch."""
    from test_live_github import SANDBOX, _gh, subprocess_run_quiet

    url = _gh("issue", "create", "-R", SANDBOX, "--title", "commodore scenario A (safe to close)", "--body", "Created by a test.").splitlines()[-1]
    try:
        prompter = ScriptedPrompter(("confirm", "Clone it into", True), ("select", "Which profile", "personal"), ("select", "Which harness", "claude"))
        shown = open_asking(monkeypatch, prompter, url)
        github = shown["github"]
        assert github["node_id"] and github["database_id"] > 0 and github["repository_github_id"] > 0
        assert shown["task"]["profile"] == "personal" and shown["task"]["workflow"] is None
        assert Path(shown["task"]["repo_path"]).parent == (sandbox / "clones").resolve()
        (create,) = mock_calls(launcher_home)
        assert create["working_directory"] == shown["task"]["repo_path"]
        assert create["prompt"] == f"/commodore {url}"  # Claude Code, the harness picked
        assert create["submit_prompt"] is True
        assert json.loads(launcher_cli("profile", "which", shown["task"]["repo_path"], "--json").stdout)["profile"] == "personal"
    finally:
        subprocess_run_quiet(["gh", "issue", "close", url])


@live
def test_live_scenario_e_a_real_pr_gets_the_commodore_invocation(sandbox, launcher_home, monkeypatch):
    """Scenario E with a real PR: nothing routes it, so the prompt is the harness's Commodore invocation of its URL,
    submitted at launch."""
    from test_live_github import _sandbox_pr

    assert run("profile", "set", str(_sandbox_clone(sandbox)), "personal").exit_code == 0
    with _sandbox_pr() as (url, branch, head):
        shown = open_asking(monkeypatch, ScriptedPrompter(("select", "Which harness", "claude")), url)
        assert shown["task"]["workflow"] is None
        (create,) = mock_calls(launcher_home)
        assert create["prompt"] == f"/commodore {url}" and create["submit_prompt"] is True
