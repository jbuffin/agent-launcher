"""Codex CLI and GitHub Copilot CLI adapters (ticket #16). Screens are invented: the real ones are unobserved."""

import re
import uuid

import pytest

from agent_launcher.agent_adapters import (
    ClaudeCodeAdapter,
    CodexCliAdapter,
    CopilotCliAdapter,
    agent_adapter_for,
)

ADAPTERS = [CodexCliAdapter(), CopilotCliAdapter()]


def matches(patterns: tuple[str, ...], screen: str) -> bool:
    return any(re.search(p, screen) for p in patterns)


def test_adapters_are_registered_under_their_type_adapter_names():
    assert isinstance(agent_adapter_for("codex-cli"), CodexCliAdapter)
    assert isinstance(agent_adapter_for("github-copilot-cli"), CopilotCliAdapter)
    assert isinstance(agent_adapter_for("claude-code"), ClaudeCodeAdapter)


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.name)
def test_prompt_patterns_fail_closed(adapter):
    pi = adapter.prompt_input()
    assert pi is not None and pi.ready  # something must positively match before anything is pasted
    for screen in ("", "booting...\n", "$ ", "Loading models\n> "):
        assert not matches(pi.ready, screen)
    for dialog in ("Do you trust the contents of this directory?\n? for shortcuts", "Press Enter to continue"):
        assert matches(pi.blocked, dialog)  # a dialog wins even if a ready pattern also appears


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
    assert absent.status == "not_verified" and absent.checked  # never "missing": bundled skills exist


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
    check = copilot.resume_check()
    assert not check.confirmed and not check.failed  # nothing observed, nothing guessed


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
    assert copilot.check_skill("zzz", env, [project]).status == "not_verified"
