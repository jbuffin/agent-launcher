"""The ten end-to-end acceptance scenarios (A-J), one test each, driven through the CLI.

Each test runs the real `commodore` commands against a temp launcher home, real git in temp repositories, the recording
session host (or, across processes, the real one with a fake harness) and a fake `gh` (the fakes of `test_github_issues`
/ `test_github_pulls`: an in-memory GitHub that answers `gh api` and `gh repo clone`). Nothing reaches GitHub. The docstring of every test lists the scenario's expected
results, numbered as in the scenario list, and the assertions below it check each one. Interactive questions are answered by a
`ScriptedPrompter` standing in for the terminal picker; a prompter with no steps proves that nothing is asked.

Scenarios A, E and G also exist as opt-in live tests at the end of this file, against a throwaway sandbox
repository you name (`COMMODORE_LAUNCHER_LIVE=1 COMMODORE_LAUNCHER_SANDBOX_REPO=<owner>/<repo>`). What neither can show (real
harnesses in a real terminal) is listed in docs/live-validation.md.
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
from conftest import harness_runs, make_github_task
from scripted import ScriptedPrompter
from test_github_issues import URL, data, env, failure, run  # noqa: F401  (env is a fixture)
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
    """`work` and `personal`, each with Claude and Codex, asking which harness to run every time."""

    def _config(work_claude: Path | None = None, **extra) -> None:
        def agents(claude: Path):
            return {"claude": {"executable": str(claude)}, "codex": {"executable": str(agent_bin / "codex")}}

        write_config(
            {
                "version": 4,
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
    result = run("open", target, *extra)
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
    """A GitHub issue is selected in a repository the launcher has never seen. Expected:

    1. Repository identified.            2. Profile selection requested.      3. Association persisted.
    4. Harness picker displayed.         5. (no workflow: removed)           6. Started in the checkout (no worktree).
    7. Harness run in this terminal.     8. Harness launched.                9. Prompt submitted at launch.
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
    # 7 and 8: one harness run, in the checkout, of the chosen agent of the chosen profile.
    (create,) = harness_runs()
    assert create["working_directory"] == str(path.resolve()) and session["harness"] == "codex"
    assert Path(create["command"][0]).name == "codex"
    # 9: the prompt is Codex's invocation of Commodore on the URL, passed on its command line and submitted.
    assert create["prompt"] == f"$commodore {URL}" and create["submit_prompt"] is True


# --- B ---------------------------------------------------------------------------------------------------------


def test_scenario_b_reopening_an_existing_issue(env, checkout, gh, two_profiles, monkeypatch, launcher_home):
    """The same issue is selected again. Expected:

    - Existing task found.  - Existing session resumed.  - No new agent selection.
    - No repeated prompt.
    """
    assert run("profile", "set", str(checkout), "personal", "--offline").exit_code == 0
    first = open_asking(monkeypatch, ScriptedPrompter(("select", "Which harness", "claude")), URL)

    again = open_asking(monkeypatch, ScriptedPrompter(), URL)  # any question fails the test
    assert again["verb"] == "Resuming" and again["task"]["id"] == first["task"]["id"]  # found, not created
    assert again["sessions"] == first["sessions"]  # the same session and conversation
    conversation = first["sessions"][0]["harness_conversation_id"]
    resumed = harness_runs()[1]
    assert resumed["argv"][-2:] == ["--resume", conversation] and resumed["prompt"] is None  # no prompt again
    assert worktree_count(checkout) == 1  # and no worktree
    assert len(harness_runs()) == 2
    with state.open_state() as conn:
        assert len(list_tasks(conn)) == 1


# --- C ---------------------------------------------------------------------------------------------------------


def test_scenario_c_work_personal_isolation(env, checkout, gh, two_profiles, agent_bin, tmp_path, launcher_home):
    """A repository assigned to `work` requests Claude, and the work Claude executable is missing. Expected:

    - Launch rejected.  - No personal Claude fallback.  - No profile reassignment.  - Clear diagnostic error.
    """
    two_profiles(work_claude=tmp_path / "no-such-dir" / "claude-work")  # `personal` still has a working claude
    assert run("profile", "set", str(checkout), "work", "--offline").exit_code == 0
    task = make_github_task(checkout, "Isolated")

    result = run("open", task["id"], "--harness", "claude", "--offline", "--dry-run", "--json")
    error = failure(result)
    # Rejected, with a diagnostic that names the profile, the agent and the missing executable.
    assert error["code"] == "harness_unresolved"
    assert "work" in error["message"] and "claude" in error["message"] and "claude-work" in error["message"]
    # No personal fallback: nothing was launched, no session recorded.
    assert run("open", task["id"], "--harness", "claude", "--offline").exit_code == 1 and harness_runs() == []
    assert worktree_count(checkout) == 1
    with state.open_state() as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    # No reassignment: the repository still belongs to `work`, and so does the task.
    assert data(run("profile", "which", str(checkout), "--offline", "--json"))["profile"] == "work"
    assert data(run("tasks", "show", task["id"], "--json"))["task"]["profile"] == "work"
    # Fixing the executable (the work one) is the only way forward, and then it launches as `work`.
    two_profiles()
    ok = data(run("open", task["id"], "--harness", "claude", "--offline"))
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
    (create,) = harness_runs()
    assert create["prompt"] == f"$commodore {PR_URL}"  # Codex's `$skill` syntax
    assert create["submit_prompt"] is True


# --- H ---------------------------------------------------------------------------------------------------------


def test_scenario_h_concurrent_launch(env, checkout, two_profiles, agent_bin, launcher_home):
    """Two processes open the same task at the same moment. Expected:

    - One canonical task.  - One primary session.  - One harness.  - No worktree.  - No corrupted database state.
    """
    assert run("profile", "set", str(checkout), "work", "--offline").exit_code == 0
    task_id = make_github_task(checkout, "Race")["id"]
    (agent_bin / "claude").write_text("#!/bin/sh\nsleep 3\n")  # a real process, still running when the other looks
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "commodore_launcher", "open", task_id, "--harness", "claude", "--offline"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
        )
        for _ in range(2)
    ]
    outs = [p.communicate(timeout=120) for p in procs]
    assert sorted(p.returncode for p in procs) == [0, 1], outs
    assert sum("already running" in err for _, err in outs) == 1  # the other one was told, and started nothing
    with state.open_state() as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        counts = [conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("tasks", "sessions")]
        assert counts == [1, 1]
        assert conn.execute("SELECT count(*) FROM launches").fetchone()[0] == 0  # nothing left half-done
    assert worktree_count(checkout) == 1


# --- I ---------------------------------------------------------------------------------------------------------


def test_scenario_i_partial_launch_failure(env, checkout, two_profiles, host, launcher_home):
    """The harness cannot be started. Expected:

    - Recoverable task state.  - Clear error.  - Retry succeeds without creating duplicates.
    """
    assert run("profile", "set", str(checkout), "work", "--offline").exit_code == 0
    task_id = make_github_task(checkout, "Flaky")["id"]
    host.fail = "Could not start claude: No such file or directory."

    failed = run("open", task_id, "--harness", "claude", "--offline")
    assert failed.exit_code == 1 and "Could not start claude" in failed.output  # clear
    shown = data(run("tasks", "show", task_id, "--json"))
    assert shown["running"] is False and len(shown["sessions"]) == 1  # recoverable: nothing holds the session

    host.fail = None  # the harness can start again
    retried = data(run("open", task_id, "--offline"))
    assert retried["action"] == "created" and "no saved conversation" in retried["notice"]  # it never ran
    assert worktree_count(checkout) == 1 and len(harness_runs()) == 1
    with state.open_state() as conn:
        counts = [conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("tasks", "sessions")]
        assert counts == [1, 1]  # no duplicates
    assert data(run("tasks", "show", task_id, "--json"))["task"]["state"] == "active"


# --- J ---------------------------------------------------------------------------------------------------------


def test_scenario_j_configure_bootstrap(tmp_path, make_repo, agent_bin, launcher_home):
    """Initial setup completes, then the user asks a harness to change the configuration. Expected:

    - Maintenance session recorded.  - Management skill selected.  - Harness run in this terminal.
    - The request submitted at launch.
    """
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({
        "profiles": [{"name": "personal", "harnesses": {"claude": {"executable": str(agent_bin / "claude")}}, "default_harness": "claude"}],
        "harness_selection": "use_default",
    }))
    done = run("setup", "--answers", str(answers), "--yes")
    assert done.exit_code == 0, done.output  # initial setup completes
    maintenance_repo = make_repo("launcher-maintenance")

    result = data(run("configure", "add ~/src as a search root", "--repo", str(maintenance_repo), "--profile", "personal"))
    assert result["task"]["source"] == "local" and result["task"]["title"] == "Configure Commodore Launcher"
    assert result["task"]["workflow"] == "commodore-launcher-configure"  # the management skill
    assert result["prompt"].startswith("/commodore-launcher ") and "add ~/src as a search root" in result["prompt"]
    (call,) = harness_runs()
    assert call["prompt"] == result["prompt"] and call["submit_prompt"] is True  # launched with the request
    assert call["working_directory"] == str(maintenance_repo.resolve())


# --- Live: A, D, E and G against the throwaway sandbox repository (opt-in) ------------------------------------
#
# `COMMODORE_LAUNCHER_LIVE=1 COMMODORE_LAUNCHER_SANDBOX_REPO=<owner>/<repo> uv run pytest tests/test_scenarios.py`. Real `gh` (read for repository IDs and issue and PR
# data; it creates and closes issues and PRs in the sandbox only) and real git; the recording host; no agent is started.
# They show what the fakes above cannot: that GitHub's real responses are understood.

live = pytest.mark.skipif(
    not (os.environ.get("COMMODORE_LAUNCHER_LIVE") and os.environ.get("COMMODORE_LAUNCHER_SANDBOX_REPO")),
    reason="set COMMODORE_LAUNCHER_LIVE=1 and COMMODORE_LAUNCHER_SANDBOX_REPO=<owner>/<repo> to run",
)


@pytest.fixture
def sandbox(monkeypatch, tmp_path, write_config):
    """Real gh, real HOME for gh's own login (read only), a config with one profile, temp roots, the recording host."""
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
            "version": 4,
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
    asked; the stable GitHub IDs are stored; the (recorded) harness starts in the clone; the prompt is Commodore's invocation, submitted at launch."""
    from test_live_github import SANDBOX, _gh, subprocess_run_quiet

    url = _gh("issue", "create", "-R", SANDBOX, "--title", "commodore scenario A (safe to close)", "--body", "Created by a test.").splitlines()[-1]
    try:
        prompter = ScriptedPrompter(("confirm", "Clone it into", True), ("select", "Which profile", "personal"), ("select", "Which harness", "claude"))
        shown = open_asking(monkeypatch, prompter, url)
        github = shown["github"]
        assert github["node_id"] and github["database_id"] > 0 and github["repository_github_id"] > 0
        assert shown["task"]["profile"] == "personal" and shown["task"]["workflow"] is None
        assert Path(shown["task"]["repo_path"]).parent == (sandbox / "clones").resolve()
        (create,) = harness_runs()
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
        (create,) = harness_runs()
        assert create["prompt"] == f"/commodore {url}" and create["submit_prompt"] is True
