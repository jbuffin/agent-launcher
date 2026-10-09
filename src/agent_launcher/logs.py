"""Structured logging and decision tracing (SPEC §28).

Logs are JSON lines in `<home>/logs/agent-launcher.log`, rotated by size with a
configurable number of kept files. Every record is redacted before it is written.
Nothing is written, and the logs directory is not created, until a record is emitted.

`trace()` is the decision-tracing call: it logs at DEBUG, so it reaches the file and
stderr only in debug mode (`--debug`, or `"debug": true` in config.json).
Never pass complete prompts or conversations to the logger.
"""

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from agent_launcher.config import LogSettings
from agent_launcher.paths import launcher_home
from agent_launcher.redact import redact_text, redact_value

LOGGER_NAME = "agent_launcher"
LOG_FILE_NAME = "agent-launcher.log"
_HANDLER_MARK = "_agent_launcher_handler"


def logs_dir() -> Path:
    return launcher_home() / "logs"


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def trace(event: str, **fields: Any) -> None:
    """Record a decision (repository, profile, agent, ... choices). Debug mode only."""
    logging.getLogger(LOGGER_NAME).debug(event, extra={"fields": fields})


def _record_fields(record: logging.LogRecord) -> dict[str, Any]:
    return redact_value(getattr(record, "fields", {}) or {}, str(Path.home()))


_RESERVED = {"ts", "level", "event", "exception"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = {
            (f"field_{k}" if k in _RESERVED else k): v for k, v in _record_fields(record).items()
        }
        entry: dict[str, Any] = {
            **fields,
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "event": redact_text(record.getMessage(), str(Path.home())),
        }
        if record.exc_info:
            entry["exception"] = redact_text(self.formatException(record.exc_info), str(Path.home()))
        return json.dumps(entry, default=str)


class DebugFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = " ".join(f"{k}={json.dumps(v, default=str)}" for k, v in _record_fields(record).items())
        message = redact_text(record.getMessage(), str(Path.home()))
        return f"debug: {message}" + (f" {fields}" if fields else "")


class _LazyRotatingFileHandler(RotatingFileHandler):
    """Creates the logs directory (private to the user) only when the first record is written."""

    def _open(self):  # type: ignore[override]
        Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        return super()._open()


def setup_logging(debug: bool = False, settings: LogSettings | None = None) -> None:
    """(Re)configure logging. Safe to call repeatedly: earlier handlers are removed first."""
    settings = settings or LogSettings()
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            logger.removeHandler(handler)
            handler.close()
    logger.propagate = False
    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    file_handler = _LazyRotatingFileHandler(
        logs_dir() / LOG_FILE_NAME,
        maxBytes=settings.max_bytes,
        backupCount=settings.backup_count,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(JsonFormatter())
    setattr(file_handler, _HANDLER_MARK, True)
    logger.addHandler(file_handler)

    if debug:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(DebugFormatter())
        setattr(stderr_handler, _HANDLER_MARK, True)
        logger.addHandler(stderr_handler)
