"""`open <pr-url>`: a pull request is a task like an issue (ticket #13), and the harness starts in the checkout (#16).

`gh` is a fake (`PullGh`, on top of #12's `FakeGh`) with a viewer and pull requests. `Origin` is a local bare
repository that gives the pull requests real commit SHAs. Nothing is checked out or fetched: what is stored is the
pull request's metadata. A real fork PR cannot be made without a second account; forks are faked (head repo ID differs).
"""

import json
import subprocess
from pathlib import Path

import pytest

from commodore_launcher import state
from commodore_launcher.doctor import CommandError, CommandResult
from commodore_launcher.github import GitHub, GitHubError, parse_github_url, parse_issue_url, parse_pull_url
from commodore_launcher.github_tasks import github_details
from commodore_launcher.tasks import list_tasks
from conftest import git_run
from test_github_issues import (  # noqa: F401  (fixtures)
    FakeGh,
    all_argv,
    checkout,
    data,
    env,
    failure,
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
    from commodore_launcher import github

    fake = PullGh(fake_github)
    monkeypatch.setattr(github, "run_gh", fake)
    return fake


class Origin:
    """A bare repository standing in for GitHub: commits pushed to it have real SHAs for the fake pull requests."""

    def __init__(self, checkout: Path, tmp: Path) -> None:
        self.bare = tmp / "origin.git"
        self.url = str(self.bare)
        subprocess.run(["git", "clone", "-q", "--bare", str(checkout), str(self.bare)], check=True)
        self.scratch = tmp / "scratch"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(self.scratch)], check=True)

    def commit_on(self, branch: str, name: str) -> str:
        """A new commit on `branch` (created from main if new), pushed to the bare origin. Returns its SHA."""
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
def origin(checkout, tmp_path) -> Origin:
    return Origin(checkout, tmp_path)


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


def test_issue_and_pr_with_the_same_database_id_are_two_tasks(env, checkout, gh, origin, launcher_home):
    sha = origin.commit_on("feat/x", "x.txt")
    origin.pull_head(5, sha)
    gh.add_issue("acme/widgets", 8, db_id=7005)  # the issue's database ID equals the PR's
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha)
    issue = data(run("open", "https://github.com/acme/widgets/issues/8"))
    pr = data(run("open", PR_URL))
    assert issue["task"]["id"] != pr["task"]["id"]
    again = data(run("open", PR_URL))
    assert again["task"]["id"] == pr["task"]["id"] and again["action"] == "resumed"
    with state.open_state() as conn:
        assert len(list_tasks(conn)) == 2


def test_unknown_viewer_is_not_own(env, checkout, gh, origin, launcher_home):
    sha = origin.commit_on("feat/x", "x.txt")
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha)
    gh.viewer_down = True
    result = data(run("open", PR_URL))
    assert data(run("tasks", "show", result["task"]["id"], "--json"))["github"]["pull"]["own"] is None


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
    result = data(run("open", PR_URL))
    shown = data(run("tasks", "show", result["task"]["id"], "--json"))["github"]
    assert shown["kind"] == "pull_request" and shown["state"] == state_name
    assert shown["node_id"] == "PR_7005" and shown["database_id"] == 7005
    assert shown["author"] == "octo" and shown["labels"] == ["bug"]
    pull = shown["pull"]
    assert pull["draft"] is True and pull["review_requested"] is True and pull["own"] is False
    assert pull["head_ref"] == "feat/x" and pull["head_sha"] == sha and pull["base_ref"] == "main"
    assert (pull["head_repo_owner"], pull["head_repo_name"], pull["head_fork"]) == ("acme", "widgets", False)
    assert pull["head_repo_id"] == 101


def test_reopening_is_one_task_and_refreshes_the_head(env, checkout, gh, origin, host):
    sha = origin.commit_on("feat/x", "x.txt")
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha)
    first = data(run("open", PR_URL))
    newer = origin.commit_on("feat/x", "y.txt")
    gh.pulls[(101, 5)]["head"]["sha"] = newer
    gh.pulls[(101, 5)]["state"] = "closed"
    gh.pulls[(101, 5)]["merged"] = True
    second = data(run("open", PR_URL))
    assert second["action"] == "resumed" and second["task"]["id"] == first["task"]["id"]
    assert len(host.runs) == 2
    with state.open_state() as conn:
        assert len(list_tasks(conn)) == 1
        details = github_details(conn, first["task"]["id"])
    assert details["state"] == "merged" and details["pull"]["head_sha"] == newer


def test_pr_is_found_by_url_offline(env, checkout, gh, origin, launcher_home):
    sha = origin.commit_on("feat/x", "x.txt")
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha)
    first = data(run("open", PR_URL))
    gh.down = True
    again = data(run("open", PR_URL + "/"))
    assert again["task"]["id"] == first["task"]["id"] and "could not be reached" in again["notice"]


def test_issue_urls_still_work_and_prs_are_not_issues(env, checkout, gh, origin, launcher_home):
    result = data(run("open", "https://github.com/acme/widgets/issues/7"))
    assert "pull" not in data(run("tasks", "show", result["task"]["id"], "--json"))["github"]


def test_nothing_ever_pushes_or_adds_a_remote(env, checkout, gh, origin, launcher_home):
    sha = origin.commit_on("feat/x", "x.txt")
    origin.pull_head(5, sha)
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha, user={"login": "octo"})
    refs_before = git_run(origin.bare, "for-each-ref")
    data(run("open", PR_URL))
    assert git_run(origin.bare, "for-each-ref") == refs_before
    assert "auth" not in all_argv(gh)
