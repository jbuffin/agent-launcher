"""No launcher worktrees: the harness starts in the repository's own checkout (issue #16, ADR 0021).

Driven through the Typer CLI with the mock terminal. `gh` is a fake (`PullGh`: issues and pull requests, with a
viewer); git runs for real in temp repos, and a PATH shim records every git argv so "no `git worktree`" holds for every
caller. A pull request's fetches are redirected to a local bare repository (`Origin`) while the checkout's remote
still reads as github.com/acme/widgets. Nothing here imports what u4 removes (worktrees, cleanup, completion).
"""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from commodore_launcher import git, github, state
from commodore_launcher.cli import app
from commodore_launcher.doctor import CommandError, CommandResult
from conftest import git_run
from test_github_issues import FakeGh

runner = CliRunner()

ISSUE_URL = "https://github.com/acme/widgets/issues/7"
ISSUE_8_URL = "https://github.com/acme/widgets/issues/8"
PR_URL = "https://github.com/acme/widgets/pull/5"


def run(*args):
    return runner.invoke(app, list(args))


def data(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def open_url(url, *extra):
    return run("open", url, "--terminal", "mock", "--json", *extra)


class PullGh(FakeGh):
    """`FakeGh` plus `gh api user` and `gh api repos/{o}/{n}/pulls/N`."""

    viewer = "me"

    def add_pull(self, full_name: str, number: int, *, db_id: int, head_ref: str, sha: str, head_repo=None) -> None:
        repo = self.repos[full_name.lower()]
        head_repo = head_repo or {"id": repo["id"], "name": "widgets", "owner": {"login": "acme"}}
        self.pulls[(repo["id"], number)] = {
            "id": db_id, "node_id": f"PR_{db_id}", "number": number, "title": "Add the feature", "state": "open",
            "draft": False, "merged": False, "user": {"login": "me"}, "labels": [], "assignees": [],
            "requested_reviewers": [], "head": {"ref": head_ref, "sha": sha, "repo": head_repo},
            "base": {"ref": "main", "repo": {"id": repo["id"]}},
        }

    pulls: dict

    def __call__(self, argv, timeout, env=None):
        if argv[:3] == ["gh", "api", "user"]:
            self.calls.append(list(argv))
            return CommandResult(0, self.viewer + "\n", "")
        if argv[:2] == ["gh", "api"] and "/pulls/" in argv[2]:
            self.calls.append(list(argv))
            if self.down:
                raise CommandError("network unreachable")
            parts = argv[2].split("/")
            repo = self.repos.get(f"{parts[1]}/{parts[2]}".lower())
            pull = self.pulls.get((repo["id"], int(parts[4]))) if repo else None
            if pull is None:
                return CommandResult(1, "", "gh: Not Found (HTTP 404)")
            url = f"https://github.com/{repo['full_name']}/pull/{pull['number']}"
            return CommandResult(0, json.dumps({**pull, "html_url": url}), "")
        return super().__call__(argv, timeout, env)


class Origin:
    """A bare repository standing in for GitHub. `git fetch origin` is pointed at it, whatever origin's URL says."""

    def __init__(self, checkout: Path, tmp: Path, monkeypatch) -> None:
        self.bare = tmp / "origin.git"
        self.url = str(self.bare)
        subprocess.run(["git", "clone", "-q", "--bare", str(checkout), str(self.bare)], check=True)
        real = git.run_git

        def redirect(repo, args, **kw):
            if args and args[0] == "fetch" and "origin" in args:
                args = [self.url if a == "origin" else a for a in args]
            return real(repo, args, **kw)

        monkeypatch.setattr(git, "run_git", redirect)
        git_run(checkout, "fetch", "-q", self.url, "+refs/heads/*:refs/remotes/origin/*")
        self.scratch = tmp / "scratch"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(self.scratch)], check=True)

    def commit_on(self, branch: str, name: str) -> str:
        git_run(self.scratch, "fetch", "-q", "origin")
        known = subprocess.run(
            ["git", "-C", str(self.scratch), "rev-parse", "--verify", "-q", f"origin/{branch}"], capture_output=True
        ).returncode == 0
        git_run(self.scratch, "checkout", "-q", "-B", branch, f"origin/{branch}" if known else "origin/main")
        (self.scratch / name).parent.mkdir(parents=True, exist_ok=True)
        (self.scratch / name).write_text(name)
        git_run(self.scratch, "add", name)
        git_run(self.scratch, "commit", "-q", "-m", f"add {name}")
        git_run(self.scratch, "push", "-q", "origin", f"{branch}:refs/heads/{branch}")
        return git_run(self.scratch, "rev-parse", "HEAD").strip()

    def pull_head(self, number: int, sha: str) -> None:
        git_run(self.scratch, "push", "-q", "origin", f"{sha}:refs/pull/{number}/head")


@pytest.fixture
def gh(fake_github, monkeypatch) -> PullGh:
    fake = PullGh(fake_github)
    fake.pulls = {}
    monkeypatch.setattr(github, "run_gh", fake)
    fake.add_repo("acme/widgets", 101)
    fake.add_issue("acme/widgets", 7, db_id=9007, title="Fix the thing")
    fake.add_issue("acme/widgets", 8, db_id=9008, title="Fix the other thing")
    return fake


@pytest.fixture
def world(tmp_path, write_config, make_repo, gh, monkeypatch):
    """A config with one profile, a search root and a clone root; the acme/widgets checkout is known and on `work`."""
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (tmp_path / "src").mkdir()

    def configure(**repositories):
        write_config(
            {
                "version": 3,
                "terminal": {"adapter": "mock"},
                "harness_selection": "use_default",
                "repositories": {
                    "search_roots": [str(tmp_path / "src")], "clone_root": str(tmp_path / "clones"), **repositories
                },
                "profiles": {"work": {"default_harness": "claude", "harnesses": {"claude": {"executable": str(exe)}}}},
            }
        )

    configure()
    checkout = make_repo("widgets", remote="https://github.com/acme/widgets")
    moved = tmp_path / "src" / "widgets"
    checkout.rename(moved)
    assert run("profile", "set", str(moved), "work").exit_code == 0
    origin = Origin(moved, tmp_path, monkeypatch)
    return SimpleNamespace(checkout=moved, origin=origin, configure=configure, clones=tmp_path / "clones")


def add_pr(world, gh, *, viewer: str, head_ref="feature", fork: bool = False):
    sha = world.origin.commit_on(head_ref, "feature.txt")
    world.origin.pull_head(5, sha)
    fork_repo = {"id": 999, "name": "widgets", "owner": {"login": "contributor"}} if fork else None
    gh.add_pull("acme/widgets", 5, db_id=9105, head_ref=head_ref, sha=sha, head_repo=fork_repo)
    gh.viewer = viewer


def same_dir(left, right) -> bool:
    return os.path.realpath(left) == os.path.realpath(right)


def mock_calls(launcher_home: Path, op: str = "create_session") -> list[dict]:
    path = launcher_home / "mock-terminal.json"
    return [c for c in json.loads(path.read_text()).get("calls", []) if c["op"] == op] if path.exists() else []


def set_mock(launcher_home: Path, **keys) -> None:
    path = launcher_home / "mock-terminal.json"
    current = json.loads(path.read_text()) if path.exists() else {}
    current.update(keys)
    for key in [k for k, v in current.items() if v is None]:
        del current[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current))


def counts() -> dict[str, int]:
    with state.open_state() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("tasks", "sessions", "launches")}


# --- 16.1: the session starts in the repository's checkout -------------------------------------------------


@pytest.mark.parametrize("kind", ["issue", "own_pr", "someone_elses_pr", "fork_pr"])
def test_the_session_starts_in_the_repositorys_checkout(world, gh, launcher_home, kind):
    url = ISSUE_URL
    if kind != "issue":
        url = PR_URL
        add_pr(world, gh, viewer="me" if kind in {"own_pr", "fork_pr"} else "someone-else", fork=kind == "fork_pr")
    result = data(open_url(url))
    (call,) = mock_calls(launcher_home)
    assert same_dir(call["working_directory"], world.checkout)
    assert same_dir(result["task"]["repo_path"], world.checkout)
    assert "worktree" not in result


def test_a_cloned_repository_starts_in_the_clone(world, gh, launcher_home):
    shutil.rmtree(world.checkout)  # no local checkout: the locator has to clone
    world.configure(auto_clone=True)
    data(open_url(ISSUE_URL))  # the repository's profile association is by GitHub ID, so it survives the removal
    (cloned,) = gh.cloned
    (call,) = mock_calls(launcher_home)
    assert same_dir(call["working_directory"], cloned)


# --- 16.3: tasks in one repository share the checkout ------------------------------------------------------


def test_two_tasks_in_one_repository_start_in_the_same_checkout(world, gh, launcher_home):
    first = data(open_url(ISSUE_URL))
    second = data(open_url(ISSUE_8_URL))
    assert first["task"]["id"] != second["task"]["id"]
    one, two = mock_calls(launcher_home)
    assert same_dir(one["working_directory"], world.checkout)
    assert same_dir(two["working_directory"], world.checkout)


def test_an_issue_and_a_pull_request_share_the_checkout(world, gh, launcher_home):
    add_pr(world, gh, viewer="me")
    data(open_url(ISSUE_URL))
    data(open_url(PR_URL))
    cwds = {os.path.realpath(c["working_directory"]) for c in mock_calls(launcher_home)}
    assert cwds == {os.path.realpath(world.checkout)}


def test_a_resumed_session_starts_in_the_checkout_too(world, gh, launcher_home):
    task = data(open_url(ISSUE_URL))["task"]["id"]
    (first,) = mock_calls(launcher_home)
    set_mock(launcher_home, gone=[first["session"]["workspace_id"]])
    resumed = data(run("open", task, "--terminal", "mock", "--json"))
    assert resumed["action"] == "resumed"
    _, second = mock_calls(launcher_home)
    assert same_dir(second["working_directory"], world.checkout)


# --- 16.2: no `git worktree`, and no git write at all -----------------------------------------------------


class GitShim:
    """A `git` first on PATH that appends each argv to a log and runs the real git, so every caller is recorded
    (`git.run_git`, `repositories.run_command`, `repo_locator`)."""

    RECORD, FIELD = "\x1e", "\x1f"

    def __init__(self, directory: Path, monkeypatch) -> None:
        real = shutil.which("git")
        assert real
        self.log = directory / "git-argv.log"
        script = directory / "git"
        script.write_text(
            "#!/bin/sh\n"
            f'for a in "$@"; do printf \'%s\\037\' "$a" >> "{self.log}"; done\n'
            f'printf \'\\036\' >> "{self.log}"\n'
            f'exec "{real}" "$@"\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        self.directory = directory
        self.monkeypatch = monkeypatch

    def start(self) -> None:
        self.log.write_text("")
        self.monkeypatch.setenv("PATH", f"{self.directory}{os.pathsep}{os.environ['PATH']}")

    def calls(self) -> list[list[str]]:
        text = self.log.read_text()
        return [rec.split(self.FIELD)[:-1] for rec in text.split(self.RECORD) if rec]


@pytest.fixture
def shim(tmp_path, monkeypatch) -> GitShim:
    directory = tmp_path / "shim"
    directory.mkdir()
    return GitShim(directory, monkeypatch)


def test_the_shim_records_git_calls(shim, world):
    shim.start()
    git_run(world.checkout, "rev-parse", "HEAD")
    assert any("rev-parse" in argv for argv in shim.calls())  # a control: the tests below cannot pass vacuously


WRITING = {"worktree", "fetch", "checkout", "switch", "update-ref", "reset", "merge", "pull", "clone", "stash"}


@pytest.mark.parametrize("kind", ["issue", "own_pr", "someone_elses_pr"])
def test_open_runs_no_git_worktree_and_no_git_write(world, gh, launcher_home, shim, kind):
    url = ISSUE_URL
    if kind != "issue":
        url = PR_URL
        add_pr(world, gh, viewer="me" if kind == "own_pr" else "someone-else")
    shim.start()
    data(open_url(url))
    ran = shim.calls()
    assert not [argv for argv in ran if WRITING & set(argv)], ran


def test_open_creates_no_worktree_anywhere(world, gh, launcher_home, tmp_path):
    add_pr(world, gh, viewer="someone-else")
    data(open_url(ISSUE_URL))
    data(open_url(PR_URL))
    assert git_run(world.checkout, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert not (world.checkout / ".git" / "worktrees").exists()
    assert not (tmp_path / "default-home" / ".commodore-launcher" / "worktrees").exists()  # the default root, under HOME


# --- the launcher leaves the checkout alone ----------------------------------------------------------------


def checkout_snapshot(checkout: Path) -> dict:
    return {
        "head": git_run(checkout, "rev-parse", "HEAD"),
        "branch": git_run(checkout, "branch", "--show-current"),
        "status": git_run(checkout, "status", "--porcelain=v2", "--branch", "--untracked-files=all"),
        "refs": git_run(checkout, "for-each-ref"),
        "worktrees": git_run(checkout, "worktree", "list", "--porcelain"),
        "config": (checkout / ".git" / "config").read_text(),
        "files": sorted(p.relative_to(checkout).as_posix() for p in checkout.rglob("*") if ".git/" not in p.as_posix()),
        "tracked": (checkout / "notes.txt").read_text(),
    }


@pytest.mark.parametrize("kind", ["own_pr", "someone_elses_pr"])
def test_the_checkouts_branch_and_working_tree_are_unchanged_after_open_of_a_pr(world, gh, launcher_home, kind):
    checkout = world.checkout
    git_run(checkout, "checkout", "-q", "-b", "scratch")
    (checkout / "notes.txt").write_text("v1\n")
    git_run(checkout, "add", "notes.txt")
    git_run(checkout, "commit", "-q", "-m", "notes")
    (checkout / "notes.txt").write_text("v2, uncommitted\n")
    (checkout / "untracked.txt").write_text("mine\n")
    add_pr(world, gh, viewer="me" if kind == "own_pr" else "someone-else")
    before = checkout_snapshot(checkout)
    assert before["branch"].strip() == "scratch"
    data(open_url(PR_URL))
    assert checkout_snapshot(checkout) == before
    (call,) = mock_calls(launcher_home)
    assert same_dir(call["working_directory"], checkout)


def test_no_launcher_ref_or_review_branch_is_created(world, gh):
    add_pr(world, gh, viewer="someone-else")
    before = git_run(world.checkout, "for-each-ref", "--format=%(refname)")
    data(open_url(PR_URL))
    after = git_run(world.checkout, "for-each-ref", "--format=%(refname)")
    assert after == before
    assert "refs/agent-launcher" not in after and "review/pr-5" not in after


# --- a launch that fails part-way is resumable -------------------------------------------------------------


def test_a_failed_launch_is_retried_by_open_with_no_duplicates(world, gh, launcher_home):
    set_mock(launcher_home, fail_create="cmux is down")
    failed = open_url(ISSUE_URL)
    assert failed.exit_code != 0
    assert json.loads(failed.stdout)["error"]["code"] == "terminal_create_failed"
    assert counts()["tasks"] == 1 and counts()["sessions"] == 0
    assert mock_calls(launcher_home) == []

    set_mock(launcher_home, fail_create=None)
    retried = data(open_url(ISSUE_URL))
    assert retried["action"] == "created" and "worktree" not in retried
    assert counts() == {"tasks": 1, "sessions": 1, "launches": 0}
    (call,) = mock_calls(launcher_home)
    assert same_dir(call["working_directory"], world.checkout)
    again = data(open_url(ISSUE_URL))
    assert again["action"] == "focused" and len(mock_calls(launcher_home)) == 1


# --- 16.4: the removed commands are gone -------------------------------------------------------------------


def command_names(*path: str) -> set[str]:
    import typer.main

    command = typer.main.get_command(app)
    for name in path:
        command = command.commands[name]
    return set(command.commands)


def test_the_removed_commands_are_not_registered():
    top = command_names()
    assert "worktrees" not in top and "cleanup" not in top
    assert "completed" not in command_names("tasks")
    assert {"open", "tasks", "profile"} <= top  # the rest stay
    assert not {"restart", "sessions", "new"} & top  # URLs only (ADR 0021)


@pytest.mark.parametrize(
    "argv", [["worktrees", "list"], ["worktrees", "inspect", "t-1"], ["worktrees", "associate", "t-1", "/x"],
             ["cleanup"], ["cleanup", "--dry-run"], ["tasks", "completed"]],
)
def test_the_removed_commands_are_unknown_when_used(argv):
    result = run(*argv)
    assert result.exit_code == 2
    assert "No such command" in result.output or "No such option" in result.output


def test_the_help_does_not_mention_worktrees_or_cleanup():
    for argv in (["--help"], ["tasks", "--help"], ["open", "--help"]):
        text = run(*argv).output.lower()
        assert "worktree" not in text, argv
    assert "cleanup" not in run("--help").output.lower()
    assert "completed" not in run("tasks", "--help").output.lower()


# --- the output has no worktree or cleanup fields ----------------------------------------------------------


def test_open_tasks_show_and_tasks_list_json_have_no_worktree_or_cleanup_keys(world, gh):
    opened = data(open_url(ISSUE_URL))
    task_id = opened["task"]["id"]
    shown = data(run("tasks", "show", task_id, "--json"))
    listed = data(run("tasks", "list", "--json"))
    stale = {"worktree", "eligible_for_cleanup", "cleanup_eligible_at"}
    for name, payload in (("open", opened), ("show", shown), ("list", listed)):
        assert not stale & _keys(payload), name


def test_the_text_output_names_no_worktree(world, gh):
    done = run("open", ISSUE_URL, "--terminal", "mock")
    assert done.exit_code == 0, done.output
    assert "orktree" not in done.output
    task_id = json.loads(run("tasks", "list", "--json").stdout)["tasks"][0]["id"]
    assert "orktree" not in run("tasks", "show", task_id).output


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_keys(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_keys(v) for v in value)) if value else set()
    return set()


def test_an_archived_task_is_refused_naming_unarchive_and_no_worktree(world, gh, launcher_home):
    task_id = data(open_url(ISSUE_URL))["task"]["id"]
    with state.open_state() as conn:
        from commodore_launcher.tasks import archive_tasks

        with state.transaction(conn):
            archive_tasks(conn, [task_id], detail="tasks archive")
    refused = open_url(ISSUE_URL)
    assert refused.exit_code != 0
    error = json.loads(refused.stdout)["error"]
    assert error["code"] == "task_archived"
    assert f"commodore tasks unarchive {task_id}" in error["message"]
    assert "worktree" not in error["message"].lower()
    assert len(mock_calls(launcher_home)) == 1  # no new session


# --- config: worktree_root and base_branches are gone ------------------------------------------------------


V2_WITH_WORKTREE_KEYS = {
    "version": 2,
    "profiles": {"work": {}},
    "repositories": {
        "search_roots": ["~/code"], "worktree_root": "~/trees", "base_branches": {"~/code/widgets": "develop"},
    },
}


def test_config_migrate_drops_worktree_root_and_base_branches(write_config, launcher_home):
    path = write_config(V2_WITH_WORKTREE_KEYS)
    done = run("config", "migrate", "--yes")
    assert done.exit_code == 0, done.output
    migrated = json.loads(path.read_text())
    assert migrated["version"] == 3
    assert migrated["repositories"] == {"search_roots": ["~/code"]}


@pytest.mark.parametrize("key,value", [("worktree_root", "~/trees"), ("base_branches", {"~/code/widgets": "develop"})])
def test_a_v3_config_with_a_worktree_key_is_invalid(write_config, key, value):
    write_config({"version": 3, "repositories": {key: value}})
    done = run("config", "validate", "--strict", "--json")  # an unknown key is a warning; `--strict` fails on one
    assert done.exit_code != 0
    assert key in done.output


def test_a_setup_answers_file_with_worktree_root_is_rejected(tmp_path):
    from commodore_launcher.wizard import SetupError, load_answers

    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"worktree_root": "~/trees"}))
    with pytest.raises(SetupError) as err:
        load_answers(answers)
    assert err.value.code == "invalid_answers"


def test_the_config_module_has_no_default_worktree_root():
    from commodore_launcher import config

    assert not hasattr(config, "DEFAULT_WORKTREE_ROOT")
    assert "worktree_root" not in config.RepositorySettings.model_fields
    assert "base_branches" not in config.RepositorySettings.model_fields


# --- the bundled skill no longer teaches removed commands --------------------------------------------------


def test_the_bundled_skill_names_no_removed_command_or_task_worktree():
    import re

    root = Path(__file__).resolve().parent.parent / "src" / "commodore_launcher" / "skills" / "commodore-launcher"
    text = "\n".join(p.read_text() for p in sorted(root.rglob("*.md")))
    assert not re.search(r"commodore cleanup|`cleanup|\bworktrees (list|inspect|associate)|worktree_root", text)
    assert "owns one worktree" not in text and "Each task has its own Git worktree" not in text


# --- kept behaviour (guard: passes before and after) -------------------------------------------------------


def test_open_by_task_id_still_refreshes_the_title_state_and_head_from_github(world, gh, launcher_home):
    task_id = data(open_url(ISSUE_URL))["task"]["id"]
    gh.issues[(101, 7)].update(title="Renamed upstream", state="closed")
    data(run("open", task_id, "--terminal", "mock", "--json"))
    shown = data(run("tasks", "show", task_id, "--json"))
    assert "Renamed upstream" in json.dumps(shown) and "closed" in json.dumps(shown)
