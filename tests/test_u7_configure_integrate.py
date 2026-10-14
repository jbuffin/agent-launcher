"""u7 (#19): `configure` and `integrate gh-dash` on the new command.

Most of #19 landed in u3/u5, so those tests are regression guards and pass today (marked GUARD). The tests marked
RED cover what u7 adds: the no-`--profile` message, the setup offer's wording, the gh-dash version and key notes,
and the term "local task" in the gh-dash docs.
"""

import json
from pathlib import Path

import pytest

from test_session_lifecycle import configure as _configure_config, data, fake_agents, mock_calls, run  # noqa: F401

from commodore_launcher import cli
from commodore_launcher.integrate import INTEGRATION_PROMPT, ISSUE_COMMAND, PR_COMMAND

ROOT = Path(__file__).resolve().parent.parent
GH_DASH_SHA = "b1376625b2e4d50f2b2d761bf2fa9ec543e79380"  # gh-dash v4.26.1
GH_DASH_DOCS = "https://www.gh-dash.dev/configuration/keybindings/"


@pytest.fixture
def cfg(_configure_config):
    return _configure_config


@pytest.fixture
def installed(monkeypatch):
    monkeypatch.setattr(cli, "gh_dash_detected", lambda *a, **k: True)


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
    result = data(run("configure", "add a search root", "--profile", "work", "--harness", harness, "--terminal", "mock", "--json"))
    expected = f"{prefix}commodore-launcher Configure Commodore Launcher\n\nadd a search root"  # title, blank line, request
    assert result["prompt"] == expected and result["prompt_submitted"] is True
    (call,) = mock_calls(launcher_home, "create_session")
    assert call["prompt"] == expected and call["submit_prompt"] is True


# --- GUARD: 19.2 the integrate prompt names `commodore open`, never `agent-launcher` -----------------------------


def test_guard_print_prompt_names_commodore_open_for_issues_and_prs_and_no_agent_launcher():
    out = run("integrate", "gh-dash", "--print-prompt").stdout
    assert "commodore open https://github.com/{{.RepoName}}/issues/{{.IssueNumber}}" in out
    assert "commodore open https://github.com/{{.RepoName}}/pull/{{.PrNumber}}" in out
    assert "agent-launcher" not in out.lower() and "{{.Url}}" not in out  # gh-dash passes no Url


def test_guard_integrate_launches_commodore_launcher_with_the_prompt_submitted(cfg, installed, launcher_home):
    result = data(run("integrate", "gh-dash", "--profile", "work", "--terminal", "mock", "--json"))
    (call,) = mock_calls(launcher_home, "create_session")
    assert call["prompt"].startswith("/commodore-launcher ") and ISSUE_COMMAND in call["prompt"] and PR_COMMAND in call["prompt"]
    assert call["submit_prompt"] is True and result["prompt_submitted"] is True


# --- GUARD: --harness, not --agent ------------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["configure"], ["integrate", "gh-dash"]])
def test_guard_help_offers_harness_and_profile_and_not_agent(argv):
    out = run(*argv, "--help").stdout
    assert "--harness" in out and "--profile" in out and "--agent" not in out
    assert run(*argv, "--agent", "claude").exit_code != 0


# --- RED: no --profile names --profile (and still never picks one) ------------------------------------------------


def _no_profile_error(result):
    assert result.exit_code == 1, result.output
    return json.loads(result.stdout)["error"]


def test_configure_without_a_profile_names_the_profile_option_and_lists_the_profiles(cfg, fake_agents, launcher_home):
    two_profiles(cfg, fake_agents)
    error = _no_profile_error(run("configure", "--terminal", "mock", "--json"))
    assert error["code"] == "unknown_repository_profile"  # same code: callers and NEEDS_CHOICE depend on it
    assert "--profile" in error["message"] and "work" in error["message"] and "play" in error["message"]


def test_integrate_without_a_profile_names_the_profile_option_and_lists_the_profiles(cfg, fake_agents, installed, launcher_home):
    two_profiles(cfg, fake_agents)
    error = _no_profile_error(run("integrate", "gh-dash", "--terminal", "mock", "--json"))
    assert error["code"] == "unknown_repository_profile"
    assert "--profile" in error["message"] and "work" in error["message"] and "play" in error["message"]


def test_configure_on_a_named_repository_without_a_profile_names_the_profile_option(cfg, make_repo, launcher_home):
    repo = make_repo("elsewhere")
    result = run("configure", "--repo", str(repo), "--terminal", "mock", "--json")
    assert "--profile" in _no_profile_error(result)["message"]


def test_guard_the_shared_profile_message_is_unchanged(cfg, make_repo, launcher_home):
    # `open` (and `profile set`) keep their own wording: the rewrite is local to configure/integrate.
    repo = make_repo("elsewhere")
    from commodore_launcher.associations import AssociationError, ensure_profile
    from commodore_launcher.repositories import identify_reference
    from commodore_launcher import state

    with state.open_state() as conn, pytest.raises(AssociationError) as caught:
        ensure_profile(conn, identify_reference(str(repo), fetch_github=False), ["work"], None)
    assert "--profile" not in str(caught.value) and "commodore profile set" in str(caught.value)


# --- RED: the setup offer no longer promises a review ------------------------------------------------------------


def test_the_setup_offer_says_the_prompt_is_submitted_at_launch(monkeypatch):
    monkeypatch.setattr(cli, "gh_dash_detected", lambda *a, **k: True)
    seen = []

    class Declines:
        def confirm(self, message, default=True):
            seen.append(message)
            return False

        def say(self, text):
            pass

    cli._offer_gh_dash(Declines())
    (message,) = seen
    assert "review its prompt first" not in message and "submit" in message.lower()


# --- RED: gh-dash version, key clash and terms -------------------------------------------------------------------


def test_the_checked_gh_dash_version_and_sources_are_recorded_beside_the_commands():
    source = (ROOT / "src" / "commodore_launcher" / "integrate.py").read_text()
    assert "4.26.1" in source and "4.26.0" not in source
    assert GH_DASH_SHA in source and GH_DASH_DOCS in source


def test_the_prompt_says_b_is_free_in_issues_and_prs_but_bound_in_notifications():
    text = INTEGRATION_PROMPT
    assert "4.26.0" not in text and "4.26.1" in text
    assert "notifications" in text and "toggleBookmark" in text and "`b`" in text


def test_the_gh_dash_doc_matches_the_prompt():
    text = (ROOT / "docs" / "gh-dash.md").read_text()
    assert "4.26.1" in text and "4.26.0" not in text
    assert "notifications" in text and "toggleBookmark" in text
    assert "local maintenance task" not in text and "local task" not in text
    assert "maintenance session" in text
    assert "{{.Url}}" not in text
