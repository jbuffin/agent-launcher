"""Packaging (ticket #24, SPEC §30): conventional metadata, and a real wheel install that survives an upgrade.

The metadata tests read `pyproject.toml` and always run. The install test builds two wheels (0.1.0 and a bumped
copy, never committed), installs them with pipx into temp directories and checks that the bundled skill is on disk,
nothing mutable lands in site-packages, and the config and state in the temp launcher home survive the upgrade.
It needs `uv`, `pipx` and a package index, so it is opt-in: `AGENT_LAUNCHER_PACKAGING=1 uv run pytest tests/test_packaging.py`.
"""

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def test_metadata_is_conventional_for_a_future_homebrew_formula():
    assert PYPROJECT["name"] == "agent-launcher"
    assert PYPROJECT["requires-python"] == ">=3.11"
    assert "license" not in PYPROJECT and not (ROOT / "LICENSE").exists(), "the license is undecided until release"
    assert PYPROJECT["scripts"] == {"agent-launcher": "agent_launcher.cli:app"}
    assert PYPROJECT["readme"] == "README.md" and (ROOT / "README.md").is_file()
    assert any(c.startswith("Programming Language :: Python :: 3.11") for c in PYPROJECT["classifiers"])
    assert PYPROJECT["urls"]["Repository"].startswith("https://github.com/")
    assert {re.split(r"[<>=]", d)[0] for d in PYPROJECT["dependencies"]} == {"typer", "pydantic", "questionary"}


def test_the_package_never_writes_under_its_own_directory():
    """Mutable state belongs under `launcher_home()`; no module may derive a write location from `__file__`."""
    offenders = []
    for path in (ROOT / "src" / "agent_launcher").rglob("*.py"):
        if path.name != "skill_bundle.py" and "__file__" in path.read_text():
            offenders.append(path.name)
    assert offenders == []


def test_bundled_skill_is_found_next_to_the_package():
    from agent_launcher.skill_bundle import skill_directory

    directory = skill_directory()
    assert (directory / "SKILL.md").is_file()
    assert (directory / "reference").is_dir()


packaging = pytest.mark.skipif(
    not os.environ.get("AGENT_LAUNCHER_PACKAGING"), reason="set AGENT_LAUNCHER_PACKAGING=1 to run"
)


def _run(argv, env, cwd=None) -> str:
    done = subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, f"{argv}\n{done.stdout}\n{done.stderr}"
    return done.stdout


def _source_copy(destination: Path, version: str) -> Path:
    """A throwaway copy of the build inputs with its own version; the checkout is untouched."""
    destination.mkdir(parents=True)
    for name in ("pyproject.toml", "README.md"):
        shutil.copy(ROOT / name, destination / name)
    shutil.copytree(ROOT / "src", destination / "src", ignore=shutil.ignore_patterns("__pycache__"))
    text = (destination / "pyproject.toml").read_text()
    (destination / "pyproject.toml").write_text(re.sub(r'^version = ".*"$', f'version = "{version}"', text, count=1, flags=re.M))
    return destination


def _site_packages_files(venv: Path) -> set[str]:
    site = next((venv / "lib").glob("python3*/site-packages"))
    return {
        str(p.relative_to(site)) for p in site.rglob("*") if p.is_file() and "__pycache__" not in p.parts
    }


@packaging
@pytest.mark.skipif(not shutil.which("uv") or not shutil.which("pipx"), reason="needs uv and pipx")
def test_pipx_install_from_wheel_and_upgrade_keeps_config_and_state(tmp_path, make_repo):
    base = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONPATH")}
    pipx_home, bin_dir, home = tmp_path / "pipx-home", tmp_path / "pipx-bin", tmp_path / "launcher-home"
    env = {
        **base,
        "PIPX_HOME": str(pipx_home),
        "PIPX_BIN_DIR": str(bin_dir),
        "PIPX_MAN_DIR": str(tmp_path / "pipx-man"),
        "AGENT_LAUNCHER_HOME": str(home),
        "HOME": str(tmp_path / "fake-home"),
    }
    (tmp_path / "fake-home").mkdir()
    exe = bin_dir / "agent-launcher"

    wheels = {}
    for version in ("0.1.0", "0.1.1"):
        source = _source_copy(tmp_path / f"src-{version}", version)
        _run(["uv", "build", "-q", "--wheel", "--out-dir", str(tmp_path / f"dist-{version}")], base, cwd=source)
        wheels[version] = next((tmp_path / f"dist-{version}").glob("*.whl"))

    _run(["pipx", "install", str(wheels["0.1.0"])], env)
    venv = pipx_home / "venvs" / "agent-launcher"
    before = _site_packages_files(venv)  # straight after install, before anything runs
    assert _run([str(exe), "version"], env).strip() == "agent-launcher 0.1.0"

    report = json.loads(_run([str(exe), "doctor", "--json"], env))
    assert report["summary"]["failures"] == 0
    assert not home.exists(), "doctor on a fresh home must not create it"

    skill = Path(json.loads(_run([str(exe), "skill", "path", "--json"], env))["path"])
    assert (skill / "SKILL.md").is_file() and (skill / "reference" / "config.md").is_file()
    assert pipx_home in skill.parents, "the skill must come from the installed package, not the checkout"

    # Real state through the installed command: a config, a profile association and a task.
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\n")
    claude.chmod(0o755)
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps(
            {
                "version": 2,
                "terminal": {"adapter": "mock"},
                "agent_selection": "use_default",
                "repositories": {"worktree_root": str(tmp_path / "trees")},
                "profiles": {"personal": {"default_agent": "claude", "agents": {"claude": {"executable": str(claude)}}}},
            }
        )
    )
    repo = make_repo("upgrade-repo")
    _run([str(exe), "profile", "set", str(repo), "personal"], env)
    created = _run([str(exe), "new", "--title", "Survives the upgrade", "--repo", str(repo)], env)
    task_id = re.search(r"t-[0-9a-z]{8}", created).group(0)
    _run([str(exe), "open", task_id], env)
    config_before = (home / "config.json").read_text()
    shown_before = _run([str(exe), "tasks", "show", task_id, "--json"], env)

    _run([str(exe), "version"], env)
    assert _site_packages_files(venv) == before, "running the program wrote into site-packages"

    _run(["pipx", "install", "--force", str(wheels["0.1.1"])], env)
    assert _run([str(exe), "version"], env).strip() == "agent-launcher 0.1.1"
    assert (home / "config.json").read_text() == config_before
    assert (home / "state.db").is_file()
    assert _run([str(exe), "tasks", "show", task_id, "--json"], env) == shown_before
    assert Path(json.loads(_run([str(exe), "skill", "path", "--json"], env))["path"], "SKILL.md").is_file()
    assert _run([str(exe), "open", task_id], env)  # focuses the existing session
    assert json.loads(_run([str(exe), "doctor", "--json"], env))["summary"]["failures"] == 0
    own = ("agent_launcher/", "agent_launcher-")
    assert {f for f in _site_packages_files(venv) if not f.startswith(own)} == {f for f in before if not f.startswith(own)}
