"""`doctor` and `open` check that Commodore is installed for the profile's harness (issue #18, ADR 0021).

Where each harness loads skills, as checked on Claude Code 2.1.296, codex-cli 0.162.1 and GitHub Copilot CLI 1.0.97
(the brief check, `u6-brief-checker`):

- Claude Code (https://code.claude.com/docs/en/skills, https://code.claude.com/docs/en/env-vars): personal
  `<config>/skills/<n>/SKILL.md` with `<config>` = `CLAUDE_CONFIG_DIR` or `~/.claude`; project `.claude/skills` from the
  working directory up to the repository root; skills of enabled plugins. It does not read `~/.agents/skills`.
- Codex (https://learn.chatgpt.com/docs/build-skills): `.agents/skills` from the working directory up to the repository
  root, `~/.agents/skills`, `/etc/codex/skills`. `CODEX_HOME/skills` is NOT documented (kept only as a harmless extra).
- Copilot (`copilot skill --help`): project `.github/skills`, `.agents/skills`, `.claude/skills`; personal
  `~/.copilot/skills`, `~/.agents/skills`. `COPILOT_HOME/skills` is inferred, not documented.

Seams: `check_skill` of each harness adapter (what is found where), `run_doctor` (the row per profile and harness) and
the Typer CLI with the mock terminal (`open`, `resume`, `configure`, `integrate gh-dash`). The fake HOME already holds
the four skills and `commodore-launcher` (see `conftest.commodore_skills_installed`); a test removes them.
"""

import json
import shutil
from pathlib import Path

import pytest

from commodore_launcher import cli, state
from commodore_launcher.agent_adapters import ClaudeCodeAdapter, CodexCliAdapter, CopilotCliAdapter
from commodore_launcher.doctor import CommandResult, run_doctor
from conftest import COMMODORE_SKILLS, MANAGEMENT_SKILL_NAME, install_skills, make_github_task
from test_github_issues import URL, checkout, data, env, mock_calls, open_url, run  # noqa: F401  (fixtures)
from test_github_pulls import gh  # noqa: F401  (the gh fixture)
from test_url_only import (  # noqa: F401  (maintenance_config is a fixture)
    all_mock_calls, configure_json, copy_archive, events, failure, maintenance_config, open_id, resume_id, row_counts,
    script, session_rows,
)

PLUGIN_ID = "plug@mkt"


# --- fixtures -----------------------------------------------------------------------------------------------------


@pytest.fixture
def home(fake_default_home: Path) -> Path:
    return fake_default_home


@pytest.fixture
def no_skills(home: Path) -> Path:
    """A HOME where nothing is installed."""
    for root in (home / ".claude", home / ".agents"):
        shutil.rmtree(root, ignore_errors=True)
    return home


@pytest.fixture
def bare(no_skills: Path) -> Path:
    """The adapter tests start from a HOME with nothing installed."""
    return no_skills


def adapter_env(home: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(home), **extra}


def write_json(path: Path, content: dict | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content))


def install_plugin(config: Path, *, plugins_root: Path | None = None, enabled: bool = True, skill: str = "commodore",
                   settings_file: Path | None = None) -> Path:
    """A Claude Code plugin laid out as on a real machine: `installed_plugins.json` points at
    `cache/mkt/plug/1.0.0/`, whose skill sits one level below `skills/` (`skills/group/<name>/SKILL.md`, a path the
    plugin's manifest declares), next to an older `0.9.0` that does not have it. Returns the install path."""
    root = plugins_root or config / "plugins"
    current, old = root / "cache" / "mkt" / "plug" / "1.0.0", root / "cache" / "mkt" / "plug" / "0.9.0"
    install_skills(current / "skills" / "group", skill)
    (old / "skills" / "group").mkdir(parents=True)
    write_json(root / "installed_plugins.json", {
        "version": 2,
        "plugins": {PLUGIN_ID: [{"scope": "user", "installPath": str(current), "version": "1.0.0"}]},
    })
    write_json(settings_file or config / "settings.json", {"enabledPlugins": {PLUGIN_ID: enabled}})
    return current


# --- Claude Code: what is found where -----------------------------------------------------------------------------


def test_claude_finds_a_skill_in_the_default_config_dir(tmp_path):
    install_skills(tmp_path / "h" / ".claude" / "skills", "commodore")
    check = ClaudeCodeAdapter().check_skill("commodore", {"HOME": str(tmp_path / "h")}, [])
    assert check.status == "available"


def test_claude_with_a_custom_config_dir_looks_there_and_not_in_dot_claude(tmp_path):
    """18.3: a profile with CLAUDE_CONFIG_DIR=~/.claude-work is checked against that directory."""
    h, work = tmp_path / "h", tmp_path / "h" / ".claude-work"
    install_skills(h / ".claude" / "skills", "commodore")
    env_ = {"HOME": str(h), "CLAUDE_CONFIG_DIR": str(work)}
    absent = ClaudeCodeAdapter().check_skill("commodore", env_, [])
    assert absent.status == "missing"
    assert str(work / "skills") in absent.checked and str(h / ".claude" / "skills") not in absent.checked
    install_skills(work / "skills", "commodore")
    assert ClaudeCodeAdapter().check_skill("commodore", env_, []).status == "available"


def test_claude_does_not_read_agents_skills(tmp_path):
    """Claude Code's documented locations do not include `~/.agents/skills`, so a skill only there is not installed."""
    install_skills(tmp_path / "h" / ".agents" / "skills", "commodore")
    assert ClaudeCodeAdapter().check_skill("commodore", {"HOME": str(tmp_path / "h")}, []).status == "missing"


def test_claude_needs_a_skill_md_not_a_bare_directory_or_a_command_file(tmp_path):
    skills = tmp_path / "h" / ".claude"
    (skills / "skills" / "commodore").mkdir(parents=True)  # no SKILL.md
    (skills / "commands").mkdir()
    (skills / "commands" / "run-preflight.md").write_text("x")
    claude = ClaudeCodeAdapter()
    assert claude.check_skill("commodore", {"HOME": str(tmp_path / "h")}, []).status == "missing"
    assert claude.check_skill("run-preflight", {"HOME": str(tmp_path / "h")}, []).status == "missing"


def test_claude_finds_a_project_skill_from_the_checkout_up_to_the_repository_root(tmp_path, make_repo):
    repo = make_repo("proj")
    sub = repo / "pkg" / "deep"
    sub.mkdir(parents=True)
    install_skills(repo / ".claude" / "skills", "commodore")
    h = {"HOME": str(tmp_path / "h")}
    assert ClaudeCodeAdapter().check_skill("commodore", h, [sub]).status == "available"
    assert ClaudeCodeAdapter().check_skill("commodore", h, []).status == "missing"  # no checkout, no project dirs


def test_claude_project_lookup_stops_at_the_repository_root(tmp_path, make_repo):
    repo = make_repo("proj")
    install_skills(repo.parent / ".claude" / "skills", "commodore")  # above the repository root
    assert ClaudeCodeAdapter().check_skill("commodore", {"HOME": str(tmp_path / "h")}, [repo]).status == "missing"


# --- Claude Code: plugin skills ---------------------------------------------------------------------------------


def test_claude_finds_a_skill_of_an_enabled_plugin_in_its_install_path(bare):
    install_plugin(bare / ".claude")
    check = ClaudeCodeAdapter().check_skill("commodore", adapter_env(bare), [])
    assert check.status == "available"


def test_claude_does_not_find_a_skill_of_a_disabled_plugin(bare):
    install_plugin(bare / ".claude", enabled=False)
    assert ClaudeCodeAdapter().check_skill("commodore", adapter_env(bare), []).status == "missing"


def test_claude_does_not_find_a_skill_of_an_installed_plugin_that_is_not_in_enabled_plugins(bare):
    install_plugin(bare / ".claude")
    write_json(bare / ".claude" / "settings.json", {"enabledPlugins": {}})
    assert ClaudeCodeAdapter().check_skill("commodore", adapter_env(bare), []).status == "missing"


def test_claude_never_finds_a_skill_in_a_stale_version_of_a_plugin(bare):
    """The cache keeps old versions for days; only the version `installed_plugins.json` points at counts."""
    current = install_plugin(bare / ".claude", skill="other")
    install_skills(current.parent / "0.9.0" / "skills" / "group", "commodore")
    assert ClaudeCodeAdapter().check_skill("commodore", adapter_env(bare), []).status == "missing"


def test_claude_does_not_search_node_modules_inside_a_plugin(bare):
    current = install_plugin(bare / ".claude", skill="other")
    install_skills(current / "node_modules" / "dep" / "skills", "commodore")
    assert ClaudeCodeAdapter().check_skill("commodore", adapter_env(bare), []).status == "missing"


def test_claude_reads_plugins_of_the_custom_config_dir_not_of_dot_claude(bare):
    install_plugin(bare / ".claude")
    work = bare / ".claude-work"
    work.mkdir()
    check = ClaudeCodeAdapter().check_skill("commodore", adapter_env(bare, CLAUDE_CONFIG_DIR=str(work)), [])
    assert check.status == "missing"
    install_plugin(work)
    assert ClaudeCodeAdapter().check_skill("commodore", adapter_env(bare, CLAUDE_CONFIG_DIR=str(work)), []).status == "available"


def test_claude_reads_the_plugins_root_from_claude_code_plugin_cache_dir(bare, tmp_path):
    root = tmp_path / "plugin-cache"
    install_plugin(bare / ".claude", plugins_root=root)
    check = ClaudeCodeAdapter().check_skill("commodore", adapter_env(bare, CLAUDE_CODE_PLUGIN_CACHE_DIR=str(root)), [])
    assert check.status == "available"


@pytest.mark.parametrize("settings", [".claude/settings.json", ".claude/settings.local.json"])
def test_claude_enables_a_plugin_from_the_projects_settings_when_there_is_a_checkout(bare, make_repo, settings):
    repo = make_repo("proj")
    install_plugin(bare / ".claude", settings_file=repo / settings)
    write_json(bare / ".claude" / "settings.json", {})
    claude = ClaudeCodeAdapter()
    assert claude.check_skill("commodore", adapter_env(bare), [repo]).status == "available"
    assert claude.check_skill("commodore", adapter_env(bare), []).status == "missing"  # `doctor` has no checkout


@pytest.mark.parametrize("record", [None, "{not json"])
def test_claude_cannot_verify_plugins_when_installed_plugins_is_missing_or_unreadable(bare, record):
    """A plugins dir with no readable record: not found must not read as a false `missing`."""
    assert ClaudeCodeAdapter().check_skill("commodore", adapter_env(bare), []).status == "missing"  # no plugins dir
    (bare / ".claude" / "plugins" / "cache" / "mkt" / "plug" / "1.0.0").mkdir(parents=True)
    if record is not None:
        write_json(bare / ".claude" / "plugins" / "installed_plugins.json", record)
    assert ClaudeCodeAdapter().check_skill("commodore", adapter_env(bare), []).status == "not_verified"


def test_claude_with_no_plugins_dir_and_no_skill_is_missing(bare):
    assert ClaudeCodeAdapter().check_skill("commodore", adapter_env(bare), []).status == "missing"


def test_claude_a_found_skill_wins_over_an_unreadable_plugin_record(bare):
    install_skills(bare / ".claude" / "skills", "commodore")
    write_json(bare / ".claude" / "plugins" / "installed_plugins.json", "{not json")
    assert ClaudeCodeAdapter().check_skill("commodore", adapter_env(bare), []).status == "available"


# --- Codex ---------------------------------------------------------------------------------------------------------


def test_codex_finds_a_skill_in_agents_skills_of_the_home(bare):
    install_skills(bare / ".agents" / "skills", "commodore")
    assert CodexCliAdapter().check_skill("commodore", adapter_env(bare), []).status == "available"


def test_codex_does_not_read_claude_skills(bare):
    install_skills(bare / ".claude" / "skills", "commodore")
    assert CodexCliAdapter().check_skill("commodore", adapter_env(bare), []).status == "missing"


def test_codex_home_does_not_move_where_agents_skills_are_read_from(bare, tmp_path):
    """`CODEX_HOME/skills` is undocumented: a custom CODEX_HOME still reads `~/.agents/skills`."""
    install_skills(bare / ".agents" / "skills", "commodore")
    env_ = adapter_env(bare, CODEX_HOME=str(tmp_path / "codex-work"))
    assert CodexCliAdapter().check_skill("commodore", env_, []).status == "available"


def test_codex_finds_a_project_skill_up_to_the_repository_root(bare, make_repo):
    repo = make_repo("proj")
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    install_skills(repo / ".agents" / "skills", "commodore")
    assert CodexCliAdapter().check_skill("commodore", adapter_env(bare), [sub]).status == "available"
    assert CodexCliAdapter().check_skill("commodore", adapter_env(bare), []).status == "missing"


def test_codex_project_lookup_stops_at_the_repository_root(bare, make_repo):
    repo = make_repo("proj")
    install_skills(repo.parent / ".agents" / "skills", "commodore")
    assert CodexCliAdapter().check_skill("commodore", adapter_env(bare), [repo]).status == "missing"


def test_codex_not_found_while_codex_plugins_are_installed_is_not_verified(bare, tmp_path):
    """Codex plugin skills have no documented path and `codex plugin list` is not called: a non-empty plugins dir
    means not found cannot be called missing."""
    codex_home = tmp_path / "codex-work"
    assert CodexCliAdapter().check_skill("commodore", adapter_env(bare, CODEX_HOME=str(codex_home)), []).status == "missing"
    (codex_home / "plugins" / "some-plugin").mkdir(parents=True)
    (codex_home / "plugins" / "some-plugin" / "plugin.json").write_text("{}")
    env_ = adapter_env(bare, CODEX_HOME=str(codex_home))
    assert CodexCliAdapter().check_skill("commodore", env_, []).status == "not_verified"
    install_skills(bare / ".agents" / "skills", "commodore")  # found wins over unverifiable plugins
    assert CodexCliAdapter().check_skill("commodore", env_, []).status == "available"


def test_codex_with_an_empty_plugins_dir_and_no_skill_is_missing(bare, tmp_path):
    codex_home = tmp_path / "codex-work"
    (codex_home / "plugins").mkdir(parents=True)
    assert CodexCliAdapter().check_skill("commodore", adapter_env(bare, CODEX_HOME=str(codex_home)), []).status == "missing"


# --- Copilot -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("where", [".copilot/skills", ".agents/skills"])
def test_copilot_finds_a_personal_skill(bare, where):
    install_skills(bare / where, "commodore")
    assert CopilotCliAdapter().check_skill("commodore", adapter_env(bare), []).status == "available"


def test_copilot_does_not_find_a_skill_only_in_claude_skills_of_the_home(bare):
    """`.claude/skills` is a project location for Copilot, not a personal one."""
    install_skills(bare / ".claude" / "skills", "commodore")
    assert CopilotCliAdapter().check_skill("commodore", adapter_env(bare), []).status == "missing"


@pytest.mark.parametrize("where", [".github/skills", ".agents/skills", ".claude/skills"])
def test_copilot_finds_a_project_skill_up_to_the_repository_root(bare, make_repo, where):
    repo = make_repo("proj")
    sub = repo / "a"
    sub.mkdir()
    install_skills(repo / where, "commodore")
    assert CopilotCliAdapter().check_skill("commodore", adapter_env(bare), [sub]).status == "available"
    assert CopilotCliAdapter().check_skill("commodore", adapter_env(bare), []).status == "missing"


def test_copilot_home_skills_is_read_but_is_inferred_not_documented(bare, tmp_path):
    chome = tmp_path / "copilot-work"
    install_skills(chome / "skills", "commodore")
    assert CopilotCliAdapter().check_skill("commodore", adapter_env(bare, COPILOT_HOME=str(chome)), []).status == "available"
    assert CopilotCliAdapter().check_skill("commodore", adapter_env(bare), []).status == "missing"


def test_a_check_names_the_places_it_looked_in(bare):
    for adapter in (ClaudeCodeAdapter(), CodexCliAdapter(), CopilotCliAdapter()):
        assert adapter.check_skill("commodore", adapter_env(bare), []).checked


# --- doctor (18.1, 18.3) -------------------------------------------------------------------------------------------


class System:
    """PATH and process execution for `doctor`: every harness exists and no process is a skill probe."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def which(self, name):
        return f"/fake/bin/{name}"

    def run(self, argv, timeout):
        self.calls.append(tuple(argv))
        return CommandResult(0, "tool 1.0\n", "")


@pytest.fixture
def exes(tmp_path) -> dict[str, str]:
    bin_dir = tmp_path / "exes"
    bin_dir.mkdir()
    paths = {}
    for name in ("claude", "codex", "copilot"):
        (bin_dir / name).write_text("#!/bin/sh\n")
        (bin_dir / name).chmod(0o755)
        paths[name] = str(bin_dir / name)
    return paths


@pytest.fixture
def profiles(write_config, exes):
    def _write(**harnesses: dict):
        write_config({
            "version": 3,
            "profiles": {"work": {
                "default_harness": next(iter(harnesses)),
                "harnesses": {h: {"executable": exes[h], **extra} for h, extra in harnesses.items()},
            }},
        })

    return _write


def doctor_checks(system: System | None = None):
    system = system or System()
    report = run_doctor(runner=system.run, which=system.which)
    return {c.id: c for c in report.checks}, report


def test_doctor_passes_when_the_four_skills_are_installed_for_every_harness_of_a_profile(profiles):
    profiles(claude={}, codex={}, copilot={})
    checks, report = doctor_checks()
    for harness in ("claude", "codex", "copilot"):
        assert checks[f"profile:work:{harness}"].status == "pass", checks[f"profile:work:{harness}"]
    assert report.ok


def test_doctor_fails_naming_the_missing_skills_and_an_install_hint(profiles, home):
    profiles(claude={})
    shutil.rmtree(home / ".claude" / "skills" / "agent-teammates")
    shutil.rmtree(home / ".claude" / "skills" / "target-evaluation")
    checks, report = doctor_checks()
    row = checks["profile:work:claude"]
    assert row.status == "fail" and not report.ok
    assert "agent-teammates" in row.detail and "target-evaluation" in row.detail
    assert "commodore," not in row.detail and "run-preflight" not in row.detail  # the installed ones are not named
    hint = row.remediation
    assert "npx skills add" in hint and "-a claude-code" in hint
    assert "agent-teammates" in hint and "target-evaluation" in hint
    assert "~/.claude/skills" in hint  # where Claude Code loads them; `doctor` shows the home directory as `~`
    assert "CLAUDE_CONFIG_DIR" not in hint  # the default config dir needs no override


def test_doctor_names_all_four_when_none_is_installed(profiles, no_skills):
    profiles(claude={})
    row = doctor_checks()[0]["profile:work:claude"]
    assert row.status == "fail" and all(name in row.detail for name in COMMODORE_SKILLS)


def test_doctor_checks_each_harness_of_a_profile_on_its_own(profiles, home):
    profiles(claude={}, codex={})
    shutil.rmtree(home / ".agents" / "skills" / "commodore")  # Codex loses it, Claude Code does not read this dir
    checks, _ = doctor_checks()
    assert checks["profile:work:claude"].status == "pass"
    assert checks["profile:work:codex"].status == "fail" and "commodore" in checks["profile:work:codex"].detail
    assert "-a codex" in checks["profile:work:codex"].remediation


def test_doctor_hint_for_copilot_uses_the_skills_cli_agent_id(profiles, no_skills):
    profiles(copilot={})
    row = doctor_checks()[0]["profile:work:copilot"]
    assert row.status == "fail"
    hint = row.remediation
    assert "npx skills add" in hint and "-a github-copilot" in hint


def test_doctor_checks_a_custom_claude_config_dir_not_dot_claude(profiles, home):
    """18.3. `~` in the profile's env is expanded as the instance does."""
    profiles(claude={"env": {"CLAUDE_CONFIG_DIR": "~/.claude-work"}})  # the four are in ~/.claude only
    row = doctor_checks()[0]["profile:work:claude"]
    assert row.status == "fail"
    hint = row.remediation  # `doctor` shows the home directory as `~`, as for every path it prints
    assert "~/.claude-work" in row.detail + hint
    assert "CLAUDE_CONFIG_DIR=~/.claude-work" in hint and "npx skills add" in hint and "-a claude-code" in hint
    assert "~/.claude-work/skills" in hint  # the copy fallback
    install_skills(home / ".claude-work" / "skills", *COMMODORE_SKILLS)
    assert doctor_checks()[0]["profile:work:claude"].status == "pass"


def test_doctor_hint_for_copilot_home_offers_a_copy_or_agents_skills(profiles, home, tmp_path):
    chome = tmp_path / "copilot-work"
    profiles(copilot={"env": {"COPILOT_HOME": str(chome)}})
    shutil.rmtree(home / ".agents" / "skills")
    row = doctor_checks()[0]["profile:work:copilot"]
    assert row.status == "fail"
    assert "COPILOT_HOME" in row.remediation
    assert "~/.agents/skills" in row.remediation or str(home / ".agents" / "skills") in row.remediation


def test_doctor_does_not_report_a_plugin_installed_skill_missing(profiles, home):
    profiles(claude={})
    shutil.rmtree(home / ".claude" / "skills")
    assert doctor_checks()[0]["profile:work:claude"].status == "fail"  # nothing installed yet
    install_plugin(home / ".claude", skill="commodore")
    for other in COMMODORE_SKILLS[1:]:
        install_skills(Path(home / ".claude" / "plugins" / "cache" / "mkt" / "plug" / "1.0.0" / "skills" / "group"), other)
    assert doctor_checks()[0]["profile:work:claude"].status == "pass"


def test_doctor_warns_instead_of_failing_when_plugins_cannot_be_read(profiles, home):
    profiles(claude={})
    shutil.rmtree(home / ".claude" / "skills")
    write_json(home / ".claude" / "plugins" / "installed_plugins.json", "{not json")
    checks, report = doctor_checks()
    assert checks["profile:work:claude"].status == "warn"
    assert report.ok


def test_doctor_looks_at_user_locations_only_not_at_the_current_directory(profiles, no_skills, tmp_path, monkeypatch):
    profiles(claude={})
    project = tmp_path / "somewhere"
    install_skills(project / ".claude" / "skills", *COMMODORE_SKILLS)
    monkeypatch.chdir(project)
    assert doctor_checks()[0]["profile:work:claude"].status == "fail"


def test_doctor_probes_no_skill_or_plugin_commands(profiles):
    profiles(claude={}, codex={}, copilot={})
    system = System()
    doctor_checks(system)
    assert system.calls and not [c for c in system.calls if {"skill", "plugin", "plugins"} & set(c[1:])]


# --- open (18.2): refuse before anything is created ---------------------------------------------------------------


def refusal_message(result) -> str:
    assert result.exit_code != 0, result.output
    return json.dumps(json.loads(result.stdout)["error"])


def nothing_started(launcher_home: Path) -> None:
    assert not all_mock_calls(launcher_home)
    counts = row_counts()
    assert counts["launches"] == 0 and counts["sessions"] == 0


def test_open_of_a_new_url_with_a_missing_skill_refuses_and_creates_no_task_launch_or_terminal(
    env, checkout, launcher_home, no_skills
):
    message = refusal_message(open_url())
    assert "commodore" in message and "npx skills add" in message
    nothing_started(launcher_home)
    assert row_counts()["tasks"] == 0 and row_counts()["task_github"] == 0


def test_open_names_only_the_missing_skills(env, checkout, launcher_home, home):
    shutil.rmtree(home / ".claude" / "skills" / "run-preflight")
    message = refusal_message(open_url())
    assert "run-preflight" in message and "target-evaluation" not in message and "agent-teammates" not in message
    nothing_started(launcher_home)


def test_open_prints_the_hint_for_a_person_on_stderr(env, checkout, launcher_home, no_skills):
    result = run("open", URL, "--terminal", "mock")
    assert result.exit_code != 0
    assert "npx skills add" in result.stderr and "commodore" in result.stderr


def test_open_of_a_pull_request_url_checks_too(env, checkout, launcher_home, no_skills, gh):
    from test_github_pulls import PR_URL

    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha="a" * 40)
    assert "npx skills add" in refusal_message(open_url(PR_URL))
    nothing_started(launcher_home)


def test_open_of_an_existing_task_on_its_first_open_refuses_before_the_launch_row(env, checkout, launcher_home, no_skills):
    task = make_github_task(checkout)
    message = refusal_message(open_id(task["id"], "--offline"))
    assert "npx skills add" in message
    nothing_started(launcher_home)
    assert json.loads(run("tasks", "show", task["id"], "--json").stdout)["task"]["state"] == task["state"]


def test_open_refuses_on_the_chosen_harness_only(env, checkout, launcher_home, home, write_config, tmp_path):
    """Claude Code has none of the four, Codex has them in `~/.agents/skills`: `--harness codex` opens, claude refuses."""
    codex = tmp_path / "bin" / "codex"
    codex.write_text("#!/bin/sh\n")
    codex.chmod(0o755)
    config = json.loads((launcher_home / "config.json").read_text())
    config["profiles"]["work"]["harnesses"]["codex"] = {"executable": str(codex)}
    write_config(config)
    shutil.rmtree(home / ".claude" / "skills")
    assert "npx skills add" in refusal_message(open_url())
    nothing_started(launcher_home)
    opened = data(open_url(URL, "--harness", "codex"))
    assert opened["action"] == "created" and opened["session"]["harness"] == "codex"


def test_open_checks_the_profiles_custom_claude_config_dir(env, checkout, launcher_home, home, write_config):
    """18.3 through `open`: the four are in `~/.claude`, the profile's config dir is `~/.claude-work`."""
    config = json.loads((launcher_home / "config.json").read_text())
    config["profiles"]["work"]["harnesses"]["claude"]["env"] = {"CLAUDE_CONFIG_DIR": "~/.claude-work"}
    write_config(config)
    message = refusal_message(open_url())
    assert str(home / ".claude-work") in message and "CLAUDE_CONFIG_DIR" in message
    nothing_started(launcher_home)
    install_skills(home / ".claude-work" / "skills", *COMMODORE_SKILLS)
    assert data(open_url())["action"] == "created"


def test_open_accepts_a_plugin_installed_skill(env, checkout, launcher_home, home):
    shutil.rmtree(home / ".claude" / "skills")
    assert "npx skills add" in refusal_message(open_url())  # nothing installed yet
    install_plugin(home / ".claude")
    for other in COMMODORE_SKILLS[1:]:
        install_skills(home / ".claude" / "plugins" / "cache" / "mkt" / "plug" / "1.0.0" / "skills" / "group", other)
    assert data(open_url())["action"] == "created"


def test_open_proceeds_with_a_notice_when_plugins_cannot_be_verified(env, checkout, launcher_home, home):
    shutil.rmtree(home / ".claude" / "skills")
    write_json(home / ".claude" / "plugins" / "installed_plugins.json", "{not json")
    opened = data(open_url())
    assert opened["action"] == "created"
    assert opened["notice"] and "skill" in opened["notice"].lower()
    assert len(mock_calls(launcher_home)) == 1


def test_a_focus_only_reopen_does_not_check(env, checkout, launcher_home, home):
    first = data(open_url())
    shutil.rmtree(home / ".claude")
    again = data(open_url())
    assert again["action"] == "focused" and again["session"]["id"] == first["session"]["id"]
    assert len(mock_calls(launcher_home)) == 1


def test_resuming_a_conversation_checks_and_a_refusal_leaves_the_session_untouched(env, checkout, launcher_home, home):
    first = data(open_url())
    task_id = first["task"]["id"]
    script(launcher_home, gone=["mock-workspace-1"])
    shutil.rmtree(home / ".claude")
    for attempt in (lambda: open_id(task_id), lambda: resume_id(task_id)):
        assert "npx skills add" in refusal_message(attempt())
    assert len(mock_calls(launcher_home)) == 1
    assert session_rows(task_id) == [(first["session"]["id"], first["session"]["harness_conversation_id"])]


def test_a_fresh_launch_in_place_checks(env, checkout, launcher_home, home):
    first = data(open_url())
    task_id = first["task"]["id"]
    with state.open_state() as conn:
        conn.execute("UPDATE agent_conversations SET conversation_id = NULL")
    script(launcher_home, gone=["mock-workspace-1"])
    shutil.rmtree(home / ".claude")
    assert "npx skills add" in refusal_message(open_url())
    assert len(mock_calls(launcher_home)) == 1
    (row,) = session_rows(task_id)
    assert row[0] == first["session"]["id"] and row[1] is None


def test_a_refusal_for_a_missing_skill_leaves_a_copy_archived_task_archived(env, checkout, launcher_home, home):
    first = data(open_url())
    task_id = first["task"]["id"]
    copy_archive(launcher_home, task_id)
    shutil.rmtree(home / ".claude")
    assert "npx skills add" in refusal_message(open_url())
    assert json.loads(run("tasks", "show", task_id, "--json").stdout)["task"]["state"] == "archived"
    assert [e for e, _ in events(task_id)] == ["archived"]
    with state.open_state() as conn:
        assert conn.execute("SELECT COUNT(*) FROM launches WHERE task_id = ?", (task_id,)).fetchone()[0] == 1
    assert len(mock_calls(launcher_home)) == 1


# --- configure and integrate gh-dash check `commodore-launcher` only ---------------------------------------------


def only_installed(home: Path, *names: str) -> None:
    shutil.rmtree(home / ".claude" / "skills", ignore_errors=True)
    install_skills(home / ".claude" / "skills", *names)


def integrate_json():
    return run("integrate", "gh-dash", "--profile", "work", "--terminal", "mock", "--json")


@pytest.fixture
def gh_dash_present(monkeypatch):
    monkeypatch.setattr(cli, "gh_dash_detected", lambda *a, **k: True)


@pytest.mark.parametrize("launcher", ["configure", "integrate"])
def test_configure_and_integrate_refuse_without_commodore_launcher_and_name_how_to_get_it(
    maintenance_config, launcher_home, home, gh_dash_present, launcher
):
    only_installed(home, *COMMODORE_SKILLS)  # the four are not what they start
    result = configure_json("add a profile") if launcher == "configure" else integrate_json()
    message = refusal_message(result)
    assert MANAGEMENT_SKILL_NAME in message
    assert "commodore skill path" in message
    assert "npx skills add jbuffin/agent-launcher" in message and "--skill commodore-launcher" in message
    assert not all_mock_calls(launcher_home)


@pytest.mark.parametrize("launcher", ["configure", "integrate"])
def test_configure_and_integrate_need_only_commodore_launcher(
    maintenance_config, launcher_home, home, gh_dash_present, launcher
):
    only_installed(home)  # nothing installed: refused
    first = configure_json("add a profile") if launcher == "configure" else integrate_json()
    assert MANAGEMENT_SKILL_NAME in refusal_message(first)
    only_installed(home, MANAGEMENT_SKILL_NAME)
    result = configure_json("add a profile") if launcher == "configure" else integrate_json()
    assert data(result)["action"] == "created"


def test_the_old_name_agent_launcher_does_not_count_as_commodore_launcher(maintenance_config, launcher_home, home):
    only_installed(home, "agent-launcher", *COMMODORE_SKILLS)
    assert MANAGEMENT_SKILL_NAME in refusal_message(configure_json("add a profile"))
    assert not all_mock_calls(launcher_home)
