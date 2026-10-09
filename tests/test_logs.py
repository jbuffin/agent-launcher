import json
import logging

from typer.testing import CliRunner

from agent_launcher.cli import app
from agent_launcher.config import LogSettings
from agent_launcher.logs import LOG_FILE_NAME, get_logger, logs_dir, setup_logging, trace

runner = CliRunner()


def read_log():
    return [json.loads(line) for line in (logs_dir() / LOG_FILE_NAME).read_text().splitlines()]


def test_nothing_is_created_until_something_is_logged(launcher_home):
    setup_logging()
    trace("decision", profile="work")  # debug record, below INFO
    assert not launcher_home.exists()


def test_records_are_json_lines_and_redacted(launcher_home):
    setup_logging()
    get_logger().info("launching with --token abc123", extra={"fields": {"env": {"GH_TOKEN": "x"}, "repo": "o/r"}})
    [entry] = read_log()
    assert entry["level"] == "info" and entry["repo"] == "o/r"
    assert "abc123" not in entry["event"] and entry["env"] == {"GH_TOKEN": "[REDACTED]"}


def test_debug_mode_writes_traces_to_file_and_stderr(capsys):
    setup_logging(debug=True)
    trace("profile selected", profile="work", token="s3cret")
    err = capsys.readouterr().err
    assert "profile selected" in err and 'profile="work"' in err and "s3cret" not in err
    [entry] = read_log()
    assert entry["level"] == "debug" and entry["token"] == "[REDACTED]"


def test_rotation_keeps_configured_number_of_files():
    setup_logging(settings=LogSettings(max_bytes=1024, backup_count=2))
    for i in range(200):
        get_logger().info("x" * 100 + str(i))
    names = sorted(p.name for p in logs_dir().iterdir())
    assert names == [LOG_FILE_NAME, f"{LOG_FILE_NAME}.1", f"{LOG_FILE_NAME}.2"]


def test_setup_is_idempotent():
    setup_logging(debug=True)
    setup_logging(debug=True)
    assert len([h for h in logging.getLogger("agent_launcher").handlers if getattr(h, "_agent_launcher_handler", False)]) == 2


def test_global_debug_flag_traces_agent_resolution(write_config, tmp_path):
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    write_config({"version": 2, "profiles": {"work": {"default_agent": "claude", "agents": {"claude": {"executable": str(exe)}}}}})
    quiet = runner.invoke(app, ["profile", "check", "work"])
    assert quiet.exit_code == 0 and "debug:" not in quiet.stderr
    loud = runner.invoke(app, ["--debug", "profile", "check", "work"])
    assert loud.exit_code == 0
    assert "debug: agent resolved" in loud.stderr and 'profile="work"' in loud.stderr


def test_config_debug_true_enables_tracing(write_config):
    write_config({"version": 2, "debug": True})
    assert "debug: command started" in runner.invoke(app, ["version"]).stderr


def test_config_log_settings_apply(write_config):
    write_config({"version": 2, "logs": {"max_bytes": 2048, "backup_count": 1}})
    result = runner.invoke(app, ["--debug", "version"])
    assert result.exit_code == 0
    handler = [h for h in logging.getLogger("agent_launcher").handlers if hasattr(h, "maxBytes")][0]
    assert (handler.maxBytes, handler.backupCount) == (2048, 1)


def test_fields_cannot_overwrite_core_log_keys():
    setup_logging()
    get_logger().info("real event", extra={"fields": {"ts": "x", "level": "x", "event": "x", "other": 1}})
    [entry] = read_log()
    assert entry["event"] == "real event" and entry["level"] == "info" and entry["ts"] != "x"
    assert entry["field_event"] == "x" and entry["other"] == 1


def test_command_started_trace_names_the_command():
    result = runner.invoke(app, ["--debug", "version"])
    assert 'command="version"' in result.stderr
