"""A terminal adapter that launches nothing and records what it was asked to do."""

import json
from pathlib import Path

from agent_launcher.config import write_json_atomic
from agent_launcher.terminals import (
    CLOSE_SESSION,
    CREATE_SESSION,
    FOCUS_SESSION,
    PREPARE_PROMPT,
    SUBMIT_PROMPT,
    CreateSessionRequest,
    CreateSessionResult,
    StaleTerminalSession,
    TerminalAdapter,
    TerminalSessionRef,
)


class MockTerminalAdapter(TerminalAdapter):
    """Calls are kept in `calls`; with `record_path` they are also appended to a JSON file,
    so a CLI-level test or a human can see what "launched". Environment values are never recorded."""

    name = "mock"

    def __init__(self, record_path: Path | None = None) -> None:
        self.record_path = record_path
        self.calls: list[dict] = []
        self.state: dict = {}
        """What a test sets to script the terminal; with a record path it is the file's other keys:
        `screens` ({workspace: text}), `gone` ([workspace]) and `resume_state`."""

    def capabilities(self) -> set[str]:
        return {CREATE_SESSION, FOCUS_SESSION, CLOSE_SESSION, PREPARE_PROMPT, SUBMIT_PROMPT}

    def available(self) -> bool:
        return True

    def _data(self) -> dict:
        if self.record_path is None:
            return self.state
        try:
            return json.loads(self.record_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def _record(self, call: dict) -> None:
        self.calls.append(call)
        if self.record_path is None:
            return
        data = self._data()
        write_json_atomic(self.record_path, {**data, "calls": [*data.get("calls", []), call]})

    def _recorded_calls(self) -> list[dict]:
        if self.record_path is None:
            return self.calls
        try:
            return json.loads(self.record_path.read_text(encoding="utf-8"))["calls"]
        except (FileNotFoundError, ValueError, KeyError):
            return []

    def create_session(self, request: CreateSessionRequest) -> CreateSessionResult:
        # Numbered from what is already recorded, so IDs stay unique across processes.
        number = sum(1 for c in self._recorded_calls() if c["op"] == "create_session") + 1
        ref = TerminalSessionRef(self.name, f"mock-workspace-{number}", f"mock-surface-{number}", created_by_launcher=True)
        self._record(
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
