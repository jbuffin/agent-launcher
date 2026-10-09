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

    def capabilities(self) -> set[str]:
        return {CREATE_SESSION, FOCUS_SESSION, CLOSE_SESSION, PREPARE_PROMPT, SUBMIT_PROMPT}

    def available(self) -> bool:
        return True

    def _record(self, call: dict) -> None:
        self.calls.append(call)
        if self.record_path is None:
            return
        try:
            existing = json.loads(self.record_path.read_text(encoding="utf-8"))["calls"]
        except (FileNotFoundError, ValueError, KeyError):
            existing = []
        write_json_atomic(self.record_path, {"calls": [*existing, call]})

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
        ref = TerminalSessionRef(self.name, f"mock-workspace-{number}", f"mock-surface-{number}")
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
        return CreateSessionResult(ref, prompt_prepared=has_prompt, prompt_submitted=has_prompt and request.submit_prompt)

    def focus_session(self, session: TerminalSessionRef) -> None:
        self._record({"op": "focus_session", "session": session.to_dict()})

    def close_session(self, session: TerminalSessionRef) -> None:
        self._record({"op": "close_session", "session": session.to_dict()})
