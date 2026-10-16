"""Codex CLI and GitHub Copilot CLI adapters (ticket #16), and how each harness says a conversation was saved."""

import uuid

import pytest

from commodore_launcher.agent_adapters import (
    PROMPT_MARKER,
    AgentAdapter,
    ClaudeCodeAdapter,
    CodexCliAdapter,
    CopilotCliAdapter,
    agent_adapter_for,
)

ADAPTERS = [CodexCliAdapter(), CopilotCliAdapter()]


def test_adapters_are_registered_under_their_type_adapter_names():
    assert isinstance(agent_adapter_for("codex-cli"), CodexCliAdapter)
    assert isinstance(agent_adapter_for("github-copilot-cli"), CopilotCliAdapter)
    assert isinstance(agent_adapter_for("claude-code"), ClaudeCodeAdapter)


def test_codex_has_no_conversation_id_and_cannot_resume():
    codex = CodexCliAdapter()
    assert codex.new_conversation_id() is None
    assert codex.resume_args("x") is None
    assert codex.conversation_args("x") == ()


def test_codex_skill_is_mentioned_with_a_dollar_sign():
    assert CodexCliAdapter().skill_invocation("triage", "https://x/1") == "$triage https://x/1"


def test_codex_skill_lookup_by_name(tmp_path):
    codex_home, project = tmp_path / "ch", tmp_path / "proj"
    (codex_home / "skills" / "a").mkdir(parents=True)
    (codex_home / "skills" / "a" / "SKILL.md").write_text("x")
    (project / ".agents" / "skills" / "b").mkdir(parents=True)
    (project / ".agents" / "skills" / "b" / "SKILL.md").write_text("x")
    env = {"CODEX_HOME": str(codex_home), "HOME": str(tmp_path / "home")}
    codex = CodexCliAdapter()
    assert codex.check_skill("a", env, [project]).status == "available"
    assert codex.check_skill("b", env, [project]).status == "available"
    absent = codex.check_skill("c", env, [project])
    assert absent.status == "missing" and absent.checked  # not found in the places Codex loads skills from (ADR 0021)


@pytest.mark.parametrize("name", ["../x", "a/b", ".hidden", "plugin:skill", ""])
def test_skill_names_that_are_not_plain_directory_names_are_never_looked_up(tmp_path, name):
    for adapter in ADAPTERS:
        assert adapter.check_skill(name, {"HOME": str(tmp_path)}, [tmp_path]).status == "not_verified"


def test_copilot_fixes_and_resumes_conversations_by_id():
    copilot = CopilotCliAdapter()
    cid = copilot.new_conversation_id()
    assert str(uuid.UUID(cid)) == cid
    assert copilot.conversation_args(cid) == ("--session-id", cid)
    # One argument with `=`: `--resume` takes an optional value.
    assert copilot.resume_args(cid) == (f"--resume={cid}",)


def test_copilot_skill_is_named_with_a_slash():
    assert CopilotCliAdapter().skill_invocation("review", "u") == "/review u"


def test_copilot_skill_lookup_by_name(tmp_path):
    project, chome = tmp_path / "proj", tmp_path / "ch"
    for base, name in ((project / ".github" / "skills", "p"), (chome / "skills", "u"), (tmp_path / "home/.agents/skills", "h")):
        (base / name).mkdir(parents=True)
        (base / name / "SKILL.md").write_text("x")
    env = {"COPILOT_HOME": str(chome), "HOME": str(tmp_path / "home")}
    copilot = CopilotCliAdapter()
    for name in ("p", "u", "h"):
        assert copilot.check_skill(name, env, [project]).status == "available"
    assert copilot.check_skill("zzz", env, [project]).status == "missing"


def test_execute_mode_puts_the_prompt_on_each_known_agents_command_line():
    # Each form checked by hand on a live launch (Claude Code 2.1.295, Codex 0.162.0, Copilot 1.0.94): the agent
    # submits the prompt once it is up, after its folder-trust dialog, with a leading `-` and newlines intact.
    assert ClaudeCodeAdapter().launch_prompt_args() == ("--", PROMPT_MARKER)
    assert CodexCliAdapter().launch_prompt_args() == ("--", PROMPT_MARKER)
    assert CopilotCliAdapter().launch_prompt_args() == (f"--interactive={PROMPT_MARKER}",)  # not `-p`: that exits
    assert AgentAdapter().launch_prompt_args() is None



def test_claude_code_has_saved_a_conversation_once_its_transcript_exists(tmp_path):
    claude = ClaudeCodeAdapter()
    config, cid = tmp_path / "claude-config", str(uuid.uuid4())
    env = {"CLAUDE_CONFIG_DIR": str(config), "HOME": str(tmp_path / "home")}
    assert claude.has_saved_conversation(cid, env) is False  # nothing submitted, nothing saved
    project = config / "projects" / "-Users-me-src-widgets"  # the shape observed on Claude Code 2.1.296
    project.mkdir(parents=True)
    (project / "other.jsonl").write_text("{}\n")
    assert claude.has_saved_conversation(cid, env) is False
    (project / f"{cid}.jsonl").write_text("{}\n")
    assert claude.has_saved_conversation(cid, env) is True
    # Without CLAUDE_CONFIG_DIR the default config directory is looked in.
    home = tmp_path / "home" / ".claude" / "projects" / "-x"
    home.mkdir(parents=True)
    (home / f"{cid}.jsonl").write_text("{}\n")
    assert claude.has_saved_conversation(cid, {"HOME": str(tmp_path / "home")}) is True
    assert claude.has_saved_conversation("../escape", env) is None  # never a path


def test_copilot_has_saved_a_conversation_once_its_session_state_exists(tmp_path):
    copilot, cid = CopilotCliAdapter(), str(uuid.uuid4())
    env = {"COPILOT_HOME": str(tmp_path / "ch"), "HOME": str(tmp_path / "home")}
    assert copilot.has_saved_conversation(cid, env) is None  # no session-state at all: a layout it does not know
    state = tmp_path / "ch" / "session-state"
    (state / "someone-else").mkdir(parents=True)
    assert copilot.has_saved_conversation(cid, env) is False
    (state / cid).mkdir()
    (state / cid / "events.jsonl").write_text("{}\n")  # the shape observed on GitHub Copilot CLI 1.0.98
    assert copilot.has_saved_conversation(cid, env) is True


def test_a_harness_that_cannot_tell_says_so():
    assert CodexCliAdapter().has_saved_conversation("x", {}) is None
    assert AgentAdapter().has_saved_conversation("x", {}) is None
