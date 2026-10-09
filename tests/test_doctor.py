import json
import sqlite3
import stat

import pytest
from typer.testing import CliRunner

from agent_launcher import state
from agent_launcher.cli import app
from agent_launcher.doctor import CommandError, CommandResult, run_command, run_doctor

runner = CliRunner()


class FakeSystem:
    """Stands in for PATH lookup and process execution: tests never touch real tools."""

    def __init__(self, installed=None, outputs=None):
        self.installed = installed if installed is not None else {"git", "gh", "cmux", "claude", "codex", "copilot"}
        self.outputs = outputs or {}
        self.calls = []

    def which(self, name):
        return f"/fake/bin/{name}" if name in self.installed else None

    def run(self, argv, timeout):
        self.calls.append((tuple(argv), timeout))
        out = self.outputs.get(tuple(argv[1:]))
        if isinstance(out, Exception):
            raise out
        return out or CommandResult(0, f"{argv[0].rsplit('/', 1)[-1]} 1.0\n", "")


def doctor(system=None, **kwargs):
    system = system or FakeSystem()
    report = run_doctor(runner=system.run, which=system.which, **kwargs)
    return {c.id: c for c in report.checks}, report


def test_all_good():
    checks, report = doctor()
    assert report.ok
    assert checks["git"].status == "pass" and "git 1.0" in checks["git"].detail
    assert checks["gh-auth"].status == "pass"
    assert checks["python"].status == "pass"
    assert {"agent:claude", "agent:codex", "agent:copilot"} <= set(checks)


def test_only_read_only_commands_with_timeouts():
    system = FakeSystem()
    doctor(system)
    allowed = {("--version",), ("auth", "status"), ("extension", "list")}
    assert system.calls
    for argv, timeout in system.calls:
        assert argv[1:] in allowed and 0 < timeout <= 10


def test_missing_git_fails_with_remediation():
    checks, report = doctor(FakeSystem(installed={"gh"}))
    assert checks["git"].status == "fail" and checks["git"].remediation
    assert not report.ok


def test_optional_tools_warn_when_missing():
    checks, report = doctor(FakeSystem(installed={"git"}))
    for id_ in ("gh", "gh-auth", "cmux", "gh-dash", "agent:claude"):
        assert checks[id_].status == "warn", id_
        assert checks[id_].remediation
    assert report.ok


def test_gh_auth_failure_is_a_warning_with_hint():
    system = FakeSystem(outputs={("auth", "status"): CommandResult(1, "", "You are not logged into any GitHub hosts.")})
    checks, _ = doctor(system)
    assert checks["gh-auth"].status == "warn" and "gh auth login" in checks["gh-auth"].remediation


def test_timeouts_are_reported_not_raised():
    system = FakeSystem(outputs={("auth", "status"): CommandError("timed out after 5s"), ("--version",): CommandError("timed out")})
    checks, _ = doctor(system)
    assert checks["gh-auth"].status == "warn" and "timed out" in checks["gh-auth"].detail
    assert checks["git"].status == "pass" and "version not reported" in checks["git"].detail


def test_gh_dash_as_gh_extension():
    ext = CommandResult(0, "gh dash\tdlvhdr/gh-dash\tv4.0\n", "")
    checks, _ = doctor(FakeSystem(outputs={("extension", "list"): ext}))
    assert checks["gh-dash"].status == "pass" and "extension" in checks["gh-dash"].detail


def test_gh_dash_as_standalone_binary():
    checks, _ = doctor(FakeSystem(installed={"git", "gh", "gh-dash"}))
    assert checks["gh-dash"].status == "pass" and "standalone" in checks["gh-dash"].detail


def test_gh_dash_missing():
    checks, _ = doctor(FakeSystem(outputs={("extension", "list"): CommandResult(0, "gh copilot\tgithub/gh-copilot\n", "")}))
    assert checks["gh-dash"].status == "warn"


def test_python_too_old():
    checks, report = doctor(python_version=(3, 10, 4))
    assert checks["python"].status == "fail" and not report.ok


def test_invalid_config_fails_and_profiles_not_checked(write_config):
    write_config({"version": 2, "profiles": {"work": {"envs": {}}}})
    checks, report = doctor()
    assert checks["config"].status == "fail" and "did you mean 'env'" not in checks["config"].detail
    assert "whole config" in checks["config"].remediation
    assert checks["profiles"].status == "warn" and "not checked" in checks["profiles"].detail
    assert not report.ok


def test_unknown_top_level_field_warns(write_config):
    write_config({"version": 2, "future": 1})
    assert doctor()[0]["config"].status == "warn"


def test_no_profiles_warns():
    assert doctor()[0]["profiles"].status == "warn"


def test_profile_agent_executables_use_resolve_agent(write_config, tmp_path):
    good = tmp_path / "claude-work"
    good.write_text("#!/bin/sh\n")
    good.chmod(0o755)
    write_config(
        {
            "version": 2,
            "profiles": {
                "work": {"default_agent": "claude", "agents": {"claude": {"executable": str(good)}}},
                "personal": {"agents": {"codex": {"executable": str(tmp_path / "missing")}}},
            },
        }
    )
    checks, report = doctor()
    assert checks["profile:work:claude"].status == "pass" and str(good) in checks["profile:work:claude"].detail
    bad = checks["profile:personal:codex"]
    assert bad.status == "fail" and "does not exist" in bad.detail and "profile edit personal" in bad.remediation
    assert checks["profile:personal"].status == "warn"  # no default agent
    assert not report.ok


def test_database_missing_ok_corrupt_fails(launcher_home):
    assert doctor()[0]["database"].status == "pass"
    launcher_home.mkdir(parents=True)
    db = launcher_home / "state.db"
    state.connect(db).close()
    assert doctor()[0]["database"].status == "pass"
    db.write_bytes(b"this is not a database" * 100)
    assert doctor()[0]["database"].status == "fail"


def test_doctor_does_not_create_or_modify_the_database(launcher_home):
    launcher_home.mkdir(parents=True)
    db = launcher_home / "state.db"
    sqlite3.connect(db).close()
    before = db.read_bytes()
    doctor()
    assert db.read_bytes() == before


def test_details_are_redacted():
    out = CommandResult(0, "Logged in as bob (token: gho_" + "a" * 36 + ")", "")
    checks, _ = doctor(FakeSystem(outputs={("auth", "status"): out}))
    assert "gho_" not in checks["gh-auth"].detail


def test_run_command_missing_binary_and_timeout(tmp_path):
    with pytest.raises(CommandError):
        run_command([str(tmp_path / "nope")], 1)
    script = tmp_path / "slow"
    script.write_text("#!/bin/sh\nsleep 5\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(CommandError, match="timed out"):
        run_command([str(script)], 0.2)


def test_cli_doctor_json_and_exit_codes(monkeypatch):
    system = FakeSystem()
    monkeypatch.setattr("agent_launcher.cli.run_doctor", lambda: run_doctor(runner=system.run, which=system.which))
    result = runner.invoke(app, ["doctor", "--json"])
    data = json.loads(result.stdout)
    assert result.exit_code == 0 and data["ok"] is True
    assert {"id", "name", "status", "detail", "remediation"} == set(data["checks"][0])
    assert data["summary"] == {"passed": 11, "warnings": 2, "failures": 0}
    assert sum(data["summary"].values()) == len(data["checks"])

    broken = FakeSystem(installed=set())
    monkeypatch.setattr("agent_launcher.cli.run_doctor", lambda: run_doctor(runner=broken.run, which=broken.which))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "[FAIL] git" in result.stdout and "->" in result.stdout


def test_database_with_uri_special_characters_in_path(tmp_path):
    odd = tmp_path / "a#b?c%d"
    odd.mkdir()
    db = odd / "state.db"
    state.connect(db).close()
    checks, _ = doctor(db_path=db)
    assert checks["database"].status == "pass"


def test_database_runs_against_the_real_schema(launcher_home):
    db = launcher_home / "state.db"
    state.connect(db).close()
    check = doctor()[0]["database"]
    assert check.status == "pass" and f"schema version {state.SCHEMA_VERSION}" in check.detail

    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE profile_associations")
    conn.commit()
    conn.close()
    check = doctor()[0]["database"]
    assert check.status == "fail" and "profile_associations" in check.detail


def test_database_from_a_newer_release_fails_and_is_untouched(launcher_home):
    db = launcher_home / "state.db"
    state.connect(db).close()
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {state.SCHEMA_VERSION + 1}")
    conn.close()
    check = doctor()[0]["database"]
    assert check.status == "fail" and "Upgrade" in check.remediation


def test_older_database_warns_that_it_will_be_upgraded(launcher_home):
    db = launcher_home / "state.db"
    state.connect(db).close()
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 0")
    conn.close()
    assert doctor()[0]["database"].status == "warn"
