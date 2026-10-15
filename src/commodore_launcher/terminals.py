"""The terminal adapter interface. Knows nothing about any one terminal.

Adapters create, focus and close terminal sessions and say which of those they support
through `capabilities()`. Calling something an adapter did not declare raises
`UnsupportedCapability`; the launcher never falls back to another adapter.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from commodore_launcher.errors import LauncherError

CREATE_SESSION = "create_session"
FOCUS_SESSION = "focus_session"

PROMPT_MARKER = "{prompt}"
"""Where the prompt goes in `CreateSessionRequest.prompt_args`."""
RESTORE_SESSION = "restore_session"
CLOSE_SESSION = "close_session"
RUN_INTERACTIVE = "run_interactive"

ALL_CAPABILITIES = frozenset(
    {CREATE_SESSION, FOCUS_SESSION, RESTORE_SESSION, CLOSE_SESSION, RUN_INTERACTIVE}
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
    """Terminal session identity: where the harness is displayed. Opaque to the core."""

    adapter: str
    workspace_id: str
    surface_id: str | None = None
    created_by_launcher: bool = False
    """Set on refs returned by `create_session` and stored with the session (older rows: false). Adapters may
    force-close only what this says the launcher created, and only after checking it is still the same session."""

    def to_dict(self) -> dict[str, Any]:
        return {"adapter": self.adapter, "workspace_id": self.workspace_id, "surface_id": self.surface_id}


@dataclass(frozen=True)
class ResumeCheck:
    """How to tell from the screen that a resumed harness restored its conversation. Set by the harness adapter."""

    confirmed: tuple[str, ...] = ()
    """Any match: the harness shows the resumed conversation."""
    failed: tuple[str, ...] = ()
    """Any match: the harness says it could not resume."""
    timeout: float = 15.0


@dataclass(frozen=True)
class CreateSessionRequest:
    title: str
    working_directory: str
    command: Sequence[str]
    """argv for the harness. Run directly, never through a shell."""
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    """Full environment for the harness. May hold secrets: adapters must not record the values."""
    pinned_env: Sequence[str] = ()
    """Names in `env` the profile sets itself. Adapters must give the harness exactly these values,
    whatever the terminal's own environment or shell start-up files hold."""
    prompt: str | None = None
    submit_prompt: bool = False
    """True: the harness submits `prompt` itself at launch, through `prompt_args`."""
    resume_check: ResumeCheck | None = None
    """Set when `command` resumes a conversation: the adapter then watches the screen for the outcome."""
    exit_when_done: bool = False
    """End the terminal's shell when `command` ends, so a temporary surface closes itself."""
    prompt_args: Sequence[str] = ()
    """Arguments appended to `command`, one of them holding `PROMPT_MARKER` where the prompt goes (the harness's
    `launch_prompt_args()`). The harness submits the prompt itself once it is up (after any dialog), so nothing is
    pasted and no screen is read. The prompt must not appear in a command line a shell echoes or keeps in its
    history."""

    def __post_init__(self) -> None:
        """A prompt travels only on the command line: it needs `prompt_args`, and exactly one element holds the marker
        (the prompt file is read once)."""
        if self.prompt is None:
            return
        if not self.prompt_args:
            raise TerminalError("prompt_args_invalid", "A prompt needs launch arguments to carry it; none were given.")
        if sum(PROMPT_MARKER in a for a in self.prompt_args) != 1:
            raise TerminalError("prompt_args_invalid", "Exactly one launch argument must hold the prompt.")


@dataclass(frozen=True)
class CreateSessionResult:
    session: TerminalSessionRef
    prompt_submitted: bool = False
    notice: str | None = None
    """Something the user must know."""
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

    def close_session(self, session: TerminalSessionRef) -> None:
        raise UnsupportedCapability(self.name, CLOSE_SESSION)

    def run_interactive(self, title: str, working_directory: str, command: Sequence[str]) -> TerminalSessionRef:
        """Run `command` (argv, never a shell string) in a new temporary terminal surface that has a TTY, for a
        command that must ask the user something (the picker) when the caller has no terminal of its own. Nothing
        is entered into it and no harness is involved."""
        raise UnsupportedCapability(self.name, RUN_INTERACTIVE)
