"""Agent adapters: what an agent's TUI needs, apart from where it is displayed (SPEC §19).

An agent adapter knows the agent's input box: how to tell it is ready and how to tell it has
started working. Terminal adapters (workspaces, surfaces) know nothing of that and take it as data.
The argv itself comes from the profile's instance (`agents.resolve_agent`), not from here.
"""

from agent_launcher.terminals import PromptInput


class AgentAdapter:
    name = "generic"

    def prompt_input(self) -> PromptInput | None:
        """None: there is no safe way to prepare a prompt, so the launcher must not try."""
        return None


class ClaudeCodeAdapter(AgentAdapter):
    """Claude Code 2.1: `claude [prompt]` always submits its argument, so no flag pre-fills the box.
    The prompt is pasted (bracketed paste) into the idle input box instead, never typed."""

    name = "claude-code"

    def prompt_input(self) -> PromptInput:
        return PromptInput(
            # The footer of an idle, empty input box, and the mode lines that replace it.
            ready=(r"\? for shortcuts", r"shift\+tab to cycle", r"bypass permissions on", r"auto-accept edits on"),
            blocked=(r"Do you trust", r"trust this folder", r"Select login method", r"Press Enter to continue"),
            busy=(r"esc to interrupt",),
            collapsed=(r"\[Pasted text",),
        )


_ADAPTERS: dict[str, AgentAdapter] = {a.name: a for a in (ClaudeCodeAdapter(),)}


def agent_adapter_for(name: str) -> AgentAdapter:
    """The adapter for an agent type's `adapter` value; unknown agents get one that prepares nothing."""
    return _ADAPTERS.get(name, AgentAdapter())
