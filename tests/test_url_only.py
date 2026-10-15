"""A task is one GitHub issue or PR; local tasks, `restart` and session adoption are gone (issue #17, ADR 0021).

Seam: the Typer CLI on the mock terminal, with `FakeGh`/`PullGh` and `fake_github` for GitHub, and `state` to prove that
a refusal "creates nothing". The copy-archive case is made by running the first-run copy's own archiver
(`copied_state.archive_copied_tasks`) over a database a real `open` has just written, so the archive event is the one the
copy writes. Old local tasks come from `old_state`, through the real first-run copy.

Expected values come from issue #17, ADR 0021, ADR 0005 and the coordinator's decisions for u5, not from the code.
"""

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

import old_state
from commodore_launcher import copied_state, state
from commodore_launcher.cli import app
from test_github_issues import URL, checkout, data, env, mock_calls, open_url, run  # noqa: F401  (fixtures)
from test_github_pulls import PR_URL, gh  # noqa: F401  (the gh fixture with pull requests)

runner = CliRunner()
EXITED = "Resume this conversation with:\n  claude --resume abc\n$ "
NEEDS_URL = "needs_issue_or_pr_url"
TABLES = (
    "tasks", "task_github", "repositories", "repository_paths", "repository_remotes", "profile_associations",
    "sessions", "launches", "task_events",
)


def failure(result) -> dict:
    assert result.exit_code == 1, result.output
    return json.loads(result.stdout)["error"]


def script(launcher_home: Path, **keys) -> None:
    """Set what the mock terminal reports (`gone`, `screens`, `resume_state`) between invocations."""
    file = launcher_home / "mock-terminal.json"
    current = json.loads(file.read_text()) if file.exists() else {}
    file.write_text(json.dumps({**current, **keys}))


def all_mock_calls(launcher_home: Path) -> list[dict]:
    file = launcher_home / "mock-terminal.json"
    return json.loads(file.read_text()).get("calls", []) if file.exists() else []


def row_counts() -> dict[str, int]:
    with state.open_state() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def open_id(task_id: str, *extra: str):
    return run("open", task_id, "--terminal", "mock", "--json", *extra)


def resume_id(task_id: str, *extra: str):
    return run("resume", task_id, "--terminal", "mock", "--json", *extra)


def events(task_id: str) -> list[tuple[str, str | None]]:
    with state.open_state() as conn:
        return conn.execute("SELECT event, detail FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()


def session_rows(task_id: str) -> list[tuple]:
    with state.open_state() as conn:
        return conn.execute(
            "SELECT s.id, c.conversation_id FROM sessions s JOIN agent_conversations c ON c.session_id = s.id "
            "WHERE s.task_id = ?",
            (task_id,),
        ).fetchall()


def copy_archive(launcher_home: Path, task_id: str) -> None:
    """What the first-run copy leaves behind for a task that had a session: archived with the copy's event, and the
    copied `launches` row of an old launch that stopped at `terminal_created` in an old worktree."""
    copied_state.archive_copied_tasks(launcher_home / "state.db")
    with state.open_state() as conn:
        conn.execute(
            "INSERT INTO launches (task_id, stage, agent, conversation_id, terminal_adapter, terminal_workspace_id, "
            "terminal_surface_id, terminal_created_by_launcher, pid, started_at, updated_at) "
            "VALUES (?, 'terminal_created', 'claude', 'old-conversation', 'mock', 'old-workspace', 'old-surface', 1, "
            "1, ?, ?)",
            (task_id, old_state.NOW, old_state.NOW),
        )


# --- 17.1: non-GitHub input fails and creates nothing -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "fix the login bug",
        "/Users/me/code/widgets",
        "https://gitlab.com/acme/widgets/issues/7",
        "http://example.com/acme/widgets/issues/7",
        "github.com/acme/widgets/issues/7",
        "https://github.com/acme/widgets",
        "https://github.com/acme/widgets/commit/abc123",
        "t",
        "task",
    ],
)
def test_open_of_anything_but_a_github_url_or_a_task_id_needs_an_issue_or_pr_url_and_creates_nothing(
    text, env, gh, launcher_home
):
    result = run("open", text, "--terminal", "mock", "--json")
    error = failure(result)
    assert error["code"] == NEEDS_URL
    assert "issue or PR URL" in error["message"]
    assert row_counts() == dict.fromkeys(TABLES, 0)
    assert not env.clones.exists() or list(env.clones.iterdir()) == []
    assert not (launcher_home / "maintenance").exists()
    assert all_mock_calls(launcher_home) == []
    assert gh.calls == []
    locks = launcher_home / "locks"
    assert not locks.exists() or list(locks.iterdir()) == []


def test_the_error_is_printed_on_stderr_for_a_person(env, gh):
    result = run("open", "fix the login bug", "--terminal", "mock")
    assert result.exit_code == 1
    assert result.stderr.startswith("error: ") and "issue or PR URL" in result.stderr


def test_an_existing_directory_is_not_a_task(env, gh, tmp_path, launcher_home):
    error = failure(run("open", str(tmp_path), "--terminal", "mock", "--json"))
    assert error["code"] == NEEDS_URL and "issue or PR URL" in error["message"]
    assert row_counts() == dict.fromkeys(TABLES, 0) and all_mock_calls(launcher_home) == []


def test_a_github_url_that_is_neither_an_issue_nor_a_pr_has_the_same_code(env, gh):
    error = failure(run("open", "https://github.com/acme/widgets/discussions/3", "--terminal", "mock", "--json"))
    assert error["code"] == NEEDS_URL and "issue or pull request URL" in error["message"]
    assert gh.calls == []


def test_free_text_that_prefixes_a_real_task_id_does_not_open_that_task(env, checkout, gh, launcher_home):
    task = json.loads(open_url().stdout)["task"]
    before, calls = row_counts(), len(all_mock_calls(launcher_home))
    for text in ("t", task["id"][:1], "t-" + "!" * 8):
        error = failure(run("open", text, "--terminal", "mock", "--json"))
        assert error["code"] == NEEDS_URL, text
    assert row_counts() == before and len(all_mock_calls(launcher_home)) == calls


def test_an_id_shaped_input_that_matches_no_task_is_task_not_found(env, checkout, launcher_home):
    open_url()
    for ref in ("t-zzzzzzzz", "t-zz"):
        error = failure(open_id(ref))
        assert error["code"] == "task_not_found", ref
    assert len(mock_calls(launcher_home)) == 1


def test_a_task_id_and_a_unique_prefix_still_open_the_task(env, checkout, launcher_home):
    task = json.loads(open_url().stdout)["task"]
    for ref in (task["id"], task["id"][:4]):
        result = json.loads(open_id(ref).stdout)
        assert result["action"] == "focused" and result["task"]["id"] == task["id"]


# --- 17.2: reopening focuses the first session -------------------------------------------------------------------


def test_an_issue_url_twice_launches_once(env, checkout, gh, launcher_home):
    first = data(open_url(URL))
    second = data(open_url(URL))
    assert first["action"] == "created" and second["action"] == "focused"
    assert second["session"]["id"] == first["session"]["id"]
    assert len(mock_calls(launcher_home)) == 1


def test_a_pr_url_twice_launches_once(env, checkout, gh, launcher_home):
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha="a" * 40)
    first = data(open_url(PR_URL))
    second = data(open_url(PR_URL))
    assert first["task"]["url"] == PR_URL and first["prompt"] == f"/commodore {PR_URL}"
    assert second["action"] == "focused" and second["session"]["id"] == first["session"]["id"]
    assert len(mock_calls(launcher_home)) == 1


def test_a_task_id_after_its_url_focuses_the_same_session(env, checkout, gh, launcher_home):
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha="a" * 40)
    for url in (URL, PR_URL):
        first = data(open_url(url))
        again = data(open_id(first["task"]["id"]))
        assert again["action"] == "focused" and again["session"]["id"] == first["session"]["id"]
    assert len(mock_calls(launcher_home)) == 2  # one launch per task, none for the reopens
    assert len(mock_calls(launcher_home, "focus_session")) == 2


# --- 17.3: resume continues the conversation; the removed commands are gone ---------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ("restart", "t-aaaaaaaa", "--yes"),
        ("new", "--title", "x", "--repo", "."),
        ("tasks", "link", "t-aaaaaaaa", "https://github.com/acme/widgets/issues/7"),
        ("sessions", "candidates", "t-aaaaaaaa"),
        ("sessions", "adopt", "t-aaaaaaaa", "w"),
        ("sessions",),
    ],
)
def test_removed_commands_are_unknown(argv):
    result = run(*argv)
    assert result.exit_code == 2
    assert "No such command" in result.output


def test_removed_commands_are_not_in_help():
    top = run("--help").output
    for word in ("restart", "sessions", "new "):
        assert word not in top, word
    assert "local task" not in top.lower()
    tasks = run("tasks", "--help").output
    for kept in ("list", "show", "archive", "unarchive"):
        assert kept in tasks
    assert "link" not in tasks
    assert "resume" in top and "open" in top
    assert "restart" not in run("open", "--help").output.lower() and "restart" not in run("resume", "--help").output.lower()


def test_resume_continues_the_stored_conversation(env, checkout, launcher_home):
    first = data(open_url())
    conversation = first["session"]["harness_conversation_id"]
    script(launcher_home, gone=["mock-workspace-1"])
    resumed = json.loads(resume_id(first["task"]["id"]).stdout)
    assert resumed["action"] == "resumed" and resumed["prompt"] is None
    assert mock_calls(launcher_home)[-1]["command"][-2:] == ["--resume", conversation]
    assert resumed["session"]["id"] == first["session"]["id"]
    assert resumed["session"]["harness_conversation_id"] == conversation
    assert len(session_rows(first["task"]["id"])) == 1


def test_resume_of_a_task_that_was_never_opened_is_no_session(env, checkout):
    task = json.loads(open_url().stdout)["task"]
    with state.open_state() as conn:
        conn.execute("DELETE FROM terminal_sessions")
        conn.execute("DELETE FROM agent_conversations")
        conn.execute("DELETE FROM sessions")
    assert failure(resume_id(task["id"]))["code"] == "no_session"


# --- resume impossible: a fresh conversation, in place (decision 1) ------------------------------------------------


def fresh_launch_checks(launcher_home, first, again, checkout, prompt):
    task_id = first["task"]["id"]
    assert again["action"] == "created"
    assert again["prompt"] == prompt and again["prompt_submitted"] is True
    assert again["session"]["id"] == first["session"]["id"]  # the same sessions row (ADR 0005)
    assert again["session"]["terminal"]["workspace_id"] != first["session"]["terminal"]["workspace_id"]
    last = mock_calls(launcher_home)[-1]
    assert last["prompt"] == prompt and last["submit_prompt"] is True
    assert "--resume" not in last["command"]
    assert last["working_directory"] == str(checkout.resolve())
    (row,) = session_rows(task_id)
    assert row[0] == first["session"]["id"]
    return row[1]


def test_a_claude_conversation_with_no_recorded_id_starts_fresh_in_place(env, checkout, launcher_home):
    first = data(open_url())
    with state.open_state() as conn:
        conn.execute("UPDATE agent_conversations SET conversation_id = NULL")
    script(launcher_home, gone=["mock-workspace-1"])
    again = data(open_url())
    conversation = fresh_launch_checks(launcher_home, first, again, checkout, f"/commodore {URL}")
    assert conversation and conversation == again["session"]["harness_conversation_id"]
    assert conversation != first["session"]["harness_conversation_id"]
    assert "restart" not in (again["notice"] or "").lower()


def test_resume_with_no_recorded_id_starts_fresh_in_place_too(env, checkout, launcher_home):
    first = data(open_url())
    with state.open_state() as conn:
        conn.execute("UPDATE agent_conversations SET conversation_id = NULL")
    script(launcher_home, gone=["mock-workspace-1"])
    again = json.loads(resume_id(first["task"]["id"]).stdout)
    fresh_launch_checks(launcher_home, first, again, checkout, f"/commodore {URL}")


def test_codex_after_exit_has_no_resume_so_it_starts_fresh_in_place(env, checkout, launcher_home, tmp_path):
    exe = tmp_path / "bin" / "codex"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    config_file = launcher_home / "config.json"
    config = json.loads(config_file.read_text())
    config["profiles"]["work"]["harnesses"]["codex"] = {"executable": str(exe)}
    config_file.write_text(json.dumps(config))
    first = data(open_url(URL, "--harness", "codex"))
    assert first["session"]["harness"] == "codex" and first["session"]["harness_conversation_id"] is None
    script(launcher_home, gone=["mock-workspace-1"])
    again = data(open_url(URL, "--harness", "codex"))
    assert again["action"] == "created" and again["session"]["id"] == first["session"]["id"]
    assert again["prompt"] == first["prompt"]  # the Commodore invocation, as at the first launch
    assert len(mock_calls(launcher_home)) == 2 and mock_calls(launcher_home)[-1]["submit_prompt"] is True
    assert len(session_rows(first["task"]["id"])) == 1


def test_a_failed_resume_names_no_removed_command(env, checkout, launcher_home):
    first = data(open_url())
    script(launcher_home, gone=["mock-workspace-1"], resume_state="failed")
    again = data(open_id(first["task"]["id"]))
    assert "restart" not in (again["notice"] or "").lower()


# --- old local tasks in a copied database -------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def old_home(fake_default_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("COMMODORE_LAUNCHER_HOME", raising=False)
    home = fake_default_home / ".agent-launcher"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"version": 2, "profiles": {"work": {}}}))
    conn = old_state.build(home / "state.db", 10)
    old_state.add_task(conn, "t-localone", "active", "Fix the login page")
    old_state.add_task(conn, "t-localtwo", "created", "Tidy the docs")
    conn.execute(
        "INSERT INTO sessions (id, task_id, profile, agent, state, created_at) "
        "VALUES ('s-oldsess1', 't-localone', 'work', 'claude', 'launched', ?)",
        (old_state.NOW,),
    )
    conn.execute("INSERT INTO agent_conversations (session_id, agent, conversation_id) VALUES ('s-oldsess1', 'claude', 'c-old')")
    conn.execute(
        "INSERT INTO terminal_sessions (session_id, adapter, workspace_id, surface_id, created_by_launcher) "
        "VALUES ('s-oldsess1', 'mock', 'old-workspace', 'old-surface', 1)"
    )
    conn.close()
    return home


def test_copied_local_tasks_stay_archived_and_listed(old_home):
    listed = json.loads(run("tasks", "list", "--json").stdout)["tasks"]
    assert {t["id"]: (t["state"], t["source"]) for t in listed} == {
        "t-localone": ("archived", "local"), "t-localtwo": ("archived", "local"),
    }
    assert "Fix the login page" in run("tasks", "list").stdout
    shown = json.loads(run("tasks", "show", "t-localone", "--json").stdout)
    assert shown["task"]["state"] == "archived" and shown["github"] is None


@pytest.mark.parametrize("command", ["open", "resume"])
def test_an_archived_local_task_cannot_be_opened_or_resumed(old_home, launcher_home, command):
    result = run(command, "t-localone", "--terminal", "mock", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] in {"task_needs_url", "task_archived"}
    assert all_mock_calls(launcher_home) == []


@pytest.mark.parametrize("command", ["open", "resume"])
@pytest.mark.parametrize("task_id", ["t-localone", "t-localtwo"])  # with a copied session, and without
def test_an_unarchived_local_task_is_still_refused_with_task_needs_url(old_home, launcher_home, command, task_id):
    assert run("tasks", "unarchive", task_id, "--json").exit_code == 0  # it stays listed and can be unarchived
    result = run(command, task_id, "--terminal", "mock", "--json")
    assert failure(result)["code"] == "task_needs_url"
    assert all_mock_calls(launcher_home) == []  # nothing focused, resumed or launched
    assert json.loads(run("tasks", "show", task_id, "--json").stdout)["task"]["id"] == task_id


# --- the copy-archived GitHub task launches fresh (the u4 decision) ----------------------------------------------


def test_open_url_of_a_copy_archived_task_unarchives_it_and_launches_fresh(env, checkout, launcher_home):
    first = data(open_url())
    task_id = first["task"]["id"]
    copy_archive(launcher_home, task_id)
    assert json.loads(run("tasks", "show", task_id, "--json").stdout)["task"]["state"] == "archived"

    again = data(open_url())
    assert again["action"] == "created" and again["task"]["state"] == "active" and again["task"]["id"] == task_id
    assert again["prompt"] == f"/commodore {URL}" and again["prompt_submitted"] is True
    assert again["session"]["id"] == first["session"]["id"]
    assert again["session"]["harness_conversation_id"] not in (None, first["session"]["harness_conversation_id"])
    last = mock_calls(launcher_home)[-1]
    assert last["prompt"] == f"/commodore {URL}" and "--resume" not in last["command"]
    assert last["working_directory"] == str(checkout.resolve())
    assert len(mock_calls(launcher_home)) == 2
    assert len(session_rows(task_id)) == 1
    with state.open_state() as conn:
        assert conn.execute("SELECT COUNT(*) FROM launches WHERE task_id = ?", (task_id,)).fetchone()[0] == 0
    assert [e for e, _ in events(task_id)][-2:] == ["archived", "unarchived"]
    # and it is an ordinary task again
    assert data(open_url())["action"] == "focused"


def test_open_id_of_a_copy_archived_task_does_the_same(env, checkout, launcher_home):
    first = data(open_url())
    task_id = first["task"]["id"]
    copy_archive(launcher_home, task_id)
    again = json.loads(open_id(task_id).stdout)
    assert again["action"] == "created" and again["task"]["state"] == "active"
    assert again["prompt"] == f"/commodore {URL}" and again["session"]["id"] == first["session"]["id"]
    assert len(session_rows(task_id)) == 1 and len(mock_calls(launcher_home)) == 2


def test_a_copy_archived_task_with_no_session_is_launched_by_a_first_open(env, checkout, launcher_home):
    first = data(open_url())
    task_id = first["task"]["id"]
    with state.open_state() as conn:
        conn.execute("DELETE FROM terminal_sessions")
        conn.execute("DELETE FROM agent_conversations")
        conn.execute("DELETE FROM sessions")
    copy_archive(launcher_home, task_id)
    again = data(open_url())
    assert again["action"] == "created" and again["prompt"] == f"/commodore {URL}"
    assert len(session_rows(task_id)) == 1


def test_a_refusal_leaves_a_copy_archived_task_archived(env, checkout, write_config, launcher_home):
    first = data(open_url())
    task_id = first["task"]["id"]
    copy_archive(launcher_home, task_id)
    config = json.loads((launcher_home / "config.json").read_text())
    config["profiles"]["work"]["harnesses"] = {}  # the harness cannot be resolved any more
    write_config(config)
    refused = run("open", URL, "--terminal", "mock", "--json")
    assert refused.exit_code == 1
    assert json.loads(run("tasks", "show", task_id, "--json").stdout)["task"]["state"] == "archived"
    assert [e for e, _ in events(task_id)] == ["archived"]
    with state.open_state() as conn:  # the copied launch record is not dropped by a refusal
        assert conn.execute("SELECT COUNT(*) FROM launches WHERE task_id = ?", (task_id,)).fetchone()[0] == 1
    assert len(mock_calls(launcher_home)) == 1


def test_a_task_the_user_archived_is_refused_and_names_unarchive(env, checkout, launcher_home):
    task_id = data(open_url())["task"]["id"]
    assert run("tasks", "archive", task_id, "--json").exit_code == 0
    for opener in (lambda: run("open", URL, "--terminal", "mock", "--json"), lambda: open_id(task_id)):
        error = failure(opener())
        assert error["code"] == "task_archived" and f"commodore tasks unarchive {task_id}" in error["message"]
    assert len(mock_calls(launcher_home)) == 1
    assert json.loads(run("tasks", "show", task_id, "--json").stdout)["task"]["state"] == "archived"


def test_the_latest_archive_event_decides_copied_or_user(env, checkout, launcher_home):
    """Copied, unarchived, then archived by the user: the user's archive wins, so `open` refuses."""
    task_id = data(open_url())["task"]["id"]
    copy_archive(launcher_home, task_id)
    assert run("tasks", "unarchive", task_id, "--json").exit_code == 0
    assert run("tasks", "archive", task_id, "--json").exit_code == 0
    assert failure(run("open", URL, "--terminal", "mock", "--json"))["code"] == "task_archived"
    assert len(mock_calls(launcher_home)) == 1


def test_a_task_archived_before_the_copy_counts_as_the_users(env, checkout, launcher_home):
    task_id = data(open_url())["task"]["id"]
    assert run("tasks", "archive", task_id, "--json").exit_code == 0
    copied_state.archive_copied_tasks(launcher_home / "state.db")  # skips an already archived task: no copy event
    assert failure(run("open", URL, "--terminal", "mock", "--json"))["code"] == "task_archived"


def test_unarchiving_a_copy_archived_task_marks_it_for_a_fresh_launch(env, checkout, launcher_home):
    first = data(open_url())
    task_id = first["task"]["id"]
    copy_archive(launcher_home, task_id)
    assert run("tasks", "unarchive", task_id, "--json").exit_code == 0
    (row,) = session_rows(task_id)
    assert row[1] is None  # the stored conversation of the old worktree is dropped

    again = data(open_url())
    assert again["action"] == "created" and again["session"]["id"] == first["session"]["id"]
    assert again["prompt"] == f"/commodore {URL}"
    assert "--resume" not in mock_calls(launcher_home)[-1]["command"]
    assert again["session"]["harness_conversation_id"] not in (None, first["session"]["harness_conversation_id"])
    assert len(session_rows(task_id)) == 1


def test_unarchiving_a_task_the_user_archived_keeps_its_conversation(env, checkout, launcher_home):
    first = data(open_url())
    task_id = first["task"]["id"]
    assert run("tasks", "archive", task_id, "--json").exit_code == 0
    assert run("tasks", "unarchive", task_id, "--json").exit_code == 0
    assert session_rows(task_id) == [(first["session"]["id"], first["session"]["harness_conversation_id"])]
    assert data(open_url())["action"] == "focused"


# --- the maintenance task is not a user's task --------------------------------------------------------------------


@pytest.fixture
def maintenance_config(write_config, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    write_config(
        {
            "version": 3,
            "terminal": {"adapter": "mock"},
            "harness_selection": "use_default",
            "profiles": {"work": {"default_harness": "claude", "harnesses": {"claude": {"executable": str(exe)}}}},
        }
    )


def configure_json(*extra):
    return run("configure", "--profile", "work", "--terminal", "mock", "--json", *extra)


def test_configure_still_launches_the_management_skill_and_focuses_on_a_rerun(maintenance_config, launcher_home):
    first = data(configure_json("make codex my default"))
    assert first["action"] == "created" and first["prompt"].startswith("/commodore-launcher ")
    assert "make codex my default" in first["prompt"]
    again = data(configure_json())
    assert again["action"] == "focused" and again["session"]["id"] == first["session"]["id"]
    assert len(mock_calls(launcher_home)) == 1


def test_integrate_gh_dash_still_launches(maintenance_config, launcher_home, monkeypatch):
    from commodore_launcher import cli

    monkeypatch.setattr(cli, "gh_dash_detected", lambda *a, **k: True)
    first = data(run("integrate", "gh-dash", "--profile", "work", "--terminal", "mock", "--json"))
    assert first["action"] == "created" and first["prompt"].startswith("/commodore-launcher ")
    assert data(run("integrate", "gh-dash", "--profile", "work", "--terminal", "mock", "--json"))["action"] == "focused"
    assert len(mock_calls(launcher_home)) == 1


def test_the_maintenance_task_is_hidden_from_task_listings(maintenance_config):
    task_id = data(configure_json())["task"]["id"]
    assert json.loads(run("tasks", "list", "--json").stdout)["tasks"] == []
    assert task_id not in run("tasks", "list").stdout
    assert run("tasks", "show", task_id, "--json").exit_code == 1


@pytest.mark.parametrize("argv", [("tasks", "archive"), ("tasks", "unarchive"), ("open",), ("resume",)])
def test_a_user_cannot_archive_open_or_resume_the_maintenance_task(maintenance_config, launcher_home, argv):
    task_id = data(configure_json())["task"]["id"]
    calls = len(all_mock_calls(launcher_home))
    expected = "active"
    if argv == ("tasks", "unarchive"):  # make it archived, so a refusal is not just "not archived"
        with state.open_state() as conn:
            conn.execute("UPDATE tasks SET state = 'archived', archived_state = 'active' WHERE id = ?", (task_id,))
        expected = "archived"
    extra = ("--terminal", "mock") if argv[0] in ("open", "resume") else ()
    result = run(*argv, task_id, *extra, "--json")
    assert result.exit_code == 1 and "error" in json.loads(result.stdout)
    assert len(all_mock_calls(launcher_home)) == calls  # no focus, no launch
    with state.open_state() as conn:
        assert conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] == expected


def test_configure_output_does_not_call_the_session_a_task(maintenance_config):
    out = run("configure", "--profile", "work", "--terminal", "mock")
    assert out.exit_code == 0, out.output
    assert "task" not in out.stdout.lower()
    again = run("configure", "--profile", "work", "--terminal", "mock")
    assert "task" not in again.stdout.lower()


def test_configure_names_no_removed_command_when_it_refuses_a_new_request(maintenance_config):
    data(configure_json("first request"))
    refused = configure_json("a second request")
    assert refused.exit_code == 1
    assert "restart" not in json.loads(refused.stdout)["error"]["message"].lower()


def test_a_profile_change_does_not_list_the_maintenance_task_among_the_affected(maintenance_config, launcher_home):
    task_id = data(configure_json())["task"]["id"]
    repo = json.loads(run("tasks", "list", "--json").stdout)  # hidden: nothing to list
    assert repo["tasks"] == []
    with state.open_state() as conn:
        path = conn.execute("SELECT repo_path FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
    config = json.loads((launcher_home / "config.json").read_text())
    config["profiles"]["personal"] = {}
    (launcher_home / "config.json").write_text(json.dumps(config))
    changed = json.loads(run("profile", "set", path, "personal", "--offline", "--json").stdout)
    assert changed["changed"] is True and changed["affected_tasks"] == []


def set_resume_args(launcher_home, args):
    config = json.loads((launcher_home / "config.json").read_text())
    config["profiles"]["work"]["harnesses"]["claude"]["resume_args"] = args
    (launcher_home / "config.json").write_text(json.dumps(config))


def test_a_failed_resume_closes_its_terminal_and_starts_fresh_in_place(env, checkout, launcher_home):
    first = data(open_url())
    script(launcher_home, gone=["mock-workspace-1"], resume_state="failed")
    again = data(open_url())
    assert again["action"] == "created" and again["resume_state"] == "failed"
    assert again["session"]["id"] == first["session"]["id"] and again["prompt"] == f"/commodore {URL}"
    # the terminal the failed resume created (workspace 2) is closed; the fresh one (workspace 3) is the stored one
    assert [c["session"]["workspace_id"] for c in mock_calls(launcher_home, "close_session")] == ["mock-workspace-2"]
    assert again["session"]["terminal"]["workspace_id"] == "mock-workspace-3"
    # the JSON shows the session as stored, with the new conversation ID, written with the terminal
    (stored,) = session_rows(first["task"]["id"])
    assert stored == (first["session"]["id"], again["session"]["harness_conversation_id"])
    assert stored[1] not in (None, first["session"]["harness_conversation_id"])
    with state.open_state() as conn:
        assert conn.execute("SELECT workspace_id FROM terminal_sessions").fetchall() == [("mock-workspace-3",)]
    assert "fresh one was started" in again["notice"] and "restart" not in again["notice"]


def test_a_forced_resume_over_a_live_harness_that_then_fails_starts_nothing_fresh(env, checkout, launcher_home):
    first = data(open_url())
    script(launcher_home, resume_state="failed")  # workspace 1 stays alive
    error = failure(resume_id(first["task"]["id"], "--force", "--yes"))
    assert error["code"] == "harness_running" and "mock-workspace-1" in error["message"]
    assert "commodore open" in error["message"] and "exited" not in error["message"]
    assert [c["session"]["workspace_id"] for c in mock_calls(launcher_home, "close_session")] == ["mock-workspace-2"]
    assert len(mock_calls(launcher_home)) == 2  # the live one and our closed resume attempt: nothing fresh
    with state.open_state() as conn:
        assert conn.execute("SELECT workspace_id FROM terminal_sessions").fetchall() == [("mock-workspace-1",)]
    assert session_rows(first["task"]["id"]) == [(first["session"]["id"], first["session"]["harness_conversation_id"])]


def test_a_harness_with_no_launch_form_is_refused_before_a_resume_terminal_exists(env, checkout, launcher_home):
    first = data(open_url())
    config = json.loads((launcher_home / "config.json").read_text())
    exe = config["profiles"]["work"]["harnesses"]["claude"]["executable"]
    config["harness_types"] = {"claude": {"adapter": "mystery", "executable": exe}}
    (launcher_home / "config.json").write_text(json.dumps(config))
    script(launcher_home, gone=["mock-workspace-1"], resume_state="failed")
    refused = run("open", URL, "--terminal", "mock", "--json")
    assert failure(refused)["code"] == "harness_no_launch_form"
    assert len(mock_calls(launcher_home)) == 1 and not mock_calls(launcher_home, "close_session")


def test_resume_of_a_url_says_resume_takes_a_task_id(env, checkout):
    open_url()
    error = failure(run("resume", URL, "--terminal", "mock", "--json"))
    assert error["code"] == NEEDS_URL and "takes the ID of a task" in error["message"]
    assert "is not a GitHub issue or PR URL" not in error["message"]


def test_resume_force_on_a_live_harness_that_cannot_resume_is_refused(env, checkout, launcher_home, tmp_path):
    exe = tmp_path / "bin" / "codex"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    config = json.loads((launcher_home / "config.json").read_text())
    config["profiles"]["work"]["harnesses"]["codex"] = {"executable": str(exe)}
    (launcher_home / "config.json").write_text(json.dumps(config))
    first = data(open_url(URL, "--harness", "codex"))
    calls = len(mock_calls(launcher_home))
    for extra in ((), ("--yes",)):
        error = failure(resume_id(first["task"]["id"], "--force", *extra))
        assert error["code"] == "harness_running" and "still running" in error["message"]
        assert "commodore open" in error["message"]
    assert len(mock_calls(launcher_home)) == calls and not mock_calls(launcher_home, "close_session")
    (row,) = session_rows(first["task"]["id"])
    assert row[0] == first["session"]["id"]


def test_a_fresh_conversation_closes_the_exited_terminal_it_replaces(env, checkout, launcher_home):
    first = data(open_url())
    with state.open_state() as conn:
        conn.execute("UPDATE agent_conversations SET conversation_id = NULL")
    script(launcher_home, screens={"mock-workspace-1": EXITED})
    again = data(open_url())
    assert again["action"] == "created" and again["session"]["id"] == first["session"]["id"]
    (closed,) = mock_calls(launcher_home, "close_session")
    assert closed["session"]["workspace_id"] == "mock-workspace-1"  # the launcher's own, exited terminal is closed
    assert again["session"]["terminal"]["workspace_id"] == "mock-workspace-2"


def test_resume_args_without_the_id_never_refuse_a_fresh_launch(env, checkout, launcher_home):
    first = data(open_url())
    task_id = first["task"]["id"]
    copy_archive(launcher_home, task_id)
    set_resume_args(launcher_home, ["--resume"])
    with state.open_state() as conn:
        before = (
            conn.execute("SELECT state FROM tasks").fetchall(),
            conn.execute("SELECT * FROM terminal_sessions").fetchall(),
            conn.execute("SELECT COUNT(*) FROM launches").fetchone()[0],
        )
    opened = run("open", URL, "--terminal", "mock", "--json")
    # resume_args are never used for a fresh launch, so this is not refused for them; it launches
    assert opened.exit_code == 0, opened.output
    assert before[0] == [("archived",)] and before[2] == 1
    assert json.loads(opened.stdout)["action"] == "created"
    assert len(session_rows(task_id)) == 1


def test_a_refusal_in_the_copy_archived_checks_changes_nothing(env, checkout, launcher_home, write_config):
    """The checks run before the unarchive, whichever refuses: here the session's harness is no longer configured."""
    first = data(open_url())
    task_id = first["task"]["id"]
    copy_archive(launcher_home, task_id)
    with state.open_state() as conn:
        before = (
            conn.execute("SELECT * FROM terminal_sessions").fetchall(),
            conn.execute("SELECT conversation_id FROM agent_conversations").fetchall(),
            conn.execute("SELECT COUNT(*) FROM launches").fetchone()[0],
        )
    config = json.loads((launcher_home / "config.json").read_text())
    config["profiles"]["work"]["harnesses"]["claude"]["executable"] = "/no/such/dir/claude"
    write_config(config)
    refused = run("open", task_id, "--terminal", "mock", "--json")
    error = failure(refused)
    assert error["code"] == "harness_unresolved"
    assert "commodore profile edit work --harness claude" in error["message"]  # names the way forward
    with state.open_state() as conn:
        assert conn.execute("SELECT state FROM tasks").fetchone()[0] == "archived"
        assert (
            conn.execute("SELECT * FROM terminal_sessions").fetchall(),
            conn.execute("SELECT conversation_id FROM agent_conversations").fetchall(),
            conn.execute("SELECT COUNT(*) FROM launches").fetchone()[0],
        ) == before
    assert [e for e, _ in events(task_id)] == ["archived"]


def test_a_copy_archived_launch_says_what_it_forgot_and_names_the_old_terminal(env, checkout, launcher_home):
    first = data(open_url())
    copy_archive(launcher_home, first["task"]["id"])
    again = data(open_url())
    assert "copied from the old launcher" in again["notice"] and "mock-workspace-1" in again["notice"]
    assert "never recorded" not in again["notice"] and "no longer exists" not in again["notice"]


def test_open_id_of_a_copy_archived_task_refreshes_its_title_first(env, checkout, gh, launcher_home):
    first = data(open_url())
    task_id = first["task"]["id"]
    copy_archive(launcher_home, task_id)
    gh.issues[(101, 7)]["title"] = "A better title"
    again = json.loads(open_id(task_id).stdout)
    assert again["task"]["title"] == "A better title"
    assert mock_calls(launcher_home)[-1]["title"].endswith("A better title")


def test_a_prefix_shared_with_the_hidden_maintenance_task_is_not_ambiguous(maintenance_config, launcher_home):
    from commodore_launcher.tasks import get_user_task, list_tasks

    data(configure_json())
    with state.open_state() as conn:
        (hidden,) = list_tasks(conn)
        conn.execute("INSERT INTO tasks (id, title, description, repository_id, repo_path, profile, state, created_at, updated_at, source) "
                     "SELECT 't-zzzzzzzz', 'Mine', '', repository_id, repo_path, profile, 'created', created_at, updated_at, 'github' FROM tasks")
        assert get_user_task(conn, "t-").id == "t-zzzzzzzz"
        with pytest.raises(Exception) as err:
            get_user_task(conn, hidden.id)
        assert err.value.code == "task_not_found"


def test_resume_of_input_that_is_not_a_task_id_needs_an_issue_or_pr_url(env, checkout, launcher_home):
    open_url()
    calls = len(all_mock_calls(launcher_home))
    for text in ("t", "fix the bug", URL):
        assert failure(run("resume", text, "--terminal", "mock", "--json"))["code"] == NEEDS_URL, text
    assert len(all_mock_calls(launcher_home)) == calls


# --- what is gone from the code and the skill -----------------------------------------------------------------------


def test_session_adoption_and_restart_are_removed_from_the_code():
    import importlib

    from commodore_launcher import github_tasks, launch, terminals

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("commodore_launcher.adoption")
    for module, name in (
        (launch, "restart_task"), (github_tasks, "link_task"), (terminals, "DISCOVER_SESSIONS"),
        (terminals, "ExternalSession"),
    ):
        assert not hasattr(module, name), f"{module.__name__}.{name}"
    assert not hasattr(terminals.TerminalAdapter, "discover_sessions")
    assert terminals.CLOSE_SESSION and hasattr(terminals.TerminalAdapter, "close_session")  # kept for a launch rollback


def test_the_bundled_skill_names_no_removed_command():
    skill = Path(json.loads(run("skill", "path", "--json").stdout)["path"])
    text = "\n".join(p.read_text() for p in [skill / "SKILL.md", *(skill / "reference").glob("*.md")])
    for gone in ("commodore restart", "`restart`", "tasks link", "commodore new", "sessions candidates", "sessions adopt"):
        assert gone not in text, gone
