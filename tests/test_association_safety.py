"""SPEC §6 safety: a repository's profile changes only because the user ran `profile set`.

One test per trigger that must NOT change an association: an org/name change, a configured default, a
suggestion from a rule, agent availability, and the terminal environment. Each one checks the stored
association before and after, and that nothing was inferred for a repository the user never assigned.
"""

import json
import subprocess

import pytest
from typer.testing import CliRunner

from agent_launcher import state
from agent_launcher.associations import AssociationError, ensure_profile, get_association, set_profile
from agent_launcher.cli import app
from agent_launcher.repositories import identify_reference

runner = CliRunner()
PROFILES = ["personal", "work"]


def run(*args):
    return runner.invoke(app, list(args))


def stored_profiles() -> list[tuple]:
    with state.open_state() as conn:
        return conn.execute("SELECT repository_id, profile FROM profile_associations ORDER BY repository_id").fetchall()


@pytest.fixture
def conn():
    connection = state.connect()
    yield connection
    connection.close()


@pytest.fixture
def fake_agents(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return exe


@pytest.fixture
def configure(write_config, fake_agents):
    def _configure(**overrides):
        config = {
            "version": 2,
            "terminal": {"adapter": "mock"},
            "profiles": {
                "work": {"default_agent": "claude", "agents": {"claude": {"executable": str(fake_agents)}}},
                "personal": {},
            },
        }
        config.update(overrides)
        write_config(config)

    _configure()
    return _configure


def test_org_or_name_change_never_changes_the_association(conn, make_repo, fake_github):
    fake_github.add("old/name", 7)
    repo = make_repo("one", "https://github.com/old/name")
    set_profile(conn, identify_reference(str(repo)), "work")
    before = stored_profiles()
    # Renamed and transferred: same GitHub ID, new owner and name.
    fake_github.rename("old/name", "neworg/renamed")
    subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin", "https://github.com/neworg/renamed"], check=True)
    after_rename = identify_reference(str(repo))
    assert ensure_profile(conn, after_rename, PROFILES, prompter=None).profile == "work"
    assert stored_profiles() == before
    # The old name is taken by a different repository: it gets nothing, and the original keeps its profile.
    fake_github.add("old/name", 8)
    newcomer = identify_reference("old/name")
    with pytest.raises(AssociationError) as exc:
        ensure_profile(conn, newcomer, PROFILES, prompter=None)
    assert exc.value.code == "unknown_repository_profile"
    assert stored_profiles() == before
    assert get_association(conn, after_rename).profile == "work"


def test_a_configured_default_never_assigns_or_changes_a_profile(conn, make_repo, configure):
    configure(agent_selection="use_default")  # `default_agent` and `use_default` are agent defaults, not profile defaults
    known = identify_reference(str(make_repo("known")))
    set_profile(conn, known, "personal")
    unknown = identify_reference(str(make_repo("unknown")))
    before = stored_profiles()
    with pytest.raises(AssociationError) as exc:
        ensure_profile(conn, unknown, ["work"], prompter=None)  # even with a single profile
    assert exc.value.code == "unknown_repository_profile"
    assert ensure_profile(conn, known, PROFILES, prompter=None).profile == "personal"
    assert stored_profiles() == before


def test_a_rule_suggestion_never_assigns_or_changes_a_profile(conn, make_repo):
    known = identify_reference(str(make_repo("known")))
    set_profile(conn, known, "personal")
    unknown = identify_reference(str(make_repo("unknown")))
    before = stored_profiles()
    # A routing rule may suggest "work" for either repository. The stored one wins; the unknown one is still unknown.
    assert ensure_profile(conn, known, PROFILES, prompter=None, suggested="work").profile == "personal"
    with pytest.raises(AssociationError) as exc:
        ensure_profile(conn, unknown, PROFILES, prompter=None, suggested="work")
    assert exc.value.code == "unknown_repository_profile" and exc.value.details["suggested_profile"] == "work"
    assert stored_profiles() == before


def test_agent_availability_never_changes_the_association(make_repo, configure, fake_agents, write_config):
    repo = make_repo("one")
    assert run("profile", "set", str(repo), "work", "--offline").exit_code == 0
    before = stored_profiles()
    fake_agents.unlink()  # the agent is no longer installed
    opened = run("new", "--title", "T", "--repo", str(repo), "--offline", "--json")
    task = json.loads(opened.stdout)["task"]
    failed = run("open", task["id"], "--offline", "--json")
    assert failed.exit_code == 1
    # The profile loses its agents entirely: still the same association, and nothing else offered.
    write_config({"version": 2, "terminal": {"adapter": "mock"}, "profiles": {"work": {}, "personal": {}}})
    assert run("open", task["id"], "--offline", "--json").exit_code == 1
    assert json.loads(run("profile", "which", str(repo), "--offline", "--json").stdout)["profile"] == "work"
    assert stored_profiles() == before


def test_terminal_environment_never_changes_the_association(make_repo, configure, monkeypatch):
    known = make_repo("known")
    unknown = make_repo("unknown")
    assert run("profile", "set", str(known), "work", "--offline").exit_code == 0
    before = stored_profiles()
    for name, value in {
        "CMUX_WORKSPACE_ID": "workspace:9",
        "CMUX_SURFACE_ID": "surface:9",
        "TERM_PROGRAM": "ghostty",
        "CLAUDE_CONFIG_DIR": "/somewhere/personal",
        "GH_TOKEN": "x",
        "PWD": str(unknown),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(unknown)
    assert json.loads(run("profile", "which", str(known), "--offline", "--json").stdout)["profile"] == "work"
    refused = run("profile", "which", str(unknown), "--offline", "--json")
    assert refused.exit_code == 1 and json.loads(refused.stdout)["error"]["code"] == "unknown_repository_profile"
    refused = run("new", "--title", "T", "--repo", str(unknown), "--offline", "--json")
    assert refused.exit_code == 1 and json.loads(refused.stdout)["error"]["code"] == "unknown_repository_profile"
    assert stored_profiles() == before
