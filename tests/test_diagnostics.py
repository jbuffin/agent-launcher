import json
import tarfile

from typer.testing import CliRunner

from agent_launcher.cli import app
from agent_launcher.doctor import CommandResult, run_doctor
from agent_launcher.logs import LOG_FILE_NAME, get_logger, logs_dir, setup_logging

runner = CliRunner()
SECRET = "ghp_" + "Z" * 36


def read_archive(path):
    with tarfile.open(path) as tar:
        return {m.name: tar.extractfile(m).read().decode() for m in tar.getmembers()}


def test_export_is_sanitised(write_config, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_launcher.cli.run_doctor",
        lambda: run_doctor(runner=lambda argv, t: CommandResult(0, f"v1 token {SECRET}", ""), which=lambda n: f"/fake/{n}"),
    )
    write_config(
        {
            "version": 2,
            "profiles": {
                "work": {
                    "default_agent": "claude",
                    "agents": {"claude": {"executable": "/fake/claude", "env": {"GH_TOKEN": SECRET}, "args": ["--token", SECRET]}},
                }
            },
        }
    )
    setup_logging()
    get_logger().info(f"started with password=hunter2 and {SECRET}")
    out = tmp_path / "bundle.tar.gz"
    result = runner.invoke(app, ["diagnostics", "export", "--output", str(out)])
    assert result.exit_code == 0, result.output
    files = read_archive(out)
    assert {"system.json", "doctor.json", "config.json"} <= set(files)
    assert any(name.startswith("logs/") for name in files)
    blob = "\n".join(files.values())
    assert SECRET not in blob and "hunter2" not in blob
    assert json.loads(files["config.json"])["config"]["profiles"]["work"]["agents"]["claude"]["env"] == {"GH_TOKEN": "[REDACTED]"}
    assert oct(out.stat().st_mode & 0o777) == "0o600"


def test_export_with_invalid_config_still_works(write_config, tmp_path):
    write_config("{")
    out = tmp_path / "b.tar.gz"
    assert runner.invoke(app, ["diagnostics", "export", "-o", str(out)]).exit_code == 0
    assert json.loads(read_archive(out)["config.json"])["config"] is None


def test_export_refuses_to_overwrite(tmp_path):
    out = tmp_path / "b.tar.gz"
    out.write_text("keep")
    result = runner.invoke(app, ["diagnostics", "export", "-o", str(out)])
    assert result.exit_code == 1 and out.read_text() == "keep"


def test_prompts_in_logged_args_are_not_exported(tmp_path):
    from agent_launcher.redact import redact_args

    setup_logging()
    get_logger().info("argv", extra={"fields": {"args": redact_args(["--prompt", "refactor the billing module"])}})
    out = tmp_path / "b.tar.gz"
    runner.invoke(app, ["diagnostics", "export", "-o", str(out)])
    assert "billing" not in "\n".join(read_archive(out).values())


def test_default_doctor_in_tests_runs_no_real_tools():
    report = run_doctor()
    assert {c.id: c.status for c in report.checks}["git"] == "fail"  # PATH is empty under the fixture


def test_export_hides_instance_env_except_safe_names(write_config, tmp_path):
    write_config(
        {
            "version": 2,
            "profiles": {
                "w": {
                    "agents": {
                        "claude": {
                            "env": {
                                "OPENAI_KEY": "k1",
                                "DB_PASS": "p1",
                                "GITHUB_PAT": "t1",
                                "MY_CUSTOM_THING": "c1",
                                "CLAUDE_CONFIG_DIR": "/opt/claude-w",
                                "PATH": "/usr/bin",
                            }
                        }
                    }
                }
            },
        }
    )
    out = tmp_path / "b.tar.gz"
    assert runner.invoke(app, ["diagnostics", "export", "-o", str(out)]).exit_code == 0
    env = json.loads(read_archive(out)["config.json"])["config"]["profiles"]["w"]["agents"]["claude"]["env"]
    assert env == {
        "OPENAI_KEY": "[REDACTED]",
        "DB_PASS": "[REDACTED]",
        "GITHUB_PAT": "[REDACTED]",
        "MY_CUSTOM_THING": "[REDACTED]",
        "CLAUDE_CONFIG_DIR": "/opt/claude-w",
        "PATH": "/usr/bin",
    }


def test_export_hides_github_account_name_but_doctor_shows_it(monkeypatch, tmp_path):
    status = CommandResult(0, "github.com\n  ✓ Logged in to github.com account octocat (keyring)\n", "")

    def fake_runner(argv, timeout):
        return status if argv[1:] == ["auth", "status"] else CommandResult(0, "v1", "")

    fake = lambda: run_doctor(runner=fake_runner, which=lambda n: f"/fake/{n}")  # noqa: E731
    monkeypatch.setattr("agent_launcher.cli.run_doctor", fake)
    assert "octocat" in runner.invoke(app, ["doctor"]).stdout
    out = tmp_path / "b.tar.gz"
    runner.invoke(app, ["diagnostics", "export", "-o", str(out)])
    assert "octocat" not in "\n".join(read_archive(out).values())


def test_export_is_private_from_creation_and_never_overwrites(tmp_path, monkeypatch):
    import os

    from agent_launcher.diagnostics import export_diagnostics

    modes = []
    real_open = os.open

    def spy(path, flags, mode=0o777, **kw):
        modes.append((flags, mode))
        return real_open(path, flags, mode, **kw)

    monkeypatch.setattr(os, "open", spy)
    monkeypatch.setattr(os, "umask", os.umask)
    out = tmp_path / "x.tar.gz"
    export_diagnostics(out, run_doctor())
    assert modes[0][0] & os.O_EXCL and modes[0][1] == 0o600
    assert (out.stat().st_mode & 0o777) == 0o600
    before = out.read_bytes()
    try:
        export_diagnostics(out, run_doctor())
    except FileExistsError:
        pass
    else:
        raise AssertionError("overwrote existing file")
    assert out.read_bytes() == before


def test_exported_doctor_summary_has_real_counts(monkeypatch, tmp_path):
    monkeypatch.setattr("agent_launcher.cli.run_doctor", lambda: run_doctor(runner=lambda a, t: CommandResult(0, "v1", ""), which=lambda n: f"/fake/{n}"))
    out = tmp_path / "b.tar.gz"
    runner.invoke(app, ["diagnostics", "export", "-o", str(out)])
    data = json.loads(read_archive(out)["doctor.json"])
    assert data["summary"]["passed"] > 0 and data["summary"]["failures"] == 0
    assert "[REDACTED]" not in json.dumps(data["summary"])


def test_doctor_finished_trace_keeps_counts():
    setup_logging(debug=True)
    run_doctor()
    entry = [json.loads(l) for l in (logs_dir() / LOG_FILE_NAME).read_text().splitlines() if "doctor finished" in l][0]
    assert isinstance(entry["passed"], int) and isinstance(entry["failures"], int)


def test_older_gh_output_account_name_is_hidden_in_export(monkeypatch, tmp_path):
    old = CommandResult(0, "github.com\n  \u2713 Logged in to github.com as octocat (/home/x/.config/gh/hosts.yml)\n", "")
    fake_runner = lambda argv, t: old if argv[1:] == ["auth", "status"] else CommandResult(0, "v1", "")  # noqa: E731
    monkeypatch.setattr("agent_launcher.cli.run_doctor", lambda: run_doctor(runner=fake_runner, which=lambda n: f"/fake/{n}"))
    assert "octocat" in runner.invoke(app, ["doctor"]).stdout
    out = tmp_path / "b.tar.gz"
    runner.invoke(app, ["diagnostics", "export", "-o", str(out)])
    assert "octocat" not in "\n".join(read_archive(out).values())
