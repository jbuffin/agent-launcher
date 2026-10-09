"""The documentation matches the program (ticket #24, SPEC §32): every CLI option is documented, every relative link
resolves, and the README covers each section SPEC §32 asks for."""

import re
from pathlib import Path

import pytest
import typer.main

from agent_launcher.cli import app

ROOT = Path(__file__).resolve().parent.parent
MARKDOWN = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]


def _commands(command, path=()):
    if hasattr(command, "commands"):
        for name, sub in command.commands.items():
            yield from _commands(sub, (*path, name))
    else:
        yield path, command


def test_every_cli_option_appears_in_the_docs():
    text = "\n".join(p.read_text() for p in MARKDOWN)
    missing = []
    for path, command in _commands(typer.main.get_command(app)):
        for param in command.params:
            for option in getattr(param, "opts", []):
                if option.startswith("--") and option != "--help" and option not in text:
                    missing.append(f"{' '.join(path)} {option}")
    assert missing == []


def test_every_command_is_in_the_readme_reference():
    readme = (ROOT / "README.md").read_text()
    # A row may group subcommands (`profile list\|add\|edit`), so a command counts when `agent-launcher <group>` is
    # followed by its name in the same table row, or its full path appears.
    rows = [line for line in readme.splitlines() if line.startswith("| `agent-launcher ")]
    missing = []
    for path, _ in _commands(typer.main.get_command(app)):
        full = " ".join(path)
        if full in readme and f"agent-launcher {full}" in readme:
            continue
        if len(path) > 1 and any(
            f"agent-launcher {path[0]} " in row and re.search(rf"(?<![\w-]){re.escape(path[-1])}(?![\w-])", row) for row in rows
        ):
            continue
        missing.append(full)
    assert missing == []


LINK = re.compile(r"\]\(([^)#\s]+)(#[^)]*)?\)")


@pytest.mark.parametrize("document", MARKDOWN, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(document):
    broken = []
    for target, _anchor in LINK.findall(document.read_text()):
        if "://" in target or target.startswith("mailto:"):
            continue
        if not (document.parent / target).resolve().exists():
            broken.append(target)
    assert broken == []


def test_readme_has_the_sections_spec_32_asks_for():
    headings = {h.strip().lower() for h in re.findall(r"^## (.+)$", (ROOT / "README.md").read_text(), re.M)}
    wanted = {
        "install", "quick start", "initial setup", "creating profiles", "configuring agents", "launching github tasks",
        "launching local tasks", "configuring workflows", "managing worktrees", "resuming sessions", "troubleshooting",
    }
    assert wanted <= headings
    assert (ROOT / "README.md").read_text().lstrip().startswith("# Agent Launcher")


def test_spec_32_document_set_exists():
    for name in ("architecture", "configuration", "profiles", "workflows", "agents", "terminal-adapters",
                 "github-integration", "sessions", "security", "development"):
        assert (ROOT / "docs" / f"{name}.md").is_file(), name


def test_docs_explain_that_profiles_are_not_a_sandbox_and_how_to_write_an_adapter():
    security = (ROOT / "docs" / "security.md").read_text().lower()
    assert "sandbox" in security
    assert "## adding an adapter" in (ROOT / "docs" / "terminal-adapters.md").read_text().lower()


def test_every_test_named_in_the_definition_of_done_exists():
    text = (ROOT / "docs" / "definition-of-done.md").read_text()
    missing = []
    for filename, name in re.findall(r"tests/(test_\w+\.py)::(test_\w+)", text):
        if not re.search(rf"^def {name}\b", (ROOT / "tests" / filename).read_text(), re.M):
            missing.append(f"{filename}::{name}")
    for filename in re.findall(r"tests/(test_\w+\.py)", text):
        if not (ROOT / "tests" / filename).is_file():
            missing.append(filename)
    # the live variants are named without a path, in test_scenarios.py
    scenarios = (ROOT / "tests" / "test_scenarios.py").read_text()
    for name in re.findall(r"`(test_live_scenario_\w+)`", text):
        if not re.search(rf"^def {name}\b", scenarios, re.M):
            missing.append(name)
    assert missing == []
