import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_launcher.agents import AgentResolutionError, resolve_agent
from agent_launcher.cli import app
from agent_launcher.config import Config, ConfigError, load_config, validate_config
from agent_launcher.profiles import InstanceEdit, add_profile, edit_profile, list_profiles

runner = CliRunner()
BASE_ENV = {"PATH": "/nonexistent-path"}


def make_exe(directory: Path, name: str, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body)
    path.chmod(0o755)
    return path


def cfg(profiles: dict, **extra) -> Config:
    return Config.model_validate({"version": 2, "profiles": profiles, **extra})


# --- schema ---


def test_builtin_agent_types_are_separate_from_instances():
    config = Config()
    assert set(config.agent_types) == {"claude", "codex", "copilot"}
    assert config.profiles == {}


def test_profile_names_are_user_defined(write_config):
    write_config({"version": 2, "profiles": {"opensource": {"agents": {"claude": {}}}, "client-x": {}}})
    assert validate_config().valid


def test_instance_must_reference_known_agent_type(write_config):
    write_config({"version": 2, "profiles": {"w": {"agents": {"gemini": {}}}}})
    errors = validate_config().errors
    assert errors[0].field == "profiles.w.agents.gemini"


def test_default_agent_must_be_configured(write_config):
    write_config({"version": 2, "profiles": {"w": {"default_agent": "claude", "agents": {}}}})
    assert validate_config().errors[0].field == "profiles.w.default_agent"


def test_args_must_be_string_list(write_config):
    write_config({"version": 2, "profiles": {"w": {"agents": {"claude": {"args": "--x"}}}}})
    assert validate_config().errors[0].field == "profiles.w.agents.claude.args"


def test_version_1_file_still_valid(write_config):
    write_config({"version": 1, "debug": True})
    assert validate_config().valid


def test_unknown_field_in_profile_is_error_and_load_refuses(write_config):
    write_config({"version": 2, "profiles": {"w": {"colour": 1, "agents": {"claude": {}}}}})
    assert [e.field for e in validate_config().errors] == ["profiles.w.colour"]
    with pytest.raises(ConfigError, match="colour"):
        load_config()


def test_unknown_field_in_agent_type_is_warning_and_ignored_by_load(write_config):
    write_config({"version": 2, "agent_types": {"claude": {"adapter": "a", "executable": "c", "x": 1}}})
    report = validate_config()
    assert report.valid and report.unknown_fields == ["agent_types.claude.x"]
    assert load_config().agent_types["claude"].executable == "c"


# --- resolution ---


def test_resolves_absolute_executable_args_env_cwd(tmp_path):
    exe = make_exe(tmp_path / "bin", "claude-work")
    work = tmp_path / "work"
    work.mkdir()
    config = cfg(
        {
            "work": {
                "default_agent": "claude",
                "agents": {
                    "claude": {
                        "executable": str(exe),
                        "args": ["--flag", "a b; rm -rf /"],
                        "env": {"CLAUDE_CONFIG_DIR": "~/.claude-work"},
                        "working_directory": str(work),
                    }
                },
            }
        }
    )
    resolved = resolve_agent("work", config=config, base_env={"PATH": "/x", "HOME": "/home/u"})
    assert resolved.argv == (str(exe), "--flag", "a b; rm -rf /")
    assert resolved.env["CLAUDE_CONFIG_DIR"] == "/home/u/.claude-work"
    assert resolved.env["PATH"] == "/x"
    assert resolved.working_directory == work
    assert resolved.adapter == "claude-code"


def test_bare_name_found_on_instance_path_not_callers(tmp_path):
    exe = make_exe(tmp_path / "wrapper-bin", "claude")
    config = cfg({"p": {"agents": {"claude": {"env": {"PATH": str(tmp_path / "wrapper-bin")}}}}})
    resolved = resolve_agent("p", "claude", config=config, base_env=BASE_ENV)
    assert resolved.executable == exe


def test_bare_name_missing_from_caller_path_fails(tmp_path):
    make_exe(tmp_path / "bin", "claude")
    config = cfg({"p": {"agents": {"claude": {}}}})
    with pytest.raises(AgentResolutionError, match="not found on the PATH"):
        resolve_agent("p", "claude", config=config, base_env=BASE_ENV)


def test_wrapper_script_resolved_as_argv(tmp_path):
    wrapper = make_exe(tmp_path, "codex-personal", "#!/bin/sh\nexec codex \"$@\"\n")
    config = cfg({"p": {"agents": {"codex": {"executable": str(wrapper), "args": ["--x"]}}}})
    assert resolve_agent("p", "codex", config=config, base_env=BASE_ENV).argv == (str(wrapper), "--x")


@pytest.mark.parametrize(
    "setup,message",
    [
        ("missing", "does not exist"),
        ("directory", "is not a file"),
        ("not_executable", "is not executable"),
        ("relative", "relative path"),
    ],
)
def test_unverifiable_executable_fails_clearly(tmp_path, setup, message):
    target = tmp_path / "agent"
    if setup == "directory":
        target.mkdir()
    elif setup == "not_executable":
        target.write_text("x")
        target.chmod(0o644)
    value = "./agent" if setup == "relative" else str(target)
    config = cfg({"p": {"agents": {"claude": {"executable": value}}}})
    with pytest.raises(AgentResolutionError, match=message):
        resolve_agent("p", "claude", config=config, base_env=BASE_ENV)


def test_bad_working_directory_fails(tmp_path):
    exe = make_exe(tmp_path, "c")
    config = cfg({"p": {"agents": {"claude": {"executable": str(exe), "working_directory": str(tmp_path / "nope")}}}})
    with pytest.raises(AgentResolutionError, match="not a directory"):
        resolve_agent("p", "claude", config=config, base_env=BASE_ENV)


def test_unknown_profile_fails(tmp_path):
    with pytest.raises(AgentResolutionError, match="does not exist"):
        resolve_agent("ghost", "claude", config=cfg({}), base_env=BASE_ENV)


def test_default_agent_used_when_none_requested(tmp_path):
    exe = make_exe(tmp_path, "cx")
    config = cfg({"p": {"default_agent": "codex", "agents": {"codex": {"executable": str(exe)}}}})
    assert resolve_agent("p", config=config, base_env=BASE_ENV).agent == "codex"


def test_no_default_agent_fails_without_guessing(tmp_path):
    exe = make_exe(tmp_path, "cx")
    config = cfg({"p": {"agents": {"codex": {"executable": str(exe)}}}})
    with pytest.raises(AgentResolutionError, match="no default agent"):
        resolve_agent("p", config=config, base_env=BASE_ENV)


def test_safety_no_cross_profile_fallback(tmp_path):
    """SPEC §31 safety test 2: a work task never falls back to a personal agent."""
    personal = make_exe(tmp_path, "claude-personal")
    config = cfg(
        {
            "work": {"default_agent": "codex", "agents": {"codex": {}}},
            "personal": {"agents": {"claude": {"executable": str(personal)}, "codex": {"executable": str(personal)}}},
        }
    )
    # work's codex is configured but unavailable: personal's working codex must not be used.
    with pytest.raises(AgentResolutionError, match="not found"):
        resolve_agent("work", config=config, base_env=BASE_ENV)
    # work has no claude instance at all: personal's claude must not be used.
    with pytest.raises(AgentResolutionError, match="not configured for this profile"):
        resolve_agent("work", "claude", config=config, base_env=BASE_ENV)
    # A working `codex` owned by personal sits on the caller's PATH. Work's instance has its own
    # (empty) PATH, so the lookup must not reach it.
    personal_bin = tmp_path / "personal-bin"
    make_exe(personal_bin, "codex")
    empty = tmp_path / "work-bin"
    empty.mkdir()
    config = cfg(
        {
            "work": {"agents": {"codex": {"env": {"PATH": str(empty)}}}},
            "personal": {"agents": {"codex": {"env": {"PATH": str(personal_bin)}}}},
        }
    )
    assert resolve_agent("personal", "codex", config=config, base_env=BASE_ENV).executable == personal_bin / "codex"
    with pytest.raises(AgentResolutionError, match="not found"):
        resolve_agent("work", "codex", config=config, base_env={"PATH": str(personal_bin)})


def test_safety_unavailable_agent_fails_safely_without_running_anything(tmp_path):
    """SPEC §31 safety test 3: an unavailable agent is a diagnostic, not a launch."""
    marker = tmp_path / "ran"
    make_exe(tmp_path / "bin", "claude", f"#!/bin/sh\ntouch {marker}\n")
    config = cfg({"p": {"agents": {"claude": {"executable": str(tmp_path / "bin" / "gone")}}}})
    with pytest.raises(AgentResolutionError) as info:
        resolve_agent("p", "claude", config=config, base_env={"PATH": str(tmp_path / "bin")})
    assert "gone" in str(info.value)
    assert not marker.exists()


def test_resolving_does_not_execute_the_executable(tmp_path):
    marker = tmp_path / "ran"
    exe = make_exe(tmp_path, "c", f"#!/bin/sh\ntouch {marker}\n")
    config = cfg({"p": {"agents": {"claude": {"executable": str(exe)}}}})
    resolve_agent("p", "claude", config=config, base_env=BASE_ENV)
    assert not marker.exists()


def test_resolve_reads_config_file_by_default(write_config, tmp_path):
    exe = make_exe(tmp_path, "c")
    write_config({"version": 2, "profiles": {"p": {"agents": {"claude": {"executable": str(exe)}}}}})
    assert resolve_agent("p", "claude", base_env=BASE_ENV).executable == exe


def test_resolve_with_invalid_config_fails(write_config):
    write_config({"version": 2, "profiles": {"p": {"agents": {"claude": {"args": 5}}}}})
    with pytest.raises(AgentResolutionError, match="configuration is not usable"):
        resolve_agent("p", "claude", base_env=BASE_ENV)


# --- profile management ---


def test_add_edit_list_preserve_unrelated_config(write_config):
    path = write_config({"version": 1, "debug": True, "future": {"a": 1}})
    add_profile("work", [InstanceEdit("claude", env={"CLAUDE_CONFIG_DIR": "~/.claude-work"})])
    add_profile("personal", [InstanceEdit("codex", executable="/opt/codex-personal")], default_agent="codex")
    edit_profile("work", [InstanceEdit("codex", args=["--x"])], default_agent="codex")
    data = json.loads(path.read_text())
    assert data["debug"] is True and data["future"] == {"a": 1}
    assert data["version"] == 2
    assert data["profiles"]["work"]["default_agent"] == "codex"
    assert data["profiles"]["work"]["agents"]["claude"] == {"env": {"CLAUDE_CONFIG_DIR": "~/.claude-work"}}
    names = [p["name"] for p in list_profiles()]
    assert names == ["personal", "work"]


def test_edit_keeps_unknown_fields_and_other_profiles(write_config):
    path = write_config(
        {
            "version": 2,
            "future": {"keep": True},
            "agent_types": {"claude": {"adapter": "claude-code", "executable": "claude", "note": "keep"}},
            "profiles": {
                "a": {"default_agent": "claude", "agents": {"claude": {"args": ["1"]}}},
                "b": {"agents": {"codex": {}}},
            },
        }
    )
    edit_profile("a", [InstanceEdit("claude", env={"X": "1"})])
    data = json.loads(path.read_text())
    assert data["future"] == {"keep": True}
    assert data["agent_types"]["claude"]["note"] == "keep"
    assert data["profiles"]["a"]["agents"]["claude"] == {"args": ["1"], "env": {"X": "1"}}
    assert data["profiles"]["b"] == {"agents": {"codex": {}}}


def test_single_agent_becomes_default_and_is_recorded(write_config):
    add_profile("solo", [InstanceEdit("claude")])
    assert list_profiles()[0]["default_agent"] == "claude"


def test_edit_env_merge_unset_and_cwd_clear(write_config, tmp_path):
    add_profile("p", [InstanceEdit("claude", env={"A": "1", "B": "2"}, working_directory="/tmp")])
    edit_profile("p", [InstanceEdit("claude", env={"C": "3"}, unset_env=["A"], clear_working_directory=True)])
    inst = list_profiles()[0]["agents"]["claude"]
    assert inst == {"env": {"B": "2", "C": "3"}}


def test_add_existing_profile_fails(write_config):
    add_profile("p", [])
    with pytest.raises(ConfigError, match="already exists"):
        add_profile("p", [])


@pytest.mark.parametrize("name", ["", "a.b", "-x", "a b", "a/b"])
def test_invalid_profile_name(name):
    with pytest.raises(ConfigError, match="invalid profile name"):
        add_profile(name, [])


def test_edit_unknown_profile_fails():
    with pytest.raises(ConfigError, match="does not exist"):
        edit_profile("ghost", [])


def test_invalid_edit_writes_nothing(write_config):
    path = write_config({"version": 2})
    before = path.read_text()
    with pytest.raises(ConfigError, match="gemini"):
        add_profile("p", [InstanceEdit("gemini")])
    with pytest.raises(ConfigError, match="default_agent"):
        add_profile("p", [InstanceEdit("claude")], default_agent="codex")
    assert path.read_text() == before


def test_remove_agent_clears_its_default(write_config):
    add_profile("p", [InstanceEdit("claude"), InstanceEdit("codex")], default_agent="claude")
    edit_profile("p", [], remove_agents=["claude"])
    profile = list_profiles()[0]
    assert profile["default_agent"] is None and list(profile["agents"]) == ["codex"]


def test_profiles_survive_restart(write_config):
    add_profile("p", [InstanceEdit("claude")])
    # a new process only has the file
    assert [p["name"] for p in list_profiles()] == ["p"]


# --- CLI ---


def test_cli_add_list_edit_check(tmp_path):
    exe = make_exe(tmp_path, "claude-w")
    result = runner.invoke(
        app,
        ["profile", "add", "work", "--agent", "claude", "--executable", str(exe), "--arg", "--model", "--arg", "x", "--env", "A=b=c"],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["profile", "edit", "work", "--agent", "claude", "--prompt-mode", "stdin"])
    assert result.exit_code == 0, result.output
    listed = json.loads(runner.invoke(app, ["profile", "list", "--json"]).stdout)
    inst = listed["profiles"][0]["agents"]["claude"]
    assert listed["profiles"][0]["default_agent"] == "claude"
    assert inst["args"] == ["--model", "x"] and inst["env"] == {"A": "b=c"} and inst["prompt_mode"] == "stdin"
    check = runner.invoke(app, ["profile", "check", "work", "--json"])
    assert check.exit_code == 0
    assert json.loads(check.stdout)["argv"] == [str(exe), "--model", "x"]
    assert "work: claude (default)" in runner.invoke(app, ["profile", "list"]).stdout


def test_cli_check_failure_is_clear_and_exit_1(tmp_path):
    runner.invoke(app, ["profile", "add", "work", "--agent", "codex", "--executable", str(tmp_path / "nope")])
    result = runner.invoke(app, ["profile", "check", "work"])
    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_cli_errors(tmp_path):
    assert runner.invoke(app, ["profile", "edit", "ghost"]).exit_code == 1
    assert runner.invoke(app, ["profile", "add", "p", "--executable", "x"]).exit_code == 1
    assert runner.invoke(app, ["profile", "add", "p", "--agent", "claude", "--env", "nope"]).exit_code == 1
    assert runner.invoke(app, ["profile", "add", "p", "--agent", "claude", "--prompt-mode", "x"]).exit_code == 1


def test_cli_list_empty():
    result = runner.invoke(app, ["profile", "list"])
    assert result.exit_code == 0 and "No profiles" in result.stdout
    assert json.loads(runner.invoke(app, ["profile", "list", "--json"]).stdout) == {"profiles": []}


def test_cli_list_with_broken_config_fails(write_config):
    write_config("{")
    assert runner.invoke(app, ["profile", "list"]).exit_code == 1


# --- misspelt identity fields (SPEC §5 example spelling) ---


SPEC_EXAMPLE = {
    "version": 2,
    "profiles": {
        "work": {
            "default_agent": "claude",
            "agents": {
                "claude": {"environment": {"CLAUDE_CONFIG_DIR": "~/.claude-work"}},
                "codex": {"command": "codex-work"},
            },
        }
    },
}


def test_spec_spelling_fails_validation_with_suggestions(write_config):
    write_config(SPEC_EXAMPLE)
    report = validate_config()
    assert not report.valid
    messages = {e.field: e.message for e in report.errors}
    assert "'env'" in messages["profiles.work.agents.claude.environment"]
    assert "'executable'" in messages["profiles.work.agents.codex.command"]


def test_spec_spelling_is_refused_by_resolution(write_config, tmp_path):
    make_exe(tmp_path, "claude")
    write_config(SPEC_EXAMPLE)
    for agent in ("claude", "codex"):
        with pytest.raises(AgentResolutionError, match="not usable"):
            resolve_agent("work", agent, base_env={"PATH": str(tmp_path), "CLAUDE_CONFIG_DIR": "/personal"})


def test_unknown_profile_level_key_is_error_but_top_level_is_warning(write_config):
    write_config({"version": 2, "future": 1, "profiles": {"w": {"colour": 1}}})
    report = validate_config()
    assert [e.field for e in report.errors] == ["profiles.w.colour"]
    assert report.unknown_fields == ["future"]


def test_nul_in_env_value_rejected(write_config):
    write_config({"version": 2, "profiles": {"w": {"agents": {"claude": {"env": {"A": "x\0y"}}}}}})
    assert validate_config().errors[0].field == "profiles.w.agents.claude.env"


def test_tilde_expanded_in_instance_path_and_home(tmp_path):
    home = tmp_path / "h"
    make_exe(home / "bin", "claude")
    config = cfg({"p": {"agents": {"claude": {"env": {"HOME": str(home), "PATH": "~/bin:/nope"}}}}})
    resolved = resolve_agent("p", "claude", config=config, base_env={"PATH": "/x", "HOME": "/elsewhere"})
    assert resolved.executable == home / "bin" / "claude"
    assert resolved.env["PATH"] == f"{home}/bin:/nope"


# --- hand-edited, structurally invalid profiles ---


@pytest.mark.parametrize(
    "profiles,match",
    [
        ({"p": ["claude"]}, "profiles.p must be an object"),
        ({"p": {"agents": ["claude"]}}, "profiles.p.agents must be an object"),
        ({"p": {"agents": {"claude": "x"}}}, "profiles.p.agents.claude must be an object"),
        ({"p": {"agents": {"claude": {"env": ["A"]}}}}, "env must be an object"),
    ],
)
def test_edit_invalid_structure_is_config_error(write_config, profiles, match):
    path = write_config({"version": 2, "profiles": profiles})
    before = path.read_text()
    with pytest.raises(ConfigError, match=match):
        edit_profile("p", [InstanceEdit("claude", env={"A": "1"})])
    assert path.read_text() == before


def test_add_with_invalid_profiles_node_is_config_error(write_config):
    write_config({"version": 2, "profiles": []})
    with pytest.raises(ConfigError, match="profiles must be an object"):
        add_profile("p", [])


def test_cli_edit_invalid_structure_no_traceback(write_config):
    write_config({"version": 2, "profiles": {"p": {"agents": ["claude"]}}})
    result = runner.invoke(app, ["profile", "edit", "p", "--agent", "claude", "--env", "A=1"])
    assert result.exit_code == 1 and "must be an object" in result.output
