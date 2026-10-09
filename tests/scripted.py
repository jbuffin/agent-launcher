"""A Prompter that plays back a script, so the whole wizard runs without a terminal."""

from collections.abc import Sequence
from dataclasses import dataclass

from agent_launcher.interaction import Choice, SetupCancelled

DEFAULT = object()
"""Answer with the prompt's own default."""
CANCEL = object()
"""Behave as if the user pressed Ctrl-C."""


@dataclass
class Step:
    kind: str
    match: str
    answer: object


class ScriptedPrompter:
    def __init__(self, *steps: tuple[str, str, object]) -> None:
        self.steps = [Step(*s) for s in steps]
        self.said: list[str] = []
        self.asked: list[tuple[str, str]] = []

    def _next(self, kind: str, message: str):
        assert self.steps, f"unexpected {kind} prompt: {message!r}"
        step = self.steps.pop(0)
        assert step.kind == kind and step.match in message, (
            f"expected {step.kind} containing {step.match!r}, got {kind} {message!r}"
        )
        self.asked.append((kind, message))
        if step.answer is CANCEL:
            raise SetupCancelled()
        return step.answer

    def done(self) -> None:
        assert not self.steps, f"unused script steps: {[(s.kind, s.match) for s in self.steps]}"

    def say(self, text: str) -> None:
        self.said.append(text)

    def confirm(self, message, default=True):
        answer = self._next("confirm", message)
        return default if answer is DEFAULT else answer

    def text(self, message, default="", validate=None):
        answer = self._next("text", message)
        value = default if answer is DEFAULT else answer
        error = validate(value) if validate else None
        assert error is None, f"scripted answer {value!r} rejected: {error}"
        return value

    def select(self, message, choices: Sequence[Choice], default=None):
        answer = self._next("select", message)
        value = default if answer is DEFAULT else answer
        assert value in {c.value for c in choices}, f"{value!r} is not a choice for {message!r}"
        return value

    def checkbox(self, message, choices: Sequence[Choice], defaults=()):
        answer = self._next("checkbox", message)
        return list(defaults) if answer is DEFAULT else list(answer)
