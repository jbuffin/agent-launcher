import json
import sqlite3

import pytest
from typer.testing import CliRunner

from agent_launcher import cli, state
from agent_launcher.cli import app
from scripted import CANCEL, ScriptedPrompter

runner = CliRunner()


@pytest.fixture(autouse=True)
def profiles(write_config):
    write_config({"version": 2, "profiles": {"work": {}, "personal": {}}})


def run(*args):
    return runner.invoke(app, list(args))


def test_set_by_path_then_which_json(make_repo, fake_github):
    fake_github.add("o/one", 3)
    repo = make_repo("one", "https://github.com/o/one")
    done = run("profile", "set", str(repo), "work", "--json")
    assert done.exit_code == 0
    data = json.loads(done.stdout)
    assert (data["profile"], data["previous_profile"], data["changed"]) == ("work", None, True)
    assert data["repository"]["github_id"] == 3
    which = run("profile", "which", str(repo), "--json")
    assert json.loads(which.stdout)["profile"] == "work"


def test_set_by_owner_name_and_by_url(fake_github):
    fake_github.add("o/one", 3)
    assert run("profile", "set", "o/one", "work").exit_code == 0
    # The same repository, named differently, is already associated.
    again = run("profile", "set", "https://github.com/O/One.git", "work")
    assert again.exit_code == 0 and "already uses profile work" in again.stdout


def test_set_unknown_profile_lists_available():
    result = run("profile", "set", "o/one", "ghost", "--json")
    assert result.exit_code == 1
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "unknown_profile" and error["available_profiles"] == ["personal", "work"]


def test_changing_needs_force(make_repo):
    repo = make_repo("one", "https://github.com/o/one")
    run("profile", "set", str(repo), "work")
    refused = run("profile", "set", str(repo), "personal", "--json")
    assert refused.exit_code == 1
    error = json.loads(refused.stdout)["error"]
    assert error["code"] == "association_exists" and error["current_profile"] == "work"
    assert "later release" in error["message"]
    assert json.loads(run("profile", "which", str(repo), "--json").stdout)["profile"] == "work"
    forced = run("profile", "set", str(repo), "personal", "--force", "--json")
    assert forced.exit_code == 0 and json.loads(forced.stdout)["previous_profile"] == "work"
    assert json.loads(run("profile", "which", str(repo), "--json").stdout)["profile"] == "personal"


def test_which_unknown_repository_non_interactive_json_error(make_repo, launcher_home):
    repo = make_repo("one")
    result = run("profile", "which", str(repo), "--json")
    assert result.exit_code == 1
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "unknown_repository_profile"
    assert error["available_profiles"] == ["personal", "work"]
    rows = sqlite3.connect(launcher_home / "state.db").execute("SELECT count(*) FROM profile_associations").fetchone()
    assert rows[0] == 0


def test_which_unknown_repository_prompts_on_a_terminal(make_repo, monkeypatch):
    repo = make_repo("one")
    prompter = ScriptedPrompter(("select", "Which profile", "personal"))
    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    monkeypatch.setattr(cli, "make_prompter", lambda: prompter)
    result = run("profile", "which", str(repo))
    assert result.exit_code == 0 and result.stdout.strip().endswith(": personal")
    prompter.done()
    monkeypatch.setattr(cli, "make_prompter", lambda: ScriptedPrompter())  # saved: no second prompt
    assert run("profile", "which", str(repo), "--json").exit_code == 0


def test_which_json_never_prompts_even_on_a_terminal(make_repo, monkeypatch):
    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    monkeypatch.setattr(cli, "make_prompter", lambda: pytest.fail("must not prompt with --json"))
    assert run("profile", "which", str(make_repo("one")), "--json").exit_code == 1


def test_cancelled_prompt_saves_nothing(make_repo, monkeypatch, launcher_home):
    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    monkeypatch.setattr(cli, "make_prompter", lambda: ScriptedPrompter(("select", "profile", CANCEL)))
    result = run("profile", "which", str(make_repo("one")))
    assert result.exit_code == 130
    assert sqlite3.connect(launcher_home / "state.db").execute("SELECT count(*) FROM profile_associations").fetchone()[0] == 0


def test_offline_flag_skips_github(make_repo, fake_github):
    run("profile", "set", str(make_repo("one", "https://github.com/o/one")), "work", "--offline")
    assert fake_github.calls == []


def test_not_a_repository(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = run("profile", "set", str(plain), "work", "--json")
    assert result.exit_code == 1 and json.loads(result.stdout)["error"]["code"] == "not_a_repository"


def test_no_profiles_configured(write_config, make_repo):
    write_config({"version": 2})
    result = run("profile", "which", str(make_repo("one")), "--json")
    assert json.loads(result.stdout)["error"]["code"] == "no_profiles"


def test_state_db_is_separate_from_config(launcher_home, make_repo):
    before = (launcher_home / "config.json").read_text()
    run("profile", "set", str(make_repo("one")), "work")
    assert (launcher_home / "state.db").exists()
    assert (launcher_home / "config.json").read_text() == before


def test_doctor_reports_the_real_database(make_repo):
    run("profile", "set", str(make_repo("one")), "work")
    result = run("doctor", "--json")
    check = next(c for c in json.loads(result.stdout)["checks"] if c["id"] == "database")
    assert check["status"] == "pass" and f"schema version {state.SCHEMA_VERSION}" in check["detail"]
