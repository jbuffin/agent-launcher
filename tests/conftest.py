import json
from pathlib import Path

import pytest

from agent_launcher.paths import HOME_ENV_VAR


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
    from agent_launcher import doctor

    def refuse(argv, timeout):
        raise doctor.CommandError("external tools are disabled in tests")

    monkeypatch.setattr(doctor, "run_command", refuse)
    monkeypatch.setattr(doctor, "_default_which", lambda name: None)


@pytest.fixture(autouse=True)
def no_real_cmux(monkeypatch: pytest.MonkeyPatch) -> None:
    """No default test may reach the real cmux, even one installed on this machine."""
    from agent_launcher import terminal_cmux

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
    return home


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
        from agent_launcher import doctor

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
    from agent_launcher import repositories

    real = repositories.run_command
    fake = FakeGitHub()

    def route(argv, timeout):
        if argv[0] == "gh":
            return fake(argv, timeout)
        return real(argv, timeout)

    monkeypatch.setattr(repositories, "run_command", route)
    return fake


@pytest.fixture
def make_repo(tmp_path: Path):
    """Create a real git repo in a temp dir, optionally with an `origin` remote."""
    import subprocess

    def _make(name: str, remote: str | None = None) -> Path:
        path = tmp_path / "repos" / name
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        if remote:
            subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
        return path

    return _make
