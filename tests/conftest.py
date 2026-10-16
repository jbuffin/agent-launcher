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
    """No test may probe real external tools (git, gh, agents) through `doctor`.

    By default `run_doctor` sees an empty PATH and a runner that does nothing. Tests that
    want specific tools inject their own `runner` and `which`.
    """
    from commodore_launcher import doctor

    def refuse(argv, timeout):
        raise doctor.CommandError("external tools are disabled in tests")

    monkeypatch.setattr(doctor, "run_command", refuse)
    monkeypatch.setattr(doctor, "_default_which", lambda name: None)


class RecordingHost:
    """A session host that runs nothing. Each `run` is recorded (environment variable names only) and releases the
    session lock as an exited harness would. A run that fixes a conversation ID (`--session-id`) saves that
    conversation where the harness would (`saves`), as a harness does once its prompt is submitted, so the next open
    resumes it. `status` is the exit status every run returns; `fail` makes the next run raise as if the harness could
    not be started."""

    name = "recording"

    def __init__(self) -> None:
        from commodore_launcher.session_host import ForegroundHost

        self._lock_host = ForegroundHost()  # the session lock is the real one: liveness is the lock
        self.runs: list[dict] = []
        self.results: list = []
        """The `OpenResult` behind each run, when it went through the CLI."""
        self.status = 0
        self.fail: str | None = None
        self.saves = True

    def running(self, task_id):
        return self._lock_host.running(task_id)

    def claim(self, task_id):
        return self._lock_host.claim(task_id)

    def run(self, claim, command) -> int:
        try:
            if self.fail:
                from commodore_launcher.errors import LauncherError

                raise LauncherError("harness_start_failed", self.fail)
            self.runs.append(
                {"argv": list(command.argv), "command": list(command.argv), "cwd": command.cwd,
                 "working_directory": command.cwd, "prompt": command.prompt,
                 "submit_prompt": command.prompt is not None and any(command.prompt in a for a in command.argv),
                 "env_keys": sorted(command.env),
                 "profile_env": list(command.profile_env), "claimed": claim is not None}
            )
            if self.saves and "--session-id" in command.argv:
                save_conversation(command.argv[command.argv.index("--session-id") + 1], command.env)
            return self.status
        finally:
            if claim is not None:
                claim.release()

    @property
    def last(self) -> dict:
        """The last CLI launch as a dict: what `open --json` printed before the harness ran in this terminal."""
        return result_dict(self.results[-1])


def save_conversation(conversation_id: str, env) -> None:
    """Leave a conversation where Claude Code and Copilot CLI save one (see `has_saved_conversation`)."""
    home = Path(env.get("HOME") or Path.home())
    claude = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "projects" / "-recorded"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / f"{conversation_id}.jsonl").write_text("{}\n")
    copilot = Path(env.get("COPILOT_HOME") or home / ".copilot") / "session-state" / conversation_id
    copilot.mkdir(parents=True, exist_ok=True)
    (copilot / "events.jsonl").write_text("{}\n")


def result_dict(result) -> dict:
    return {
        "action": result.action,
        "task": result.task.to_dict(),
        "session": result.session.to_dict() if result.session else None,
        "prompt": result.prompt,
        "notice": result.notice,
        "command": list(result.command.argv),
    }


_current_host: list[RecordingHost] = []


def last_launch() -> dict:
    """The last CLI launch of the current test, as `RecordingHost.last`."""
    return _current_host[-1].last


def harness_runs() -> list[dict]:
    """What the current test's recording host ran, oldest first."""
    return _current_host[-1].runs


def launch_data(result) -> dict:
    """A CLI result's JSON, or for a session command that ran its harness (no JSON to print), the launch."""
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout) if result.stdout.strip() else last_launch()


@pytest.fixture(autouse=True)
def host(monkeypatch: pytest.MonkeyPatch) -> RecordingHost:
    """Every CLI session command runs its harness on this host: no test starts a real harness. A test of the real
    host builds a `ForegroundHost` itself."""
    from commodore_launcher import cli

    recording = RecordingHost()
    real_run_harness = cli._run_harness

    def run_harness(host, result):
        recording.results.append(result)
        return real_run_harness(host, result)

    _current_host[:] = [recording]
    monkeypatch.setattr(cli, "make_host", lambda: recording)
    monkeypatch.setattr(cli, "_run_harness", run_harness)
    return recording


def launched(host: RecordingHost, result):
    """Run the harness of an `OpenResult` from a direct `open_task` call, as the CLI does: the session lock is
    released when the (recorded) harness exits. Returns the result."""
    if result.claim is not None or result.session is not None:
        host.run(result.claim, result.command)
    return result


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
