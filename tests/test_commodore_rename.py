"""The rename surface of issue #13: package, distribution, console script, env vars, bundled skill, user-facing text.

Until the rename lands every test here fails because `commodore_launcher` cannot be imported (or the metadata still
says `agent-launcher`), which is the right reason for this red phase.
"""

import importlib
import importlib.metadata
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
OLD_PREFIX = "AGENT_" + "LAUNCHER_"  # spelled in two parts so this file does not match its own scan


def test_the_package_imports_as_commodore_launcher():
    package = importlib.import_module("commodore_launcher")
    assert Path(package.__file__).parent == ROOT / "src" / "commodore_launcher"
    assert not (ROOT / "src" / "agent_launcher" / "__init__.py").exists()


def test_distribution_and_console_script_in_pyproject():
    assert PYPROJECT["project"]["name"] == "commodore-launcher"
    assert PYPROJECT["project"]["scripts"] == {"commodore": "commodore_launcher.cli:app"}
    assert PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/commodore_launcher"]


def test_installed_metadata_has_one_console_script_named_commodore():
    scripts = {e.name: e.value for e in importlib.metadata.distribution("commodore-launcher").entry_points if e.group == "console_scripts"}
    assert scripts == {"commodore": "commodore_launcher.cli:app"}


def test_the_version_is_read_from_the_commodore_launcher_distribution():
    import commodore_launcher

    assert commodore_launcher.__version__ == importlib.metadata.version("commodore-launcher")
    assert commodore_launcher.__version__ != "0.0.0+unknown"


def test_the_home_directory_and_variable_are_renamed(monkeypatch):
    from commodore_launcher import paths

    assert paths.HOME_ENV_VAR == "COMMODORE_LAUNCHER_HOME"
    monkeypatch.delenv("COMMODORE_LAUNCHER_HOME")
    assert paths.launcher_home() == Path.home() / ".commodore-launcher"
    monkeypatch.setenv("COMMODORE_LAUNCHER_HOME", "")
    assert paths.launcher_home() == Path.home() / ".commodore-launcher"
    monkeypatch.setenv("COMMODORE_LAUNCHER_HOME", "/home/u/elsewhere")
    assert paths.launcher_home() == Path("/home/u/elsewhere")
    assert paths.config_path() == Path("/home/u/elsewhere/config.json")


def test_resolving_the_home_never_copies_or_creates_anything(monkeypatch, fake_default_home):
    from commodore_launcher import paths

    (fake_default_home / ".agent-launcher").mkdir()
    (fake_default_home / ".agent-launcher" / "config.json").write_text("{}")
    monkeypatch.delenv("COMMODORE_LAUNCHER_HOME")
    paths.launcher_home()
    assert not (fake_default_home / ".commodore-launcher").exists()


def test_the_mock_terminal_delay_variable_is_renamed():
    from commodore_launcher import terminal_mock

    assert terminal_mock.CREATE_DELAY_ENV == "COMMODORE_LAUNCHER_MOCK_CREATE_DELAY"


def test_no_code_or_test_still_uses_an_old_environment_variable():
    offenders = []
    for base in (ROOT / "src", ROOT / "tests"):
        for path in base.rglob("*.py"):
            if path != Path(__file__).resolve() and OLD_PREFIX in path.read_text():
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


@pytest.mark.parametrize("name", ["LIVE", "PACKAGING", "SANDBOX_REPO", "LIVE_DIR", "LIVE_EXECUTE"])
def test_the_opt_in_test_switches_use_the_new_prefix(name):
    text = "\n".join(
        (ROOT / "tests" / f).read_text() for f in ("test_live_github.py", "test_live_cmux.py", "test_scenarios.py", "test_packaging.py")
    )
    assert f"COMMODORE_LAUNCHER_{name}" in text


def test_the_bundled_skill_is_commodore_launcher():
    from commodore_launcher import skill_bundle

    assert skill_bundle.SKILL_NAME == "commodore-launcher"
    directory = skill_bundle.skill_directory()
    assert directory.name == "commodore-launcher"
    assert directory.parent == ROOT / "src" / "commodore_launcher" / "skills"
    frontmatter = (directory / "SKILL.md").read_text().split("---")[1]
    assert re.search(r"^name: commodore-launcher$", frontmatter, re.M)
    assert not (ROOT / "src" / "commodore_launcher" / "skills" / "agent-launcher").exists()


def test_the_bundled_skill_names_no_old_command_variable_or_product():
    from commodore_launcher.skill_bundle import skill_directory

    for path in skill_directory().rglob("*.md"):
        text = path.read_text().replace(".agent-launcher", "")  # the old home may be named as the copy's source
        assert OLD_PREFIX not in text, path.name
        assert "agent-launcher" not in text and "Agent Launcher" not in text, path.name


def test_version_prints_the_new_command_name():
    from typer.testing import CliRunner

    from commodore_launcher import __version__
    from commodore_launcher.cli import app

    result = CliRunner().invoke(app, ["version"])
    assert result.stdout.strip() == f"commodore {__version__}"


def _texts(command, path=()):
    yield " ".join(path), command.help or ""
    for param in command.params:
        yield " ".join(path) + " " + param.name, getattr(param, "help", None) or ""
    for name, sub in getattr(command, "commands", {}).items():
        yield from _texts(sub, (*path, name))


def test_cli_help_text_says_commodore_not_agent_launcher():
    import typer

    from commodore_launcher.cli import app

    root = typer.main.get_command(app)
    offenders = []
    for where, text in _texts(root):
        cleaned = text.replace(".agent-launcher", "")
        if "agent-launcher" in cleaned or "Agent Launcher" in cleaned or OLD_PREFIX in cleaned:
            offenders.append(where)
    assert offenders == []
    debug = next(p for p in root.params if p.name == "debug")
    assert "commodore --debug" in debug.help


def test_readme_title_is_commodore_launcher():
    """The first H1, not the first byte: the README opens with the hero image."""
    assert re.search(r"^# (.+)$", (ROOT / "README.md").read_text(), re.M).group(1) == "Commodore Launcher"
