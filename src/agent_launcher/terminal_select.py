"""Choosing the terminal adapter from config (`terminal.adapter`) or an override."""

from agent_launcher.paths import launcher_home
from agent_launcher.terminal_cmux import CmuxAdapter
from agent_launcher.terminal_mock import MockTerminalAdapter
from agent_launcher.terminals import TerminalAdapter, TerminalError

IMPLEMENTED = ("cmux", "mock")


def mock_record_path():
    return launcher_home() / "mock-terminal.json"


def select_adapter(name: str) -> TerminalAdapter:
    """The named adapter, or an error. Never substitutes another one."""
    if name == "cmux":
        return CmuxAdapter()
    if name == "mock":
        return MockTerminalAdapter(mock_record_path())
    raise TerminalError(
        "terminal_adapter_unavailable",
        f"Terminal adapter {name!r} is not available in this release (available: {', '.join(IMPLEMENTED)}). "
        "Use `--terminal mock` to try the flow without a terminal.",
        adapter=name,
        available=list(IMPLEMENTED),
    )
