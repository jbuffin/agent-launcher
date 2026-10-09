"""`open <pr-url>`: own-branch and review checkouts (ticket #13).

`gh` is a fake (`PullGh`, on top of #12's `FakeGh`) with a viewer and pull requests. Git runs for real: the checkout's
`git fetch origin` is redirected to a local bare repository (`Origin`), so fetches of branches and of
`refs/pull/N/head` really happen while the remote still reads as github.com/acme/widgets. A real fork PR cannot be made without a second account; forks are faked here
(head repo ID differs) and the gap is documented in docs/github-integration.md.
"""

import json
import subprocess
from pathlib import Path

import pytest

from agent_launcher import git, state
from agent_launcher.config import load_config
from agent_launcher.doctor import CommandError, CommandResult
from agent_launcher.github import GitHub, GitHubError, parse_github_url, parse_issue_url, parse_pull_url
from agent_launcher.github_tasks import github_details, open_github
from agent_launcher.tasks import list_tasks
from agent_launcher.worktrees import get_worktree
from conftest import git_run
from scripted import ScriptedPrompter
from test_github_issues import (  # noqa: F401  (fixtures)
    FakeGh,
    all_argv,
    checkout,
    data,
    env,
    failure,
    make_adapter,
    mock_calls,
    run,
)

PR_URL = "https://github.com/acme/widgets/pull/5"


class PullGh(FakeGh):
    viewer = "me"

    def __init__(self, fake_github) -> None:
        super().__init__(fake_github)
        self.pulls: dict[tuple[int, int], dict] = {}
        self.viewer_down = False

    def add_pull(self, full_name: str, number: int, *, db_id: int, head_ref: str, sha: str, **extra) -> None:
        repo = self.repos[full_name.lower()]
        head_repo = extra.pop("head_repo", {"id": repo["id"], "name": "widgets", "owner": {"login": "acme"}})
        self.pulls[(repo["id"], number)] = {
            "id": db_id, "node_id": f"PR_{db_id}", "number": number, "title": "Add the feature", "state": "open",
            "draft": False, "merged": False, "user": {"login": "me"}, "labels": [], "assignees": [],
            "requested_reviewers": [], "head": {"ref": head_ref, "sha": sha, "repo": head_repo},
            "base": {"ref": "main", "repo": {"id": repo["id"]}}, **extra,
        }

    def __call__(self, argv, timeout, env=None):
        if argv[:3] == ["gh", "api", "user"]:
            self.calls.append(list(argv))
            if self.viewer_down:
                return CommandResult(1, "", "gh: HTTP 401")
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
            return CommandResult(0, json.dumps({**pull, "html_url": f"https://github.com/{repo['full_name']}/pull/{pull['number']}"}), "")
        return super().__call__(argv, timeout, env)


@pytest.fixture
def gh(fake_github, monkeypatch) -> PullGh:
    from agent_launcher import github

    fake = PullGh(fake_github)
    monkeypatch.setattr(github, "run_gh", fake)
    return fake  # git.fetch_branch is NOT stubbed: the fetches below hit the local bare "origin"


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
        """A new commit on `branch` (created from main if new), pushed to the bare origin. Returns its SHA."""
        git_run(self.scratch, "fetch", "-q", "origin")
        known = subprocess.run(
            ["git", "-C", str(self.scratch), "rev-parse", "--verify", "-q", f"origin/{branch}"], capture_output=True
        ).returncode == 0
        git_run(self.scratch, "checkout", "-q", "-B", branch, f"origin/{branch}" if known else "origin/main")
        (self.scratch / name).write_text(name)
        git_run(self.scratch, "add", name)
        git_run(self.scratch, "commit", "-q", "-m", f"add {name}")
        git_run(self.scratch, "push", "-q", "origin", f"{branch}:refs/heads/{branch}")
        return git_run(self.scratch, "rev-parse", "HEAD").strip()

    def pull_head(self, number: int, sha: str) -> None:
        git_run(self.scratch, "push", "-q", "origin", f"{sha}:refs/pull/{number}/head")


@pytest.fixture
def origin(checkout, tmp_path, monkeypatch) -> Origin:
    return Origin(checkout, tmp_path, monkeypatch)


def head_sha(path) -> str:
    return git_run(Path(path), "rev-parse", "HEAD").strip()


def branch_of(path) -> str:
    return git_run(Path(path), "branch", "--show-current").strip()


def open_pr(prompter=None, launcher_home=None, **kw):
    with state.open_state() as conn:
        return open_github(
            conn, kw.pop("url", PR_URL), config=load_config(), adapter=make_adapter(launcher_home), prompter=prompter,
            **{"github": GitHub(), **kw}
        )


# --- URLs -----------------------------------------------------------------------------------


def test_url_kinds():
    assert parse_pull_url("https://github.com/Acme/widgets/pull/5/").kind == "pull_request"
    assert parse_github_url("https://github.com/acme/widgets/issues/5").kind == "issue"
    with pytest.raises(GitHubError):
        parse_issue_url(PR_URL)
    with pytest.raises(GitHubError):
        parse_pull_url("https://github.com/acme/widgets/issues/5")
    with pytest.raises(GitHubError):
        parse_pull_url("https://github.com/acme/widgets/pull/0")


# --- Own PR ---------------------------------------------------------------------------------


def test_own_pr_uses_its_head_branch_even_when_absent_locally(env, checkout, gh, origin, launcher_home):
    sha = origin.commit_on("feat/x", "x.txt")
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha)
    assert git.branch_exists(str(checkout), "feat/x") is False  # absent locally: normal

    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    tree = result["worktree"]
    assert tree["branch"] == "feat/x" and tree["ownership"] == "created"
    assert branch_of(tree["path"]) == "feat/x" and head_sha(tree["path"]) == sha
    assert git.branch_upstream(tree["path"], "feat/x") == "refs/remotes/origin/feat/x"
    assert result["task"]["source"] == "github" and result["prompt"] == PR_URL
    assert not git.branch_exists(str(checkout), "review/pr-5")


def test_own_pr_with_a_local_head_branch_not_checked_out(env, checkout, gh, origin, launcher_home):
    sha = origin.commit_on("feat/x", "x.txt")
    git_run(checkout, "fetch", "-q", origin.url, "+refs/heads/*:refs/remotes/origin/*")
    git_run(checkout, "branch", "--no-track", "feat/x", "origin/feat/x")
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha)
    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    tree = result["worktree"]
    assert branch_of(tree["path"]) == "feat/x"
    assert git.branch_upstream(tree["path"], "feat/x") == "refs/remotes/origin/feat/x"  # set, it had none
    assert "Set the upstream of your local 'feat/x'" in result["notice"]


def test_own_pr_head_checked_out_elsewhere_is_offered_for_adoption(env, checkout, gh, origin, launcher_home, tmp_path):
    sha = origin.commit_on("feat/x", "x.txt")
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha)
    git_run(checkout, "fetch", "-q", origin.url, "+refs/heads/*:refs/remotes/origin/*")
    existing = tmp_path / "mine"
    git_run(checkout, "worktree", "add", "-q", "--track", "-b", "feat/x", str(existing), "origin/feat/x")
    before = git_run(checkout, "worktree", "list")

    # No terminal: refused, never forced, nothing created.
    error = failure(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert error["code"] == "worktree_candidate_exists" and str(existing.resolve()) in error["worktrees"][0]
    assert git_run(checkout, "worktree", "list") == before

    prompter = ScriptedPrompter(("select", "no worktree yet", "wt:0"))
    result = open_pr(prompter, launcher_home)
    prompter.done()
    assert result.worktree is not None and result.worktree.ownership == "adopted"
    assert Path(result.worktree.path) == existing.resolve()
    assert git_run(checkout, "worktree", "list") == before  # no unnecessary worktree
    with state.open_state() as conn:
        assert get_worktree(conn, result.task.id).path == str(existing.resolve())  # association persisted


def test_head_branch_in_the_main_checkout_falls_back_to_review(env, checkout, gh, origin, launcher_home):
    sha = origin.commit_on("feat/x", "x.txt")
    git_run(checkout, "fetch", "-q", origin.url, "+refs/heads/*:refs/remotes/origin/*")
    git_run(checkout, "checkout", "-q", "-b", "feat/x", "origin/feat/x")
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha)
    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert result["worktree"]["branch"] == "review/pr-5"
    assert "already checked out" in result["notice"]
    assert branch_of(checkout) == "feat/x"  # the main checkout was not touched


def test_own_pr_whose_head_branch_is_gone_falls_back_to_review(env, checkout, gh, origin, launcher_home):
    sha = origin.commit_on("feat/x", "x.txt")
    origin.pull_head(5, sha)
    git_run(origin.bare, "branch", "-q", "-D", "feat/x")  # merged-and-deleted style
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha)
    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert result["worktree"]["branch"] == "review/pr-5" and head_sha(result["worktree"]["path"]) == sha
    assert "neither local nor on origin" in result["notice"]


# --- Someone else's PR, forks -----------------------------------------------------------------


def review_assertions(result, checkout, sha, origin):
    tree = result["worktree"]
    assert tree["branch"] == "review/pr-5" and tree["ownership"] == "created"
    assert head_sha(tree["path"]) == sha and branch_of(tree["path"]) == "review/pr-5"
    # Never set up to push anywhere: no upstream, no push remote.
    assert git.branch_upstream(tree["path"], "review/pr-5") is None
    config = subprocess.run(
        ["git", "-C", str(checkout), "config", "--get-regexp", r"^branch\.review/pr-5\."], capture_output=True, text=True
    )
    assert config.stdout.strip() == ""
    assert git.ref_exists(str(checkout), "refs/agent-launcher/pr-5")
    assert git_run(checkout, "remote").split() == ["origin"]  # no fork remote was added
    # The contributor's branch on the "remote" is untouched.
    assert git_run(origin.bare, "rev-parse", "refs/pull/5/head").strip() == sha


def test_someone_elses_pr_gets_an_isolated_review_worktree(env, checkout, gh, origin, launcher_home):
    sha = origin.commit_on("feat/x", "x.txt")
    origin.pull_head(5, sha)
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha, user={"login": "octo"})
    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    review_assertions(result, checkout, sha, origin)
    assert not git.branch_exists(str(checkout), "feat/x")


def test_fork_pr_gets_a_review_worktree_even_when_it_is_the_users_own(env, checkout, gh, origin, launcher_home):
    sha = origin.commit_on("feat/x", "x.txt")  # a same-named branch on origin must not be used for a fork PR
    origin.pull_head(5, sha)
    fork = {"id": 999, "name": "widgets", "owner": {"login": "me"}}
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha, head_repo=fork)
    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    review_assertions(result, checkout, sha, origin)
    pull = data(run("tasks", "show", result["task"]["id"], "--json"))["github"]["pull"]
    assert pull["head_fork"] is True and pull["head_repo_id"] == 999 and pull["head_repo_owner"] == "me"


def test_pr_from_a_deleted_fork_is_reviewed(env, checkout, gh, origin, launcher_home):
    sha = origin.commit_on("feat/x", "x.txt")
    origin.pull_head(5, sha)
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha, head_repo=None)
    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    review_assertions(result, checkout, sha, origin)
    pull = data(run("tasks", "show", result["task"]["id"], "--json"))["github"]["pull"]
    assert pull["head_fork"] is True and pull["head_repo_id"] is None


def test_own_check_forced_off_gives_the_review_worktree(env, checkout, gh, origin, launcher_home):
    sha = origin.commit_on("feat/x", "x.txt")
    origin.pull_head(5, sha)
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha)  # authored by the viewer
    result = open_pr(None, launcher_home, github=GitHub(viewer="someone-else"))
    review_assertions({"worktree": result.worktree.to_dict()}, checkout, sha, origin)


def test_issue_and_pr_with_the_same_database_id_are_two_tasks(env, checkout, gh, origin, launcher_home):
    sha = origin.commit_on("feat/x", "x.txt")
    origin.pull_head(5, sha)
    gh.add_issue("acme/widgets", 8, db_id=7005)  # the issue's database ID equals the PR's
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha)
    issue = data(run("open", "https://github.com/acme/widgets/issues/8", "--terminal", "mock", "--json"))
    pr = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert issue["task"]["id"] != pr["task"]["id"]
    again = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert again["task"]["id"] == pr["task"]["id"] and again["action"] == "focused"
    with state.open_state() as conn:
        assert len(list_tasks(conn)) == 2


def test_prunable_worktree_holding_the_head_branch_falls_back_to_review(env, checkout, gh, origin, launcher_home, tmp_path):
    sha = origin.commit_on("feat/x", "x.txt")
    origin.pull_head(5, sha)
    git_run(checkout, "fetch", "-q", origin.url, "+refs/heads/*:refs/remotes/origin/*")
    gone = tmp_path / "gone"
    git_run(checkout, "worktree", "add", "-q", "--track", "-b", "feat/x", str(gone), "origin/feat/x")
    import shutil

    shutil.rmtree(gone)  # git still lists it, as prunable
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha)
    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert result["worktree"]["branch"] == "review/pr-5" and "missing or prunable" in result["notice"]


def test_picker_names_the_branch_that_would_be_used(env, checkout, gh, origin, launcher_home, tmp_path):
    sha = origin.commit_on("feat/x", "x.txt")
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha)
    git_run(checkout, "worktree", "add", "-q", "-b", "scratch", str(tmp_path / "other"), "main")

    class Recording(ScriptedPrompter):
        labels: list[str] = []

        def select(self, message, choices, default=None):
            self.labels = [c.label for c in choices]
            return super().select(message, choices, default)

    prompter = Recording(("select", "no worktree yet", "new"))
    result = open_pr(prompter, launcher_home)
    assert result.worktree.branch == "feat/x"
    assert any("branch feat/x" in label for label in prompter.labels) and not any("review/pr-5" in l for l in prompter.labels)


def test_unknown_viewer_is_not_own(env, checkout, gh, origin, launcher_home):
    sha = origin.commit_on("feat/x", "x.txt")
    origin.pull_head(5, sha)
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha)
    gh.viewer_down = True
    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert result["worktree"]["branch"] == "review/pr-5"
    assert data(run("tasks", "show", result["task"]["id"], "--json"))["github"]["pull"]["own"] is None


def test_hostile_branch_name_never_becomes_an_option(env, checkout, gh, origin, launcher_home):
    sha = origin.commit_on("feat/x", "x.txt")
    origin.pull_head(5, sha)
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="--upload-pack=touch pwned", sha=sha)
    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert result["worktree"]["branch"] == "review/pr-5"  # an invalid ref name is never checked out


def test_review_worktree_needs_a_head_that_can_be_fetched(env, checkout, gh, origin, launcher_home):
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha="a" * 40, user={"login": "octo"})
    # origin has no refs/pull/5/head and the commit is nowhere local: nothing is created, the task is kept.
    error = failure(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert error["code"] == "pr_head_unavailable"
    with state.open_state() as conn:
        (task,) = list_tasks(conn)
        assert get_worktree(conn, task.id) is None


def test_fetch_failure_is_soft_when_the_ref_is_already_local(env, checkout, gh, origin, launcher_home, monkeypatch):
    sha = origin.commit_on("feat/x", "x.txt")
    origin.pull_head(5, sha)
    git_run(checkout, "fetch", "-q", origin.url, "+refs/pull/5/head:refs/agent-launcher/pr-5")
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha, user={"login": "octo"})
    origin.url = "/nonexistent"
    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert result["worktree"]["branch"] == "review/pr-5" and "Could not fetch" in result["notice"]


# --- What is stored ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra, state_name",
    [({}, "open"), ({"state": "closed"}, "closed"), ({"state": "closed", "merged": True}, "merged")],
)
def test_pr_state_draft_and_review_requested_are_captured(env, checkout, gh, origin, launcher_home, extra, state_name):
    sha = origin.commit_on("feat/x", "x.txt")
    origin.pull_head(5, sha)
    gh.add_pull(
        "acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha, draft=True, user={"login": "octo"},
        requested_reviewers=[{"login": "Me"}], labels=[{"name": "bug"}], **extra,
    )
    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    shown = data(run("tasks", "show", result["task"]["id"], "--json"))["github"]
    assert shown["kind"] == "pull_request" and shown["state"] == state_name
    assert shown["node_id"] == "PR_7005" and shown["database_id"] == 7005
    assert shown["author"] == "octo" and shown["labels"] == ["bug"]
    pull = shown["pull"]
    assert pull["draft"] is True and pull["review_requested"] is True and pull["own"] is False
    assert pull["head_ref"] == "feat/x" and pull["head_sha"] == sha and pull["base_ref"] == "main"
    assert (pull["head_repo_owner"], pull["head_repo_name"], pull["head_fork"]) == ("acme", "widgets", False)
    assert pull["head_repo_id"] == 101


def test_reopening_is_one_task_and_refreshes_the_head(env, checkout, gh, origin, launcher_home):
    sha = origin.commit_on("feat/x", "x.txt")
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha)
    first = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    newer = origin.commit_on("feat/x", "y.txt")
    gh.pulls[(101, 5)]["head"]["sha"] = newer
    gh.pulls[(101, 5)]["state"] = "closed"
    gh.pulls[(101, 5)]["merged"] = True
    second = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert second["action"] == "focused" and second["task"]["id"] == first["task"]["id"]
    assert len(mock_calls(launcher_home)) == 1
    with state.open_state() as conn:
        assert len(list_tasks(conn)) == 1
        details = github_details(conn, first["task"]["id"])
    assert details["state"] == "merged" and details["pull"]["head_sha"] == newer


def test_pr_is_found_by_url_offline(env, checkout, gh, origin, launcher_home):
    sha = origin.commit_on("feat/x", "x.txt")
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha)
    first = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    gh.down = True
    again = data(run("open", PR_URL + "/", "--terminal", "mock", "--json"))
    assert again["task"]["id"] == first["task"]["id"] and "could not be reached" in again["notice"]


def test_issue_urls_still_work_and_prs_are_not_issues(env, checkout, gh, origin, launcher_home):
    result = data(run("open", "https://github.com/acme/widgets/issues/7", "--terminal", "mock", "--json"))
    assert "pull" not in data(run("tasks", "show", result["task"]["id"], "--json"))["github"]


def test_nothing_ever_pushes_or_adds_a_remote(env, checkout, gh, origin, launcher_home):
    sha = origin.commit_on("feat/x", "x.txt")
    origin.pull_head(5, sha)
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha, user={"login": "octo"})
    refs_before = git_run(origin.bare, "for-each-ref")
    data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert git_run(origin.bare, "for-each-ref") == refs_before
    assert "auth" not in all_argv(gh)
