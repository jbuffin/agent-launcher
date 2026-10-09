"""`agent-launcher integrate gh-dash`, the setup offer, the picker-surface fallback (ticket #21), on the mock terminal."""

import ast
import json
import re
from pathlib import Path

import pytest

from test_session_lifecycle import configure, configure as _configure_config, data, fake_agents, mock_calls, repo, run, task  # noqa: F401

from agent_launcher import cli, integrate, state
from agent_launcher.integrate import GH_DASH_TASK_TITLE, INTEGRATION_PROMPT, ISSUE_COMMAND, PR_COMMAND
from agent_launcher.tasks import list_tasks
from scripted import ScriptedPrompter

SRC = Path(__file__).resolve().parent.parent / "src" / "agent_launcher"


@pytest.fixture
def cfg(_configure_config):
    return _configure_config


@pytest.fixture
def installed(monkeypatch):
    monkeypatch.setattr(cli, "gh_dash_detected", lambda *a, **k: True)


def integrate_cmd(*extra):
    return run("integrate", "gh-dash", "--terminal", "mock", "--json", *extra)


# --- detection -----------------------------------------------------------------------------------


def test_detects_a_gh_extension_and_a_standalone_binary():
    from agent_launcher.doctor import CommandResult

    gh = lambda name: "/bin/gh" if name == "gh" else None  # noqa: E731
    ext = lambda argv, t: CommandResult(0, "gh dash\tdlvhdr/gh-dash\tv4.26.0\n", "")  # noqa: E731
    none = lambda argv, t: CommandResult(0, "gh stack\tgithub/gh-stack\tv0.2.0\n", "")  # noqa: E731
    assert integrate.gh_dash_detected(gh, ext)
    assert not integrate.gh_dash_detected(gh, none)
    assert integrate.gh_dash_detected(lambda n: "/bin/gh-dash" if n == "gh-dash" else None, none)


def test_not_installed_is_refused_with_the_manual_way(cfg, launcher_home):
    result = integrate_cmd("--profile", "work")  # the autouse fixture has no tools
    assert result.exit_code == 1 and json.loads(result.stdout)["error"]["code"] == "gh_dash_not_found"
    assert "docs/gh-dash.md" in json.loads(result.stdout)["error"]["message"]
    with state.open_state() as conn:
        assert list_tasks(conn) == []


# --- the maintenance task ------------------------------------------------------------------------


def test_integrate_launches_the_skill_with_the_prepared_prompt(cfg, installed, launcher_home):
    result = data(integrate_cmd("--profile", "work"))
    assert result["action"] == "created" and result["workflow"] == "agent-launcher-configure"
    assert result["task"]["title"] == GH_DASH_TASK_TITLE and result["task"]["source"] == "local"
    assert result["prompt"].startswith("/agent-launcher ")  # the management skill
    assert result["prompt_prepared"] and not result["prompt_submitted"]  # for the user to edit, not sent
    for needle in ("gh-dash", "Preserve all existing", ISSUE_COMMAND, PR_COMMAND, "Validate", "Report exactly"):
        assert needle in result["prompt"] or needle.lower() in result["prompt"].lower()
    (call,) = mock_calls(launcher_home, "create_session")
    assert call["prompt"] == result["prompt"]


def test_the_prompt_covers_the_spec_instruction_list():
    text = INTEGRATION_PROMPT.lower()
    for part in ("version", "locate", "keybindings", "documentation", "preserve", "issues", "conflict", "validate",
                 "test", "report"):
        assert part in text, part
    assert "{{.IssueNumber}}" in ISSUE_COMMAND and "{{.PrNumber}}" in PR_COMMAND and "{{.RepoName}}" in PR_COMMAND


def test_integrate_and_configure_have_separate_tasks(cfg, installed, launcher_home):
    first = data(integrate_cmd("--profile", "work"))
    other = data(run("configure", "--terminal", "mock", "--json"))
    assert other["task"]["id"] != first["task"]["id"] and other["action"] == "created"
    again = data(integrate_cmd())
    assert again["action"] == "focused" and again["task"]["id"] == first["task"]["id"]


def test_print_prompt_needs_nothing(cfg, launcher_home):
    out = run("integrate", "gh-dash", "--print-prompt")
    assert out.exit_code == 0 and out.stdout.strip() == INTEGRATION_PROMPT.strip()
    assert not launcher_home.joinpath("state.db").exists()


# --- setup offers it -----------------------------------------------------------------------------


def test_setup_offer_defaults_to_no_and_declining_launches_nothing(monkeypatch, launcher_home):
    monkeypatch.setattr(cli, "gh_dash_detected", lambda *a, **k: True)
    seen = []

    class Declines:
        def confirm(self, message, default=True):
            seen.append((message, default))
            return default  # just press Enter

        def say(self, text):
            pass

    cli._offer_gh_dash(Declines())
    assert len(seen) == 1 and "gh-dash is installed" in seen[0][0] and seen[0][1] is False
    assert not (launcher_home / "mock-terminal.json").exists()


def test_setup_says_nothing_when_gh_dash_is_missing(monkeypatch):
    monkeypatch.setattr(cli, "gh_dash_detected", lambda *a, **k: False)
    prompter = ScriptedPrompter()
    cli._offer_gh_dash(prompter)
    prompter.done()


def test_setup_accepting_creates_the_task_and_launches_with_the_skill_prompt(cfg, installed, launcher_home, monkeypatch):
    data(run("configure", "--terminal", "mock", "--profile", "work", "--json"))  # the maintenance repository gets its profile
    monkeypatch.setattr(cli, "is_interactive", lambda: False)  # no picker: everything is already decided
    prompter = ScriptedPrompter(("confirm", "gh-dash is installed", True))
    cli._offer_gh_dash(prompter)
    prompter.done()
    with state.open_state() as conn:
        (task,) = [t for t in list_tasks(conn) if t.title == GH_DASH_TASK_TITLE]
    assert task.workflow == "agent-launcher-configure"
    launches = [c for c in mock_calls(launcher_home, "create_session") if c["prompt"] and "gh-dash" in c["prompt"]]
    assert len(launches) == 1
    assert launches[0]["prompt"].startswith("/agent-launcher ") and ISSUE_COMMAND in launches[0]["prompt"]
    assert not launches[0]["submit_prompt"]


def test_a_refused_launch_does_not_undo_or_fail_setup(cfg, installed, launcher_home, monkeypatch):
    monkeypatch.setattr(cli, "is_interactive", lambda: False)  # the maintenance repository has no profile, nothing can ask
    cli._offer_gh_dash(ScriptedPrompter(("confirm", "gh-dash is installed", True)))  # does not raise
    with state.open_state() as conn:
        assert list_tasks(conn) == []


# --- the picker fallback -------------------------------------------------------------------------


def test_without_a_terminal_the_picker_runs_in_a_new_surface(cfg, task, launcher_home):
    # Two agents and `ask_each`-style selection: with no terminal, `open` cannot choose.
    cfg(agent_selection="always_ask")
    plain = run("open", task["id"], "--terminal", "mock", "--json")
    assert plain.exit_code == 1 and json.loads(plain.stdout)["error"]["code"] == "agent_selection_needed"
    result = run("open", task["id"], "--terminal", "mock", "--picker-surface")
    assert result.exit_code == 0 and "new terminal surface" in result.stderr
    (call,) = mock_calls(launcher_home, "run_interactive")
    assert call["command"][1:5] == ["-m", "agent_launcher", "open", task["id"]] and "--picker-surface" not in call["command"]
    assert "--terminal" in call["command"] and not mock_calls(launcher_home, "create_session")


def test_the_fallback_does_not_hide_other_errors(cfg, launcher_home):
    result = run("open", "nope", "--terminal", "mock", "--picker-surface", "--offline")
    assert result.exit_code == 1 and not (launcher_home / "mock-terminal.json").exists()


def test_a_terminal_without_the_capability_shows_the_original_refusal(cfg, task, launcher_home, monkeypatch):
    from agent_launcher.terminal_mock import MockTerminalAdapter

    cfg(agent_selection="always_ask")
    monkeypatch.setattr(MockTerminalAdapter, "run_interactive", lambda self, *a: (_ for _ in ()).throw(
        __import__("agent_launcher.terminals", fromlist=["x"]).UnsupportedCapability("mock", "run_interactive")))
    result = run("open", task["id"], "--terminal", "mock", "--picker-surface")
    assert result.exit_code == 1 and "agent" in result.stderr.lower()


# --- no YAML editing ----------------------------------------------------------------------------


def _code_only(source: str) -> str:
    """The source with every string literal (and so every docstring and the prompt text) blanked out."""
    lines = source.splitlines()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for n in range(node.lineno - 1, node.end_lineno):
                start = node.col_offset if n == node.lineno - 1 else 0
                end = node.end_col_offset if n == node.end_lineno - 1 else len(lines[n])
                lines[n] = lines[n][:start] + " " * (end - start) + lines[n][end:]
    return "\n".join(lines)


def test_agent_launcher_never_writes_gh_dash_configuration():
    """The agent edits gh-dash's config, guided by a prompt; no code here may. Checked on code with string literals
    removed: no module imports a YAML library, `integrate.py` does no file I/O, and no module names a gh-dash path
    in code. The prompt text may name the path."""
    for path in SRC.rglob("*.py"):
        code = _code_only(path.read_text())
        assert not re.search(r"^\s*(import|from)\s+(yaml|ruamel|strictyaml)", code, re.M), path
        assert "GH_DASH_CONFIG" not in code and "gh_dash_config" not in code.lower(), path
    own = _code_only((SRC / "integrate.py").read_text())
    assert not re.search(r"\bopen\(|write_text|write_bytes|\.write\(|shutil\.|os\.replace|unlink|mkdir|touch\(|Path\(", own)
    assert "yaml" not in (SRC.parent.parent / "pyproject.toml").read_text().lower()
    # the check itself sees code and not strings
    assert "open(" not in _code_only('x = "open(1)"\n') and "open(" in _code_only("open(1)\n")
