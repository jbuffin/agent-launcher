# 0001: Foundation stack

Status: accepted

- **Typer** for the CLI: type-hint driven, subcommand groups, good test runner. Click-based, so no heavy framework.
- **Pydantic v2** for config validation: precise per-field errors, and `extra="forbid"` lets us detect unknown fields. Validation runs on the raw JSON object; the object (not the model) is what we write back, so unknown fields survive edits.
- **questionary** for the interactive picker (SPEC §21), added now to settle the choice; first used by a later ticket.
- **pytest** + **uv** (lockfile committed) for tests and environments; **hatchling** as build backend.
- **Unknown fields are warnings, not errors**, in `config validate` (`--strict` upgrades them), so a config written by a newer release degrades gracefully while still being reported.
