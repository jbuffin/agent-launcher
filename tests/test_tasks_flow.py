import json
import os
import sqlite3
import uuid

import pytest
from typer.testing import CliRunner

from commodore_launcher import cli, state
from commodore_launcher.cli import app
from commodore_launcher.config import Config
from commodore_launcher.interaction import Choice
from commodore_launcher.picker import PickerError, pick_agent
from commodore_launcher.tasks import Task, TaskError, get_task, list_tasks
from commodore_launcher.session_host import ForegroundHost, HarnessCommand
from conftest import RecordingHost, harness_runs, last_launch, make_github_task
from scripted import ScriptedPrompter

runner = CliRunner()


def run(*args):
    return runner.invoke(app, list(args))


@pytest.fixture
def fake_agents(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "codex"):
        exe = bin_dir / name
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    return bin_dir


@pytest.fixture
def configure(write_config, fake_agents):
    def _configure(**extra):
        agents = {n: {"executable": str(fake_agents / n)} for n in ("claude", "codex")}
        write_config(
            {
                "version": 4,
                "profiles": {
                    "work": {"default_harness": "claude", "harnesses": agents},
                    "personal": {"default_harness": "codex", "harnesses": {"codex": agents["codex"]}},
                },
                **extra,
            }
        )

    _configure()
    return _configure


@pytest.fixture
def repo(make_repo):
    path = make_repo("one")
    assert run("profile", "set", str(path), "work", "--offline").exit_code == 0
    return path


# --- registry -------------------------------------------------------------------------


def test_task_ids_are_short_opaque_and_unique(configure, repo):
    ids = {make_github_task(repo, f"t{i}")["id"] for i in range(30)}
    assert len(ids) == 30
    assert all(i.startswith("t-") and len(i) == 10 for i in ids)


def test_prefix_lookup_and_errors(configure, repo):
    task = make_github_task(repo, "a")
    with state.open_state() as conn:
        assert get_task(conn, task["id"][:5]).id == task["id"]
        assert get_task(conn, task["id"].upper()).id == task["id"]
        with pytest.raises(TaskError) as missing:
            get_task(conn, "t-zzzzzzzz")
        assert missing.value.code == "task_not_found"
        with pytest.raises(TaskError):
            get_task(conn, "")
        assert [t.id for t in list_tasks(conn)] == [task["id"]]


# --- picker ---------------------------------------------------------------------------


def pick(mode, agents=("claude", "codex"), default="claude", last_used=None, prompter=None, requested=None):
    return pick_agent(
        "work", list(agents), mode=mode, default=default, last_used=last_used, prompter=prompter, requested=requested
    )


def test_always_ask_asks_even_with_one_agent_and_highlights_last_used():
    seen = {}

    class Spy(ScriptedPrompter):
        def select(self, message, choices, default=None):
            seen.update(default=default, labels=[c.label for c in choices])
            return super().select(message, choices, default)

    prompter = Spy(("select", "harness", "codex"))
    result = pick("always_ask", last_used="codex", prompter=prompter)
    assert (result.agent, result.asked) == ("codex", True)
    assert seen["default"] == "codex" and "codex (last used)" in seen["labels"] and "claude (default)" in seen["labels"]
    assert pick("always_ask", agents=("claude",), prompter=ScriptedPrompter(("select", "harness", "claude"))).asked


def test_highlight_falls_back_to_default_when_last_used_is_not_in_profile():
    seen = {}

    class Spy(ScriptedPrompter):
        def select(self, message, choices, default=None):
            seen["default"] = default
            return super().select(message, choices, default)

    pick("always_ask", last_used="copilot", prompter=Spy(("select", "harness", "codex")))
    assert seen["default"] == "claude"


def test_use_default_never_asks():
    assert pick("use_default").agent == "claude"
    assert not pick("use_default").asked


def test_use_default_without_default_behaves_as_ask_if_multiple():
    assert pick("use_default", agents=("codex",), default=None).agent == "codex"
    with pytest.raises(PickerError) as err:
        pick("use_default", default=None)
    assert err.value.code == "harness_selection_needed"


def test_ask_if_multiple():
    assert pick("ask_if_multiple", agents=("codex",), default=None).agent == "codex"
    prompter = ScriptedPrompter(("select", "harness", "codex"))
    assert pick("ask_if_multiple", prompter=prompter).agent == "codex"


def test_asking_without_a_prompter_is_structured_error():
    with pytest.raises(PickerError) as err:
        pick("always_ask", last_used="codex")
    assert err.value.code == "harness_selection_needed"
    assert err.value.details["available_harnesses"] == ["claude", "codex"]
    assert err.value.details["suggested_harness"] == "codex"


def test_requested_agent_must_belong_to_profile_and_skips_prompt():
    assert pick("always_ask", requested="codex").agent == "codex"
    with pytest.raises(PickerError) as err:
        pick("always_ask", agents=("codex",), requested="claude")
    assert err.value.code == "harness_not_in_profile"
    with pytest.raises(PickerError) as none:
        pick("always_ask", agents=())
    assert none.value.code == "no_harnesses"


# --- the session host -----------------------------------------------------------------


def test_a_harness_command_never_shows_its_environment_values():
    command = HarnessCommand(("claude",), "/r", {"TOKEN": "secret"}, ("TOKEN",))
    assert "secret" not in repr(command) and "TOKEN" in repr(command)


def test_the_foreground_host_gives_the_harness_its_environment_and_directory(tmp_path):
    out = tmp_path / "out"
    status = ForegroundHost().run(
        None, HarnessCommand(("/bin/sh", "-c", 'printf "%s %s" "$PROFILE_VAR" "$PWD" > "$OUT"'), str(tmp_path),
                             {"PROFILE_VAR": "from-the-profile", "OUT": str(out), "PATH": os.environ["PATH"]}),
    )
    assert status == 0 and out.read_text() == f"from-the-profile {tmp_path}"



def test_no_module_knows_a_terminal():
    from pathlib import Path

    import commodore_launcher

    core = Path(commodore_launcher.__file__).parent
    assert not list(core.glob("terminal*.py"))
    for module in core.glob("*.py"):
        if module.name != "state.py":  # its migration notice says cmux is no longer used
            assert "cmux" not in module.read_text().lower(), module.name


# --- migration ------------------------------------------------------------------------


def test_migration_keeps_two_identities_in_separate_tables(launcher_home):
    with state.open_state() as conn:
        def cols(table):
            return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

        assert {"id"} <= cols("tasks") and "conversation_id" not in cols("tasks") and "workspace_id" not in cols("tasks")
        assert "conversation_id" in cols("agent_conversations")
        assert not cols("terminal_sessions")  # where a harness runs is not recorded (ADR 0022)
        assert "state" in cols("tasks")


def test_v1_database_upgrades_in_place(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    state.MIGRATIONS[0](conn)
    conn.execute("PRAGMA user_version = 1")
    conn.close()
    with state.open_state(path) as upgraded:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == state.SCHEMA_VERSION
        upgraded.execute("SELECT * FROM tasks")


# --- CLI end to end -------------------------------------------------------------------


def test_list_and_show_persist_across_invocations(configure, repo):
    task = make_github_task(repo, "Fix login")
    assert (task["profile"], task["state"], task["harness"], task["source"]) == ("work", "created", None, "github")
    listed = json.loads(run("tasks", "list", "--json").stdout)["tasks"]
    assert [t["id"] for t in listed] == [task["id"]]
    shown = json.loads(run("tasks", "show", task["id"], "--json").stdout)
    assert shown["task"]["title"] == "Fix login" and shown["task"]["url"] == task["url"]
    assert shown["task"]["repo_path"] == str(repo.resolve()) and shown["sessions"] == []
    assert task["id"] in run("tasks", "list").stdout
    assert "Fix login" in run("tasks", "show", task["id"]).stdout


def test_open_use_default_launches_and_keeps_identities_apart(configure, repo, launcher_home):
    configure(harness_selection="use_default")
    task = _new_task(repo, "Fix")
    opened = run("open", task["id"], "--offline")
    assert opened.exit_code == 0, opened.output
    data = last_launch()
    assert data["prompt"] == f"/commodore {task['url']}" and data["session"]["harness"] == "claude"
    assert data["task"]["state"] == "active" and data["task"]["harness"] == "claude"
    assert data["task"]["id"] == task["id"]
    assert data["session"]["id"] != task["id"]
    # The conversation ID is fixed at launch (`--session-id`) and stored, never discovered afterwards.
    conversation = data["session"]["harness_conversation_id"]
    assert str(uuid.UUID(conversation)) == conversation
    call = harness_runs()[0]
    assert call["command"][1:3] == ["--session-id", conversation]
    # The agent starts in the repository's checkout (#16).
    assert call["working_directory"] == str(repo.resolve()) and call["prompt"] == f"/commodore {task['url']}"
    assert "worktree" not in data
    assert call["command"][0].endswith("claude") and call["command"][-1] == call["prompt"]
    shown = json.loads(run("tasks", "show", task["id"], "--json").stdout)
    assert len(shown["sessions"]) == 1


def test_there_is_no_terminal_option(configure, repo):
    configure(harness_selection="use_default")
    task = _new_task(repo, "Fix")
    for argv in (("open", task["id"]), ("resume", task["id"]), ("configure",)):
        refused = run(*argv, "--terminal", "mock")
        assert refused.exit_code == 2 and "No such option" in refused.output


def test_non_interactive_open_needs_agent_then_remembers_last_used(configure, repo):
    task = _new_task(repo, "Fix")
    needed = run("open", task["id"], "--offline", "--dry-run", "--json")
    assert needed.exit_code == 1
    error = json.loads(needed.stdout)["error"]
    assert error["code"] == "harness_selection_needed" and error["available_harnesses"] == ["claude", "codex"]
    assert run("tasks", "show", task["id"], "--json").stdout and json.loads(
        run("tasks", "show", task["id"], "--json").stdout
    )["task"]["state"] == "created"
    assert run("open", task["id"], "--harness", "codex", "--offline").exit_code == 0
    # Another launch is asked interactively; the last-used agent is highlighted.
    seen = {}

    class Spy(ScriptedPrompter):
        def select(self, message, choices, default=None):
            seen["default"] = default
            return super().select(message, choices, default)

    prompter = Spy(("select", "harness", "claude"))
    run_interactive(prompter, "open", _new_task(repo)["id"], "--offline")
    assert seen["default"] == "codex"
    prompter.done()


def run_interactive(prompter, *args):
    cli.make_prompter = lambda: prompter
    cli.is_interactive = lambda: True
    return run(*args)


@pytest.fixture(autouse=True)
def restore_cli(monkeypatch):
    monkeypatch.setattr(cli, "make_prompter", cli.make_prompter)
    monkeypatch.setattr(cli, "is_interactive", cli.is_interactive)


def test_agent_of_another_profile_is_never_offered_or_accepted(configure, make_repo):
    repo = make_repo("two")
    assert run("profile", "set", str(repo), "personal", "--offline").exit_code == 0
    task = _new_task(repo, "T")
    refused = run("open", task["id"], "--harness", "claude", "--offline", "--dry-run", "--json")
    assert refused.exit_code == 1
    assert json.loads(refused.stdout)["error"]["code"] == "harness_not_in_profile"
    prompter = ScriptedPrompter(("select", "harness", "codex"))
    seen = {}
    orig = prompter.select

    def spy(message, choices, default=None):
        seen["choices"] = [c.value for c in choices]
        return orig(message, choices, default)

    prompter.select = spy
    done = run_interactive(prompter, "open", task["id"], "--offline")
    assert done.exit_code == 0 and seen["choices"] == ["codex"]


def test_unknown_task_is_not_found(configure):
    missing = run("tasks", "show", "t-nothing", "--json")
    assert missing.exit_code == 1 and json.loads(missing.stdout)["error"]["code"] == "task_not_found"
    assert run("open", "t-nothing").exit_code == 1


def test_open_refuses_when_repository_profile_changed(configure, repo):
    task = _new_task(repo, "T")
    assert run("profile", "set", str(repo), "personal", "--keep-tasks", "--offline").exit_code == 0
    result = run("open", task["id"], "--harness", "codex", "--offline", "--dry-run", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "profile_mismatch"


def _new_task(repo, title="T"):
    """A GitHub issue task for the repository, as `open <url>` leaves it (see `conftest.make_github_task`)."""
    return make_github_task(repo, title)


def test_the_prompt_is_submitted_at_launch_on_the_command_line(configure, repo, launcher_home):
    configure(harness_selection="use_default")
    task = _new_task(repo)
    assert run("open", task["id"], "--offline").exit_code == 0
    assert last_launch()["prompt"] == f"/commodore {task['url']}"
    call = harness_runs()[0]
    assert call["argv"][-2:] == ["--", last_launch()["prompt"]]  # Claude Code takes it on its command line and submits it


def test_always_ask_with_one_agent_asks_on_cli(configure, make_repo):
    repo = make_repo("solo")
    assert run("profile", "set", str(repo), "personal", "--offline").exit_code == 0
    task = _new_task(repo)
    needed = run("open", task["id"], "--offline", "--dry-run", "--json")
    error = json.loads(needed.stdout)["error"]
    assert error["code"] == "harness_selection_needed" and "several" not in error["message"]
    prompter = ScriptedPrompter(("select", "harness", "codex"))
    assert run_interactive(prompter, "open", task["id"], "--offline").exit_code == 0
    prompter.done()


def _open_with(host, config_overrides, repo):
    from commodore_launcher.config import load_config
    from commodore_launcher.launch import open_task

    with state.open_state() as conn:
        task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]
        return open_task(
            conn, task_id, config=load_config(), host=host, prompter=None, agent="claude", offline=True
        )


def test_a_held_session_lock_on_a_first_open_leaves_no_session_row(configure, repo):
    from commodore_launcher.errors import LauncherError

    _new_task(repo)

    class Held(RecordingHost):
        def claim(self, task_id):
            return None  # a harness of this task is running (an old launcher's, say)

    with pytest.raises(LauncherError) as err:
        _open_with(Held(), {}, repo)
    assert err.value.code == "harness_running"
    with state.open_state() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert conn.execute("SELECT state, agent FROM tasks").fetchone() == ("created", None)


def test_open_refuses_when_checkout_is_a_different_repository(configure, repo):
    import shutil
    import subprocess

    task = _new_task(repo)
    shutil.rmtree(repo)
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/o/other"], check=True)
    result = run("open", task["id"], "--harness", "claude", "--offline", "--dry-run", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "repository_changed"
    with state.open_state() as conn:
        assert conn.execute("SELECT COUNT(*) FROM profile_associations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_the_profile_environment_is_the_harness_environment(write_config, fake_agents, make_repo, fake_default_home):
    from conftest import COMMODORE_SKILLS, install_skills

    install_skills(fake_default_home / ".claude-work" / "skills", *COMMODORE_SKILLS)  # the profile's config dir
    write_config(
        {
            "version": 4,
            "harness_selection": "use_default",
            "profiles": {
                "work": {
                    "default_harness": "claude",
                    "harnesses": {"claude": {"executable": str(fake_agents / "claude"), "env": {"CLAUDE_CONFIG_DIR": "~/.claude-work"}}},
                }
            },
        }
    )
    repo = make_repo("envrepo")
    assert run("profile", "set", str(repo), "work", "--offline").exit_code == 0
    task = _new_task(repo, "Fix login")
    plan = json.loads(run("open", task["id"], "--offline", "--dry-run", "--json").stdout)
    assert plan["profile_env"] == ["CLAUDE_CONFIG_DIR"] and "--session-id" in plan["command"]
    assert run("open", task["id"], "--offline").exit_code == 0
    (call,) = harness_runs()
    # No shell: the environment is the process's own, so a shell's start-up files cannot change it, and nothing is
    # typed where a scrollback or a shell history would keep it.
    assert call["profile_env"] == ["CLAUDE_CONFIG_DIR"] and "CLAUDE_CONFIG_DIR" in call["env_keys"]
    assert "Fix login" not in " ".join(call["argv"])
