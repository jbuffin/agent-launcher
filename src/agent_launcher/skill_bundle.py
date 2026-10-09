"""The bundled management skill (SPEC §24): where it lives inside the installed package.

It ships as package data under `agent_launcher/skills/agent-launcher/`, so a pipx or wheel install has it and no
Node is needed at run time. `npx skills add` installs it from the repository; `agent-launcher skill path` prints
this directory for a manual copy.
"""

from pathlib import Path

from agent_launcher.errors import LauncherError

SKILL_NAME = "agent-launcher"


def skill_directory() -> Path:
    path = Path(__file__).resolve().parent / "skills" / SKILL_NAME
    if not (path / "SKILL.md").is_file():
        raise LauncherError(
            "skill_missing_from_install",
            f"The bundled skill is missing from this installation (looked for {path / 'SKILL.md'}). "
            "Reinstall agent-launcher.",
            path=str(path),
        )
    return path
