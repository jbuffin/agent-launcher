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


@pytest.fixture(autouse=True)
def no_real_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may probe real external tools (git, gh, cmux, agents) through `doctor`.

    By default `run_doctor` sees an empty PATH and a runner that does nothing. Tests that
    want specific tools inject their own `runner` and `which`.
    """
    from agent_launcher import doctor

    def refuse(argv, timeout):
        raise doctor.CommandError("external tools are disabled in tests")

    monkeypatch.setattr(doctor, "run_command", refuse)
    monkeypatch.setattr(doctor, "_default_which", lambda name: None)
