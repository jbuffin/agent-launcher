"""Task worktrees (ticket #10), on real git in temp repositories and bare "remotes"."""

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_launcher import git, state
from agent_launcher.cli import app
from agent_launcher.config import load_config
from agent_launcher.errors import LauncherError
from agent_launcher.tasks import get_task
from agent_launcher.worktrees import (
    branch_name_for,
    ensure_worktree,
    get_worktree,
    slugify,
    working_directory,
    worktree_path_for,
)
from conftest import git_run
from scripted import ScriptedPrompter

runner = CliRunner()


def run(*args):
    return runner.invoke(app, list(args))


def data(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def failure(result):
    assert result.exit_code != 0
    return json.loads(result.stdout)["error"]


@pytest.fixture
def fake_agents(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return exe


@pytest.fixture
def configure(write_config, fake_agents, tmp_path):
    def _configure(**repositories):
        write_config(
            {
                "version": 2,
                "terminal": {"adapter": "mock"},
                "agent_selection": "use_default",
                "repositories": {"worktree_root": str(tmp_path / "trees"), **repositories},
                "profiles": {"work": {"default_agent": "claude", "agents": {"claude": {"executable": str(fake_agents)}}}},
            }
        )

    _configure()
    return _configure


@pytest.fixture
def new_task(configure):
    def _new(repo: Path, title: str = "Fix login") -> str:
        assert run("profile", "set", str(repo), "work", "--offline").exit_code in (0, 1)
        return data(run("new", "--title", title, "--repo", str(repo), "--offline", "--json"))["task"]["id"]

    return _new


@pytest.fixture
def repo(make_repo):
    return make_repo("one")


def ensure(task_id, prompter=None, offline=True):
    with state.open_state() as conn:
        return ensure_worktree(conn, get_task(conn, task_id), load_config(), prompter, offline=offline)


def make_remote(tmp_path: Path, default: str = "trunk") -> tuple[Path, Path]:
    """A bare remote whose default branch is `default`, and a clone of it (origin/HEAD set)."""
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-q", "-b", default, str(seed)], check=True)
    git_run(seed, "commit", "-q", "--allow-empty", "-m", "seed")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
    subprocess.run(["git", "--git-dir", str(bare), "symbolic-ref", "HEAD", f"refs/heads/{default}"], check=True)
    clone = tmp_path / "repos" / "clone"
    clone.parent.mkdir(exist_ok=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    return bare, clone


def push_commit(bare: Path, tmp_path: Path, branch: str, message: str) -> str:
    other = tmp_path / f"pusher-{message}"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    git_run(other, "checkout", "-q", "-B", branch)
    git_run(other, "commit", "-q", "--allow-empty", "-m", message)
    git_run(other, "push", "-q", "origin", branch)
    return git_run(other, "rev-parse", "HEAD").strip()


# --- git module -------------------------------------------------------------------------------------------


def test_porcelain_z_parsing_handles_odd_paths_and_flags():
    out = (
        "worktree /r/main\0HEAD aaa\0branch refs/heads/main\0\0"
        "worktree /r/with space\nand newline\0HEAD bbb\0detached\0locked because\0\0"
        "worktree /r/old\0HEAD ccc\0branch refs/heads/task/x\0prunable gitdir file points to non-existent location\0\0"
        "worktree /r/bare\0bare\0\0"
    )
    main, odd, old, bare = git.parse_worktree_list(out)
    assert main.branch_name == "main" and not main.detached
    assert odd.path == "/r/with space\nand newline" and odd.detached and odd.locked and odd.branch is None
    assert old.prunable and old.branch_name == "task/x"
    assert bare.bare and bare.head is None


def test_list_worktrees_reads_real_git(repo, tmp_path):
    other = tmp_path / "side tree"
    git_run(repo, "worktree", "add", "-q", "-b", "side", "--", str(other))
    entries = git.list_worktrees(repo)
    assert [e.branch_name for e in entries] == ["main", "side"]
    assert git.canonical(entries[1].path) == git.canonical(other)


def test_slugify_and_branch_names_are_safe(repo, new_task):
    assert slugify("  Fix: the --force $(rm -rf /) bug!!  ") == "fix-the-force-rm-rf-bug"
    assert slugify("日本語") == "" and slugify("-----") == ""
    task_id = new_task(repo, "日本語")
    with state.open_state() as conn:
        assert branch_name_for(get_task(conn, task_id), str(repo)) == f"task/{task_id}"


# --- creating ---------------------------------------------------------------------------------------------


def test_worktree_is_created_on_a_task_branch_under_the_root(repo, new_task, tmp_path):
    task_id = new_task(repo, "Fix login")
    result = ensure(task_id)
    record = result.record
    assert result.action == "created" and record.ownership == "created"
    assert record.branch == f"task/{task_id}-fix-login"
    expected = tmp_path / "trees" / f"one-{record.repository_id}" / task_id
    assert Path(record.path) == expected.resolve() and (expected / ".git").exists()
    assert git_run(expected, "symbolic-ref", "--short", "HEAD").strip() == record.branch
    # Without --no-track the branch would track (and `git push` would target) the base.
    assert git.run_git(repo, ["config", "--get", f"branch.{record.branch}.merge"], ok=(0, 1)).stdout == ""
    with state.open_state() as conn:
        assert working_directory(conn, get_task(conn, task_id)) == record.path


def test_hostile_titles_never_reach_git_as_options_or_leave_the_root(repo, new_task, tmp_path):
    for title in ("--orphan x", "-b evil", "../../escape", "a/../b", "; touch pwned", "$(touch pwned)", "x" * 500):
        task_id = new_task(repo, title)
        record = ensure(task_id).record
        assert Path(record.path).parent.parent == (tmp_path / "trees").resolve()
        assert record.branch.startswith(f"task/{task_id}") and len(record.branch) < 70
    assert not (tmp_path / "pwned").exists() and not Path("pwned").exists()
    assert not (tmp_path / "escape").exists()


def test_two_repositories_with_one_name_get_separate_directories(make_repo, new_task, tmp_path):
    a = make_repo("same")
    b = tmp_path / "elsewhere" / "same"
    b.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(b)], check=True)
    git_run(b, "commit", "-q", "--allow-empty", "-m", "i")
    pa, pb = ensure(new_task(a)).record.path, ensure(new_task(b)).record.path
    assert Path(pa).parent != Path(pb).parent


def test_base_branch_comes_from_origin_head_not_main(tmp_path, new_task):
    bare, clone = make_remote(tmp_path, "trunk")
    tip = push_commit(bare, tmp_path, "trunk", "newer")  # origin is ahead of the clone: fetching is allowed
    result = ensure(new_task(clone), offline=False)
    assert result.record.base_ref == "refs/remotes/origin/trunk"
    assert git_run(result.record.path, "rev-parse", "HEAD").strip() == tip
    # The local checkout's own branch was not moved.
    assert git_run(clone, "rev-parse", "trunk").strip() != tip


def test_configured_base_branch_beats_origin_head(tmp_path, new_task, configure):
    bare, clone = make_remote(tmp_path, "trunk")
    develop = push_commit(bare, tmp_path, "develop", "dev")
    configure(base_branches={str(clone.resolve()): "develop"})
    record = ensure(new_task(clone), offline=False).record
    assert record.base_ref == "refs/remotes/origin/develop"
    assert git_run(record.path, "rev-parse", "HEAD").strip() == develop


def test_without_a_remote_the_local_default_is_used(repo, new_task):
    git_run(repo, "branch", "-m", "main", "develop")
    assert ensure(new_task(repo)).record.base_ref == "refs/heads/develop"


def test_failed_fetch_is_soft(tmp_path, new_task):
    bare, clone = make_remote(tmp_path)
    subprocess.run(["mv", str(bare), str(bare) + ".gone"], check=True)
    result = ensure(new_task(clone), offline=False)
    assert result.record.base_ref == "refs/remotes/origin/trunk"
    assert result.notices and "Could not fetch" in result.notices[0]


def test_offline_does_not_fetch(tmp_path, new_task):
    bare, clone = make_remote(tmp_path)
    tip = push_commit(bare, tmp_path, "trunk", "later")
    result = ensure(new_task(clone), offline=True)
    assert git_run(result.record.path, "rev-parse", "HEAD").strip() != tip and not result.notices


def test_repository_without_commits_is_a_structured_error(make_repo, new_task):
    empty = make_repo("empty", commit=False)
    task_id = new_task(empty)
    with pytest.raises(LauncherError) as caught:
        ensure(task_id)
    assert caught.value.code == "base_branch_missing"


def test_an_existing_path_is_never_overwritten(repo, new_task, tmp_path):
    task_id = new_task(repo)
    with state.open_state() as conn:
        target = worktree_path_for(get_task(conn, task_id), load_config())
    target.mkdir(parents=True)
    (target / "mine.txt").write_text("keep")
    with pytest.raises(LauncherError) as caught:
        ensure(task_id)
    assert caught.value.code == "worktree_path_exists" and (target / "mine.txt").read_text() == "keep"


def test_second_ensure_reuses_the_record_and_missing_dirs_are_not_recreated(repo, new_task):
    task_id = new_task(repo)
    first = ensure(task_id)
    again = ensure(task_id)
    assert again.action == "existing" and again.record == first.record
    git_run(repo, "worktree", "remove", "--force", first.record.path)
    with pytest.raises(LauncherError) as caught:
        ensure(task_id)
    assert caught.value.code == "worktree_missing"


# --- adopting ---------------------------------------------------------------------------------------------


def foreign_worktree(repo: Path, tmp_path: Path, branch: str = "someone-elses") -> Path:
    path = tmp_path / "foreign" / branch.replace("/", "_")
    git_run(repo, "worktree", "add", "-q", "-b", branch, "--", str(path))
    return path.resolve()


def test_adoption_is_asked_once_recorded_and_not_asked_again(repo, new_task, tmp_path):
    existing = foreign_worktree(repo, tmp_path)
    task_id = new_task(repo)
    script = ScriptedPrompter(("select", "Adopt an existing one", "wt:0"))
    result = ensure(task_id, script)
    script.done()
    assert result.action == "adopted" and result.record.ownership == "adopted"
    assert Path(result.record.path) == existing and result.record.branch == "someone-elses"
    # The same prompter script is exhausted: a second ask would fail the test.
    again = ensure(task_id, ScriptedPrompter())
    assert again.action == "existing" and again.record.ownership == "adopted"


def test_choosing_new_creates_instead_of_adopting(repo, new_task, tmp_path):
    foreign_worktree(repo, tmp_path)
    result = ensure(new_task(repo), ScriptedPrompter(("select", "Adopt", "new")))
    assert result.record.ownership == "created"


def test_without_a_prompter_nothing_is_adopted(repo, new_task, tmp_path):
    foreign_worktree(repo, tmp_path)
    assert ensure(new_task(repo)).record.ownership == "created"


def test_a_branch_name_match_alone_is_not_ownership(repo, new_task, tmp_path):
    task_id = new_task(repo, "Fix login")
    lookalike = foreign_worktree(repo, tmp_path, f"task/{task_id}-fix-login")
    # Not interactive: the branch exists, so a new worktree cannot be cut. It is refused, never adopted.
    with pytest.raises(LauncherError) as caught:
        ensure(task_id)
    err = caught.value
    assert err.code == "branch_conflict" and str(lookalike) in err.message and "worktrees associate" in err.message
    assert err.details["worktree"] == str(lookalike)
    with state.open_state() as conn:
        assert get_worktree(conn, task_id) is None
    # Interactive: the same tree is offered, and only becomes the task's when chosen. "new" is not offered.
    script = ScriptedPrompter(("select", "Adopt", "wt:0"))
    assert ensure(task_id, script).record.ownership == "adopted"


def test_a_dirty_worktree_needs_confirmation_and_is_never_cleaned(repo, new_task, tmp_path):
    existing = foreign_worktree(repo, tmp_path)
    (existing / "wip.txt").write_text("unsaved")
    task_id = new_task(repo)
    declined = ScriptedPrompter(("select", "Adopt", "wt:0"), ("confirm", "uncommitted changes", False))
    with pytest.raises(LauncherError) as caught:
        ensure(task_id, declined)
    assert caught.value.code == "declined"
    accepted = ScriptedPrompter(("select", "Adopt", "wt:0"), ("confirm", "uncommitted changes", True))
    result = ensure(task_id, accepted)
    assert result.action == "adopted" and "uncommitted" in result.notices[0]
    assert (existing / "wip.txt").read_text() == "unsaved"


def test_main_checkout_and_other_tasks_trees_are_not_offered(repo, new_task):
    first = ensure(new_task(repo)).record
    # The only other worktree belongs to a task, and the main checkout never counts: no choices, so it creates.
    second = ensure(new_task(repo, "Second"), ScriptedPrompter())
    assert second.action == "created" and second.record.path != first.path


# --- the CLI ------------------------------------------------------------------------------------------------


def test_associate_adopts_explicitly_and_remembers(repo, new_task, tmp_path):
    existing = foreign_worktree(repo, tmp_path)
    task_id = new_task(repo)
    out = data(run("worktrees", "associate", task_id, str(existing), "--json"))
    assert out["worktree"]["ownership"] == "adopted" and out["dirty"] is False
    assert ensure(task_id).action == "existing"
    # Idempotent for the same tree; a different one is refused.
    assert data(run("worktrees", "associate", task_id, str(existing), "--json"))["worktree"]["path"] == str(existing)
    other = foreign_worktree(repo, tmp_path, "another")
    assert failure(run("worktrees", "associate", task_id, str(other), "--json"))["code"] == "worktree_already_set"


def test_associate_refuses_main_checkout_strangers_and_taken_trees(repo, new_task, tmp_path, make_repo):
    task_id, other_task = new_task(repo), new_task(repo, "Other")
    assert failure(run("worktrees", "associate", task_id, str(repo), "--json"))["code"] == "main_worktree"
    assert failure(run("worktrees", "associate", task_id, str(tmp_path), "--json"))["code"] == "not_a_worktree"
    stranger = make_repo("stranger")
    assert failure(run("worktrees", "associate", task_id, str(stranger), "--json"))["code"] == "not_a_worktree"
    taken = ensure(other_task).record.path
    assert failure(run("worktrees", "associate", task_id, taken, "--json"))["code"] == "worktree_taken"


def test_associate_reports_dirty_and_leaves_it_alone(repo, new_task, tmp_path):
    existing = foreign_worktree(repo, tmp_path)
    (existing / "wip.txt").write_text("unsaved")
    out = data(run("worktrees", "associate", new_task(repo), str(existing), "--json"))
    assert out["dirty"] is True and (existing / "wip.txt").read_text() == "unsaved"
    assert git_run(existing, "status", "--porcelain").strip() == "?? wip.txt"


def test_list_and_inspect(repo, new_task):
    task_id = new_task(repo)
    assert data(run("worktrees", "list", "--json")) == {"worktrees": []}
    assert failure(run("worktrees", "inspect", task_id, "--json"))["code"] == "no_worktree"
    record = ensure(task_id).record
    (listed,) = data(run("worktrees", "list", "--json"))["worktrees"]
    assert listed["task_id"] == task_id and listed["exists"] is True and listed["ownership"] == "created"
    info = data(run("worktrees", "inspect", task_id, "--json"))["worktree"]
    assert info["path"] == record.path and info["listed"] and info["exists"] and info["dirty"] is False
    assert info["branch_now"] == record.branch and info["head"]
    (Path(record.path) / "new.txt").write_text("x")
    assert data(run("worktrees", "inspect", task_id, "--json"))["worktree"]["dirty"] is True
    text = run("worktrees", "inspect", task_id)
    assert text.exit_code == 0 and "ownership: created" in text.output


# --- open -----------------------------------------------------------------------------------------------------


def test_open_runs_the_agent_in_the_worktree_and_reuses_it_on_restart(repo, new_task, launcher_home):
    task_id = new_task(repo)
    opened = data(run("open", task_id, "--offline", "--json"))
    path = opened["worktree"]["path"]
    calls = json.loads((launcher_home / "mock-terminal.json").read_text())["calls"]
    assert calls[0]["working_directory"] == path
    assert data(run("restart", task_id, "--yes", "--offline", "--json"))["action"] == "restarted"
    calls = json.loads((launcher_home / "mock-terminal.json").read_text())["calls"]
    assert [c["working_directory"] for c in calls if c["op"] == "create_session"] == [path, path]


def test_a_task_opened_before_worktrees_keeps_its_repository_directory(repo, new_task):
    task_id = new_task(repo)
    with state.open_state() as conn:
        task = get_task(conn, task_id)
        assert working_directory(conn, task) == task.repo_path


def test_branch_conflict_on_open_launches_nothing(repo, new_task, launcher_home):
    task_id = new_task(repo, "Fix")
    git_run(repo, "branch", f"task/{task_id}-fix")
    err = failure(run("open", task_id, "--offline", "--json"))
    assert err["code"] == "branch_conflict"
    assert not (launcher_home / "mock-terminal.json").exists() or not json.loads(
        (launcher_home / "mock-terminal.json").read_text()
    )["calls"]
    with state.open_state() as conn:
        assert get_worktree(conn, task_id) is None


def test_schema_has_the_worktrees_table_at_version_four():
    with state.open_state() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == state.SCHEMA_VERSION >= 4
        cols = {r[1] for r in conn.execute("PRAGMA table_info(worktrees)")}
        assert {"task_id", "path", "branch", "ownership", "base_ref"} <= cols
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO worktrees VALUES ('x', 1, '/p', NULL, 'guessed', NULL, 'now')"
            )


# --- review round 1 ---------------------------------------------------------------------------------------


def mock_calls(launcher_home):
    return json.loads((launcher_home / "mock-terminal.json").read_text())["calls"]


def test_restart_and_resume_stop_before_acting_when_the_worktree_is_gone(repo, new_task, launcher_home):
    task_id = new_task(repo)
    opened = data(run("open", task_id, "--offline", "--json"))
    git_run(repo, "worktree", "remove", "--force", opened["worktree"]["path"])
    before = mock_calls(launcher_home)
    session_before = data(run("tasks", "show", task_id, "--json"))["sessions"]
    for args in (("restart", task_id, "--yes"), ("resume", task_id, "--force", "--yes")):
        err = failure(run(*args, "--offline", "--json"))
        assert err["code"] == "worktree_missing" and opened["worktree"]["path"] in err["message"]
    assert mock_calls(launcher_home) == before  # nothing closed, nothing started
    assert data(run("tasks", "show", task_id, "--json"))["sessions"] == session_before


def test_origin_without_origin_head_still_supplies_the_base(tmp_path, new_task):
    bare, clone = make_remote(tmp_path, "trunk")
    git.run_git(clone, ["remote", "set-head", "origin", "--delete"])
    assert git.origin_head(clone) is None
    tip = push_commit(bare, tmp_path, "trunk", "ahead")
    record = ensure(new_task(clone), offline=False).record
    assert record.base_ref == "refs/remotes/origin/trunk"
    assert git_run(record.path, "rev-parse", "HEAD").strip() == tip


def test_a_guessed_base_is_named_in_the_notice(repo, new_task):
    git_run(repo, "checkout", "-q", "-b", "feature-x")
    result = ensure(new_task(repo))
    assert result.record.base_ref == "refs/heads/feature-x"
    assert any("refs/heads/feature-x" in n and "guessed" in n for n in result.notices)
    configured_notice = ensure(new_task(repo, "Other")).notices
    assert configured_notice  # still guessed: nothing configured


def test_path_exists_message_points_at_associate(repo, new_task):
    task_id = new_task(repo)
    with state.open_state() as conn:
        target = worktree_path_for(get_task(conn, task_id), load_config())
    target.mkdir(parents=True)
    with pytest.raises(LauncherError) as caught:
        ensure(task_id)
    assert f"worktrees associate {task_id}" in caught.value.message


def test_lookalike_candidate_refuses_without_a_prompter(repo, new_task, tmp_path):
    task_id = new_task(repo, "Fix login")
    old = foreign_worktree(repo, tmp_path, f"task/{task_id}-an-older-title")
    with pytest.raises(LauncherError) as caught:
        ensure(task_id)
    assert caught.value.code == "worktree_candidate_exists" and str(old) in caught.value.message
    assert caught.value.details["worktrees"] == [str(old)]
    with state.open_state() as conn:
        assert get_worktree(conn, task_id) is None
    # Asked on a terminal, the user can still decide to create a new one.
    result = ensure(task_id, ScriptedPrompter(("select", "Adopt", "new")))
    assert result.record.ownership == "created" and result.record.path != str(old)


def test_interactive_branch_conflict_offers_a_suffixed_branch(repo, new_task):
    task_id = new_task(repo, "Fix login")
    git_run(repo, "branch", f"task/{task_id}-fix-login")
    result = ensure(task_id, ScriptedPrompter(("select", "Adopt", "new")))
    assert result.record.branch == f"task/{task_id}-fix-login-2" and result.record.ownership == "created"
    other = new_task(repo, "Fix login")
    git_run(repo, "branch", f"task/{other}-fix-login")
    with pytest.raises(LauncherError) as caught:
        ensure(other, ScriptedPrompter(("select", "Adopt", "cancel")))
    assert caught.value.code == "declined"


def test_associate_refuses_a_task_with_a_session_unless_forced(repo, new_task, tmp_path):
    existing = foreign_worktree(repo, tmp_path)
    task_id = new_task(repo)
    data(run("open", task_id, "--offline", "--json"))
    # The open created a worktree; a legacy task (session, no record) is the case to protect.
    with state.open_state() as conn:
        conn.execute("DELETE FROM worktrees WHERE task_id = ?", (task_id,))
    err = failure(run("worktrees", "associate", task_id, str(existing), "--json"))
    assert err["code"] == "session_exists" and "--force" in err["message"]
    forced = data(run("worktrees", "associate", task_id, str(existing), "--force", "--json"))
    assert forced["worktree"]["ownership"] == "adopted"


def test_tasks_show_includes_the_worktree(repo, new_task):
    task_id = new_task(repo)
    assert data(run("tasks", "show", task_id, "--json"))["worktree"] is None
    record = ensure(task_id).record
    assert data(run("tasks", "show", task_id, "--json"))["worktree"]["path"] == record.path
    text = run("tasks", "show", task_id).output
    assert record.path in text and "(created" in text


@pytest.mark.parametrize("bad", ["", "-x", "a..b", "a b", "a~1", "x.lock", "a//b", "@", "a@{b", ".hidden", "end/"])
def test_invalid_base_branch_names_fail_at_load(write_config, bad):
    from agent_launcher.config import ConfigError

    write_config({"version": 2, "repositories": {"base_branches": {"/some/repo": bad}}})
    with pytest.raises(ConfigError):
        load_config()
