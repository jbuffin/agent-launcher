"""u7 (#19): `configure` on the new command. The gh-dash integration it once shared is gone (ADR 0022).

Most of #19 landed in u3/u5, so these are regression guards (marked GUARD), plus the no-`--profile` message u7 added.
"""

import json

import pytest

from conftest import harness_runs
from test_session_lifecycle import configure as _configure_config, data, fake_agents, run  # noqa: F401


@pytest.fixture
def cfg(_configure_config):
    return _configure_config


def two_profiles(cfg, fake_agents):
    cfg(
        profiles={
            "work": {"default_harness": "claude", "harnesses": {"claude": {"executable": str(fake_agents / "claude")}}},
            "play": {"default_harness": "codex", "harnesses": {"codex": {"executable": str(fake_agents / "codex")}}},
        }
    )


# --- GUARD: 19.1 configure launches the management skill with the request, for each harness ----------------------


@pytest.mark.parametrize("harness, prefix", [("claude", "/"), ("codex", "$")])
def test_guard_configure_prompt_is_the_harness_invocation_of_the_skill_with_the_request(cfg, launcher_home, harness, prefix):
    result = data(run("configure", "add a search root", "--profile", "work", "--harness", harness))
    expected = f"{prefix}commodore-launcher Configure Commodore Launcher\n\nadd a search root"  # title, blank line, request
    assert result["prompt"] == expected
    (call,) = harness_runs()
    assert call["prompt"] == expected and call["argv"][-2:] == ["--", expected]  # submitted by the harness at launch


# --- GUARD: --harness, not --agent ------------------------------------------------------------------------------


def test_guard_help_offers_harness_and_profile_and_not_agent():
    out = run("configure", "--help").stdout
    assert "--harness" in out and "--profile" in out and "--agent" not in out
    assert run("configure", "--agent", "claude").exit_code != 0


def test_there_is_no_integrate_command():
    assert run("integrate", "gh-dash").exit_code != 0
    assert "integrate" not in run("--help").stdout


# --- no --profile names --profile (and still never picks one) ----------------------------------------------------


def _no_profile_error(result):
    assert result.exit_code == 1, result.output
    return json.loads(result.stdout)["error"]


def test_configure_without_a_profile_names_the_profile_option_and_lists_the_profiles(cfg, fake_agents, launcher_home):
    two_profiles(cfg, fake_agents)
    error = _no_profile_error(run("configure", "--dry-run", "--json"))
    assert error["code"] == "unknown_repository_profile"  # same code: callers depend on it
    assert "--profile" in error["message"] and "work" in error["message"] and "play" in error["message"]


def test_configure_on_a_named_repository_without_a_profile_names_the_profile_option(cfg, make_repo, launcher_home):
    repo = make_repo("elsewhere")
    result = run("configure", "--repo", str(repo), "--dry-run", "--json")
    assert "--profile" in _no_profile_error(result)["message"]


def test_guard_the_shared_profile_message_is_unchanged(cfg, make_repo, launcher_home):
    # `open` (and `profile set`) keep their own wording: the rewrite is local to configure.
    repo = make_repo("elsewhere")
    from commodore_launcher.associations import AssociationError, ensure_profile
    from commodore_launcher.repositories import identify_reference
    from commodore_launcher import state

    with state.open_state() as conn, pytest.raises(AssociationError) as caught:
        ensure_profile(conn, identify_reference(str(repo), fetch_github=False), ["work"], None)
    assert "--profile" not in str(caught.value) and "commodore profile set" in str(caught.value)
