"""Agent adapters: what an agent's TUI needs, apart from where it is displayed (SPEC §19).

An agent adapter knows the agent's input box: how to tell it is ready and how to tell it has
started working. Terminal adapters (workspaces, surfaces) know nothing of that and take it as data.
The argv itself comes from the profile's instance (`agents.resolve_agent`), not from here.
"""

import re
import uuid

from agent_launcher.terminals import PromptInput, ResumeCheck


class AgentAdapter:
    name = "generic"

    def prompt_input(self) -> PromptInput | None:
        """None: there is no safe way to prepare a prompt, so the launcher must not try."""
        return None

    def new_conversation_id(self) -> str | None:
        """An ID to fix at launch, or None when the agent cannot take one (then none is stored)."""
        return None

    def conversation_args(self, conversation_id: str) -> tuple[str, ...]:
        """Arguments that make a fresh launch use `conversation_id`."""
        return ()

    def resume_args(self, conversation_id: str) -> tuple[str, ...] | None:
        """Arguments that resume `conversation_id`, or None when there is no supported way."""
        return None

    def resume_check(self) -> ResumeCheck | None:
        return None

    def has_exited(self, screen: str) -> bool:
        """True only on positive evidence that the agent process ended. Unsure means False: never resume
        over an agent that may still be running."""
        return False


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

    # Verified against Claude Code 2.1.295 `--help`: `--session-id <uuid>` fixes the conversation ID
    # at launch and `--resume <id>` resumes it. Resuming an ID with no saved conversation prints
    # `No conversation found with session ID: <id>` (observed).
    def new_conversation_id(self) -> str:
        return str(uuid.uuid4())

    def conversation_args(self, conversation_id: str) -> tuple[str, ...]:
        return ("--session-id", conversation_id)

    def resume_args(self, conversation_id: str) -> tuple[str, ...]:
        return ("--resume", conversation_id)

    def resume_check(self) -> ResumeCheck:
        # `confirmed` stays empty until the resumed screen has been observed live: guessing a pattern
        # could report a restoration that did not happen. Until then the outcome is "not confirmed".
        return ResumeCheck(confirmed=(), failed=(r"No conversation found",))

    def has_exited(self, screen: str) -> bool:
        # Claude Code's exit text offering to resume (unobserved wording): only that counts as an exit.
        return bool(re.search(r"Resume this (conversation|session) with", screen))


_ADAPTERS: dict[str, AgentAdapter] = {a.name: a for a in (ClaudeCodeAdapter(),)}


def agent_adapter_for(name: str) -> AgentAdapter:
    """The adapter for an agent type's `adapter` value; unknown agents get one that prepares nothing."""
    return _ADAPTERS.get(name, AgentAdapter())
