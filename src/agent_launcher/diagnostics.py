"""`diagnostics export`: a sanitised bundle for bug reports (SPEC §28).

The archive holds the doctor report, the config, system facts and recent logs, all passed
through `redact`. It never includes the database, prompts or conversations.
"""

import io
import copy
import json
import os
import re
import platform
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

from agent_launcher import __version__
from agent_launcher.config import ConfigError, read_raw
from agent_launcher.doctor import DoctorReport
from agent_launcher.logs import logs_dir
from agent_launcher.paths import config_path
from agent_launcher.redact import redact_env_allowlist, redact_text, redact_value


def default_archive_name() -> str:
    return f"agent-launcher-diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"


def _json_bytes(data: Any) -> bytes:
    return (json.dumps(data, indent=2) + "\n").encode("utf-8")


_ACCOUNT = re.compile(r"(?i)(\baccount\s+|\blogged in to \S+ as\s+|\blogged in as\s+)[^\s(]+")


def _redact_config(raw: dict[str, Any], home: str) -> Any:
    """Redact the config; instance env values are hidden unless the name is on the safe list."""
    raw = copy.deepcopy(raw)
    profiles = raw.get("profiles")
    for profile in profiles.values() if isinstance(profiles, dict) else []:
        agents = profile.get("agents") if isinstance(profile, dict) else None
        for instance in agents.values() if isinstance(agents, dict) else []:
            if isinstance(instance, dict) and isinstance(instance.get("env"), dict):
                instance["env"] = redact_env_allowlist(instance["env"], home)
    return redact_value(raw, home)


def _redact_report(report: DoctorReport, home: str) -> Any:
    """The doctor report for export: also hides the GitHub account name (visible in interactive doctor)."""
    data = report.to_dict()
    for check in data["checks"]:
        if check["id"] == "gh-auth":
            check["detail"] = _ACCOUNT.sub(lambda m: f"{m.group(1)}[REDACTED]", check["detail"])
    return redact_value(data, home)


def build_bundle(report: DoctorReport, config_file: Path | None = None) -> dict[str, bytes]:
    """The sanitised files of the bundle, by archive name."""
    home = str(Path.home())
    path = config_file or config_path()
    try:
        raw = read_raw(path)
        config: Any = _redact_config(raw, home) if raw is not None else None
        config_note = None if raw is not None else "no config file"
    except ConfigError as exc:
        config, config_note = None, redact_text(str(exc), home)

    files = {
        "system.json": _json_bytes(
            {
                "agent_launcher": __version__,
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "machine": platform.machine(),
            }
        ),
        "doctor.json": _json_bytes(_redact_report(report, home)),
        "config.json": _json_bytes({"config": config, "note": config_note}),
    }
    log_root = logs_dir()
    if log_root.is_dir():
        for log in sorted(log_root.glob("agent-launcher.log*")):
            if log.is_file():
                text = log.read_text(encoding="utf-8", errors="replace")
                files[f"logs/{log.name}"] = redact_text(text, home).encode("utf-8")
    return files


def export_diagnostics(output: Path, report: DoctorReport, config_file: Path | None = None) -> list[str]:
    """Write the bundle as a .tar.gz at `output` (mode 0600). Returns the member names."""
    files = build_bundle(report, config_file)
    now = time.time()
    output.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL: never overwrite, and the file is private from the moment it exists.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle, tarfile.open(fileobj=handle, mode="w:gz") as tar:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size, info.mtime, info.mode = len(data), now, 0o600
                tar.addfile(info, io.BytesIO(data))
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return list(files)
