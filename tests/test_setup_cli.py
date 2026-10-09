import json

import pytest
from scripted import DEFAULT, ScriptedPrompter
from typer.testing import CliRunner

from agent_launcher import cli
from agent_launcher.config import load_config, validate_config

runner = CliRunner()

ANSWERS = {"prompt_execution": "execute", "search_roots": ["~/Projects"]}


@pytest.fixture
def answers(tmp_path):
    path = tmp_path / "answers.json"
    path.write_text(json.dumps(ANSWERS))
    return path


def test_setup_with_answers_and_yes(answers, launcher_home):
    result = runner.invoke(cli.app, ["setup", "--answers", str(answers), "--yes"])
    assert result.exit_code == 0, result.output
    assert '"execute"' in result.output
    assert load_config().prompt_execution == "execute" and validate_config().valid
    again = runner.invoke(cli.app, ["setup", "--answers", str(answers), "--yes", "--json"])
    assert json.loads(again.stdout)["changed"] is False


def test_setup_needs_yes_when_not_a_terminal(answers, launcher_home):
    result = runner.invoke(cli.app, ["setup", "--answers", str(answers), "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "needs_confirmation"
    assert not launcher_home.exists()


def test_setup_without_terminal_or_answers_fails(launcher_home):
    result = runner.invoke(cli.app, ["setup", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "needs_input"


def test_setup_structured_error_for_ambiguity(tmp_path, launcher_home):
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"profiles": [{"name": "a", "agents": {"claude": {}, "codex": {}}}]}))
    result = runner.invoke(cli.app, ["setup", "--answers", str(path), "--yes", "--json"])
    assert result.exit_code == 1
    err = json.loads(result.stdout)["error"]
    assert err["code"] in {"executable_not_found", "ambiguous_default"} and err["field"]


def test_dry_run(answers, launcher_home):
    result = runner.invoke(cli.app, ["setup", "--answers", str(answers), "--dry-run"])
    assert result.exit_code == 0 and "execute" in result.output
    assert not launcher_home.exists()


def test_bare_command_without_config_offers_setup(monkeypatch, launcher_home):
    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    prompter = ScriptedPrompter(("confirm", "Set up Agent Launcher now?", False))
    monkeypatch.setattr(cli, "make_prompter", lambda: prompter)
    result = runner.invoke(cli.app, [])
    assert result.exit_code == 0
    assert "No configuration found" in result.output and "Usage" in result.output
    prompter.done()
    assert not launcher_home.exists()


def test_bare_command_accepting_runs_the_wizard(monkeypatch, launcher_home):
    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    prompter = ScriptedPrompter(
        ("confirm", "Set up Agent Launcher now?", True),
        ("text", "Profile name", "me"),
        ("checkbox", "Which agents", ["claude"]),
        ("text", "Executable or wrapper", "/bin/sh"),
        ("select", "CLAUDE_CONFIG_DIR", "__none__"),
        ("text", "Extra environment", ""),
        ("confirm", "Add a profile?", False),
        ("select", "Terminal adapter", DEFAULT),
        ("text", "search roots", ""),
        ("text", "Worktree root", DEFAULT),
        ("select", "Workflow selection", DEFAULT),
        ("select", "Prompt execution", DEFAULT),
        ("select", "Agent selection", DEFAULT),
        ("confirm", "Apply", True),
    )
    monkeypatch.setattr(cli, "make_prompter", lambda: prompter)
    result = runner.invoke(cli.app, [])
    assert result.exit_code == 0, result.output
    prompter.done()
    assert "me" in load_config().profiles


def test_bare_command_with_config_just_shows_help(write_config):
    write_config({"version": 2})
    result = runner.invoke(cli.app, [])
    assert "Usage" in result.output and "No configuration" not in result.output


def test_bare_command_non_interactive_hints(launcher_home):
    result = runner.invoke(cli.app, [])
    assert "agent-launcher setup" in result.output and not launcher_home.exists()
