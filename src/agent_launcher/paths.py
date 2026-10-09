"""Where Agent Launcher keeps its files.

Everything lives under one root, `~/.agent-launcher/` by default. Setting
`AGENT_LAUNCHER_HOME` moves the whole root; tests always do this.
"""

import os
from pathlib import Path

HOME_ENV_VAR = "AGENT_LAUNCHER_HOME"


def launcher_home() -> Path:
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agent-launcher"


def config_path() -> Path:
    return launcher_home() / "config.json"
