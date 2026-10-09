import json
import os
from pathlib import Path

import pytest
from scripted import CANCEL, DEFAULT, ScriptedPrompter

from agent_launcher.config import load_config, read_raw, validate_config
from agent_launcher.detect import detect, find_config_dirs
from agent_launcher.doctor import CommandResult
from agent_launcher.interaction import SetupCancelled
from agent_launcher.wizard import SetupAnswers, SetupError, load_answers, run_setup


def make_exe(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\necho 1.0\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def bin_dir(tmp_path):
    d = tmp_path / "bin"
    for name in ("claude", "codex", "git", "gh", "claude-work"):
        make_exe(d, name)
    return d


@pytest.fixture
def which(bin_dir):
    def _which(name):
        p = bin_dir / name
        return str(p) if p.exists() else None

    return _which


def runner(argv, timeout):
    return CommandResult(0, "tool 1.0\n", "")


@pytest.fixture
def detection(fake_home, bin_dir, which):
    (fake_home / ".claude-personal").mkdir()
    (fake_home / ".claude-work").mkdir()
    (fake_home / ".claude.json").write_text("{}")
    return detect(which=which, runner=runner, home=fake_home, path_env=str(bin_dir), os_name="TestOS 1")


def answers_file(tmp_path, data) -> Path:
    path = tmp_path / "answers.json"
    path.write_text(json.dumps(data))
    return path


GOOD = {
    "profiles": [
        {
            "name": "personal",
            "agents": {"claude": {"env": {"CLAUDE_CONFIG_DIR": "~/.claude-personal"}}},
        }
    ],
    "search_roots": ["~/Projects"],
    "prompt_execution": "prepare",
}


# --- detection -----------------------------------------------------------------------------


def test_detection_lists_tools_wrappers_and_config_dir_names(detection):
    ids = {c.id for c in detection.checks}
    assert {"python", "git", "gh", "gh-auth", "cmux", "gh-dash", "agent:claude", "agent:codex"} <= ids
    assert detection.agent_paths["claude"].endswith("/bin/claude")
    assert detection.agent_paths["copilot"] is None
    assert list(detection.wrappers) == ["claude-work"]
    assert detection.config_dirs == {"claude": [".claude-personal", ".claude-work"]}
    assert detection.os == "TestOS 1"


def test_config_dir_detection_never_reads_contents(fake_home):
    secret = fake_home / ".claude-personal"
    secret.mkdir()
    (secret / "credentials.json").write_text("TOKEN")
    secret.chmod(0o000)  # listing HOME must not need to open it
    try:
        assert find_config_dirs(fake_home) == {"claude": [".claude-personal"]}
    finally:
        secret.chmod(0o755)


# --- non-interactive ------------------------------------------------------------------------


def test_answers_file_writes_config_and_is_idempotent(tmp_path, which):
    answers = load_answers(answers_file(tmp_path, GOOD))
    first = run_setup(answers=answers, assume_yes=True, which=which)
    assert first.applied and first.changed
    config = load_config()
    assert config.profiles["personal"].default_agent == "claude"
    assert config.profiles["personal"].agents["claude"].env == {"CLAUDE_CONFIG_DIR": "~/.claude-personal"}
    assert config.repositories.search_roots == ["~/Projects"]
    assert validate_config().valid

    before = (tmp_path / "launcher-home" / "config.json").read_bytes()
    second = run_setup(answers=answers, assume_yes=True, which=which)
    assert not second.changed and not second.applied
    assert (tmp_path / "launcher-home" / "config.json").read_bytes() == before


def test_rerun_never_removes_existing_config(write_config, which):
    write_config(
        {
            "version": 2,
            "mystery": {"keep": True},
            "repositories": {"search_roots": ["~/Old"], "clone_root": "~/Clone"},
            "profiles": {
                "work": {
                    "default_agent": "codex",
                    "agents": {"codex": {"env": {"CODEX_HOME": "~/.codex-work"}, "args": ["--x"]}},
                }
            },
        }
    )
    run_setup(answers=SetupAnswers.model_validate(GOOD), assume_yes=True, which=which)
    raw = read_raw()
    assert raw["mystery"] == {"keep": True}
    assert raw["repositories"]["search_roots"] == ["~/Old", "~/Projects"]
    assert raw["repositories"]["clone_root"] == "~/Clone"
    assert raw["profiles"]["work"]["agents"]["codex"]["args"] == ["--x"]
    assert set(raw["profiles"]) == {"work", "personal"}


def test_old_version_is_refused_when_setup_would_write(write_config, which):
    path = write_config({"version": 1})
    before = path.read_text()
    run_setup(answers=SetupAnswers(), assume_yes=True, which=which)  # nothing to change: no write, no error
    assert path.read_text() == before
    with pytest.raises(SetupError, match="config migrate") as exc:
        run_setup(answers=SetupAnswers(prompt_execution="execute"), assume_yes=True, which=which)
    assert exc.value.code == "config_outdated"
    assert path.read_text() == before


def test_dry_run_writes_nothing(launcher_home, which):
    result = run_setup(answers=SetupAnswers.model_validate(GOOD), dry_run=True, which=which)
    assert result.changed and not result.applied and "+" in result.diff
    assert not launcher_home.exists()


def test_without_yes_and_without_prompter_refuses(launcher_home, which):
    with pytest.raises(SetupError) as exc:
        run_setup(answers=SetupAnswers.model_validate(GOOD), which=which)
    assert exc.value.code == "needs_confirmation"
    assert not launcher_home.exists()


@pytest.mark.parametrize(
    ("data", "code"),
    [
        # two profiles, same claude config dir
        (
            {"profiles": [
                {"name": "a", "agents": {"claude": {"env": {"CLAUDE_CONFIG_DIR": "~/.c"}}}},
                {"name": "b", "agents": {"claude": {"env": {"CLAUDE_CONFIG_DIR": "~/.c/"}}}},
            ]},
            "ambiguous_identity",
        ),
        # two profiles, one without any identity
        (
            {"profiles": [
                {"name": "a", "agents": {"claude": {"env": {"CLAUDE_CONFIG_DIR": "~/.c"}}}},
                {"name": "b", "agents": {"claude": {}}},
            ]},
            "ambiguous_identity",
        ),
        ({"profiles": [{"name": "a", "agents": {"copilot": {}}}]}, "executable_not_found"),
        ({"profiles": [{"name": "a", "agents": {"claude": {"executable": "/nope/claude"}}}]}, "executable_not_found"),
        ({"profiles": [{"name": "a", "agents": {"claude": {}, "codex": {}}}]}, "ambiguous_default"),
        ({"profiles": [{"name": "a", "agents": {"gemini": {}}}]}, "invalid_answers"),
        ({"profiles": [{"name": "a"}]}, "invalid_answers"),
        ({"profiles": [{"name": "a", "agents": {"claude": {}}, "default_agent": "codex"}]}, "invalid_answers"),
        ({"profiles": [{"name": "a", "agents": {"claude": {}}}, {"name": "a", "agents": {"claude": {}}}]}, "invalid_answers"),
    ],
)
def test_ambiguous_or_missing_values_fail_with_structured_error(launcher_home, which, tmp_path, data, code):
    answers = load_answers(answers_file(tmp_path, data))
    with pytest.raises(SetupError) as exc:
        run_setup(answers=answers, assume_yes=True, which=which)
    assert exc.value.code == code
    assert exc.value.to_dict()["error"]["code"] == code
    assert not launcher_home.exists()


@pytest.mark.parametrize(
    "data",
    [{"typo": 1}, {"search_roots": ["relative/path"]}, {"prompt_execution": "maybe"}, {"profiles": [{"name": "bad name"}]}],
)
def test_bad_answers_file_is_rejected(tmp_path, data):
    with pytest.raises(SetupError) as exc:
        load_answers(answers_file(tmp_path, data))
    assert exc.value.code == "invalid_answers"


def test_missing_and_malformed_answers_file(tmp_path):
    with pytest.raises(SetupError):
        load_answers(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    with pytest.raises(SetupError):
        load_answers(bad)


def test_unusable_existing_config_is_not_touched(write_config, which, launcher_home):
    path = write_config("{not json")
    with pytest.raises(SetupError) as exc:
        run_setup(answers=SetupAnswers.model_validate(GOOD), assume_yes=True, which=which)
    assert exc.value.code == "config_invalid"
    assert path.read_text() == "{not json"


def test_existing_identity_is_checked_against_new_profile(write_config, which, tmp_path):
    write_config({"version": 2, "profiles": {"work": {"agents": {"claude": {"env": {"CLAUDE_CONFIG_DIR": "~/.claude-personal"}}}}}})
    with pytest.raises(SetupError) as exc:
        run_setup(answers=SetupAnswers.model_validate(GOOD), assume_yes=True, which=which)
    assert exc.value.code == "ambiguous_identity"


# --- interactive ----------------------------------------------------------------------------


def first_run_script(*, apply=True):
    return [
        ("text", "Profile name", "personal"),
        ("checkbox", "Which agents", ["claude"]),
        ("confirm", "Use", True),
        ("select", "CLAUDE_CONFIG_DIR", "~/.claude-personal"),
        ("text", "Extra environment", ""),
        ("confirm", "Add a profile?", False),
        ("select", "Terminal adapter", DEFAULT),
        ("text", "search roots", "~/Projects, ~/Developer"),
        ("text", "Worktree root", DEFAULT),
        ("select", "Workflow selection", DEFAULT),
        ("select", "Prompt execution", "execute"),
        ("select", "Agent selection", DEFAULT),
        ("confirm", "Apply", apply),
    ]


def test_interactive_first_run(detection, which):
    p = ScriptedPrompter(*first_run_script())
    result = run_setup(prompter=p, detection=detection, which=which)
    p.done()
    assert result.applied
    config = load_config()
    assert config.profiles["personal"].agents["claude"].env == {"CLAUDE_CONFIG_DIR": "~/.claude-personal"}
    assert config.repositories.search_roots == ["~/Projects", "~/Developer"]
    assert config.prompt_execution == "execute"
    assert config.agent_selection == "always_ask"
    assert config.workflow_routing.selection_mode == "automatic"
    shown = "\n".join(p.said)
    assert "TestOS 1" in shown and ".claude-personal, .claude-work" in shown and "claude-work" in shown
    assert "+++ config.json (proposed)" in shown  # diff shown before apply


def test_interactive_declining_the_diff_writes_nothing(detection, which, launcher_home):
    p = ScriptedPrompter(*first_run_script(apply=False))
    result = run_setup(prompter=p, detection=detection, which=which)
    assert not result.applied and not launcher_home.exists()


def test_cancel_writes_nothing(detection, which, launcher_home):
    p = ScriptedPrompter(("text", "Profile name", "personal"), ("checkbox", "Which agents", CANCEL))
    with pytest.raises(SetupCancelled):
        run_setup(prompter=p, detection=detection, which=which)
    assert not launcher_home.exists()


def test_interactive_does_not_preselect_an_identity(detection, which):
    p = ScriptedPrompter(*first_run_script())
    p.steps[3].answer = DEFAULT  # accept the prompt's own default for CLAUDE_CONFIG_DIR
    run_setup(prompter=p, detection=detection, which=which)
    assert load_config().profiles["personal"].agents["claude"].env == {}


def test_interactive_rerun_is_idempotent_and_keeps_profiles(detection, which, tmp_path):
    run_setup(prompter=ScriptedPrompter(*first_run_script()), detection=detection, which=which)
    path = tmp_path / "launcher-home" / "config.json"
    before = path.read_bytes()
    p = ScriptedPrompter(
        ("confirm", "Add a profile?", False),
        ("select", "Terminal adapter", DEFAULT),
        ("text", "search roots", ""),
        ("text", "Worktree root", DEFAULT),
        ("select", "Workflow selection", DEFAULT),
        ("select", "Prompt execution", DEFAULT),
        ("select", "Agent selection", DEFAULT),
    )
    result = run_setup(prompter=p, detection=detection, which=which)
    p.done()
    assert not result.changed
    assert path.read_bytes() == before
    assert any("Nothing to change" in s for s in p.said)


def test_interactive_second_profile_with_shared_identity_needs_confirmation(detection, which):
    run_setup(prompter=ScriptedPrompter(*first_run_script()), detection=detection, which=which)
    p = ScriptedPrompter(
        ("confirm", "Add a profile?", True),
        ("text", "Profile name", "work"),
        ("checkbox", "Which agents", ["claude"]),
        ("confirm", "Use", True),
        ("select", "CLAUDE_CONFIG_DIR", "~/.claude-personal"),
        ("text", "Extra environment", ""),
        ("confirm", "Keep this profile anyway", False),
        ("confirm", "Add a profile?", False),
        ("select", "Terminal adapter", DEFAULT),
        ("text", "search roots", ""),
        ("text", "Worktree root", DEFAULT),
        ("select", "Workflow selection", DEFAULT),
        ("select", "Prompt execution", DEFAULT),
        ("select", "Agent selection", DEFAULT),
    )
    run_setup(prompter=p, detection=detection, which=which)
    p.done()
    assert set(load_config().profiles) == {"personal"}
    assert any("Ambiguous identity" in s for s in p.said)


def test_interactive_wrapper_and_extra_env(detection, which, bin_dir):
    p = ScriptedPrompter(
        ("text", "Profile name", "work"),
        ("checkbox", "Which agents", ["claude"]),
        ("confirm", "Use", False),
        ("text", "Executable or wrapper", str(bin_dir / "claude-work")),
        ("select", "CLAUDE_CONFIG_DIR", "~/.claude-work"),
        ("text", "Extra environment", "FOO=bar"),
        ("text", "Extra environment", ""),
        ("confirm", "Add a profile?", False),
        ("select", "Terminal adapter", DEFAULT),
        ("text", "search roots", ""),
        ("text", "Worktree root", DEFAULT),
        ("select", "Workflow selection", DEFAULT),
        ("select", "Prompt execution", DEFAULT),
        ("select", "Agent selection", DEFAULT),
        ("confirm", "Apply", True),
    )
    run_setup(prompter=p, detection=detection, which=which)
    inst = load_config().profiles["work"].agents["claude"]
    assert inst.executable == str(bin_dir / "claude-work") and inst.env["FOO"] == "bar"


# --- never touches agent configuration -------------------------------------------------------


def test_agent_config_dirs_are_untouched(detection, which, fake_home):
    secret = fake_home / ".claude-personal" / "settings.json"
    secret.write_text('{"k": 1}')
    snapshot = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in fake_home.rglob("*") if p.is_file()}
    run_setup(prompter=ScriptedPrompter(*first_run_script()), detection=detection, which=which)
    run_setup(answers=SetupAnswers.model_validate(GOOD), assume_yes=True, which=which)
    after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in fake_home.rglob("*") if p.is_file()}
    assert after == snapshot
    assert set(os.listdir(fake_home)) == {".claude-personal", ".claude-work", ".claude.json"}


# --- review fixes: identity spelling, effective agent types ---------------------------------


@pytest.mark.parametrize(
    "second",
    ["{home}/.claude-x", "$HOME/.claude-x", "${HOME}/.claude-x", "~/./.claude-x", "~//.claude-x/", "~/sub/../.claude-x"],
)
def test_identity_spellings_of_one_directory_are_ambiguous(which, tmp_path, fake_home, second):
    data = {"profiles": [
        {"name": "a", "agents": {"claude": {"env": {"CLAUDE_CONFIG_DIR": "~/.claude-x"}}}},
        {"name": "b", "agents": {"claude": {"env": {"CLAUDE_CONFIG_DIR": second.replace("{home}", str(fake_home))}}}},
    ]}
    with pytest.raises(SetupError) as exc:
        run_setup(answers=load_answers(answers_file(tmp_path, data)), assume_yes=True, which=which)
    assert exc.value.code == "ambiguous_identity"
    assert "CLAUDE_CONFIG_DIR=" in exc.value.message


def test_existing_profile_with_absolute_form_conflicts_with_tilde_form(write_config, which, fake_home):
    write_config({"version": 2, "profiles": {"work": {"agents": {"claude": {"env": {"CLAUDE_CONFIG_DIR": f"{fake_home}/.claude-personal"}}}}}})
    with pytest.raises(SetupError) as exc:
        run_setup(answers=SetupAnswers.model_validate(GOOD), assume_yes=True, which=which)
    assert exc.value.code == "ambiguous_identity"


def test_ambiguity_message_names_the_shared_key(which, tmp_path):
    data = {"profiles": [
        {"name": "a", "agents": {"claude": {"env": {"HOME": "/h/x"}}}},
        {"name": "b", "agents": {"claude": {"env": {"HOME": "/h/x"}}}},
    ]}
    with pytest.raises(SetupError) as exc:
        run_setup(answers=load_answers(answers_file(tmp_path, data)), assume_yes=True, which=which)
    assert exc.value.message.startswith("HOME=/h/x")


def test_custom_agent_type_executable_is_checked(write_config, which, bin_dir):
    make_exe(bin_dir, "aider-chat")
    write_config({"version": 2, "agent_types": {"aider": {"adapter": "x", "executable": "aider-chat"}}})
    data = {"profiles": [{"name": "p", "agents": {"aider": {}}}]}
    run_setup(answers=SetupAnswers.model_validate(data), assume_yes=True, which=which)
    assert "p" in load_config().profiles


def test_overridden_builtin_executable_is_what_is_checked(write_config, which):
    # `claude` is on PATH, but this config runs `claude-missing`.
    write_config({"version": 2, "agent_types": {"claude": {"adapter": "claude-code", "executable": "claude-missing"}}})
    data = {"profiles": [{"name": "p", "agents": {"claude": {}}}]}
    with pytest.raises(SetupError) as exc:
        run_setup(answers=SetupAnswers.model_validate(data), assume_yes=True, which=which)
    assert exc.value.code == "executable_not_found" and "claude-missing" in exc.value.message


def test_executable_tilde_uses_instance_home_override(tmp_path, which):
    from agent_launcher.wizard import executable_problem

    other = tmp_path / "other-home"
    make_exe(other / "bin", "tool")
    assert executable_problem("~/bin/tool", {"HOME": str(other)}, which) is None
    assert executable_problem("~/bin/tool", {}, which) is not None


def test_two_copilot_profiles_must_each_set_copilot_home():
    from agent_launcher.wizard import identity_problems

    profiles = {
        "a": {"agents": {"copilot": {"env": {"COPILOT_HOME": "/x/a"}}}},
        "b": {"agents": {"copilot": {}}},
        "c": {"agents": {"copilot": {"env": {"COPILOT_HOME": "/x/a"}}}},
    }
    problems = dict(identity_problems(profiles, {"a", "b", "c"}))
    assert "COPILOT_HOME" in problems["profiles.b.agents.copilot"]
    assert "profiles.a.agents.copilot" in problems and "profiles.c.agents.copilot" in problems  # same directory
    assert not identity_problems({"a": profiles["a"], "b": {"agents": {"copilot": {"env": {"COPILOT_HOME": "/x/b"}}}}}, {"a", "b"})
