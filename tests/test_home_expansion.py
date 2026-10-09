import pytest

from agent_launcher.agents import _instance_env, instance_home, normalize_path
from agent_launcher.config import validate_data
from agent_launcher.wizard import AgentAnswer


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("//x/y", "/x/y"),
        ("///x//y/./z/", "/x/y/z"),
        ("/a/../b", "/b"),
        ("~/a//b", "/h/a/b"),
        ("$HOME/a", "/h/a"),
        ("${HOME}/a/", "/h/a"),
        ("$HOMEX/a", "$HOMEX/a"),
    ],
)
def test_normalize_path(value, expected):
    assert normalize_path(value, "/h") == expected


@pytest.mark.parametrize("spelling", ["$HOME/.c", "${HOME}/.c", "~/.c"])
def test_launch_expansion_matches_normalize_path(spelling):
    env = _instance_env({"HOME": "/caller"}, {"HOME": "/inst", "CLAUDE_CONFIG_DIR": spelling, "PATH": f"{spelling}/bin:/usr/bin"})
    assert env["CLAUDE_CONFIG_DIR"] == normalize_path(spelling, "/inst") == "/inst/.c"
    assert env["PATH"] == "/inst/.c/bin:/usr/bin"


def test_home_override_itself_expands_against_caller_home():
    assert _instance_env({"HOME": "/caller"}, {"HOME": "$HOME/sub"})["HOME"] == "/caller/sub"
    assert instance_home({"HOME": "${HOME}/sub"}, "/caller") == "/caller/sub"


def test_empty_home_is_treated_the_same_everywhere():
    assert instance_home({"HOME": ""}, "/caller") == "/caller"
    assert _instance_env({"HOME": "/caller"}, {"HOME": "", "X": "~/a"}) == {"HOME": "/caller", "X": "/caller/a"}


def test_empty_home_is_a_validation_error():
    errors, _ = validate_data({"version": 2, "profiles": {"p": {"agents": {"claude": {"env": {"HOME": ""}}}}}})
    assert errors and "HOME must not be empty" in errors[0].message
    with pytest.raises(ValueError):
        AgentAnswer(env={"HOME": ""})
