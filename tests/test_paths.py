from pathlib import Path

from agent_launcher import paths


def test_env_override_moves_root(launcher_home: Path):
    assert paths.launcher_home() == launcher_home
    assert paths.config_path() == launcher_home / "config.json"


def test_default_root_is_dot_agent_launcher(monkeypatch):
    monkeypatch.delenv(paths.HOME_ENV_VAR)
    assert paths.launcher_home() == Path.home() / ".agent-launcher"
