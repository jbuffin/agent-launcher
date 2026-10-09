"""Choosing which of a profile's agents to launch (SPEC §6).

Only the profile's own agents are ever offered. Modes (config `agent_selection`):

- `always_ask` (default): ask every time, even with one agent.
- `use_default`: the profile's default agent; with none, behave as `ask_if_multiple`.
- `ask_if_multiple`: ask only when there is more than one.

The prompt highlights, in order: the workflow's preferred agent (advisory: only if the profile has it), the last agent
used in this profile, then the profile's default.
Asking without a prompter (non-interactive) is an error that names the choices.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from agent_launcher.errors import LauncherError
from agent_launcher.interaction import Choice, Prompter
from agent_launcher.logs import trace


class PickerError(LauncherError):
    pass


@dataclass(frozen=True)
class AgentPick:
    agent: str
    asked: bool


def pick_agent(
    profile: str,
    agents: Sequence[str],
    *,
    mode: str,
    default: str | None,
    last_used: str | None,
    prompter: Prompter | None,
    requested: str | None = None,
    preferred: str | None = None,
) -> AgentPick:
    available = sorted(agents)
    if not available:
        raise PickerError(
            "no_agents",
            f"Profile {profile!r} has no agents. Add one with `agent-launcher profile edit {profile} --agent claude`.",
            profile=profile,
        )
    if requested is not None:
        if requested not in available:
            raise PickerError(
                "agent_not_in_profile",
                f"Profile {profile!r} has no agent {requested!r}; no other profile's agent is used instead.",
                profile=profile,
                requested_agent=requested,
                available_agents=available,
            )
        return AgentPick(requested, False)

    if mode == "use_default" and default in available:
        assert default is not None
        trace("agent picked", profile=profile, agent=default, reason="use_default")
        return AgentPick(default, False)
    if mode in ("ask_if_multiple", "use_default") and len(available) == 1:
        trace("agent picked", profile=profile, agent=available[0], reason="only agent")
        return AgentPick(available[0], False)

    highlight = next((a for a in (preferred, last_used, default) if a in available), None)
    if prompter is None:
        raise PickerError(
            "agent_selection_needed",
            f"Profile {profile!r} needs an agent choice ({'mode ' + mode}) but cannot ask here; "
            f"pass --agent ({', '.join(available)}).",
            profile=profile,
            available_agents=available,
            suggested_agent=highlight,
        )
    labels = {
        a: f"{a} (workflow preference)" if a == preferred
        else f"{a} (last used)" if a == last_used
        else f"{a} (default)" if a == default
        else a
        for a in available
    }
    chosen = prompter.select(
        f"Which agent should run this task under profile {profile}?",
        [Choice(a, labels[a]) for a in available],
        default=highlight,
    )
    trace("agent picked", profile=profile, agent=chosen, reason="asked")
    return AgentPick(chosen, True)
