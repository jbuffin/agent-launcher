"""Prepare and execute through the cmux adapter for every agent adapter (ticket #16, AC "never sends Enter
accidentally, verified per tool"). The screens are invented from each adapter's own patterns: the real ones
are unobserved and stay the engineer's live check."""

import re

import pytest
from test_terminal_cmux import FakeCmux, adapter, request

from agent_launcher.agent_adapters import ClaudeCodeAdapter, CodexCliAdapter, CopilotCliAdapter

PROMPT = "Fix the login bug"

# adapter -> (idle input box, dialog that owns the keyboard, working indicator)
SCREENS = {
    "claude-code": ("│ >  │\n  ? for shortcuts\n", "Do you trust the files in this folder?\n", "  esc to interrupt\n"),
    "codex-cli": ("› \n  ? for shortcuts\n", "Do you trust the contents of this directory?\n", "  esc to interrupt\n"),
    "github-copilot-cli": (
        "> Type @ to mention files, / for commands, or ? for shortcuts\n",
        "Confirm folder trust: do you trust the files in this folder?\n",
        "  Esc to cancel\n",
    ),
}
ADAPTERS = [ClaudeCodeAdapter(), CodexCliAdapter(), CopilotCliAdapter()]
agents = pytest.mark.parametrize("agent", ADAPTERS, ids=lambda a: a.name)


def hit(patterns, text):
    return any(re.search(p, text) for p in patterns)


@agents
def test_screens_match_the_adapters_own_patterns(agent):
    idle, dialog, busy = SCREENS[agent.name]
    pi = agent.prompt_input()
    assert hit(pi.ready, idle) and not hit(pi.blocked, idle)
    assert hit(pi.blocked, dialog)
    assert hit(pi.busy, busy)


def prepared(agent, **kw):
    idle = SCREENS[agent.name][0]
    fake = FakeCmux(screens=[idle], after_paste=f"> {PROMPT}\n" + idle)
    return fake, adapter(fake).create_session(request(PROMPT, prompt_input=agent.prompt_input(), **kw))


@agents
def test_prepare_pastes_once_and_never_sends_a_key(agent):
    fake, result = prepared(agent)
    assert result.prompt_prepared and not result.prompt_submitted and result.notice is None
    (paste,) = fake.commands("paste")
    assert "--submit" not in paste and "--force" not in paste
    assert [stdin for a, stdin in fake.calls if a[1:2] == ["paste"]] == [PROMPT]
    assert not fake.commands("send") and not fake.commands("send-key")


@agents
def test_a_dialog_blocks_the_paste_and_falls_back_to_the_clipboard(agent):
    fake = FakeCmux(screens=[SCREENS[agent.name][1]])
    result = adapter(fake).create_session(request(PROMPT, prompt_input=agent.prompt_input()))
    assert not result.prompt_prepared and not fake.commands("paste") and not fake.commands("send-key")
    assert "dialog" in result.notice and "clipboard" in result.notice and fake.clipboard == PROMPT


@agents
def test_execute_sends_exactly_one_enter_after_the_paste_is_verified(agent):
    fake, result = prepared(agent, submit_prompt=True)
    assert result.prompt_prepared and result.prompt_submitted and result.notice is None
    order = [a[1] for a, _ in fake.calls]
    assert order.count("paste") == 1 and order.count("send-key") == 1
    assert order.index("send-key") > order.index("paste") and order[-1] == "send-key"
    assert fake.commands("send-key")[0][-1] == "enter" and not fake.commands("send")
    assert "--submit" not in fake.commands("paste")[0]


@agents
@pytest.mark.parametrize("why", ["dialog", "no box", "refused", "not seen", "working"])
def test_execute_never_sends_enter_when_the_paste_or_the_check_failed(agent, why):
    idle, dialog, busy = SCREENS[agent.name]
    fake = {
        "dialog": FakeCmux(screens=[dialog]),
        "no box": FakeCmux(screens=["booting...\n"]),
        "refused": FakeCmux(screens=[idle], paste_rc=1),
        "not seen": FakeCmux(screens=[idle], after_paste=idle),
        "working": FakeCmux(screens=[idle], after_paste=f"{PROMPT}\n{busy}"),
    }[why]
    result = adapter(fake).create_session(request(PROMPT, prompt_input=agent.prompt_input(), submit_prompt=True))
    assert not result.prompt_submitted and not result.prompt_prepared
    assert not fake.commands("send-key") and not fake.commands("send")
    assert "not submitted" in result.notice and fake.clipboard == PROMPT
