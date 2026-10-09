"""The bundled management skill and `agent-launcher configure` (ticket #20), on the mock terminal."""

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from test_session_lifecycle import configure as _configure_config, data, fake_agents, mock_calls, run  # noqa: F401

from agent_launcher import state
from agent_launcher.cli import app as cli_app
from agent_launcher.configure import MAINTENANCE_TITLE
from agent_launcher.tasks import list_tasks

ROOT = Path(__file__).resolve().parent.parent


def frontmatter(text: str) -> dict[str, str]:
    head = text.split("---\n")[1]
    return {k.strip(): v.strip() for k, v in (line.split(":", 1) for line in head.splitlines() if ": " in line or line.endswith(":"))}


# --- the skill -----------------------------------------------------------------------------------


def test_skill_path_prints_the_bundled_directory():
    out = data(run("skill", "path", "--json"))
    path = Path(out["path"])
    assert out["name"] == "agent-launcher" and (path / "SKILL.md").is_file()
    assert run("skill", "path").stdout.strip() == str(path)


def test_skill_frontmatter_is_valid_and_references_resolve():
    skill = Path(data(run("skill", "path", "--json"))["path"])
    text = (skill / "SKILL.md").read_text()
    meta = frontmatter(text)
    assert meta["name"] == "agent-launcher" and meta["description"]
    assert "Hard rules" in text and "state.db" in text and "--keep-tasks" in text
    import re

    for link in re.findall(r"\]\((reference/[^)#]+)", text):
        assert (skill / link).is_file(), link


def _registered(words: tuple[str, ...]) -> bool:
    """Is `agent-launcher <words>` a registered command? A group's second word must be one of its subcommands."""
    import typer

    node = typer.main.get_command(cli_app)
    for word in words:
        if not hasattr(node, "commands"):
            return True  # the rest are an argument of a plain command (`open <task>`)
        if word not in node.commands:
            return False
        node = node.commands[word]
    return True


def test_skill_only_names_commands_that_exist():
    """Every `agent-launcher <command> [<sub>]` the skill shows is a real, registered command."""
    import re

    skill = Path(data(run("skill", "path", "--json"))["path"])
    seen: set[tuple[str, ...]] = set()
    for file in [skill / "SKILL.md", *sorted((skill / "reference").glob("*.md"))]:
        for words in re.findall(r"agent-launcher((?: [a-z][a-z-]*){1,2})", file.read_text()):
            seen.add(tuple(words.split()))
    assert ("doctor",) in seen and ("profile", "set") in seen
    assert not _registered(("profile", "bogus")) and _registered(("open", "task"))  # the check itself
    missing = [" ".join(w) for w in sorted(seen) if not _registered(w)]
    assert not missing, f"the skill names commands that do not exist: {missing}"


def test_skill_is_in_the_built_wheel(tmp_path):
    subprocess.run(["uv", "build", "--wheel", "-q", "-o", str(tmp_path)], cwd=ROOT, check=True, capture_output=True)
    wheel = next(tmp_path.glob("*.whl"))
    names = zipfile.ZipFile(wheel).namelist()
    assert "agent_launcher/skills/agent-launcher/SKILL.md" in names
    assert any(n.startswith("agent_launcher/skills/agent-launcher/reference/") for n in names)


# --- configure -----------------------------------------------------------------------------------


@pytest.fixture
def cfg(_configure_config):
    return _configure_config


def configure_cmd(*extra):
    return run("configure", "--terminal", "mock", "--json", *extra)


def test_configure_creates_a_maintenance_task_and_launches_with_the_skill(cfg, launcher_home):
    result = data(configure_cmd("--profile", "work", "make codex my default"))
    assert result["action"] == "created"
    task = result["task"]
    assert task["title"] == MAINTENANCE_TITLE and task["source"] == "local" and task["profile"] == "work"
    assert result["workflow"] == "agent-launcher-configure"
    assert result["prompt"].startswith("/agent-launcher ") and "make codex my default" in result["prompt"]
    repo = launcher_home / "maintenance"
    assert task["repo_path"] == str(repo.resolve())
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True).returncode == 0
    # the normal path: its own worktree, one session, one terminal launch
    assert result["worktree"]["path"] != str(repo) and Path(result["worktree"]["path"]).is_dir()
    assert len(mock_calls(launcher_home, "create_session")) == 1


def test_configure_reuses_the_maintenance_task_and_focuses_its_session(cfg, launcher_home):
    first = data(configure_cmd("--profile", "work"))
    again = data(configure_cmd())
    assert again["action"] == "focused" and again["task"]["id"] == first["task"]["id"]
    assert len(mock_calls(launcher_home, "create_session")) == 1
    with state.open_state() as conn:
        assert len([t for t in list_tasks(conn) if t.title == MAINTENANCE_TITLE]) == 1


def test_configure_refuses_a_new_request_for_an_opened_task(cfg, launcher_home):
    data(configure_cmd("--profile", "work"))
    refused = configure_cmd("something else")
    assert refused.exit_code == 1 and json.loads(refused.stdout)["error"]["code"] == "configure_request_ignored"


def test_configure_never_assigns_a_profile_on_its_own(cfg, launcher_home):
    refused = configure_cmd()  # no terminal, no --profile: the maintenance repository has no profile
    assert refused.exit_code == 1 and json.loads(refused.stdout)["error"]["code"] == "unknown_repository_profile"
    with state.open_state() as conn:
        assert list_tasks(conn) == []
    assert not (launcher_home / "mock-terminal.json").exists()


def test_configure_does_not_reassign_a_repository_that_has_a_profile(cfg, fake_agents, launcher_home):
    data(configure_cmd("--profile", "work"))
    cfg(
        profiles={
            "work": {"default_agent": "claude", "agents": {"claude": {"executable": str(fake_agents / "claude")}}},
            "other": {"default_agent": "codex", "agents": {"codex": {"executable": str(fake_agents / "codex")}}},
        }
    )
    refused = configure_cmd("--profile", "other")
    assert refused.exit_code == 1 and json.loads(refused.stdout)["error"]["code"] == "configure_profile_mismatch"
    assert data(run("profile", "which", str(launcher_home / "maintenance"), "--offline", "--json"))["profile"] == "work"


def test_configure_on_a_named_repository(cfg, make_repo, launcher_home):
    repo = make_repo("mine")
    result = data(configure_cmd("--repo", str(repo), "--profile", "work"))
    assert result["task"]["repo_path"] == str(repo.resolve())
    assert not (launcher_home / "maintenance").exists()  # the default repository is only made when it is used


def test_configure_picks_an_agent_of_the_repositorys_profile_only(cfg, launcher_home):
    refused = configure_cmd("--profile", "work", "--agent", "copilot")
    assert refused.exit_code == 1 and json.loads(refused.stdout)["error"]["code"] in {"agent_not_in_profile", "agent_unresolved"}


def test_configure_with_a_missing_skill_still_launches_with_a_notice(cfg, launcher_home):
    result = data(configure_cmd("--profile", "work"))
    assert result["notice"] and "agent-launcher" in result["notice"]  # not installed in the (temp) agent skills dirs



def test_a_hostile_global_git_config_cannot_break_the_maintenance_repository(cfg, launcher_home, tmp_path, monkeypatch):
    hostile = tmp_path / "hostile-gitconfig"
    hostile.write_text("[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = /usr/bin/false\n[core]\n\thooksPath = /nonexistent\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    result = data(configure_cmd("--profile", "work"))
    assert result["action"] == "created"
    assert hostile.read_text().count("gpgsign") == 1  # global config untouched


def test_an_initial_commit_that_failed_is_made_on_the_next_run(cfg, launcher_home, monkeypatch):
    from agent_launcher import configure as mod
    from agent_launcher.git import GitError

    real = mod._initial_commit
    monkeypatch.setattr(mod, "_initial_commit", lambda path: (_ for _ in ()).throw(GitError("git_failed", "boom")))
    failed = configure_cmd("--profile", "work")
    error = json.loads(failed.stdout)["error"]
    assert failed.exit_code == 1 and error["code"] == "maintenance_repository_failed" and "--repo" in error["message"]
    repo = launcher_home / "maintenance"
    assert (repo / ".git").exists()  # init ran; there is no commit yet
    monkeypatch.setattr(mod, "_initial_commit", real)
    assert data(configure_cmd("--profile", "work"))["action"] == "created"
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True).returncode == 0


def test_a_repository_with_commits_is_left_alone(cfg, launcher_home):
    data(configure_cmd("--profile", "work"))
    repo = launcher_home / "maintenance"
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    from agent_launcher.configure import ensure_maintenance_repository

    ensure_maintenance_repository()
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout == head


def test_a_new_request_for_an_unopened_maintenance_task_is_reported(cfg, launcher_home):
    # Make the first launch fail after the task exists: an unknown agent stops it before any session.
    failed = configure_cmd("--profile", "work", "first request", "--agent", "copilot")
    assert failed.exit_code == 1
    with state.open_state() as conn:
        (task,) = [t for t in list_tasks(conn) if t.title == MAINTENANCE_TITLE]
        assert task.description == "first request" and task.workflow == "agent-launcher-configure"
    refused = configure_cmd("a different request")
    assert refused.exit_code == 1 and json.loads(refused.stdout)["error"]["code"] == "configure_request_ignored"
    assert data(configure_cmd("first request"))["task"]["id"] == task.id  # the same request, or none, carries on


def test_a_users_own_task_with_the_same_title_is_not_the_maintenance_task(cfg, make_repo, launcher_home):
    repo = make_repo("mine")
    run("profile", "set", str(repo), "work", "--offline")
    own = data(run("new", "--title", MAINTENANCE_TITLE, "--repo", str(repo), "--offline", "--json"))["task"]
    result = data(configure_cmd("--repo", str(repo)))
    assert result["task"]["id"] != own["id"] and result["workflow"] == "agent-launcher-configure"
