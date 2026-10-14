import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from commodore_launcher import cli, state
from commodore_launcher.cli import app
from commodore_launcher.config import Config
from commodore_launcher.interaction import Choice
from commodore_launcher.picker import PickerError, pick_agent
from commodore_launcher.tasks import Task, TaskError, create_task, get_task, list_tasks
from commodore_launcher.terminal_mock import MockTerminalAdapter
from commodore_launcher.terminals import (
    TerminalError,
    CREATE_SESSION,
    CreateSessionRequest,
    TerminalSessionRef,
    UnsupportedCapability,
)
from conftest import make_launchable
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
                "version": 3,
                "terminal": {"adapter": "mock"},
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


def mock_calls(launcher_home):
    return json.loads((launcher_home / "mock-terminal.json").read_text())["calls"]


# --- registry -------------------------------------------------------------------------


def test_task_ids_are_short_opaque_and_unique(configure, repo):
    ids = set()
    with state.open_state() as conn:
        rid = conn.execute("SELECT id FROM repositories").fetchone()
        rid = rid[0] if rid else 1
        for i in range(30):
            ids.add(create_task(conn, f"t{i}", "", rid, str(repo), "work").id)
    assert len(ids) == 30
    assert all(i.startswith("t-") and len(i) == 10 for i in ids)


def test_prefix_lookup_and_errors(configure, repo):
    with state.open_state() as conn:
        rid = conn.execute("SELECT id FROM repositories").fetchone()[0]
        task = create_task(conn, "a", "", rid, str(repo), "work")
        assert get_task(conn, task.id[:5]).id == task.id
        assert get_task(conn, task.id.upper()).id == task.id
        with pytest.raises(TaskError) as missing:
            get_task(conn, "t-zzzzzzzz")
        assert missing.value.code == "task_not_found"
        with pytest.raises(TaskError):
            get_task(conn, "")
        with pytest.raises(TaskError) as empty_title:
            create_task(conn, "  ", "", rid, str(repo), "work")
        assert empty_title.value.code == "invalid_task"
        assert [t.id for t in list_tasks(conn)] == [task.id]


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


# --- prompt, adapter ------------------------------------------------------------------


def test_mock_adapter_records_without_env_values(tmp_path):
    adapter = MockTerminalAdapter(tmp_path / "rec.json")
    assert CREATE_SESSION in adapter.capabilities()
    result = adapter.create_session(
        CreateSessionRequest(
            "t", "/r", ["claude"], env={"TOKEN": "secret"}, prompt="hi", submit_prompt=True, prompt_args=("--", "{prompt}")
        )
    )
    assert result.prompt_submitted is True
    with pytest.raises(TerminalError, match="launch arguments"):  # as cmux: a prompt travels only on the command line
        adapter.create_session(CreateSessionRequest("t", "/r", ["claude"], prompt="hi"))
    text = (tmp_path / "rec.json").read_text()
    assert "TOKEN" in text and "secret" not in text
    assert adapter.calls[0]["op"] == "create_session"


def test_unsupported_capability_is_explicit():
    class Minimal(MockTerminalAdapter):
        name = "minimal"

        def capabilities(self):
            return {CREATE_SESSION}

    with pytest.raises(UnsupportedCapability) as err:
        Minimal().require("focus_session")
    assert err.value.code == "unsupported_capability"
    from commodore_launcher.terminals import TerminalAdapter

    class Bare(TerminalAdapter):
        name = "bare"

        def capabilities(self):
            return set()

        def available(self):
            return True

        def create_session(self, request):
            raise AssertionError

    with pytest.raises(UnsupportedCapability):
        Bare().close_session(TerminalSessionRef("bare", "w"))


def test_core_has_no_cmux_types():
    from pathlib import Path

    import commodore_launcher

    core = Path(commodore_launcher.__file__).parent
    for name in ("terminals.py", "launch.py", "sessions.py", "tasks.py", "picker.py"):
        assert "cmux" not in (core / name).read_text().lower(), name


# --- migration ------------------------------------------------------------------------


def test_migration_keeps_three_identities_in_separate_tables(launcher_home):
    with state.open_state() as conn:
        def cols(table):
            return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

        assert {"id"} <= cols("tasks") and "conversation_id" not in cols("tasks") and "workspace_id" not in cols("tasks")
        assert "conversation_id" in cols("agent_conversations")
        assert "workspace_id" in cols("terminal_sessions")
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


def test_new_list_show_persist_across_invocations(configure, repo):
    created = run("new", "--title", "Fix login", "--description", "It breaks", "--repo", str(repo), "--offline", "--json")
    assert created.exit_code == 0, created.stdout
    task = json.loads(created.stdout)["task"]
    assert (task["profile"], task["state"], task["harness"]) == ("work", "created", None)
    listed = json.loads(run("tasks", "list", "--json").stdout)["tasks"]
    assert [t["id"] for t in listed] == [task["id"]]
    shown = json.loads(run("tasks", "show", task["id"], "--json").stdout)
    assert shown["task"]["title"] == "Fix login" and shown["task"]["description"] == "It breaks"
    assert shown["task"]["repo_path"] == str(repo.resolve()) and shown["sessions"] == []
    assert task["id"] in run("tasks", "list").stdout
    assert "Fix login" in run("tasks", "show", task["id"]).stdout


def test_open_use_default_launches_via_mock_and_keeps_identities_apart(configure, repo, launcher_home):
    configure(harness_selection="use_default")
    task = _new_task(repo, "Fix")
    opened = run("open", task["id"], "--offline", "--json")
    assert opened.exit_code == 0, opened.stdout
    data = json.loads(opened.stdout)
    assert data["prompt"] == "/commodore-launcher Fix" and data["session"]["harness"] == "claude"
    assert data["task"]["state"] == "active" and data["task"]["harness"] == "claude"
    assert data["task"]["id"] == task["id"]
    assert data["session"]["id"] != task["id"]
    # The conversation ID is fixed at launch (`--session-id`) and stored, never discovered afterwards.
    conversation = data["session"]["harness_conversation_id"]
    assert str(uuid.UUID(conversation)) == conversation
    assert data["session"]["terminal"]["workspace_id"] == "mock-workspace-1"
    call = mock_calls(launcher_home)[0]
    assert call["command"][-2:] == ["--session-id", conversation]
    # The agent runs in the task's own worktree, not the repository directory.
    assert call["working_directory"] == data["worktree"]["path"] != str(repo.resolve()) and call["prompt"] == "/commodore-launcher Fix"
    assert data["worktree"]["ownership"] == "created" and Path(call["working_directory"]).is_dir()
    assert call["command"][0].endswith("claude") and call["submit_prompt"] is True
    assert call["title"] == f"{repo.name} — Fix"
    shown = json.loads(run("tasks", "show", task["id"], "--json").stdout)
    assert len(shown["sessions"]) == 1


def test_terminal_override_beats_config(configure, repo, launcher_home):
    configure(harness_selection="use_default", terminal={"adapter": "wezterm"})
    task = _new_task(repo, "Fix")
    refused = run("open", task["id"], "--offline", "--json")
    assert refused.exit_code == 1
    assert json.loads(refused.stdout)["error"]["code"] == "terminal_adapter_unavailable"
    assert run("open", task["id"], "--terminal", "mock", "--offline", "--json").exit_code == 0


def test_non_interactive_open_needs_agent_then_remembers_last_used(configure, repo):
    task = _new_task(repo, "Fix")
    needed = run("open", task["id"], "--offline", "--json")
    assert needed.exit_code == 1
    error = json.loads(needed.stdout)["error"]
    assert error["code"] == "harness_selection_needed" and error["available_harnesses"] == ["claude", "codex"]
    assert run("tasks", "show", task["id"], "--json").stdout and json.loads(
        run("tasks", "show", task["id"], "--json").stdout
    )["task"]["state"] == "created"
    assert run("open", task["id"], "--harness", "codex", "--offline", "--json").exit_code == 0
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
    refused = run("open", task["id"], "--harness", "claude", "--offline", "--json")
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


def test_unknown_repository_profile_fails_noninteractively(configure, make_repo):
    repo = make_repo("three")
    result = run("new", "--title", "T", "--repo", str(repo), "--offline", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "unknown_repository_profile"


def test_new_rejects_non_directory_and_unknown_task(configure):
    assert run("new", "--title", "T", "--repo", "/nonexistent/x", "--json").exit_code == 1
    missing = run("tasks", "show", "t-nothing", "--json")
    assert missing.exit_code == 1 and json.loads(missing.stdout)["error"]["code"] == "task_not_found"
    assert run("open", "t-nothing", "--json").exit_code == 1


def test_open_refuses_when_repository_profile_changed(configure, repo):
    task = _new_task(repo, "T")
    assert run("profile", "set", str(repo), "personal", "--keep-tasks", "--offline").exit_code == 0
    result = run("open", task["id"], "--harness", "codex", "--offline", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "profile_mismatch"


def _new_task(repo, title="T"):
    """A local task that `open` can launch (see `conftest.make_launchable`)."""
    made = json.loads(run("new", "--title", title, "--repo", str(repo), "--offline", "--json").stdout)["task"]
    make_launchable(made["id"])
    return made


def test_the_prompt_is_submitted_at_launch_on_the_command_line(configure, repo, launcher_home):
    configure(harness_selection="use_default")
    task = _new_task(repo)
    data = json.loads(run("open", task["id"], "--offline", "--json").stdout)
    assert data["prompt_submitted"] is True and data["prompt"] == "/commodore-launcher T"
    call = mock_calls(launcher_home)[0]
    assert call["submit_prompt"] is True and call["prompt"] == data["prompt"]
    assert call["prompt_args"] == ["--", "{prompt}"]  # Claude Code takes it on its command line and submits it


def test_mock_workspace_ids_are_unique_across_invocations(configure, repo, launcher_home):
    configure(harness_selection="use_default")
    for _ in range(2):
        assert run("open", _new_task(repo)["id"], "--offline", "--json").exit_code == 0
    ids = [c["session"]["workspace_id"] for c in mock_calls(launcher_home)]
    assert ids == ["mock-workspace-1", "mock-workspace-2"]


def test_always_ask_with_one_agent_asks_on_cli(configure, make_repo):
    repo = make_repo("solo")
    assert run("profile", "set", str(repo), "personal", "--offline").exit_code == 0
    task = _new_task(repo)
    needed = run("open", task["id"], "--offline", "--json")
    error = json.loads(needed.stdout)["error"]
    assert error["code"] == "harness_selection_needed" and "several" not in error["message"]
    prompter = ScriptedPrompter(("select", "harness", "codex"))
    assert run_interactive(prompter, "open", task["id"], "--offline").exit_code == 0
    prompter.done()


def _open_with(adapter, config_overrides, repo):
    from commodore_launcher.config import load_config
    from commodore_launcher.launch import open_task

    with state.open_state() as conn:
        task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]
        return open_task(
            conn, task_id, config=load_config(), adapter=adapter, prompter=None, agent="claude", offline=True
        )


def _minimal_adapter(caps):
    from commodore_launcher.terminals import TerminalAdapter

    class Minimal(MockTerminalAdapter):
        name = "minimal"

        def capabilities(self):
            return set(caps)

    return Minimal()


def test_create_session_failure_leaves_no_session_row(configure, repo):
    _new_task(repo)

    class Failing(MockTerminalAdapter):
        def create_session(self, request):
            raise RuntimeError("terminal exploded")

    with pytest.raises(RuntimeError):
        _open_with(Failing(), {}, repo)
    with state.open_state() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert conn.execute("SELECT state, agent FROM tasks").fetchone() == ("launch_failed", None)


def test_open_refuses_when_checkout_is_a_different_repository(configure, repo):
    import shutil
    import subprocess

    task = _new_task(repo)
    shutil.rmtree(repo)
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/o/other"], check=True)
    result = run("open", task["id"], "--harness", "claude", "--offline", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "repository_changed"
    with state.open_state() as conn:
        assert conn.execute("SELECT COUNT(*) FROM profile_associations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_open_through_the_cmux_adapter_with_a_fake_cmux(write_config, fake_agents, make_repo, monkeypatch):
    from test_terminal_cmux import FakeCmux, adapter

    claude = str(fake_agents / "claude")
    write_config(
        {
            "version": 3,
            "terminal": {"adapter": "cmux"},
            "harness_selection": "use_default",
            "profiles": {
                "work": {
                    "default_harness": "claude",
                    "harnesses": {"claude": {"executable": claude, "env": {"CLAUDE_CONFIG_DIR": "~/.claude-work"}}},
                }
            },
        }
    )
    repo = make_repo("cmuxrepo")
    assert run("profile", "set", str(repo), "work", "--offline").exit_code == 0

    def new_task():
        return _new_task(repo, "Fix login")

    def launch(fake, task):
        monkeypatch.setattr(cli, "select_adapter", lambda name: adapter(fake))
        return run("open", task["id"], "--offline")

    # The prompt rides on the command line, read from a private temp file: nothing is pasted, typed or read back.
    fake = FakeCmux()
    out = launch(fake, new_task())
    assert out.exit_code == 0, out.output
    (argv,) = fake.commands("new-workspace")
    assert argv[argv.index("--name") + 1] == f"{repo.name} — Fix login"
    cwd = argv[argv.index("--cwd") + 1]
    assert cwd != str(repo.resolve()) and Path(cwd).name.startswith("t-")
    home = os.environ["HOME"]
    typed = argv[argv.index("--command") + 1]
    assert typed.startswith(f" /usr/bin/env CLAUDE_CONFIG_DIR={home}/.claude-work {claude} --session-id ")
    assert "$(cat -- " in typed and "Fix login" not in typed
    assert not fake.commands("paste") and not fake.commands("send-key") and not fake.commands("read-screen")
