"""What the README, the docs and the bundled skill may and may not say after the Commodore Launcher rewrite (#20).

The checks scan the current documents (README.md, `docs/` without `docs/adr/` and without `docs/agents/`, which is the
repository's own issue-tracker configuration) and the bundled skill. ADRs stay as history. Each allow-list entry below
says why it exists."""

import ast
import hashlib
import re
from pathlib import Path

import pytest

from commodore_launcher import migrations
from test_harness import agent_uses

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "src" / "commodore_launcher" / "skills" / "commodore-launcher"
SKILL_FILES = sorted(SKILL.rglob("*.md"))
DOCS = [
    ROOT / "README.md",
    *sorted(p for p in (ROOT / "docs").rglob("*.md") if p.relative_to(ROOT / "docs").parts[0] not in {"adr", "agents"}),
]
SCANNED = [*DOCS, *SKILL_FILES]


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


# --- the README hero (#23 on main) --------------------------------------------------------------------------

HERO = """<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/hero-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/hero-light.png">
  <img alt="Commodore Launcher: plans the mission, assembles a task force of coding agents and sees the work through to a merged PR" src="docs/images/hero-light.png" width="1280">
</picture>"""
"""The block above the H1 on `origin/main` at 341f504, pinned as a literal so the stack's rebase onto main is trivial. Its
alt text says "mission" and "agents"; fixing that is a follow-up on main, so the scans below skip this block."""

HERO_SHA256 = {
    "hero-dark.png": "7e54935eed26f381e26106f4f2ab3522d71fab5cabfc06236c5ced26039a9d3f",
    "hero-light.png": "2e680be1c2ec5e7e66c7c6ff3018afd1945f96b7e7962717241040dc395c5405",
}


def test_the_readme_hero_is_the_one_on_main():
    readme = (ROOT / "README.md").read_text()
    assert readme.split("\n# ", 1)[0].strip() == HERO


@pytest.mark.parametrize("name", sorted(HERO_SHA256))
def test_the_hero_images_are_the_ones_on_main(name):
    assert hashlib.sha256((ROOT / "docs" / "images" / name).read_bytes()).hexdigest() == HERO_SHA256[name]


def _text(path: Path) -> str:
    return path.read_text().replace(HERO, "") if path.name == "README.md" and path.parent == ROOT else path.read_text()


# --- 20.1: no removed concept in the current documents ---------------------------------------------------------

UPGRADE_NOTES = "Upgrading from Agent Launcher"
"""The one section (an H2 of `docs/config-migrations.md`) that describes what the rename and the removals changed. It has
to name the old commands, keys and files, so the concept scan skips it."""

REMOVED = {
    "an old command": r"(?:`|^\s*|\| `)(?:agent-launcher|commodore) (?:new|restart|cleanup|workflows|worktrees|prompt|sessions)\b",
    "`restart` as a command": r"`restart`|\brestart <id>",
    "the old tasks subcommands": r"\btasks (?:link|completed)\b",
    "prepare or execute mode": r"prepare mode|execute mode|--prepare|--execute|prepare/execute|prompt_execution",
    "a workflow": r"--workflow|--ask-workflow|workflows\.json|workflow (?:rules?|routing)|workflow_routing|\bworkflows (?:list|init|test)\b",
    "a prompt template": r"prompt templates?|`templates/`|templates/ ",
    "the removed --agent option": r"--agent(?![\w-])|--default-agent|--remove-agent",
    "a task worktree": r"task worktrees?|\bworktrees (?:list|inspect|associate)\b|`worktrees`|`cleanup`",
    "a local task you can create or open": (
        r"(?i)(?:creat\w*|open\w*|launch\w*|start\w*|new|make|made)\s+(?:a\s+|an\s+)?local tasks?\b|^#+\s.*\blocal tasks?\b"
    ),
    "a maintenance task (it is a maintenance session)": r"(?i)maintenance tasks?",
    "SPEC.md or a SPEC section": r"SPEC\.md|SPEC\s*§",
    "mission (Commodore's word, not the launcher's)": r"(?i)\bmission\b",
    "target for a launcher concept": r"(?i)\b(?:clone|launch|task|session)\s+targets?\b|\bnaming the target\b",
    "the old command name": r"(?<![\w/.~-])agent-launcher(?![\w/.-])",
}

ALLOWED_PHRASES = [
    "Commodore picks the mission",  # ADR 0021 and GLOSSARY.md: the mission is Commodore's, not the launcher's
    "target-evaluation",  # the name of one of Commodore's four skills
]
"""Substrings removed before the scan. `~/.agent-launcher`, the `jbuffin/agent-launcher` repository, `refs/agent-launcher/`
and the `clone_target_exists` / `invalid_clone_target` error codes need no entry: the patterns above do not match them."""

ALLOWED_LINES = {
    "docs/configuration.md": [r"^\| `workflows\.json`, `templates/` \|"],  # the home table: leftover files, describes current behaviour
    "docs/diagnostics.md": [r"^\| Leftover files \|"],  # the doctor row that lists them, same reason
}


def _sections(text: str, title: str) -> set[int]:
    """Line numbers (0-based) under the H2 whose title is `title`."""
    lines = text.splitlines()
    inside, out = False, set()
    for number, line in enumerate(lines):
        if line.startswith("## "):
            inside = line[3:].strip() == title
        if inside:
            out.add(number)
    return out


def _skipped(path: Path, text: str) -> set[int]:
    skipped = set()
    if rel(path) == "docs/config-migrations.md":
        skipped |= _sections(text, UPGRADE_NOTES)
    for number, line in enumerate(text.splitlines()):
        if any(re.search(pattern, line) for pattern in ALLOWED_LINES.get(rel(path), [])):
            skipped.add(number)
    return skipped


def _scan(patterns: dict[str, str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in SCANNED:
        text = _text(path)
        skipped = _skipped(path, text)
        for number, line in enumerate(text.splitlines()):
            if number in skipped:
                continue
            for phrase in ALLOWED_PHRASES:
                line = line.replace(phrase, " ")
            for what, pattern in patterns.items():
                if re.search(pattern, line, re.M):
                    found.setdefault(rel(path), []).append(f"{number + 1}: {what}: {line.strip()[:90]}")
    return found


@pytest.mark.parametrize("what", sorted(REMOVED))
def test_the_current_docs_do_not_describe_a_removed_concept(what):
    assert _scan({what: REMOVED[what]}) == {}


# --- "agent" where the harness is meant (ADR 0021) -----------------------------------------------------------------


CODE_NAMES = re.compile(r"agent_adapters|agent_conversations|agent_conversation_id|\bagents\.py|agent_launcher|\bresolve_agent\b|\btest_\w+")
"""Names that still exist in the code or the database and that a doc may need to point at: the modules `agent_adapters.py`
and `agents.py`, the table `agent_conversations` and its column `agent_conversation_id` (the database keeps them,
ADR 0021's rename covers the CLI, the docs and the config keys), the function `resolve_agent`, and test names, which
the definition of done cites as evidence and which stay as they are."""


def test_no_current_doc_says_agent_for_the_harness():
    """Same rule and allow-list as the CLI help and the skill (`test_harness.agent_uses`). Three additions: "Agent
    Launcher", the old product name, in the sentences that say what the rename did, the code names above, and the upgrade
    notes, which name the old spellings of keys and ids."""
    found = {}
    for path in DOCS:
        text = _text(path)
        skipped = _skipped(path, text)
        hits = []
        for number, line in enumerate(text.splitlines()):
            if number in skipped:
                continue
            hits += [f"{number + 1}: {use}: {line.strip()[:90]}" for use in agent_uses(CODE_NAMES.sub(" ", line.replace("Agent Launcher", " ")))]
        if hits:
            found[rel(path)] = hits
    assert found == {}


# --- SPEC.md is deleted and nothing cites it ---------------------------------------------------------------------

SPEC_CITATION = re.compile(r"SPEC\.md|SPEC\s*§")


def _docstrings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    nodes = [tree, *(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))]
    return [d for n in nodes if (d := ast.get_docstring(n, clean=False))]


def test_nothing_in_the_source_cites_spec():
    offenders = {
        rel(p): [m for m in SPEC_CITATION.findall(p.read_text())]
        for p in sorted((ROOT / "src").rglob("*"))
        if p.is_file() and p.suffix in {".py", ".md", ".json", ".toml"} and SPEC_CITATION.search(p.read_text())
    }
    assert offenders == {}


def test_no_test_docstring_cites_spec():
    """Test names (`test_spec_*`, `SPEC_EXAMPLE`) stay: the definition of done cites them by name."""
    offenders = {
        rel(p): [d.strip().splitlines()[0] for d in _docstrings(p) if SPEC_CITATION.search(d)]
        for p in sorted((ROOT / "tests").glob("*.py"))
        if any(SPEC_CITATION.search(d) for d in _docstrings(p))
    }
    assert offenders == {}


def test_nothing_else_in_the_repository_links_to_spec_md():
    """GLOSSARY.md, AGENTS.md, pyproject.toml and the like. ADRs stay as history; tests are covered by their docstrings."""
    skip = {".git", ".venv", "tests", "adr", "src", "__pycache__", ".pytest_cache", "node_modules"}
    offenders = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or skip & set(p.relative_to(ROOT).parts) or p.suffix not in {".md", ".toml", ".json", ".yml", ".yaml", ".txt", ".py"}:
            continue
        if "SPEC.md" in p.read_text(errors="ignore"):
            offenders.append(rel(p))
    assert offenders == []


# --- the u3 carried wording: a session does not always start on Commodore ------------------------------------------

FALSE_SENTENCES = re.compile(
    r"(?i)(?:every|each|a)\s+(?:new\s+)?session\s+(?:now\s+)?(?:always\s+)?starts?\s+(?:the\s+harness\s+)?on\s+Commodore"
    r"|prompt is always the harness's invocation of Commodore"
)


def test_no_doc_or_skill_says_every_session_starts_on_commodore():
    """`configure` and `integrate gh-dash` start `commodore-launcher` on their request text, so "every session starts on
    Commodore" is false. The known sites: the skill's SKILL.md, the README, `definition-of-done.md`, and the docstring of
    `migrations.py`."""
    sources = [*SCANNED, ROOT / "src" / "commodore_launcher" / "migrations.py"]
    offenders = [f"{rel(p)}: {m.group(0)}" for p in sources if (m := FALSE_SENTENCES.search(_text(p)))]
    assert offenders == []


def test_the_architecture_doc_names_both_maintenance_launches():
    lines = [l for l in (ROOT / "docs" / "architecture.md").read_text().splitlines() if "maintenance" in l]
    assert lines, "architecture.md no longer mentions the maintenance session"
    assert all("integrate gh-dash" in l for l in lines if "configure" in l)


# --- what the README says about install (decisions 1 and 2) ------------------------------------------------------


def test_the_readme_gives_the_install_line_that_works_today():
    readme = (ROOT / "README.md").read_text()
    assert "pipx install git+https://github.com/jbuffin/agent-launcher.git" in readme
    assert "commodore-launcher.git" not in readme  # that repository does not exist yet
    assert re.search(r"(?i)renamed to `?commodore-launcher", readme)


def test_the_readme_installs_the_commodore_skills_and_the_management_skill():
    readme = (ROOT / "README.md").read_text()
    for skill in ("commodore", "agent-teammates", "run-preflight", "target-evaluation"):
        assert f"`{skill}`" in readme, skill
    assert re.search(r"(?i)yet to be announced|not yet (?:been )?(?:announced|published)", readme)
    assert ".agents/skills" in readme and "CLAUDE_CONFIG_DIR" in readme  # where Codex and Claude Code load skills
    assert "npx skills add jbuffin/agent-launcher --skill commodore-launcher -g" in readme


# --- the definition of done is re-based on the trace criteria (decision 5) -----------------------------------------


@pytest.mark.parametrize("unit", range(13, 21))
def test_the_definition_of_done_has_rows_for_each_sub_issue(unit):
    assert re.search(rf"\b{unit}\.\d+\b", (ROOT / "docs" / "definition-of-done.md").read_text())


# --- the upgrade notes (decision 6) -----------------------------------------------------------------------------


def _upgrade_notes() -> str:
    text = (ROOT / "docs" / "config-migrations.md").read_text()
    lines = text.splitlines()
    inside = _sections(text, UPGRADE_NOTES)
    assert inside, f"docs/config-migrations.md has no `## {UPGRADE_NOTES}` section"
    return "\n".join(lines[i] for i in sorted(inside))


def test_the_2_to_3_step_is_described_with_every_key_it_renames_and_drops():
    notes = _upgrade_notes()
    renames = {**migrations._TOP_LEVEL_RENAMES, **migrations._PROFILE_RENAMES}
    dropped = [*migrations._DROPPED_TOP_LEVEL, *migrations._DROPPED_REPOSITORY, *migrations._DROPPED_HARNESS]
    missing = [name for name in [*renames, *renames.values(), *dropped] if f"`{name}`" not in notes]
    assert missing == []
    assert "Nothing else changes" not in (ROOT / "docs" / "config-migrations.md").read_text()


def test_the_upgrade_notes_list_the_breaking_changes_for_scripts():
    notes = _upgrade_notes()
    wanted = [
        "harness_conversation_id", "agent_conversation_id",  # JSON field
        "harness:", "agent:",  # doctor ids (`agent:<name>` became `harness:<name>`)
        "harness_selection_needed", "no_harnesses", "requested_harness",  # error codes
        "--default-harness", "--remove-harness", "--default-agent", "--remove-agent",
        "--execute", "--prepare", "--workflow",
        "commodore new", "commodore restart", "commodore prompt", "commodore cleanup", "commodore workflows",
        "commodore worktrees", "commodore sessions", "tasks link", "tasks completed",
        "workflows.json", "templates/", "~/.agent-launcher",
    ]
    assert [w for w in wanted if w not in notes] == []
