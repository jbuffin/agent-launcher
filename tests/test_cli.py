import json

from typer.testing import CliRunner

from agent_launcher import __version__
from agent_launcher.cli import app

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"agent-launcher {__version__}"


def test_version_json():
    result = runner.invoke(app, ["version", "--json"])
    assert json.loads(result.stdout) == {"version": __version__}


def test_show_without_file_shows_defaults(launcher_home):
    result = runner.invoke(app, ["config", "show", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["exists"] is False
    assert data["config"]["version"] == 2 and data["config"]["debug"] is False
    assert data["config"]["profiles"] == {}
    assert data["path"] == str(launcher_home / "config.json")
    assert not launcher_home.exists()


def test_show_includes_unknown_fields(write_config):
    write_config({"version": 1, "extra": 1})
    data = json.loads(runner.invoke(app, ["config", "show", "--json"]).stdout)
    assert data["config"]["extra"] == 1


def test_show_bad_json_fails(write_config):
    write_config("{")
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 1


def test_validate_ok(write_config):
    write_config({"version": 1})
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0 and "ok" in result.stdout


def test_validate_error_names_field(write_config):
    write_config({"version": 1, "debug": "x"})
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 1
    assert "debug" in result.output


def test_validate_unknown_warns_and_strict_fails(write_config):
    write_config({"version": 1, "mystery": True})
    warn = runner.invoke(app, ["config", "validate"])
    assert warn.exit_code == 0 and "mystery" in warn.output
    strict = runner.invoke(app, ["config", "validate", "--strict"])
    assert strict.exit_code == 1


def test_validate_json(write_config):
    write_config({"version": 1, "mystery": True, "debug": 3})
    result = runner.invoke(app, ["config", "validate", "--json"])
    data = json.loads(result.stdout)
    assert result.exit_code == 1
    assert data["valid"] is False
    assert data["unknown_fields"] == ["mystery"]
    assert data["errors"][0]["field"] == "debug"


def test_show_json_failure_emits_error_body(write_config):
    path = write_config("{")
    result = runner.invoke(app, ["config", "show", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["path"] == str(path)
    assert "not valid JSON" in data["error"]
