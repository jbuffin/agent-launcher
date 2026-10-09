"""Agent Launcher: launch AI coding agents against issues, PRs and local tasks."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agent-launcher")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"
