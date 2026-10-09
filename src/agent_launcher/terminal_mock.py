"""A terminal adapter that launches nothing and records what it was asked to do."""

import fcntl
import json
import os
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from agent_launcher.config import write_json_atomic
from agent_launcher.terminals import (
    CLOSE_SESSION,
    CREATE_SESSION,
    DISCOVER_SESSIONS,
    FOCUS_SESSION,
    PREPARE_PROMPT,
    RUN_INTERACTIVE,
    SUBMIT_PROMPT,
    CreateSessionRequest,
    CreateSessionResult,
    ExternalSession,
    StaleTerminalSession,
    TerminalAdapter,
    TerminalError,
    TerminalSessionRef,
)

CREATE_DELAY_ENV = "AGENT_LAUNCHER_MOCK_CREATE_DELAY"
"""Seconds `create_session` sleeps, so tests can make concurrent launches overlap."""


class MockTerminalAdapter(TerminalAdapter):
    """Calls are kept in `calls`; with `record_path` they are also appended to a JSON file,
    so a CLI-level test or a human can see what "launched". Environment values are never recorded."""

    name = "mock"

    def __init__(self, record_path: Path | None = None) -> None:
        self.record_path = record_path
        self.calls: list[dict] = []
        self.state: dict = {}
        """What a test sets to script the terminal; with a record path it is the file's other keys:
        `screens` ({workspace: text}), `gone` ([workspace]), `resume_state` and `fail_create` (a message:
        `create_session` then fails with it, creating nothing) and `external` (a list of `{workspace_id, surface_id,
        title, cwd}` that `discover_sessions` reports)."""

    def capabilities(self) -> set[str]:
        return {CREATE_SESSION, FOCUS_SESSION, DISCOVER_SESSIONS, CLOSE_SESSION, PREPARE_PROMPT, SUBMIT_PROMPT, RUN_INTERACTIVE}

    def available(self) -> bool:
        return True

    def _data(self) -> dict:
        if self.record_path is None:
            return self.state
        try:
            return json.loads(self.record_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Serialise read-modify-write of the record file across processes."""
        if self.record_path is None:
            yield
            return
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.record_path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _append(self, call: dict) -> None:
        """Append without locking; the caller holds `_exclusive`."""
        self.calls.append(call)
        if self.record_path is None:
            return
        data = self._data()
        write_json_atomic(self.record_path, {**data, "calls": [*data.get("calls", []), call]})

    def _record(self, call: dict) -> None:
        with self._exclusive():
            self._append(call)

    def _recorded_calls(self) -> list[dict]:
        if self.record_path is None:
            return self.calls
        try:
            return json.loads(self.record_path.read_text(encoding="utf-8"))["calls"]
        except (FileNotFoundError, ValueError, KeyError):
            return []

    def create_session(self, request: CreateSessionRequest) -> CreateSessionResult:
        failure = self._data().get("fail_create")
        if failure:
            raise TerminalError("terminal_create_failed", str(failure), adapter=self.name)
        delay = float(os.environ.get(CREATE_DELAY_ENV, "0") or 0)
        if delay:
            time.sleep(delay)
        with self._exclusive():
            # Numbered from what is already recorded, so IDs stay unique across processes.
            number = sum(1 for c in self._recorded_calls() if c["op"] == "create_session") + 1
            ref = TerminalSessionRef(
                self.name, f"mock-workspace-{number}", f"mock-surface-{number}", created_by_launcher=True
            )
            self._append(
                {
                    "op": "create_session",
                    "title": request.title,
                    "working_directory": request.working_directory,
                    "command": list(request.command),
                    "env_keys": sorted(request.env),
                    "prompt": request.prompt,
                    "submit_prompt": request.submit_prompt,
                    "session": ref.to_dict(),
                }
            )
        has_prompt = request.prompt is not None
        resume_state = self._data().get("resume_state", "unconfirmed") if request.resume_check else None
        return CreateSessionResult(
            ref,
            prompt_prepared=has_prompt,
            prompt_submitted=has_prompt and request.submit_prompt,
            resume_state=resume_state,
        )

    def run_interactive(self, title: str, working_directory: str, command: Sequence[str]) -> TerminalSessionRef:
        with self._exclusive():
            number = sum(1 for c in self._recorded_calls() if c["op"] == "run_interactive") + 1
            ref = TerminalSessionRef(self.name, f"mock-picker-{number}", None, created_by_launcher=True)
            self._append(
                {"op": "run_interactive", "title": title, "working_directory": working_directory,
                 "command": list(command), "session": ref.to_dict()}
            )
        return ref

    def discover_sessions(self) -> list[ExternalSession]:
        return [
            ExternalSession(
                TerminalSessionRef(self.name, e["workspace_id"], e.get("surface_id"), created_by_launcher=False),
                e.get("title", ""),
                e.get("cwd"),
            )
            for e in self._data().get("external", [])
        ]

    def read_screen(self, session: TerminalSessionRef) -> str | None:
        data = self._data()
        closed = any(c["op"] == "close_session" and c["session"]["workspace_id"] == session.workspace_id for c in self._recorded_calls())
        if closed or session.workspace_id in data.get("gone", []):
            return None
        return data.get("screens", {}).get(session.workspace_id, "")

    def prepare_prompt(self, session, prompt, spec) -> CreateSessionResult:
        self._record({"op": "prepare_prompt", "session": session.to_dict(), "prompt": prompt})
        return CreateSessionResult(session, prompt_prepared=True)

    def focus_session(self, session: TerminalSessionRef) -> None:
        if self.read_screen(session) is None:
            raise StaleTerminalSession(self.name, session)
        self._record({"op": "focus_session", "session": session.to_dict()})

    def close_session(self, session: TerminalSessionRef) -> None:
        self._record({"op": "close_session", "session": session.to_dict()})
