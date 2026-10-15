import json
from pathlib import Path

import pytest

from commodore_launcher.paths import HOME_ENV_VAR


@pytest.fixture(autouse=True)
def launcher_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the config root at a temp dir for every test, so none touch the real home."""
    home = tmp_path / "launcher-home"
    monkeypatch.setenv(HOME_ENV_VAR, str(home))
    return home


@pytest.fixture
def write_config(launcher_home: Path):
    """Write a config.json (dict -> JSON, str -> verbatim) and return its path."""

    def _write(content: dict | str) -> Path:
        launcher_home.mkdir(parents=True, exist_ok=True)
        path = launcher_home / "config.json"
        path.write_text(content if isinstance(content, str) else json.dumps(content))
        return path

    return _write


@pytest.fixture(autouse=True)
def no_real_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may probe real external tools (git, gh, cmux, agents) through `doctor`.

    By default `run_doctor` sees an empty PATH and a runner that does nothing. Tests that
    want specific tools inject their own `runner` and `which`.
    """
    from commodore_launcher import doctor

    def refuse(argv, timeout):
        raise doctor.CommandError("external tools are disabled in tests")

    monkeypatch.setattr(doctor, "run_command", refuse)
    monkeypatch.setattr(doctor, "_default_which", lambda name: None)


@pytest.fixture(autouse=True)
def no_real_cmux(monkeypatch: pytest.MonkeyPatch) -> None:
    """No default test may reach the real cmux, even one installed on this machine."""
    from commodore_launcher import terminal_cmux

    def refuse(argv, timeout, stdin=None):
        raise AssertionError(f"a test tried to run {argv[0]}; inject a runner")

    monkeypatch.setattr(terminal_cmux, "run_process", refuse)
    monkeypatch.setattr(terminal_cmux, "find_cli", lambda: None)


@pytest.fixture(autouse=True)
def fake_default_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HOME is a temp dir for every test, so nothing can see or touch the real ~/.claude*."""
    home = tmp_path / "default-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # An agent's own config-dir overrides would point the skill lookup at the real skills of whoever runs the tests.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_PLUGIN_CACHE_DIR", raising=False)
    return home


COMMODORE_SKILLS = ("commodore", "agent-teammates", "run-preflight", "target-evaluation")
MANAGEMENT_SKILL_NAME = "commodore-launcher"


def install_skills(root: Path, *names: str) -> None:
    """Install skills the way `npx skills add` leaves them: `<root>/<name>/SKILL.md`."""
    for name in names:
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: test\n---\n")


@pytest.fixture(autouse=True)
def commodore_skills_installed(fake_default_home: Path, request: pytest.FixtureRequest) -> None:
    """The four skills Commodore needs, and `commodore-launcher`, are installed under the fake HOME where Claude Code
    (`~/.claude/skills`) and Codex and Copilot (`~/.agents/skills`) load them, so `open` and `doctor` tests that are
    not about skills see an installed Commodore. A test about missing skills removes them (`shutil.rmtree`). A module
    that lists the fake HOME's contents sets `NO_COMMODORE_SKILLS = True` and gets an empty one."""
    if getattr(request.module, "NO_COMMODORE_SKILLS", False):
        return
    for root in (fake_default_home / ".claude" / "skills", fake_default_home / ".agents" / "skills"):
        install_skills(root, *COMMODORE_SKILLS, MANAGEMENT_SKILL_NAME)


class FakeGitHub:
    """Stands in for `gh api repos/{owner}/{name}`. Git itself runs for real in temp repos."""

    def __init__(self) -> None:
        self.canonical: dict[str, str] = {}
        self.repos: dict[str, tuple[int, str]] = {}
        """lower-case owner/name -> (repository id, node id)"""
        self.offline = False
        self.calls: list[list[str]] = []

    def add(self, full_name: str, repo_id: int) -> None:
        self.repos[full_name.lower()] = (repo_id, f"R_node{repo_id}")

    def rename(self, old: str, new: str) -> None:
        """A rename or transfer: same ID, new name. The old name no longer resolves."""
        self.repos[new.lower()] = self.repos.pop(old.lower())
        self.canonical[new.lower()] = new

    def __call__(self, argv, timeout):
        from commodore_launcher import doctor

        self.calls.append(list(argv))
        if self.offline:
            raise doctor.CommandError("network unreachable")
        name = argv[2].removeprefix("repos/")
        found = self.repos.get(name.lower())
        if found is None:
            return doctor.CommandResult(1, "", "gh: Not Found (HTTP 404)")
        canonical = self.canonical.get(name.lower(), name)
        body = {"id": found[0], "node_id": found[1], "full_name": canonical}
        return doctor.CommandResult(0, json.dumps(body), "")


@pytest.fixture(autouse=True)
def fake_github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    """Repository identification uses real git but a fake `gh`: no test reaches GitHub."""
    from commodore_launcher import repositories

    real = repositories.run_command
    fake = FakeGitHub()

    def route(argv, timeout):
        if argv[0] == "gh":
            return fake(argv, timeout)
        return real(argv, timeout)

    monkeypatch.setattr(repositories, "run_command", route)
    return fake


GIT_IDENTITY = ["-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]


def git_run(repo: Path, *args: str) -> str:
    """Run real git in a temp repo with a fixed identity; returns stdout."""
    import subprocess

    done = subprocess.run(
        ["git", *GIT_IDENTITY, "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return done.stdout


@pytest.fixture
def make_repo(tmp_path: Path):
    """Create a real git repo in a temp dir (branch `main`, one commit), optionally with an `origin` remote."""
    import subprocess

    def _make(name: str, remote: str | None = None, commit: bool = True) -> Path:
        path = tmp_path / "repos" / name
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
        if remote:
            subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
        if commit:
            git_run(path, "commit", "-q", "--allow-empty", "-m", "initial")
        return path

    return _make


_github_task_numbers = iter(range(1, 10_000))


def make_github_task(repo: Path, title: str = "Fix") -> dict:
    """A GitHub issue task for a local checkout whose repository already has a profile, written the way `open <url>`
    leaves it, without GitHub or a clone. `open <id> --offline` then launches it. Returns `Task.to_dict()`."""
    from commodore_launcher import state
    from commodore_launcher.associations import ensure_profile
    from commodore_launcher.config import load_config
    from commodore_launcher.github import IssueMetadata, RepoMetadata
    from commodore_launcher.github_tasks import create_github_task
    from commodore_launcher.repositories import identify_reference

    number = next(_github_task_numbers)
    identity = identify_reference(str(repo), fetch_github=False)
    assert identity.path is not None
    meta = IssueMetadata(
        "issue", f"I_{number}", 5000 + number, number, title, "open", (), "octo", (),
        f"https://github.com/acme/widgets/issues/{number}", RepoMetadata(101, "R_101", "acme/widgets"),
    )
    with state.open_state() as conn:
        resolved = ensure_profile(conn, identity, sorted(load_config().profiles), None)
        task, _ = create_github_task(conn, meta, resolved.repository_id, identity.path, resolved.profile)
    return task.to_dict()
