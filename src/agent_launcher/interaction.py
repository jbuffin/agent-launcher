"""The only place the wizard talks to a human.

The wizard asks through a `Prompter`, never through questionary directly, so tests
script every answer (see `tests/scripted.py`) and run the whole flow without a terminal.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

import typer


class SetupCancelled(Exception):
    """The user pressed Ctrl-C or Esc. Nothing has been written."""


@dataclass(frozen=True)
class Choice:
    value: str
    label: str


Validate = Callable[[str], str | None]
"""Returns an error message, or None when the text is acceptable."""


class Prompter(Protocol):
    def say(self, text: str) -> None: ...

    def confirm(self, message: str, default: bool = True) -> bool: ...

    def text(self, message: str, default: str = "", validate: Validate | None = None) -> str: ...

    def select(self, message: str, choices: Sequence[Choice], default: str | None = None) -> str: ...

    def checkbox(self, message: str, choices: Sequence[Choice], defaults: Sequence[str] = ()) -> list[str]: ...


class QuestionaryPrompter:
    """Terminal prompts. Ctrl-C and Esc raise `SetupCancelled`."""

    def say(self, text: str) -> None:
        typer.echo(text)

    @staticmethod
    def _answer(value):  # questionary returns None when the user cancels
        if value is None:
            raise SetupCancelled()
        return value

    def confirm(self, message: str, default: bool = True) -> bool:
        import questionary

        return self._answer(questionary.confirm(message, default=default).ask())

    def text(self, message: str, default: str = "", validate: Validate | None = None) -> str:
        import questionary

        def check(value: str) -> bool | str:
            error = validate(value) if validate else None
            return error or True

        return self._answer(questionary.text(message, default=default, validate=check).ask())

    def select(self, message: str, choices: Sequence[Choice], default: str | None = None) -> str:
        import questionary

        items = [questionary.Choice(title=c.label, value=c.value) for c in choices]
        return self._answer(questionary.select(message, choices=items, default=default).ask())

    def checkbox(self, message: str, choices: Sequence[Choice], defaults: Sequence[str] = ()) -> list[str]:
        import questionary

        items = [questionary.Choice(title=c.label, value=c.value, checked=c.value in defaults) for c in choices]
        return self._answer(questionary.checkbox(message, choices=items).ask())
