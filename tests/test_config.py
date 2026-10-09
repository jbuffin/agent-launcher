import json
import os
from pathlib import Path

import pytest

from agent_launcher import config
from agent_launcher.config import Config, ConfigError, update_config, validate_config


def test_missing_file_is_valid_defaults(launcher_home: Path):
    report = validate_config()
    assert report.valid and not report.exists
    assert config.effective_config() == Config().model_dump()
    assert config.effective_config()["version"] == 2


def test_valid_file(write_config):
    write_config({"version": 1, "debug": True})
    report = validate_config()
    assert report.valid and report.exists and report.unknown_fields == []


def test_wrong_type_names_field(write_config):
    write_config({"version": 1, "debug": "maybe"})
    report = validate_config()
    assert not report.valid
    assert report.errors[0].field == "debug"


def test_missing_version(write_config):
    write_config({"debug": True})
    assert [e.field for e in validate_config().errors] == ["version"]


def test_newer_version_rejected(write_config):
    write_config({"version": 99})
    report = validate_config()
    assert [e.field for e in report.errors] == ["version"]
    assert "newer" in report.errors[0].message


def test_non_integer_version_one_error(write_config):
    write_config({"version": "1"})
    assert [e.field for e in validate_config().errors] == ["version"]


def test_unknown_fields_reported_not_errors(write_config):
    write_config({"version": 1, "colour": "red"})
    report = validate_config()
    assert report.valid
    assert report.unknown_fields == ["colour"]
    assert config.effective_config()["colour"] == "red"


def test_bad_json_and_non_object(write_config):
    write_config("{nope")
    assert validate_config().errors[0].field == "(file)"
    write_config("[1]")
    assert "JSON object" in validate_config().errors[0].message


def test_update_preserves_unrelated_and_unknown(write_config):
    path = write_config({"version": 1, "debug": False, "future_thing": {"a": 1}})
    update_config({"debug": True})
    assert json.loads(path.read_text()) == {"version": 1, "debug": True, "future_thing": {"a": 1}}


def test_update_creates_file_with_version(launcher_home: Path):
    update_config({"debug": True})
    assert json.loads((launcher_home / "config.json").read_text()) == {"version": 2, "debug": True}


def test_invalid_update_writes_nothing(write_config):
    path = write_config({"version": 1})
    before = path.read_text()
    with pytest.raises(ConfigError, match="debug"):
        update_config({"debug": "x"})
    assert path.read_text() == before


def test_failed_write_leaves_original_and_no_temp(write_config, monkeypatch):
    path = write_config({"version": 1})
    before = path.read_text()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        update_config({"debug": True})
    assert path.read_text() == before
    assert [p.name for p in path.parent.iterdir()] == ["config.json"]


def test_non_utf8_bytes_is_config_error(launcher_home: Path):
    launcher_home.mkdir(parents=True)
    (launcher_home / "config.json").write_bytes(b"\xff\xfe\x00{")
    report = validate_config()
    assert not report.valid
    assert "UTF-8" in report.errors[0].message
    assert str(launcher_home / "config.json") in report.errors[0].message


def test_directory_at_config_path_is_config_error(launcher_home: Path):
    (launcher_home / "config.json").mkdir(parents=True)
    report = validate_config()
    assert not report.valid
    assert str(launcher_home / "config.json") in report.errors[0].message


def test_write_preserves_existing_mode(write_config):
    path = write_config({"version": 1})
    path.chmod(0o640)
    update_config({"debug": True})
    assert path.stat().st_mode & 0o777 == 0o640


def test_write_fsyncs_parent_directory(write_config, monkeypatch):
    path = write_config({"version": 1})
    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(os.fstat(fd).st_mode), real_fsync(fd)))
    update_config({"debug": True})
    import stat

    assert any(stat.S_ISDIR(m) for m in synced)
