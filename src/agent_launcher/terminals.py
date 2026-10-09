"""The terminal adapter interface (SPEC §19). Knows nothing about any one terminal.

Adapters create, focus and close terminal sessions and say which of those they support
through `capabilities()`. Calling something an adapter did not declare raises
`UnsupportedCapability`; the launcher never falls back to another adapter.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_launcher.errors import LauncherError

CREATE_SESSION = "create_session"
FOCUS_SESSION = "focus_session"
DISCOVER_SESSIONS = "discover_sessions"
PREPARE_PROMPT = "prepare_prompt"
SUBMIT_PROMPT = "submit_prompt"
RESTORE_SESSION = "restore_session"
CLOSE_SESSION = "close_session"

ALL_CAPABILITIES = frozenset(
    {CREATE_SESSION, FOCUS_SESSION, DISCOVER_SESSIONS, PREPARE_PROMPT, SUBMIT_PROMPT, RESTORE_SESSION, CLOSE_SESSION}
)


class UnsupportedCapability(LauncherError):
    def __init__(self, adapter: str, capability: str) -> None:
        super().__init__(
            "unsupported_capability",
            f"Terminal adapter {adapter!r} does not support {capability!r}.",
            adapter=adapter,
            capability=capability,
        )


class TerminalError(LauncherError):
    pass


@dataclass(frozen=True)
class TerminalSessionRef:
    """Terminal session identity: where the agent is displayed. Opaque to the core."""

    adapter: str
    workspace_id: str
    surface_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"adapter": self.adapter, "workspace_id": self.workspace_id, "surface_id": self.surface_id}


@dataclass(frozen=True)
class CreateSessionRequest:
    title: str
    working_directory: str
    command: Sequence[str]
    """argv for the agent. Run directly, never through a shell."""
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    """Full environment for the agent. May hold secrets: adapters must not record the values."""
    prompt: str | None = None
    submit_prompt: bool = False
    """False prepares the prompt for the user to review; True submits it."""


@dataclass(frozen=True)
class CreateSessionResult:
    session: TerminalSessionRef
    prompt_prepared: bool = False
    prompt_submitted: bool = False


class TerminalAdapter(ABC):
    name: str

    @abstractmethod
    def capabilities(self) -> set[str]: ...

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def create_session(self, request: CreateSessionRequest) -> CreateSessionResult: ...

    def require(self, capability: str) -> None:
        if capability not in self.capabilities():
            raise UnsupportedCapability(self.name, capability)

    def focus_session(self, session: TerminalSessionRef) -> None:
        raise UnsupportedCapability(self.name, FOCUS_SESSION)

    def discover_sessions(self) -> list[TerminalSessionRef]:
        raise UnsupportedCapability(self.name, DISCOVER_SESSIONS)

    def close_session(self, session: TerminalSessionRef) -> None:
        raise UnsupportedCapability(self.name, CLOSE_SESSION)
