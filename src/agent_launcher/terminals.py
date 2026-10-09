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


class StaleTerminalSession(TerminalError):
    """The recorded workspace or surface no longer exists."""

    def __init__(self, adapter: str, session: "TerminalSessionRef") -> None:
        super().__init__(
            "terminal_session_stale",
            f"The recorded {adapter} session (workspace {session.workspace_id}) no longer exists.",
            adapter=adapter,
            workspace_id=session.workspace_id,
        )


@dataclass(frozen=True)
class TerminalSessionRef:
    """Terminal session identity: where the agent is displayed. Opaque to the core."""

    adapter: str
    workspace_id: str
    surface_id: str | None = None
    created_by_launcher: bool = False
    """Set on refs returned by `create_session` and stored with the session (older rows: false). Adapters may
    force-close only what this says the launcher created, and only after checking it is still the same session."""

    def to_dict(self) -> dict[str, Any]:
        return {"adapter": self.adapter, "workspace_id": self.workspace_id, "surface_id": self.surface_id}


@dataclass(frozen=True)
class ExternalSession:
    """A terminal session the launcher did not create, with what the adapter could read about it without
    touching its screen. Evidence for a match, never proof: the core decides (`adoption`)."""

    session: TerminalSessionRef
    title: str = ""
    cwd: str | None = None
    """The working directory the terminal reports, when it reports one."""


@dataclass(frozen=True)
class PromptInput:
    """How an agent's TUI takes a prompt without submitting it (SPEC §18). Set by the agent adapter.

    All fields are regular expressions matched against the visible terminal text. A terminal adapter
    enters the prompt only once `ready` matches and never while `blocked` does; `busy` after entry
    means the agent started working, so the prompt was submitted.
    """

    ready: tuple[str, ...]
    """Any match: the agent is waiting at its input box."""
    blocked: tuple[str, ...] = ()
    """Any match: a dialog (trust, login, permission) owns the keyboard; nothing is entered."""
    busy: tuple[str, ...] = ()
    collapsed: tuple[str, ...] = ()
    """What the TUI shows in place of a long pasted text."""
    ready_timeout: float = 30.0


@dataclass(frozen=True)
class ResumeCheck:
    """How to tell from the screen that a resumed agent restored its conversation. Set by the agent adapter."""

    confirmed: tuple[str, ...] = ()
    """Any match: the agent shows the resumed conversation."""
    failed: tuple[str, ...] = ()
    """Any match: the agent says it could not resume."""
    timeout: float = 15.0


@dataclass(frozen=True)
class CreateSessionRequest:
    title: str
    working_directory: str
    command: Sequence[str]
    """argv for the agent. Run directly, never through a shell."""
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    """Full environment for the agent. May hold secrets: adapters must not record the values."""
    pinned_env: Sequence[str] = ()
    """Names in `env` the profile sets itself. Adapters must give the agent exactly these values,
    whatever the terminal's own environment or shell start-up files hold."""
    prompt: str | None = None
    submit_prompt: bool = False
    """False prepares the prompt for the user to review; True submits it."""
    prompt_input: PromptInput | None = None
    """From the agent adapter. Without it a terminal adapter cannot tell when the agent is ready,
    so it must not enter the prompt itself."""
    resume_check: ResumeCheck | None = None
    """Set when `command` resumes a conversation: the adapter then watches the screen for the outcome."""


@dataclass(frozen=True)
class CreateSessionResult:
    session: TerminalSessionRef
    prompt_prepared: bool = False
    prompt_submitted: bool = False
    notice: str | None = None
    """Something the user must know, e.g. that the prompt was not prepared and where it is instead."""
    resume_state: str | None = None
    """For a resuming request: `confirmed`, `failed` or `unconfirmed` (resume attempted, nothing seen either way)."""


class TerminalAdapter(ABC):
    name: str

    @abstractmethod
    def capabilities(self) -> set[str]: ...

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def create_session(self, request: CreateSessionRequest) -> CreateSessionResult: ...

    def unavailable_reason(self) -> str | None:
        """Why `available()` is false, in words the user can act on."""
        return None

    def require(self, capability: str) -> None:
        if capability not in self.capabilities():
            raise UnsupportedCapability(self.name, capability)

    def focus_session(self, session: TerminalSessionRef) -> None:
        raise UnsupportedCapability(self.name, FOCUS_SESSION)

    def read_screen(self, session: TerminalSessionRef) -> str | None:
        """The visible text of the session, or None when the session no longer exists."""
        raise UnsupportedCapability(self.name, "read_screen")

    def prepare_prompt(self, session: TerminalSessionRef, prompt: str, spec: PromptInput | None) -> CreateSessionResult:
        """Enter `prompt` into an existing session's agent without submitting it."""
        raise UnsupportedCapability(self.name, PREPARE_PROMPT)

    def discover_sessions(self) -> list[ExternalSession]:
        """Sessions that exist now, with their title and working directory. Read-only: it never reads a
        terminal's screen, which may hold anything the user is doing. Only sessions that can be verified
        later (those with a surface) are returned, and none is marked `created_by_launcher`."""
        raise UnsupportedCapability(self.name, DISCOVER_SESSIONS)

    def close_session(self, session: TerminalSessionRef) -> None:
        raise UnsupportedCapability(self.name, CLOSE_SESSION)
