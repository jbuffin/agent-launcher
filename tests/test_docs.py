"""The documentation matches the program (#20): every CLI option is documented outside the ADRs, every relative link
resolves, the README's command reference lists exactly the commands the program has, and the README and the doc set
have the shape the issue asks for."""

import re
from pathlib import Path

import pytest
import typer.main

from commodore_launcher.cli import app

ROOT = Path(__file__).resolve().parent.parent
MARKDOWN = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
CURRENT_DOCS = [ROOT / "README.md", *(p for p in MARKDOWN[1:] if p.relative_to(ROOT / "docs").parts[0] != "adr")]
"""README and `docs/` without the ADRs, which stay as history and may mention what no longer exists."""


def _commands(command, path=()):
    if hasattr(command, "commands"):
        for name, sub in command.commands.items():
            yield from _commands(sub, (*path, name))
    else:
        yield path, command


def test_every_cli_option_appears_in_the_docs():
    text = "\n".join(p.read_text() for p in CURRENT_DOCS)
    missing = []
    for path, command in _commands(typer.main.get_command(app)):
        for param in command.params:
            for option in getattr(param, "opts", []):
                if option.startswith("--") and option != "--help" and option not in text:
                    missing.append(f"{' '.join(path)} {option}")
    assert missing == []


def _leaf_paths():
    return {" ".join(path) for path, _ in _commands(typer.main.get_command(app))}


def _readme_reference_paths() -> list[str]:
    """The commands in the README's "Command reference" table. The first cell of a row holds one or more code spans
    (a later span may drop the `commodore`); the words run up to the first argument or option (`<id>`, `[--json]`, `--debug`),
    and a word written `a\\|b\\|c` stands for each alternative."""
    readme = (ROOT / "README.md").read_text()
    section = re.search(r"^## Command reference\n(.*?)(?=^## |\Z)", readme, re.M | re.S)
    assert section, "the README has no `## Command reference` section"
    paths = []
    for row in section.group(1).splitlines():
        if not row.startswith("| `commodore "):
            continue
        first_cell = row[2:].split(" | ")[0]
        for span in re.findall(r"`([^`]+)`", first_cell):
            if span.startswith("--"):
                continue
            words = []
            for word in span.removeprefix("commodore ").split():
                if not re.fullmatch(r"[a-z][\w-]*(?:\\\|[a-z][\w-]*)*", word):
                    break
                words.append(word)
            if not words:
                continue
            for alternative in words[-1].split("\\|"):
                paths.append(" ".join([*words[:-1], alternative]))
    return paths


def test_the_readme_command_reference_lists_exactly_the_commands_the_program_has():
    listed = _readme_reference_paths()
    expected = _leaf_paths()
    assert sorted(set(expected) - set(listed)) == [], "commands missing from the README reference"
    assert sorted(set(listed) - set(expected)) == [], "commands in the README reference that do not exist"


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


README_SECTIONS = {"install", "quick start", "command reference", "configuration"}
DOC_SET = ("architecture", "configuration", "profiles", "harnesses", "terminal-adapters", "github-integration",
           "sessions", "security", "development")
REMOVED_DOCS = ("agents", "workflows", "worktrees", "cleanup")


def test_the_readme_has_the_title_and_the_sections_the_issue_asks_for():
    readme = (ROOT / "README.md").read_text()
    headings = {h.strip().lower() for h in re.findall(r"^## (.+)$", readme, re.M)}
    assert README_SECTIONS <= headings, sorted(README_SECTIONS - headings)
    assert re.search(r"^# (.+)$", readme, re.M).group(1) == "Commodore Launcher"


@pytest.mark.parametrize("name", DOC_SET)
def test_the_doc_set_exists(name):
    assert (ROOT / "docs" / f"{name}.md").is_file()


@pytest.mark.parametrize("name", REMOVED_DOCS)
def test_the_docs_of_removed_features_and_the_old_agents_page_are_gone(name):
    assert not (ROOT / "docs" / f"{name}.md").exists()


def test_the_readme_links_the_harnesses_page():
    assert "docs/harnesses.md" in (ROOT / "README.md").read_text()


def test_spec_md_is_gone():
    assert not (ROOT / "SPEC.md").exists()


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
