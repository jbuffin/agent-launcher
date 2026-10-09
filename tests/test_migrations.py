"""Config/workflow migrations and `config edit`, against synthetic old-version files in a temp launcher home."""

import json
import stat

import pytest
from typer.testing import CliRunner

from agent_launcher import cli, config as config_module, migrations, state
from agent_launcher.associations import set_profile
from agent_launcher.cli import app
from agent_launcher.config import read_raw
from agent_launcher.config_edit import edit_config, editor_argv
from agent_launcher.errors import LauncherError
from agent_launcher.migrations import MigrationTarget, apply_migration, plan_migration
from agent_launcher.repositories import identify_reference
from scripted import ScriptedPrompter

runner = CliRunner()


def run(*args):
    return runner.invoke(app, list(args))


V1 = {"version": 1, "debug": True, "future_setting": {"keep": [1, 2]}, "terminal": {"adapter": "mock"}}


def backups(home):
    return sorted((home / "backups").glob("*.json")) if (home / "backups").exists() else []


# --- v1 -> v2 through the real framework -----------------------------------------------------------------


def test_dry_run_shows_the_plan_and_writes_nothing(write_config, launcher_home):
    path = write_config(V1)
    before = path.read_bytes()
    done = run("config", "migrate", "--dry-run")
    assert done.exit_code == 0, done.output
    assert "would migrate from version 1 to 2" in done.stdout
    assert '-  "version": 1' in done.stdout and '+  "version": 2' in done.stdout
    assert path.read_bytes() == before
    assert backups(launcher_home) == []


def test_dry_run_json(write_config):
    write_config(V1)
    data = json.loads(run("config", "migrate", "--dry-run", "--json").stdout)
    entry = next(f for f in data["files"] if f["file"] == "config.json")
    assert (entry["status"], entry["from_version"], entry["to_version"], entry["significant"]) == ("pending", 1, 2, False)


def test_migrate_backs_up_then_writes_and_keeps_unknown_fields(write_config, launcher_home):
    path = write_config(V1)
    original = path.read_bytes()
    done = run("config", "migrate")
    assert done.exit_code == 0, done.output
    new = read_raw(path)
    assert new["version"] == 2
    assert {k: v for k, v in new.items() if k != "version"} == {k: v for k, v in V1.items() if k != "version"}
    [backup] = backups(launcher_home)
    assert backup.name.startswith("config.") and backup.read_bytes() == original
    assert config_module.validate_config(path).valid


def test_migrate_twice_is_a_noop(write_config, launcher_home):
    write_config(V1)
    run("config", "migrate")
    again = run("config", "migrate")
    assert again.exit_code == 0 and "is current" in again.stdout
    assert len(backups(launcher_home)) == 1


def test_file_newer_than_the_code_is_refused_untouched(write_config, launcher_home):
    path = write_config({"version": 99, "x": 1})
    before = path.read_bytes()
    done = run("config", "migrate")
    assert done.exit_code == 1
    assert "newer than this release supports" in done.output
    assert path.read_bytes() == before and backups(launcher_home) == []


def test_missing_version_and_bad_json_are_refused(write_config):
    path = write_config({"debug": True})
    assert run("config", "migrate").exit_code == 1
    path.write_text("{nope")
    done = run("config", "migrate")
    assert done.exit_code == 1 and "not valid JSON" in done.output
    assert path.read_text() == "{nope"


def test_no_files_is_fine(launcher_home):
    done = run("config", "migrate")
    assert done.exit_code == 0 and "not present" in done.stdout


def test_old_workflows_file_is_handled_by_the_same_framework(write_config, launcher_home):
    write_config({"version": 2})
    (launcher_home / "workflows.json").write_text(json.dumps({"version": 1, "workflows": []}))
    done = run("config", "migrate", "--dry-run", "--json")
    flows = next(f for f in json.loads(done.stdout)["files"] if f["file"] == "workflows.json")
    assert flows["status"] == "current"


# --- associations survive (SPEC safety test 5) -----------------------------------------------------------


def test_repository_associations_survive_migration(write_config, make_repo, fake_github):
    fake_github.add("o/one", 3)
    write_config({"version": 1, "profiles": {"work": {}, "personal": {}}})
    repo = make_repo("one", "https://github.com/o/one")
    with state.open_state() as conn:
        set_profile(conn, identify_reference(str(repo)), "work")

    def stored():
        with state.open_state() as conn:
            return conn.execute("SELECT repository_id, profile FROM profile_associations").fetchall()

    before = stored()
    assert before and run("config", "migrate").exit_code == 0
    assert stored() == before
    assert json.loads(run("profile", "which", str(repo), "--json").stdout)["profile"] == "work"


# --- significant changes, validation failure, restore (synthetic targets) --------------------------------


def synthetic(path, *, valid_after=True):
    def to_v2(data):
        return {**data, "version": 2, "renamed": data.pop("old_name", None)}

    def validate(data):
        return [] if valid_after else ["synthetic problem"]

    return MigrationTarget("syn.json", path, 2, {1: to_v2}, validate)


def test_significant_change_is_detected_and_unknown_fields_survive(tmp_path):
    path = tmp_path / "syn.json"
    path.write_text(json.dumps({"version": 1, "old_name": "a", "unknown": {"x": 1}}))
    plan = plan_migration(synthetic(path))
    assert plan.status == "pending" and plan.significant
    assert "renamed" in plan.changed_keys and plan.new["unknown"] == {"x": 1}


def test_failed_validation_restores_the_original_bytes(launcher_home, tmp_path):
    path = tmp_path / "syn.json"
    # Not canonical JSON formatting, so a restore that re-serialised would be caught.
    path.write_bytes(b'{"version":1,   "k": 1}\n')
    original = path.read_bytes()
    target = synthetic(path)
    plan = plan_migration(target)
    assert plan.status == "pending"
    result = apply_migration(plan_with_validator(plan, lambda data: ["broke after write"]))  # valid when planned, not on disk
    assert not result.ok and result.restored
    assert path.read_bytes() == original
    assert result.backup.read_bytes() == original


def plan_with_validator(plan, validator):
    plan.target = MigrationTarget(plan.target.name, plan.target.path, plan.target.current_version, plan.target.steps, validator)
    return plan


def test_a_migration_that_would_produce_an_invalid_file_writes_nothing(launcher_home, tmp_path):
    path = tmp_path / "syn.json"
    path.write_text(json.dumps({"version": 1}))
    plan = plan_migration(synthetic(path, valid_after=False))
    assert plan.status == "error" and "nothing is written" in plan.problems[0]
    with pytest.raises(LauncherError):
        apply_migration(plan)
    assert json.loads(path.read_text()) == {"version": 1} and backups(launcher_home) == []


def test_cli_significant_migration_needs_confirmation(monkeypatch, tmp_path, launcher_home):
    path = tmp_path / "syn.json"
    path.write_text(json.dumps({"version": 1, "old_name": "a"}))
    monkeypatch.setattr(migrations, "default_targets", lambda: [synthetic(path)])
    monkeypatch.setattr(cli, "is_interactive", lambda: False)
    refused = run("config", "migrate")
    assert refused.exit_code == 1 and "--yes" in refused.output
    assert json.loads(path.read_text())["version"] == 1 and backups(launcher_home) == []

    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    no = ScriptedPrompter(("confirm", "migrate", False))
    monkeypatch.setattr(cli, "make_prompter", lambda: no)
    assert run("config", "migrate").exit_code == 130
    assert json.loads(path.read_text())["version"] == 1

    yes = ScriptedPrompter(("confirm", "migrate", True))
    monkeypatch.setattr(cli, "make_prompter", lambda: yes)
    assert run("config", "migrate").exit_code == 0
    assert json.loads(path.read_text())["version"] == 2


def test_cli_yes_applies_a_significant_migration(monkeypatch, tmp_path, launcher_home):
    path = tmp_path / "syn.json"
    path.write_text(json.dumps({"version": 1, "old_name": "a"}))
    monkeypatch.setattr(migrations, "default_targets", lambda: [synthetic(path)])
    monkeypatch.setattr(cli, "is_interactive", lambda: False)
    assert run("config", "migrate", "--yes").exit_code == 0
    assert json.loads(path.read_text())["version"] == 2 and len(backups(launcher_home)) == 1


def test_backups_never_overwrite_each_other(tmp_path, launcher_home):
    path = tmp_path / "syn.json"
    path.write_text("{}")
    first, second = migrations.make_backup(path), migrations.make_backup(path)
    assert first != second and first.exists() and second.exists()


# --- config edit -----------------------------------------------------------------------------------------


@pytest.fixture
def fake_editor(tmp_path, monkeypatch):
    """An editor script that replaces the file with the next text in a queue (one file per edit session)."""
    queue = tmp_path / "editor-queue"
    queue.mkdir()
    script = tmp_path / "fake-editor.sh"
    script.write_text(
        '#!/bin/sh\nnext=$(ls "$EDITOR_QUEUE" | sort | head -n 1)\n'
        '[ -z "$next" ] && exit 0\ncat "$EDITOR_QUEUE/$next" > "$1"\nrm "$EDITOR_QUEUE/$next"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("EDITOR_QUEUE", str(queue))
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", str(script))
    counter = {"n": 0}

    def push(content):
        counter["n"] += 1
        text = content if isinstance(content, str) else json.dumps(content)
        (queue / f"{counter['n']:03d}").write_text(text)

    return push


def test_edit_saves_a_valid_result_atomically(write_config, fake_editor):
    path = write_config({"version": 2, "debug": False})
    fake_editor({"version": 2, "debug": True, "extra": 1})
    outcome = edit_config(None)
    assert outcome.status == "saved" and outcome.warnings == ["extra: unknown field"]
    assert read_raw(path) == {"version": 2, "debug": True, "extra": 1}
    assert not list(path.parent.glob(".config.json.*"))


def test_edit_unchanged_writes_nothing(write_config, fake_editor):
    path = write_config('{"version": 2}')
    stat_before = path.stat().st_mtime_ns
    assert edit_config(None).status == "unchanged"
    assert path.stat().st_mtime_ns == stat_before


def test_edit_invalid_then_discard_never_writes(write_config, fake_editor):
    path = write_config({"version": 2})
    before = path.read_text()
    fake_editor({"version": 2, "debug": "not a bool"})
    prompter = ScriptedPrompter(("select", "not valid", "discard"))
    assert edit_config(prompter).status == "discarded"
    assert path.read_text() == before


def test_edit_invalid_then_reedit_then_saved(write_config, fake_editor):
    path = write_config({"version": 2})
    fake_editor("{broken")
    fake_editor({"version": 2, "debug": True})
    prompter = ScriptedPrompter(("select", "not valid", "edit"))
    assert edit_config(prompter).status == "saved"
    assert read_raw(path)["debug"] is True
    assert any("not valid JSON" in line for line in prompter.said)


def test_edit_without_a_terminal_discards_an_invalid_result(write_config, fake_editor):
    path = write_config({"version": 2})
    before = path.read_text()
    fake_editor({"version": 2, "debug": 5})
    with pytest.raises(LauncherError) as exc:
        edit_config(None)
    assert exc.value.code == "config_invalid" and path.read_text() == before


def test_edit_refuses_a_newer_version_and_non_objects(write_config, fake_editor):
    path = write_config({"version": 2})
    before = path.read_text()
    for bad in ({"version": 50}, [1, 2]):
        fake_editor(bad)
        with pytest.raises(LauncherError):
            edit_config(None)
    assert path.read_text() == before


def test_edit_creates_a_missing_config_from_a_minimal_template(launcher_home, fake_editor):
    fake_editor({"version": 2, "debug": True})
    assert edit_config(None).status == "saved"
    assert read_raw()["debug"] is True


def test_edit_refuses_when_the_file_changed_underneath(write_config, tmp_path, monkeypatch):
    path = write_config({"version": 2})
    script = tmp_path / "racer.sh"
    script.write_text(f'#!/bin/sh\necho \'{{"version": 2, "debug": true}}\' > "$1"\necho \'{{"version": 2, "debug": false, "x": 1}}\' > "{path}"\n')
    script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(script))
    monkeypatch.delenv("VISUAL", raising=False)
    with pytest.raises(LauncherError) as exc:
        edit_config(None)
    assert exc.value.code == "config_changed"
    assert read_raw(path)["x"] == 1


def test_editor_failure_saves_nothing(write_config, tmp_path, monkeypatch):
    path = write_config({"version": 2})
    script = tmp_path / "fail.sh"
    script.write_text('#!/bin/sh\necho \'{"version": 2, "debug": true}\' > "$1"\nexit 3\n')
    script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(script))
    monkeypatch.delenv("VISUAL", raising=False)
    with pytest.raises(LauncherError) as exc:
        edit_config(None)
    assert exc.value.code == "editor_failed" and read_raw(path) == {"version": 2}


def test_editor_selection(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    assert editor_argv() == ["vi"]
    monkeypatch.setenv("EDITOR", "nano -w")
    assert editor_argv() == ["nano", "-w"]
    monkeypatch.setenv("VISUAL", "code --wait")
    assert editor_argv() == ["code", "--wait"]
    monkeypatch.setenv("VISUAL", "'unterminated")
    with pytest.raises(LauncherError):
        editor_argv()


def test_edit_preserves_file_mode_and_cli_reports(write_config, fake_editor, monkeypatch):
    path = write_config({"version": 2})
    path.chmod(0o600)
    fake_editor({"version": 2, "debug": True})
    monkeypatch.setattr(cli, "is_interactive", lambda: False)
    done = run("config", "edit")
    assert done.exit_code == 0 and "Saved" in done.stdout
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    fake_editor({"version": 2, "debug": 9})
    bad = run("config", "edit")
    assert bad.exit_code == 1 and "discarded" in bad.output


# --- review follow-ups -----------------------------------------------------------------------------------


def test_restore_when_the_write_itself_raises(tmp_path, launcher_home, monkeypatch):
    path = tmp_path / "syn.json"
    path.write_bytes(b'{"version":1,  "k": 1}\n')
    original = path.read_bytes()
    plan = plan_migration(synthetic(path))

    def torn_write(target, data):
        target.write_text("{half")
        raise OSError("disk full")

    monkeypatch.setattr(config_module, "write_json_atomic", torn_write)
    result = apply_migration(plan)
    assert not result.ok and result.restored and "writing failed" in result.problems[0]
    assert path.read_bytes() == original


def test_apply_refuses_when_the_file_changed_since_the_plan(tmp_path, launcher_home):
    path = tmp_path / "syn.json"
    path.write_text(json.dumps({"version": 1}))
    plan = plan_migration(synthetic(path))
    path.write_text(json.dumps({"version": 1, "added_meanwhile": True}))
    with pytest.raises(LauncherError) as exc:
        apply_migration(plan)
    assert exc.value.code == "config_changed"
    assert json.loads(path.read_text())["added_meanwhile"] and backups(launcher_home) == []


def test_backup_failure_is_a_structured_error(tmp_path, launcher_home, monkeypatch):
    path = tmp_path / "syn.json"
    path.write_text(json.dumps({"version": 1}))
    plan = plan_migration(synthetic(path))

    def boom(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(migrations.shutil, "copy2", boom)
    with pytest.raises(LauncherError) as exc:
        apply_migration(plan)
    assert exc.value.code == "backup_failed" and json.loads(path.read_text()) == {"version": 1}
    monkeypatch.setattr(migrations, "default_targets", lambda: [synthetic(path)])
    monkeypatch.setattr(cli, "is_interactive", lambda: False)
    done = run("config", "migrate", "--yes")
    assert done.exit_code == 1 and "could not back up" in done.output and "Traceback" not in done.output


def test_edit_missing_file_unchanged_writes_nothing(launcher_home, tmp_path, monkeypatch):
    script = tmp_path / "noop.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(script))
    monkeypatch.delenv("VISUAL", raising=False)
    assert edit_config(None).status == "unchanged"
    assert not (launcher_home / "config.json").exists()


def test_edit_non_utf8_output_is_structured(write_config, tmp_path, monkeypatch):
    path = write_config({"version": 2})
    before = path.read_text()
    script = tmp_path / "latin.sh"
    script.write_text("#!/bin/sh\nprintf '\\377\\376' > \"$1\"\n")
    script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(script))
    monkeypatch.delenv("VISUAL", raising=False)
    with pytest.raises(LauncherError) as exc:
        edit_config(None)
    assert exc.value.code == "config_invalid" and path.read_text() == before


def test_doctor_warns_on_an_old_config_version(write_config):
    from agent_launcher.doctor import check_config

    path = write_config({"version": 1})
    check = check_config(path)
    assert check.status == "warn" and "config migrate" in check.remediation
    write_config({"version": 2})
    assert check_config(path).status == "pass"


def test_outdated_code_is_the_same_for_profiles_and_setup(write_config):
    from agent_launcher.profiles import ConfigOutdatedError, InstanceEdit, add_profile

    write_config({"version": 1})
    with pytest.raises(ConfigOutdatedError) as exc:
        add_profile("work", [InstanceEdit("claude")])
    assert exc.value.code == "config_outdated"
