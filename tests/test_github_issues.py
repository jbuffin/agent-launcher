"""`open <issue-url>`: GitHub identity, repository resolution, cloning and the launch (ticket #12).

`gh` is a fake (`FakeGh`) that answers `gh api` and `gh repo clone`; git itself runs for real in temp repos.
No test reaches GitHub, and none may run a `gh auth` command.
"""

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_launcher import git, github, state
from agent_launcher.cli import app
from agent_launcher.config import load_config
from agent_launcher.doctor import CommandError, CommandResult
from agent_launcher.errors import LauncherError
from agent_launcher.github import GitHub, GitHubError, parse_issue_url
from agent_launcher.github_tasks import create_github_task, find_task, open_issue
from agent_launcher.launch import open_task
from agent_launcher.sessions import primary_session
from agent_launcher.tasks import list_tasks
from agent_launcher.terminal_mock import MockTerminalAdapter
from conftest import git_run
from scripted import ScriptedPrompter

runner = CliRunner()
EVIL_TITLE = "Fix $(touch pwned) `id` ; rm -rf ~ --upload-pack=x\n\x1b[31mred"


def run(*args):
    return runner.invoke(app, list(args))


def data(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def failure(result):
    assert result.exit_code != 0
    return json.loads(result.stdout)["error"]


class FakeGh:
    """`gh api repos/{o}/{n}[/issues/N]` and `gh repo clone`, over a small in-memory GitHub."""

    def __init__(self, fake_github) -> None:
        self.fake_github = fake_github
        self.repos: dict[str, dict] = {}
        self.issues: dict[tuple[int, int], dict] = {}
        self.calls: list[list[str]] = []
        self.down = False
        self.cloned: list[str] = []

    def add_repo(self, full_name: str, repo_id: int) -> None:
        self.repos[full_name.lower()] = {"id": repo_id, "node_id": f"R_{repo_id}", "full_name": full_name}
        self.fake_github.add(full_name, repo_id)

    def add_issue(self, full_name: str, number: int, *, db_id: int, title: str = "Fix the thing", **extra) -> None:
        repo = self.repos[full_name.lower()]
        self.issues[(repo["id"], number)] = {
            "id": db_id, "node_id": f"I_{db_id}", "number": number, "title": title, "state": "open",
            "labels": [{"name": "bug"}], "user": {"login": "octo"}, "assignees": [{"login": "me"}], **extra,
        }

    def rename(self, old: str, new: str) -> None:
        repo = self.repos[old.lower()]
        repo["full_name"] = new
        self.repos[new.lower()] = repo  # the old name still resolves, as a GitHub redirect does
        self.fake_github.rename(old, new)

    def __call__(self, argv, timeout, env=None):
        self.calls.append(list(argv))
        if self.down:
            raise CommandError("network unreachable")
        if argv[:3] == ["gh", "repo", "clone"]:
            return self._clone(argv[3], argv[4])
        assert argv[:2] == ["gh", "api"], f"unexpected gh call {argv}"
        parts = argv[2].split("/")
        repo = self.repos.get(f"{parts[1]}/{parts[2]}".lower())
        if repo is None:
            return CommandResult(1, "", "gh: Not Found (HTTP 404)")
        if len(parts) == 3:
            return CommandResult(0, json.dumps(repo), "")
        issue = self.issues.get((repo["id"], int(parts[4])))
        if issue is None:
            return CommandResult(1, "", "gh: Not Found (HTTP 404)")
        url = f"https://github.com/{repo['full_name']}/issues/{issue['number']}"
        return CommandResult(0, json.dumps({**issue, "html_url": url}), "")

    def _clone(self, name: str, target: str) -> CommandResult:
        repo = self.repos[name.lower()]
        subprocess.run(["git", "init", "-q", "-b", "main", target], check=True)
        subprocess.run(["git", "-C", target, "remote", "add", "origin", f"https://github.com/{repo['full_name']}"], check=True)
        git_run(Path(target), "commit", "-q", "--allow-empty", "-m", "initial")
        self.cloned.append(target)
        return CommandResult(0, "", "")


@pytest.fixture
def gh(fake_github, monkeypatch) -> FakeGh:
    fake = FakeGh(fake_github)
    monkeypatch.setattr(github, "run_gh", fake)
    monkeypatch.setattr(git, "fetch_branch", lambda repo, branch: None)  # no network, even to a fake origin
    return fake


@pytest.fixture
def env(tmp_path, write_config, gh):
    """A config with one profile, a search root and a clone root, and the sandbox repo known to the fake gh."""
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (tmp_path / "src").mkdir()
    gh.add_repo("acme/widgets", 101)
    gh.add_issue("acme/widgets", 7, db_id=9007, title=EVIL_TITLE)

    class Env:
        root = tmp_path
        search = tmp_path / "src"
        clones = tmp_path / "clones"

        def configure(self, **repositories):
            write_config(
                {
                    "version": 2,
                    "terminal": {"adapter": "mock"},
                    "agent_selection": "use_default",
                    "repositories": {
                        "worktree_root": str(tmp_path / "trees"),
                        "search_roots": [str(tmp_path / "src")],
                        "clone_root": str(tmp_path / "clones"),
                        **repositories,
                    },
                    "profiles": {"work": {"default_agent": "claude", "agents": {"claude": {"executable": str(exe)}}}},
                }
            )

    e = Env()
    e.configure()
    return e


@pytest.fixture
def checkout(env, make_repo):
    """A local checkout of acme/widgets inside the search root, already associated with `work`."""
    path = make_repo("widgets", remote="https://github.com/acme/widgets")
    moved = env.search / "widgets"
    path.rename(moved)
    assert run("profile", "set", str(moved), "work").exit_code == 0
    return moved


URL = "https://github.com/acme/widgets/issues/7"


def mock_calls(launcher_home, op="create_session"):
    path = launcher_home / "mock-terminal.json"
    return [c for c in json.loads(path.read_text())["calls"] if c["op"] == op] if path.exists() else []


def open_url(url=URL, *extra):
    return run("open", url, "--terminal", "mock", "--json", *extra)


def all_argv(gh):
    return [a for call in gh.calls for a in call]


# --- URL parsing ------------------------------------------------------------------------------


def test_parse_issue_url():
    ref = parse_issue_url("https://github.com/Acme/widgets/issues/7/")
    assert (ref.owner, ref.name, ref.number) == ("Acme", "widgets", 7)


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/acme/widgets/issues/7",
        "https://github.com/acme/widgets/pull/7",
        "https://github.com/acme/widgets/issues/0",
        "https://github.com/acme/widgets/issues/x",
        "https://github.com/acme/widgets/issues/7/../../x",
        "https://user:pw@github.com/acme/widgets/issues/7",
        "https://github.com/acme/../issues/7",
        "https://github.com/-bad/widgets/issues/7",
        "https://github.com/acme/widgets",
        "file:///etc/passwd",
    ],
)
def test_parse_issue_url_rejects(url):
    with pytest.raises(GitHubError) as err:
        parse_issue_url(url)
    assert err.value.code == "unsupported_url"


# --- Scenario A -------------------------------------------------------------------------------


def test_open_issue_end_to_end(env, checkout, gh, launcher_home):
    result = data(open_url())
    task = result["task"]
    assert task["source"] == "github" and task["url"] == URL
    assert task["profile"] == "work" and task["repo_path"] == str(checkout.resolve())
    # The prompt is the URL, exactly; the title (untrusted) is nowhere in it.
    assert result["prompt"] == URL
    (create,) = mock_calls(launcher_home)
    assert create["prompt"] == URL
    tree = Path(result["worktree"]["path"])
    assert tree.is_dir() and create["working_directory"] == str(tree)
    assert result["worktree"]["ownership"] == "created"

    shown = data(run("tasks", "show", task["id"], "--json"))["github"]
    assert shown["node_id"] == "I_9007" and shown["database_id"] == 9007
    assert shown["repository_github_id"] == 101 and shown["number"] == 7
    assert shown["labels"] == ["bug"] and shown["author"] == "octo" and shown["assignees"] == ["me"]


def test_untrusted_title_never_reaches_a_command(env, checkout, gh):
    task = data(open_url())["task"]
    assert "\n" not in task["title"] and "\x1b" not in task["title"]
    assert not any("touch pwned" in a or "rm -rf" in a for a in all_argv(gh))
    assert not (env.root / "pwned").exists() and not Path("pwned").exists()
    branch = git_run(checkout, "branch", "--list", "task/*")
    assert "$" not in branch and "`" not in branch and ";" not in branch


def test_same_issue_twice_is_one_task_and_focuses(env, checkout, gh, launcher_home):
    first = data(open_url())
    second = data(open_url())
    assert second["action"] == "focused"
    assert second["task"]["id"] == first["task"]["id"]
    assert second["session"]["id"] == first["session"]["id"]
    assert len(mock_calls(launcher_home)) == 1
    with state.open_state() as conn:
        assert len(list_tasks(conn)) == 1


def test_renamed_repository_finds_the_same_task(env, checkout, gh, launcher_home):
    first = data(open_url())
    gh.rename("acme/widgets", "acme/gadgets")  # the old URL redirects; the new one is canonical
    again = data(open_url("https://github.com/acme/gadgets/issues/7"))
    assert again["task"]["id"] == first["task"]["id"] and again["action"] == "focused"
    assert len(mock_calls(launcher_home)) == 1
    with state.open_state() as conn:
        assert len(list_tasks(conn)) == 1
    # The stored URL follows the rename; the identity did not change.
    assert data(run("tasks", "show", first["task"]["id"], "--json"))["github"]["url"].endswith("acme/gadgets/issues/7")


def test_renamed_repository_is_found_by_id_for_a_new_issue(env, checkout, gh):
    gh.rename("acme/widgets", "acme/gadgets")  # the checkout's origin still says widgets
    gh.add_issue("acme/gadgets", 8, db_id=9008)
    result = data(open_url("https://github.com/acme/gadgets/issues/8"))
    assert result["task"]["repo_path"] == str(checkout.resolve())


def test_issue_task_is_a_normal_task_afterwards(env, checkout, gh):
    task = data(open_url())["task"]
    again = data(run("open", task["id"], "--terminal", "mock", "--json"))
    assert again["action"] == "focused" and again["task"]["id"] == task["id"]


# --- Never changes the gh account; shared auth -------------------------------------------------


def test_only_read_calls_and_no_auth_commands(env, checkout, gh):
    data(open_url())
    for call in gh.calls:
        assert call[:2] == ["gh", "api"], call
    assert "auth" not in [c[1] for c in gh.calls]


def test_auth_seam_supplies_environment():
    seen = []

    def fake(argv, timeout, env=None):
        seen.append(env)
        return CommandResult(0, '{"id": 1, "node_id": "R_1", "full_name": "a/b"}', "")

    class PerProfile:
        def env(self, profile):
            return {"GH_CONFIG_DIR": f"/x/{profile}"}

    GitHub(runner=fake).repository("a/b")
    GitHub(runner=fake, auth=PerProfile()).repository("a/b")
    assert seen == [None, {"GH_CONFIG_DIR": "/x/None"}]


@pytest.mark.parametrize(
    "stderr,code",
    [
        ("gh: Not Found (HTTP 404)", "not_found"),
        ("To get started with GitHub CLI, please run:  gh auth login", "github_not_authenticated"),
        ("HTTP 403: API rate limit exceeded", "github_forbidden"),
        ("weird", "github_error"),
    ],
)
def test_gh_failures_are_structured(stderr, code):
    gh_ = GitHub(runner=lambda argv, t, e=None: CommandResult(1, "", stderr))
    with pytest.raises(GitHubError) as err:
        gh_.repository("a/b")
    assert err.value.code == code


def test_unreachable_and_missing_gh():
    def boom(msg):
        def runner_(argv, t, e=None):
            raise CommandError(msg)
        return runner_

    with pytest.raises(GitHubError) as err:
        GitHub(runner=boom("timed out after 20s")).repository("a/b")
    assert err.value.code == "github_unreachable"
    with pytest.raises(GitHubError) as err:
        GitHub(runner=boom("No such file or directory")).repository("a/b")
    assert err.value.code == "gh_unavailable"


def test_pull_request_in_issues_endpoint_is_refused(env, gh):
    gh.add_issue("acme/widgets", 9, db_id=9009, pull_request={"url": "x"})
    with pytest.raises(GitHubError) as err:
        GitHub(runner=gh).issue(parse_issue_url("https://github.com/acme/widgets/issues/9"))
    assert err.value.code == "unsupported_url"


def test_missing_issue_and_repository(env, checkout, gh):
    assert failure(open_url("https://github.com/acme/widgets/issues/99"))["code"] == "not_found"
    assert failure(open_url("https://github.com/nobody/nothing/issues/1"))["code"] == "not_found"


# --- Offline -------------------------------------------------------------------------------------


def test_offline_first_open_needs_github(env, checkout, gh):
    error = failure(open_url(URL, "--offline"))
    assert error["code"] == "github_required"
    assert gh.calls == []


def test_offline_reopen_finds_the_task_by_url(env, checkout, gh):
    first = data(open_url())
    again = data(open_url(URL, "--offline"))
    assert again["task"]["id"] == first["task"]["id"] and again["action"] == "focused"


def test_unreachable_github_falls_back_to_a_known_task(env, checkout, gh):
    first = data(open_url())
    gh.down = True
    again = data(open_url())
    assert again["task"]["id"] == first["task"]["id"]
    assert "could not be reached" in again["notice"]
    gh.down = True
    assert failure(open_url("https://github.com/acme/widgets/issues/8"))["code"] == "github_required"


# --- Repository resolution -----------------------------------------------------------------------


def test_explicit_mapping_wins(env, make_repo, tmp_path, gh):
    mapped = make_repo("elsewhere", remote="https://github.com/acme/widgets")
    decoy = env.search / "widgets"
    make_repo("decoy", remote="https://github.com/acme/widgets").rename(decoy)
    env.configure(mappings={"Acme/Widgets": str(mapped)})
    assert run("profile", "set", str(mapped), "work").exit_code == 0
    assert data(open_url())["task"]["repo_path"] == str(mapped.resolve())


def test_invalid_mapping_is_an_error_not_a_fallback(env, checkout, make_repo):
    other = make_repo("other", remote="https://github.com/acme/other")
    env.configure(mappings={"acme/widgets": str(other)})
    assert failure(open_url())["code"] == "mapping_invalid"
    env.configure(mappings={"acme/widgets": str(env.root / "missing")})
    assert failure(open_url())["code"] == "mapping_invalid"


def test_cached_path_is_used_and_validated(env, checkout, gh, make_repo):
    data(open_url())
    gh.add_issue("acme/widgets", 8, db_id=9008)
    env.configure(search_roots=[])  # only the remembered path can find it now
    moved = data(open_url("https://github.com/acme/widgets/issues/8"))
    assert moved["task"]["repo_path"] == str(checkout.resolve())
    # The checkout is replaced by another repository at the same path: the cache is not trusted.
    import shutil

    shutil.rmtree(checkout)
    make_repo("impostor", remote="https://github.com/acme/other").rename(checkout)
    gh.add_repo("acme/other", 202)
    gh.add_issue("acme/widgets", 9, db_id=9009)
    assert failure(open_url("https://github.com/acme/widgets/issues/9"))["code"] == "repository_not_found"


def test_only_configured_roots_are_searched(env, make_repo, gh):
    make_repo("widgets", remote="https://github.com/acme/widgets")  # in tmp/repos, not a configured root
    error = failure(open_url())
    assert error["code"] == "repository_not_found"
    assert gh.cloned == []


def test_search_is_shallow_and_skips_linked_worktrees(env, make_repo):
    from agent_launcher.repo_locator import _scan

    (env.search / "a" / "b").mkdir(parents=True)
    make_repo("deep", remote="https://github.com/acme/widgets").rename(env.search / "a" / "b" / "deep")  # 3 levels
    (env.search / "owner").mkdir()
    make_repo("two", remote="https://github.com/acme/widgets").rename(env.search / "owner" / "two")  # 2 levels
    make_repo("top", remote="https://github.com/acme/widgets").rename(env.search / "top")  # 1 level
    git_run(env.search / "top", "worktree", "add", "-q", "-b", "w", str(env.search / "linked"))
    assert {p.name for p in _scan(load_config())} == {"top", "two"}


def test_two_checkouts_are_ambiguous(env, make_repo):
    make_repo("w1", remote="https://github.com/acme/widgets").rename(env.search / "w1")
    make_repo("w2", remote="https://github.com/acme/widgets").rename(env.search / "w2")
    error = failure(open_url())
    assert error["code"] == "ambiguous_repository_location" and len(error["paths"]) == 2


# --- Cloning -------------------------------------------------------------------------------------


def make_adapter(launcher_home):
    return MockTerminalAdapter(launcher_home / "mock-terminal.json")


def open_direct(url, prompter, gh, launcher_home, **kw):
    with state.open_state() as conn:
        return open_issue(
            conn, url, config=load_config(), adapter=make_adapter(launcher_home), prompter=prompter,
            github=GitHub(), **kw
        )


def test_missing_repository_declined_does_not_clone(env, gh, launcher_home):
    prompter = ScriptedPrompter(("confirm", "Clone it into", False))
    with pytest.raises(LauncherError) as err:
        open_direct(URL, prompter, gh, launcher_home)
    prompter.done()
    assert err.value.code == "repository_not_found"
    assert gh.cloned == [] and not env.clones.exists()


def test_clone_default_answer_is_no(env, gh, launcher_home):
    from agent_launcher.interaction import Prompter  # noqa: F401  (documenting the protocol)

    seen = {}

    class Recording(ScriptedPrompter):
        def confirm(self, message, default=True):
            seen["default"] = default
            return False

    with pytest.raises(LauncherError):
        open_direct(URL, Recording(), gh, launcher_home)
    assert seen["default"] is False


def test_non_interactive_missing_repository_is_structured_and_clones_nothing(env, gh):
    error = failure(open_url())
    assert error["code"] == "repository_not_found" and error["repository"] == "acme/widgets"
    assert error["target"] == str(env.clones / "widgets")
    assert gh.cloned == []


def test_clone_then_profile_association_before_launch(env, gh, launcher_home):
    order = []
    adapter = make_adapter(launcher_home)
    real = adapter.create_session

    def create(request):
        with state.open_state() as conn:
            row = conn.execute("SELECT profile FROM profile_associations").fetchone()
        order.append(("launch", row[0] if row else None))
        return real(request)

    adapter.create_session = create
    prompter = ScriptedPrompter(
        ("confirm", "Clone it into", True), ("select", "Which profile", "work")
    )
    with state.open_state() as conn:
        result = open_issue(conn, URL, config=load_config(), adapter=adapter, prompter=prompter, github=GitHub())
    prompter.done()
    target = env.clones / "widgets"
    assert gh.cloned == [str(target)]
    assert ["gh", "repo", "clone", "acme/widgets", str(target)] in gh.calls
    assert order == [("launch", "work")]  # the association existed when the agent launched
    assert result.task.repo_path == str(target.resolve()) and "Cloned" in (result.notice or "")


def test_auto_clone_without_a_profile_launches_nothing_and_retry_does_not_reclone(env, gh, launcher_home):
    env.configure(auto_clone=True)
    error = failure(open_url())
    assert error["code"] == "unknown_repository_profile"
    assert len(gh.cloned) == 1 and mock_calls(launcher_home) == []
    with state.open_state() as conn:
        assert list_tasks(conn) == []
    assert run("profile", "set", gh.cloned[0], "work").exit_code == 0
    result = data(open_url())
    assert len(gh.cloned) == 1  # found in clone_root, not cloned again
    assert result["task"]["repo_path"] == str(Path(gh.cloned[0]).resolve())


def test_clone_target_exists(env, gh):
    env.configure(auto_clone=True)
    (env.clones / "widgets").mkdir(parents=True)
    (env.clones / "acme-widgets").mkdir()
    assert failure(open_url())["code"] == "clone_target_exists"
    assert gh.cloned == []


# --- Storage -------------------------------------------------------------------------------------


def test_github_identity_is_unique_per_issue(env, checkout, gh):
    task = data(open_url())["task"]
    meta = GitHub(runner=gh).issue(parse_issue_url(URL))
    with state.open_state() as conn:
        again, created = create_github_task(conn, meta, 1, str(checkout), "work")
        assert not created and again.id == task["id"]
        assert find_task(conn, meta).id == task["id"]
        assert conn.execute("SELECT count(*) FROM task_github").fetchone()[0] == 1


def test_bodies_and_comments_are_not_stored(env, checkout, gh):
    gh.issues[(101, 7)]["body"] = "SECRET BODY"
    data(open_url())
    with state.open_state() as conn:
        dump = " ".join(str(v) for t in ("tasks", "task_github") for row in conn.execute(f"SELECT * FROM {t}") for v in row)
    assert "SECRET BODY" not in dump


def test_migration_keeps_existing_tasks_local(env, make_repo):
    repo = make_repo("old")
    assert run("profile", "set", str(repo), "work", "--offline").exit_code == 0
    task = data(run("new", "--title", "Local", "--repo", str(repo), "--offline", "--json"))["task"]
    assert task["source"] == "local" and task["url"] is None
    with state.open_state() as conn:
        assert conn.execute("SELECT version FROM (SELECT user_version AS version FROM pragma_user_version)").fetchone()[0] >= 6
        assert conn.execute("SELECT source FROM tasks").fetchone()[0] == "local"


# --- Review follow-ups ---------------------------------------------------------------------------


def test_implicit_clone_root_is_not_searched_but_an_earlier_clone_target_is_reused(env, gh, make_repo, monkeypatch):
    from agent_launcher import repo_locator
    from agent_launcher.config import Config

    home = Path.home()
    env.configure(search_roots=[])
    cfg = load_config()
    cfg = cfg.model_copy(update={"repositories": cfg.repositories.model_copy(update={"clone_root": None})})
    assert repo_locator._scan(cfg) == []  # nothing implicit is scanned
    # A checkout sitting at the exact place a clone would go is found without cloning.
    default_root = home / "Projects"
    default_root.mkdir()
    make_repo("w", remote="https://github.com/acme/widgets").rename(default_root / "widgets")
    assert run("profile", "set", str(default_root / "widgets"), "work").exit_code == 0
    with state.open_state() as conn:
        located = repo_locator.locate(
            conn, cfg, GitHub(runner=gh).repository("acme/widgets"), github=GitHub(runner=gh), prompter=None
        )
    assert located.path == str((default_root / "widgets").resolve()) and not located.cloned and gh.cloned == []


def test_remote_id_lookups_are_cached(env, checkout, gh):
    gh.rename("acme/widgets", "acme/gadgets")  # origin still says widgets: found by asking GitHub for its ID
    gh.add_issue("acme/gadgets", 8, db_id=9008)
    gh.add_issue("acme/gadgets", 9, db_id=9009)
    data(open_url("https://github.com/acme/gadgets/issues/8"))
    before = [c for c in gh.calls if c[:3] == ["gh", "api", "repos/acme/widgets"]]
    data(open_url("https://github.com/acme/gadgets/issues/9"))
    after = [c for c in gh.calls if c[:3] == ["gh", "api", "repos/acme/widgets"]]
    assert before and len(after) == len(before)  # the second open did not ask about the checkout again
    with state.open_state() as conn:
        assert conn.execute("SELECT github_id FROM github_remote_ids").fetchall() == [(101,)]


def test_offline_reopen_ignores_query_fragment_and_slash(env, checkout, gh):
    first = data(open_url())
    for variant in (URL + "?notification_referrer_id=x", URL + "#issuecomment-1", URL + "/", URL.replace("github.com", "GitHub.com")):
        again = data(open_url(variant, "--offline"))
        assert again["task"]["id"] == first["task"]["id"], variant


def test_html_url_from_github_is_validated(env, gh):
    gh.add_issue("acme/widgets", 5, db_id=9005)
    real = gh.__call__

    def odd(argv, timeout, env=None):
        result = real(argv, timeout, env)
        if argv[2].endswith("/issues/5"):
            body = json.loads(result.stdout)
            body["html_url"] = "https://evil.example/x"
            return CommandResult(0, json.dumps(body), "")
        return result

    meta = GitHub(runner=odd).issue(parse_issue_url("https://github.com/acme/widgets/issues/5"))
    assert meta.url == "https://github.com/acme/widgets/issues/5"


def test_mapping_to_subdirectory_or_linked_worktree_uses_the_main_checkout(env, checkout, gh):
    sub = checkout / "sub"
    sub.mkdir()
    linked = env.root / "linked-tree"
    git_run(checkout, "worktree", "add", "-q", "-b", "wt", str(linked))
    for mapped in (sub, linked):
        env.configure(mappings={"acme/widgets": str(mapped)}, search_roots=[])
        with state.open_state() as conn:
            located = __import__("agent_launcher.repo_locator", fromlist=["locate"]).locate(
                conn, load_config(), GitHub(runner=gh).repository("acme/widgets"), github=GitHub(runner=gh), prompter=None
            )
        assert located.path == str(checkout.resolve()), mapped


def test_busy_issue_lock_has_a_clear_message(env, checkout, gh, monkeypatch):
    from agent_launcher import locks
    from agent_launcher.locks import task_lock

    monkeypatch.setattr(locks, "LOCK_WAIT_SECONDS", 0.1)
    with task_lock("issue-9007"):
        error = failure(open_url())
    assert error["code"] == "task_busy" and "this issue is in progress" in error["message"]
