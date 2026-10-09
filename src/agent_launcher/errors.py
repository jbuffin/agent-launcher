"""A refusal the caller can act on, with a stable code and JSON-safe details."""

from typing import Any


class LauncherError(Exception):
    """`code` is stable for JSON consumers; `details` must be JSON-safe."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, **self.details}}
