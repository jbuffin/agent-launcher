"""Issue #14: "harness" replaces "agent", and config version 3.

Seams (all public):
- the Typer CLI through a runner (the option, the `--help` walk, `doctor`, `config validate`, `config migrate`);
- `migrations` (`default_targets`, `migrate_data`, `plan_migration`, `apply_migration`);
- u1's one-time copy, reached through the CLI callback.

The expected version-3 config is written out as a literal, never computed by renaming the fixture, so a step that
renames the wrong thing cannot agree with itself.
"""

import ast
import dataclasses
import json
import re
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from commodore_launcher import config as config_module, migrations
from commodore_launcher.cli import app
from commodore_launcher.config import read_raw, validate_data
from commodore_launcher.doctor import run_doctor
from commodore_launcher.migrations import apply_migration, migrate_data, plan_migration
from test_doctor import FakeSystem
from test_session_lifecycle import configure, data, fake_agents, mock_calls, repo, run, task  # noqa: F401  (fixtures)

runner = CliRunner()
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "commodore_launcher"


# --- 14.1: the option -----------------------------------------------------------------------------------


def test_open_with_harness_skips_the_picker(configure, task, launcher_home):  # noqa: F811
    configure(harness_selection="always_ask")
    # Control: with two harnesses and `always_ask`, a non-interactive open has to be told which one.
    needed = run("open", task["id"], "--offline", "--json")
    assert needed.exit_code == 1
    assert json.loads(needed.stdout)["error"]["code"] == "harness_selection_needed"

    opened = data(run("open", task["id"], "--harness", "codex", "--offline", "--json"))
    assert opened["session"]["harness"] == "codex"
    assert "agent" not in opened["session"]
    (call,) = mock_calls(launcher_home, "create_session")
    assert call["command"][0].endswith("codex")


def test_open_a_github_url_with_harness_skips_the_picker(write_config, tmp_path, launcher_home, fake_github, monkeypatch):
    from test_github_issues import URL, FakeGh, open_url  # the fake `gh`, shared with the issue tests
    from commodore_launcher import github

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "codex"):
        (bin_dir / name).write_text("#!/bin/sh\n")
        (bin_dir / name).chmod(0o755)
    fake = FakeGh(fake_github)
    monkeypatch.setattr(github, "run_gh", fake)
    fake.add_repo("acme/widgets", 101)
    fake.add_issue("acme/widgets", 7, db_id=9007, title="An issue")
    (tmp_path / "src").mkdir()
    write_config(
        {
            "version": 3,
            "terminal": {"adapter": "mock"},
            "harness_selection": "always_ask",
            "repositories": {"search_roots": [str(tmp_path / "src")]},
            "profiles": {
                "work": {
                    "default_harness": "claude",
                    "harnesses": {n: {"executable": str(bin_dir / n)} for n in ("claude", "codex")},
                }
            },
        }
    )
    from conftest import git_run

    checkout = tmp_path / "src" / "widgets"
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", "https://github.com/acme/widgets"], check=True)
    git_run(checkout, "commit", "-q", "--allow-empty", "-m", "initial")
    assert run("profile", "set", str(checkout), "work").exit_code == 0

    assert json.loads(open_url(URL).stdout)["error"]["code"] == "harness_selection_needed"
    opened = json.loads(open_url(URL, "--harness", "codex").stdout)
    assert opened["session"]["harness"] == "codex"


OLD_FLAG_COMMANDS = [
    ["open", "t-1", "--agent", "codex"],
    ["profile", "add", "p", "--agent", "claude"],
    ["profile", "edit", "p", "--agent", "claude"],
    ["profile", "check", "p", "--agent", "claude"],
    ["configure", "--agent", "claude"],
    ["integrate", "gh-dash", "--agent", "claude"],
    ["profile", "add", "p", "--default-agent", "claude"],
    ["profile", "edit", "p", "--default-agent", "claude"],
    ["profile", "edit", "p", "--remove-agent", "claude"],
]


@pytest.mark.parametrize("argv", OLD_FLAG_COMMANDS, ids=lambda argv: " ".join(argv[:3]) + " " + argv[-2])
def test_the_old_agent_option_is_an_unknown_option_with_no_alias(argv):
    result = runner.invoke(app, argv)
    assert result.exit_code == 2, result.output
    assert "No such option" in result.output


NEW_FLAG_COMMANDS = [
    (["open"], "--harness"),
    (["profile", "add"], "--harness"),
    (["profile", "add"], "--default-harness"),
    (["profile", "edit"], "--harness"),
    (["profile", "edit"], "--default-harness"),
    (["profile", "edit"], "--remove-harness"),
    (["profile", "check"], "--harness"),
    (["configure"], "--harness"),
    (["integrate", "gh-dash"], "--harness"),
]


@pytest.mark.parametrize("command, option", NEW_FLAG_COMMANDS, ids=lambda v: v if isinstance(v, str) else " ".join(v))
def test_the_harness_options_are_documented_in_help(command, option):
    result = runner.invoke(app, [*command, "--help"])
    assert result.exit_code == 0, result.output
    assert option in result.output


def test_setup_answers_use_harness_keys(write_config, tmp_path, fake_agents):  # noqa: F811
    exe = fake_agents / "claude"
    answers = tmp_path / "answers.json"
    old = {"profiles": [{"name": "a", "agents": {"claude": {"executable": str(exe)}}, "default_agent": "claude"}]}
    answers.write_text(json.dumps(old))
    refused = runner.invoke(app, ["setup", "--answers", str(answers), "--yes", "--json"])
    assert refused.exit_code == 1
    assert json.loads(refused.stdout)["error"]["code"] == "invalid_answers"
    new = {"profiles": [{"name": "a", "harnesses": {"claude": {"executable": str(exe)}}, "default_harness": "claude"}]}
    answers.write_text(json.dumps(new))
    ok = runner.invoke(app, ["setup", "--answers", str(answers), "--yes", "--json"])
    assert ok.exit_code == 0, ok.output
    saved = read_raw()["profiles"]["a"]
    assert saved["default_harness"] == "claude" and list(saved["harnesses"]) == ["claude"]


# --- 14.2: version 3 ------------------------------------------------------------------------------------

V2 = {
    "version": 2,
    "debug": True,
    "future_setting": {"keep": [1, 2]},  # an unknown top-level key survives
    "prompt_execution": "execute",  # dropped by u3's step, with the other prompt keys (tests/test_commodore_prompt.py)
    "agent_selection": "use_default",
    "repositories": {"search_roots": ["/Users/me/src"]},
    "terminal": {"adapter": "mock"},
    "agent_types": {"aider": {"adapter": "claude-code", "executable": "aider-chat"}},
    "profiles": {
        "work": {
            "default_agent": "claude",
            "agents": {
                "claude": {"executable": "/Users/me/bin/claude", "args": ["--model", "x"], "env": {"CLAUDE_CONFIG_DIR": "/Users/me/.claude-work"}},
                "aider": {},
            },
        },
        "personal": {},
    },
}

V3 = {
    "version": 3,
    "debug": True,
    "future_setting": {"keep": [1, 2]},
    "harness_selection": "use_default",
    "repositories": {"search_roots": ["/Users/me/src"]},
    "terminal": {"adapter": "mock"},
    "harness_types": {"aider": {"adapter": "claude-code", "executable": "aider-chat"}},
    "profiles": {
        "work": {
            "default_harness": "claude",
            "harnesses": {
                "claude": {"executable": "/Users/me/bin/claude", "args": ["--model", "x"], "env": {"CLAUDE_CONFIG_DIR": "/Users/me/.claude-work"}},
                "aider": {},
            },
        },
        "personal": {},
    },
}

V1 = {"version": 1, "debug": True, "future_setting": {"keep": [1, 2]}, "terminal": {"adapter": "mock"}}


def config_target() -> migrations.MigrationTarget:
    return next(t for t in migrations.default_targets() if t.name == "config.json")


def backups(home: Path) -> list[Path]:
    return sorted((home / "backups").glob("*.json")) if (home / "backups").exists() else []


def test_the_current_config_version_is_3():
    assert config_module.CURRENT_VERSION == 3
    assert config_target().current_version == 3


def test_the_step_renames_exactly_the_agent_keys_and_nothing_else():
    assert migrate_data(config_target(), V2) == V3


def test_the_step_does_not_modify_its_input():
    before = json.loads(json.dumps(V2))
    migrated = migrate_data(config_target(), V2)
    assert migrated["version"] == 3 and V2 == before


def test_a_v1_file_chains_through_to_3():
    assert migrate_data(config_target(), V1) == {**V1, "version": 3}


def test_a_config_without_agent_keys_only_changes_its_version():
    assert migrate_data(config_target(), {"version": 2, "debug": True}) == {"version": 3, "debug": True}


@pytest.mark.parametrize("profiles", [[], "x", None, {"w": "x"}, {"w": []}, {"w": {"agents": "x"}}])
def test_the_step_tolerates_a_malformed_profiles_value(profiles):
    migrated = migrate_data(config_target(), {"version": 2, "profiles": profiles})
    assert migrated["version"] == 3  # validation, not the step, reports what is wrong with the value


def test_a_profile_holding_both_spellings_is_an_error_not_a_merge():
    both = {"version": 2, "profiles": {"w": {"agents": {"claude": {}}, "harnesses": {"codex": {}}}}}
    with pytest.raises(ValueError, match="agents"):
        migrate_data(config_target(), both)


def test_planning_a_profile_with_both_spellings_reports_it_and_writes_nothing(write_config, launcher_home):
    path = write_config({"version": 2, "profiles": {"w": {"agents": {"claude": {}}, "harnesses": {"codex": {}}}}})
    before = path.read_bytes()
    plan = plan_migration(config_target())
    assert plan.status == "error" and "agents" in " ".join(plan.problems) and "harnesses" in " ".join(plan.problems)
    done = runner.invoke(app, ["config", "migrate", "--yes"])
    assert done.exit_code == 1
    assert path.read_bytes() == before and backups(launcher_home) == []


def test_planning_with_a_non_object_profiles_value_is_an_error_status_not_a_crash(write_config):
    write_config({"version": 2, "profiles": []})
    assert plan_migration(config_target()).status in {"error", "pending"}


def test_dry_run_lists_the_renamed_keys_as_the_change(write_config, launcher_home):
    path = write_config(V2)
    before = path.read_bytes()
    done = runner.invoke(app, ["config", "migrate", "--dry-run", "--json"])
    assert done.exit_code == 0, done.output
    entry = next(f for f in json.loads(done.stdout)["files"] if f["file"] == "config.json")
    assert (entry["status"], entry["from_version"], entry["to_version"], entry["significant"]) == ("pending", 2, 3, True)
    assert entry["changed_keys"] == ["agent_selection", "agent_types", "harness_selection", "harness_types", "profiles", "prompt_execution"]
    assert path.read_bytes() == before and backups(launcher_home) == []


def test_config_migrate_writes_a_backup_and_a_valid_v3_file(write_config, launcher_home):
    path = write_config(V2)
    original = path.read_bytes()
    done = runner.invoke(app, ["config", "migrate", "--yes"])
    assert done.exit_code == 0, done.output
    assert json.loads(path.read_text()) == V3
    (backup,) = backups(launcher_home)
    assert backup.read_bytes() == original
    assert config_module.validate_config(path).valid
    assert runner.invoke(app, ["config", "validate"]).exit_code == 0


def test_a_v3_file_is_left_alone(write_config, launcher_home):
    path = write_config(V3)
    before = path.read_bytes()
    done = runner.invoke(app, ["config", "migrate", "--yes"])
    assert done.exit_code == 0 and "is current" in done.stdout
    assert path.read_bytes() == before and backups(launcher_home) == []


def test_a_failed_validation_after_the_v3_write_restores_the_original_bytes(write_config, launcher_home):
    path = write_config(V2)
    original = path.read_bytes()
    plan = plan_migration(config_target())
    assert plan.status == "pending"
    plan.target = dataclasses.replace(plan.target, validate=lambda data: ["broke after the write"])
    result = apply_migration(plan)
    assert not result.ok and result.restored
    assert path.read_bytes() == original
    assert result.backup.read_bytes() == original


def test_a_v2_config_is_refused_everywhere_else_with_a_pointer_to_config_migrate(write_config, make_repo):
    from commodore_launcher.config import ConfigError, load_config

    write_config(V2)
    with pytest.raises(ConfigError) as exc:
        load_config()
    assert getattr(exc.value, "code", None) == "config_outdated" and "config migrate" in str(exc.value)
    # A command that reads the config through `load_config` says the same, and `config migrate` is still allowed.
    shown = runner.invoke(app, ["profile", "which", str(make_repo("one")), "--offline"])
    assert shown.exit_code == 1 and "config migrate" in shown.output


def test_the_config_docstring_no_longer_says_old_files_are_valid():
    """Version 1 and 2 files are refused by `load_config` and migrated explicitly (ADR 0019)."""
    assert "files are still valid" not in Path(config_module.__file__).read_text()


# --- 14.2: through the first-run copy -------------------------------------------------------------------


@pytest.fixture
def copy_homes(fake_default_home, monkeypatch):
    monkeypatch.delenv("COMMODORE_LAUNCHER_HOME", raising=False)
    return fake_default_home / ".agent-launcher", fake_default_home / ".commodore-launcher"


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _copy_lines(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if line.strip() and not line.startswith("debug:")]  # V2 has debug on


def test_the_copy_of_a_v2_home_is_migrated_to_v3_and_the_old_home_is_unchanged(copy_homes):
    old, new = copy_homes
    old.mkdir()
    (old / "config.json").write_text(json.dumps(V2, indent=2))
    before = _tree(old)
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0, result.output
    assert json.loads((new / "config.json").read_text()) == V3
    assert _tree(old) == before
    assert not (new / "backups").exists()  # the old home is the backup
    assert len(_copy_lines(result.stderr)) == 1


def test_the_copy_of_a_v1_home_is_migrated_through_to_v3(copy_homes):
    old, new = copy_homes
    old.mkdir()
    (old / "config.json").write_text(json.dumps(V1))
    assert runner.invoke(app, ["version"]).exit_code == 0
    assert json.loads((new / "config.json").read_text()) == {**V1, "version": 3}


def test_the_copy_of_a_v3_home_keeps_the_bytes(copy_homes):
    old, new = copy_homes
    old.mkdir()
    raw = json.dumps(V3).encode() + b"\n"
    (old / "config.json").write_bytes(raw)
    assert runner.invoke(app, ["version"]).exit_code == 0
    assert (new / "config.json").read_bytes() == raw


def test_a_copy_whose_migrated_config_fails_validation_keeps_the_original_bytes(copy_homes):
    old, new = copy_homes
    old.mkdir()
    raw = json.dumps({"version": 2, "profiles": {"w": {"agents": {"claude": {}}, "harnesses": {"codex": {}}}}}).encode()
    (old / "config.json").write_bytes(raw)
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0, result.output  # the copy still succeeds, and so does the command
    assert (new / "config.json").read_bytes() == raw
    lines = _copy_lines(result.stderr)
    assert len(lines) == 1 and "config migrate" in lines[0]
    assert not (new / "backups").exists()


def test_a_copy_whose_migrated_config_is_invalid_for_another_reason_keeps_the_original_bytes(copy_homes):
    """The step succeeds here (nothing to rename); the migrated file then fails validation, which is a different path."""
    old, new = copy_homes
    old.mkdir()
    raw = json.dumps({"version": 2, "debug": "yes"}).encode()
    (old / "config.json").write_bytes(raw)
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0, result.output
    assert (new / "config.json").read_bytes() == raw
    lines = _copy_lines(result.stderr)
    assert len(lines) == 1 and "config migrate" in lines[0]


def test_a_copy_of_a_config_that_is_not_json_keeps_the_original_bytes(copy_homes):
    old, new = copy_homes
    old.mkdir()
    (old / "config.json").write_bytes(b"{ not json")
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0, result.output
    assert (new / "config.json").read_bytes() == b"{ not json"


# --- 14.3: the unknown `agents` key ---------------------------------------------------------------------


def test_config_validate_names_harnesses_for_a_profile_agents_key(write_config):
    write_config({"version": 3, "profiles": {"p": {"agents": {"claude": {}}}}})
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 1
    assert "profiles.p.agents" in result.output
    assert "unknown field 'agents'" in result.output
    assert "did you mean 'harnesses'" in result.output


def test_the_unknown_field_suggestions_follow_the_renamed_fields():
    def messages(profile):
        errors, _ = validate_data({"version": 3, "profiles": {"p": profile}})
        return {e.field: e.message for e in errors}

    assert "did you mean 'default_harness'" in messages({"default_agent": "claude"})["profiles.p.default_agent"]
    assert "did you mean 'default_harness'" in messages({"default": "claude"})["profiles.p.default"]
    assert "did you mean 'harnesses'" in messages({"instances": {}})["profiles.p.instances"]
    inside = messages({"harnesses": {"claude": {"environment": {}}}})
    assert "did you mean 'env'" in inside["profiles.p.harnesses.claude.environment"]


def test_the_old_field_names_are_not_fields_any_more():
    assert "agents" not in config_module.Profile.model_fields and "harnesses" in config_module.Profile.model_fields
    assert "default_agent" not in config_module.Profile.model_fields
    fields = config_module.Config.model_fields
    assert {"harness_types", "harness_selection"} <= set(fields) and not {"agent_types", "agent_selection"} & set(fields)


# --- 14.4: no user-facing "agent" where the harness is meant ---------------------------------------------

AGENT = re.compile(r"(?<![A-Za-z])agents?(?![A-Za-z])", re.IGNORECASE)
"""Letter boundaries, not `\\b`: `default_agent` and `--remove-agent` must match, `AGENTS.md` is handled below."""

ALLOWED = re.compile(
    "|".join(
        [
            r"agent-launcher",  # the old product name: the old home, `refs/agent-launcher/`, the repository URL
            r"AGENTS\.md",  # the repository convention file
            r"\.agents/",  # the `.agents/skills` directory that some harnesses read skills from
            r"agent skills?",  # "agent skill" is a published term (the Agent Skills format), not the harness
            r"agent-teammates",
        ]
    ),
    re.IGNORECASE,
)


def agent_uses(text: str) -> list[str]:
    return [m.group(0) for m in AGENT.finditer(ALLOWED.sub(" ", text))]


def _command_texts(command, path=()):
    where = " ".join(path) or "commodore"
    yield where, command.help or ""
    yield where, getattr(command, "short_help", None) or ""
    for param in command.params:
        yield f"{where} {param.name}", " ".join([getattr(param, "help", None) or "", *param.opts, *getattr(param, "secondary_opts", [])])
        yield f"{where} {param.name} metavar", getattr(param, "metavar", None) or ""
    for name, sub in getattr(command, "commands", {}).items():
        yield from _command_texts(sub, (*path, name))


def test_no_command_help_says_agent_for_the_harness():
    root = typer.main.get_command(app)
    offenders = {where: agent_uses(text) for where, text in _command_texts(root) if agent_uses(text)}
    assert offenders == {}


def _walk(command, path=()):
    yield path, command
    for name, sub in getattr(command, "commands", {}).items():
        yield from _walk(sub, (*path, name))


def _doctor_text(report, scrub: str = "") -> list[str]:
    out = []
    for check in report.checks:
        for part in (check.id, check.name, check.detail, check.remediation or ""):
            out.append(part.replace(scrub, "") if scrub else part)
    return out


def test_doctor_check_ids_names_and_messages_say_harness(write_config, tmp_path):
    exe = tmp_path / "claude-work"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    scenarios = []
    system = FakeSystem()
    scenarios.append(run_doctor(runner=system.run, which=system.which))  # no config: defaults apply
    missing = FakeSystem(installed={"git"})
    scenarios.append(run_doctor(runner=missing.run, which=missing.which))  # tools missing
    write_config(
        {
            "version": 3,
            "profiles": {
                "work": {"default_harness": "claude", "harnesses": {"claude": {"executable": str(exe)}}},
                "personal": {"harnesses": {"codex": {"executable": str(tmp_path / "missing")}}},  # missing, no default
            },
        }
    )
    scenarios.append(run_doctor(runner=system.run, which=system.which))
    texts = [t for report in scenarios for t in _doctor_text(report, scrub=str(tmp_path))]
    assert [t for t in texts if agent_uses(t)] == []
    ids = {c.id for report in scenarios for c in report.checks}
    assert {"harness:claude", "harness:codex", "harness:copilot"} <= ids
    assert not {i for i in ids if i.startswith("agent:")}


def _comment_nodes(tree: ast.AST) -> set[int]:
    """Bare string statements: module, class and function docstrings and attribute docstrings. Not user-facing."""
    return {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}


SQL = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|PRAGMA|WITH)\b|\bFROM\b|\bWHERE\b", re.MULTILINE)
COLUMN_LIST = re.compile(r"^[a-z_.]+(, ?[a-z_.]+)+$")


def test_no_source_string_says_agent_for_the_harness():
    """Prose literals only: prompts, errors, notices, trace messages. Docstrings are covered by the `--help` walk.

    Skipped on purpose: comments and docstrings; SQL and column lists (the state database's columns keep their
    names); strings with no whitespace (JSON keys, error codes and ids: the CLI tests pin those); `migrations.py`,
    which must name the old spellings.
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "migrations.py":
            continue
        tree = ast.parse(path.read_text())
        comments = _comment_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in comments:
                text = node.value
                if not re.search(r"\s", text.strip()) or SQL.search(text) or COLUMN_LIST.match(text.strip()):
                    continue
                if agent_uses(text):
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno}: {text.strip()[:70]}")
    assert offenders == []


def test_the_bundled_management_skill_says_harness():
    offenders = []
    for path in sorted((SRC / "skills").rglob("*.md")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if agent_uses(line):
                offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()[:70]}")
    assert offenders == []


def test_the_bundled_skill_describes_the_v3_keys():
    text = "\n".join(p.read_text() for p in (SRC / "skills").rglob("*.md"))
    for key in ("harnesses", "default_harness", "harness_selection", "--harness"):
        assert key in text, key


# --- fix round 1: a version 2 file is outdated, not broken ---------------------------------------------------


def test_config_validate_on_a_typical_v2_file_exits_0_with_a_version_warning(write_config):
    write_config(V2)
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0, result.output
    assert "warning: version: " in result.stderr and "config migrate" in result.stderr
    assert "agent_types" not in result.output and "agents" not in result.output  # only future_setting is unknown
    as_json = json.loads(runner.invoke(app, ["config", "validate", "--json"]).stdout)
    assert as_json["valid"] is True and "config migrate" in as_json["outdated"]


def test_doctor_on_a_typical_v2_file_warns_with_the_migrate_remediation(write_config):
    write_config(V2)
    system = FakeSystem()
    report = run_doctor(runner=system.run, which=system.which)
    check = next(c for c in report.checks if c.id == "config")
    assert check.status == "warn" and "config migrate" in (check.remediation or "")


def test_a_v2_file_whose_migration_would_be_invalid_reports_those_errors(write_config):
    write_config({**V2, "debug": "yes"})
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 1 and "debug" in result.output
    both = {"version": 2, "profiles": {"w": {"agents": {"claude": {}}, "harnesses": {}}}}
    errors, _ = validate_data(both)
    assert [e.field for e in errors] == ["version"] and "agents" in errors[0].message


def test_version_0_is_never_called_outdated():
    assert config_module.outdated_version({"version": 0}) is None
    assert config_module.outdated_version({"version": True}) is None
    assert config_module.outdated_version({"version": 3}) is None
    assert config_module.outdated_version({"version": 1}) == 1 and config_module.outdated_version([]) is None


def test_version_0_gets_the_same_answer_from_load_edit_and_validate(write_config):
    from commodore_launcher.config import ConfigError, load_config

    write_config({"version": 0})
    with pytest.raises(ConfigError) as exc:
        load_config()
    assert getattr(exc.value, "code", None) != "config_outdated"
    assert runner.invoke(app, ["config", "validate"]).exit_code == 1


def test_interactive_setup_on_a_v2_file_refuses_before_asking(write_config):
    from commodore_launcher.wizard import SetupError, run_setup

    write_config(V2)

    class NoQuestions:
        def __getattr__(self, name):
            raise AssertionError(f"asked: {name}")

    with pytest.raises(SetupError) as exc:
        run_setup(prompter=NoQuestions())
    assert exc.value.code == "config_outdated"


def test_diagnostics_export_hides_the_env_of_a_v2_config_too(write_config, tmp_path):
    import tarfile

    write_config(V2 | {"profiles": {"w": {"agents": {"claude": {"env": {"GIT_AUTHOR_EMAIL": "alice@acme.example"}}}}}})
    out = tmp_path / "bundle.tar.gz"
    assert runner.invoke(app, ["diagnostics", "export", "--output", str(out)]).exit_code == 0
    with tarfile.open(out) as tar:
        text = tar.extractfile("config.json").read().decode()
    assert "alice@acme.example" not in text


def test_a_failed_copy_migration_changes_nothing_else(copy_homes):
    old, new = copy_homes
    old.mkdir()
    config = {
        "version": 2,
        "profiles": {"w": {"agents": {"claude": {}}, "harnesses": {"codex": {}}}},  # cannot migrate
        "repositories": {"search_roots": ["~/code"]},
    }
    (old / "config.json").write_text(json.dumps(config))
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0, result.output
    copied = json.loads((new / "config.json").read_text())
    assert copied == config  # still version 2, still both spellings
    (line,) = _copy_lines(result.stderr)
    assert line.count("config migrate") == 1


# --- fix round 2 ----------------------------------------------------------------------------------------

BAD_FALLBACK = {"version": 2, "workflow_routing": {"fallback": "nope"}}


def test_update_config_refuses_an_outdated_file_and_writes_nothing(write_config):
    from commodore_launcher.config import ConfigOutdatedError, update_config

    path = write_config({"version": 2, "debug": False})
    before = path.read_bytes()
    with pytest.raises(ConfigOutdatedError) as exc:
        update_config({"debug": True})
    assert exc.value.code == "config_outdated" and path.read_bytes() == before


def test_the_rename_conflict_says_what_to_do_next_in_validate_and_migrate(write_config):
    write_config({"version": 2, "profiles": {"w": {"agents": {"claude": {}}, "harnesses": {}}}})
    for argv in (["config", "validate"], ["config", "migrate", "--dry-run"]):
        result = runner.invoke(app, argv)
        assert result.exit_code == 1 and "keep one of them and run `commodore config migrate` again" in result.output, argv


def test_the_failed_copy_note_is_true_and_names_config_migrate_once(copy_homes):
    old, new = copy_homes
    old.mkdir()
    (old / "config.json").write_text(json.dumps({"version": 2, "profiles": {"w": {"agents": {}, "harnesses": {}}}}))
    (line,) = _copy_lines(runner.invoke(app, ["version"]).stderr)
    assert line.count("config migrate") == 1 and "copied at that version" in line and "copied as it was" not in line


def test_diagnostics_export_hides_env_under_every_spelling_of_env(write_config, tmp_path):
    import tarfile

    spellings = [a for a, target in config_module._ALIASES.items() if target == "env"]
    assert {"environment", "environment_variables", "envs"} <= set(spellings)
    instances = {f"h{i}": {key: {"GIT_AUTHOR_EMAIL": f"alice{i}@acme.example"}} for i, key in enumerate(spellings)}
    write_config({"version": 2, "profiles": {"w": {"agents": instances}}})
    out = tmp_path / "bundle.tar.gz"
    assert runner.invoke(app, ["diagnostics", "export", "--output", str(out)]).exit_code == 0
    with tarfile.open(out) as tar:
        text = tar.extractfile("config.json").read().decode()
    assert "acme.example" not in text
