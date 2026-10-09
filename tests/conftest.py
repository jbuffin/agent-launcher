import json
from pathlib import Path

import pytest

from agent_launcher.paths import HOME_ENV_VAR


@pytest.fixture(autouse=True)
def launcher_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the config root at a temp dir for every test, so none touch the real home."""
    home = tmp_path / "launcher-home"
    monkeypatch.setenv(HOME_ENV_VAR, str(home))
    return home


@pytest.fixture
def write_config(launcher_home: Path):
    """Write a config.json (dict -> JSON, str -> verbatim) and return its path."""

    def _write(content: dict | str) -> Path:
        launcher_home.mkdir(parents=True, exist_ok=True)
        path = launcher_home / "config.json"
        path.write_text(content if isinstance(content, str) else json.dumps(content))
        return path

    return _write
