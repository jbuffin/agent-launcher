"""Harness adapters: what a harness's TUI needs, apart from where it is displayed (SPEC §19).

A harness adapter knows how its harness is started on a first prompt (`launch_prompt_args`), how a skill is invoked
(`skill_invocation`) and how a conversation is resumed. Terminal adapters (workspaces, surfaces) know nothing of that
and take it as data.
The argv itself comes from the profile's instance (`agents.resolve_agent`), not from here.
"""

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from commodore_launcher.terminals import PROMPT_MARKER, ResumeCheck


@dataclass(frozen=True)
class SkillCheck:
    status: str
    """`available`, `missing` (certainly absent) or `not_verified`: not found by name, or not findable by name. A
    lookup by name proves presence, not absence: harnesses also provide bundled, plugin and managed skills."""
    checked: tuple[str, ...] = ()
    """The places looked in, for the message. Empty when nothing could be looked up."""


class AgentAdapter:
    name = "generic"

    def launch_prompt_args(self) -> tuple[str, ...] | None:
        """Arguments that start the harness with a first prompt it submits itself, `PROMPT_MARKER` where the prompt
        goes. None when the harness has no such form: it cannot be given a prompt at launch, and `open` refuses it."""
        return None

    def new_conversation_id(self) -> str | None:
        """An ID to fix at launch, or None when the harness cannot take one (then none is stored)."""
        return None

    def conversation_args(self, conversation_id: str) -> tuple[str, ...]:
        """Arguments that make a fresh launch use `conversation_id`."""
        return ()

    def resume_args(self, conversation_id: str) -> tuple[str, ...] | None:
        """Arguments that resume `conversation_id`, or None when there is no supported way."""
        return None

    def resume_check(self) -> ResumeCheck | None:
        return None

    def has_exited(self, screen: str) -> bool:
        """True only on positive evidence that the harness process ended. Unsure means False: never resume
        over a harness that may still be running."""
        return False

    def skill_invocation(self, skill: str, argument: str) -> str | None:
        """The prompt that runs `skill` on `argument` the way this harness takes it (Claude Code: `/skill argument`),
        or None when the harness has no such syntax."""
        return None

    def check_skill(self, skill: str, env: Mapping[str, str], project_dirs: Sequence[Path]) -> SkillCheck:
        """Whether `skill` exists, by name only, in the directories this harness reads skills from. It never
        reads or runs a skill, and it says `not_verified` rather than guess when the harness keeps skills somewhere
        it cannot see (bundled, plugin or managed skills). `missing` only for a case that is certain."""
        return SkillCheck("not_verified")


class ClaudeCodeAdapter(AgentAdapter):
    """Claude Code 2.1: `claude [prompt]` submits its argument."""

    name = "claude-code"

    def launch_prompt_args(self) -> tuple[str, ...]:
        # `claude [prompt]` submits its argument. `--` so a prompt starting with `-` is not read as an option.
        return ("--", PROMPT_MARKER)

    # Verified against Claude Code 2.1.295 `--help`: `--session-id <uuid>` fixes the conversation ID
    # at launch and `--resume <id>` resumes it. Resuming an ID with no saved conversation prints
    # `No conversation found with session ID: <id>` (observed).
    def new_conversation_id(self) -> str:
        return str(uuid.uuid4())

    def conversation_args(self, conversation_id: str) -> tuple[str, ...]:
        return ("--session-id", conversation_id)

    def resume_args(self, conversation_id: str) -> tuple[str, ...]:
        return ("--resume", conversation_id)

    def resume_check(self) -> ResumeCheck:
        # `confirmed` stays empty until the resumed screen has been observed live: guessing a pattern
        # could report a restoration that did not happen. Until then the outcome is "not confirmed".
        return ResumeCheck(confirmed=(), failed=(r"No conversation found",))

    def skill_invocation(self, skill: str, argument: str) -> str:
        return f"/{skill} {argument}"

    def check_skill(self, skill: str, env: Mapping[str, str], project_dirs: Sequence[Path]) -> SkillCheck:
        if ":" in skill:  # `plugin:skill`: plugin contents are not looked up by name here
            return SkillCheck("not_verified")
        config_dir = env.get("CLAUDE_CONFIG_DIR") or str(Path(env.get("HOME") or Path.home()) / ".claude")
        roots = [Path(config_dir).expanduser(), *(Path(d) / ".claude" for d in project_dirs)]
        places = [p for root in roots for p in (root / "skills" / skill, root / "commands" / f"{skill}.md")]
        found = any(p.exists() for p in places)
        return SkillCheck("available" if found else "not_verified", tuple(str(r / "skills") for r in roots))

    def has_exited(self, screen: str) -> bool:
        # Claude Code's exit text offering to resume (unobserved wording): only that counts as an exit.
        return bool(re.search(r"Resume this (conversation|session) with", screen))


def _skills_by_name(skill: str, roots: Sequence[Path]) -> SkillCheck:
    """`available` when `<root>/<skill>/SKILL.md` exists under one of `roots`, else `not_verified` (bundled,
    plugin and admin skills cannot be found by name). A name that is not a plain directory name is never looked up."""
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]*", skill):
        return SkillCheck("not_verified")
    found = any((root / skill / "SKILL.md").is_file() for root in roots)
    return SkillCheck("available" if found else "not_verified", tuple(str(r) for r in roots))


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or Path.home())


class CodexCliAdapter(AgentAdapter):
    """Codex CLI 0.162: `codex [prompt]` submits its argument. There is no way to fix a conversation ID at launch
    (it is generated by Codex and only findable afterwards in its session files), so conversations are not
    resumable (ADR 0005, ADR 0012). Skills are mentioned as `$skill`."""

    name = "codex-cli"

    def launch_prompt_args(self) -> tuple[str, ...]:
        # `codex [PROMPT]` starts the interactive session with it submitted (0.162 `--help`).
        return ("--", PROMPT_MARKER)

    # Syntax checked against the official docs, https://developers.openai.com/codex/skills (it redirects to
    # https://learn.chatgpt.com/docs/build-skills, the page read: "type `$` to mention a skill"), and `codex-cli 0.162.1`,
    # whose `--help` documents `[PROMPT]` but no skill syntax. The docs describe the form typed in the session:
    # launch-argument form not checked live.
    def skill_invocation(self, skill: str, argument: str) -> str:
        return f"${skill} {argument}"

    def check_skill(self, skill: str, env: Mapping[str, str], project_dirs: Sequence[Path]) -> SkillCheck:
        codex_home = Path(env.get("CODEX_HOME") or _home(env) / ".codex").expanduser()
        roots = [*(Path(d) / ".agents" / "skills" for d in project_dirs), _home(env) / ".agents" / "skills",
                 codex_home / "skills"]
        return _skills_by_name(skill, roots)


class CopilotCliAdapter(AgentAdapter):
    """GitHub Copilot CLI 1.0: `copilot -i <prompt>` submits its prompt. `--session-id <uuid>` fixes the
    conversation ID at launch and `--resume=<id>` resumes it (`--help`). Skills are named in the prompt as `/skill`."""

    name = "github-copilot-cli"

    def launch_prompt_args(self) -> tuple[str, ...]:
        # `-i, --interactive <prompt>`: interactive mode with the prompt executed (1.0.94 `--help`). Attached with
        # `=` so a prompt starting with `-` stays its value. Not `-p`, which runs without the interactive session.
        return (f"--interactive={PROMPT_MARKER}",)

    def new_conversation_id(self) -> str:
        return str(uuid.uuid4())

    def conversation_args(self, conversation_id: str) -> tuple[str, ...]:
        return ("--session-id", conversation_id)

    def resume_args(self, conversation_id: str) -> tuple[str, ...]:
        # `--resume` takes an optional value, so the id must be attached with `=`: a separate argument
        # would be read as the prompt or ignored.
        return (f"--resume={conversation_id}",)

    def resume_check(self) -> ResumeCheck:
        # No wording observed, so none is guessed: the outcome is "resume attempted, not confirmed".
        return ResumeCheck()

    # Syntax checked against the official docs,
    # https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-skills (a skill is named in the
    # prompt as `/skill-name`), and `GitHub Copilot CLI 1.0.97`, whose `--help` documents `-i, --interactive <prompt>`
    # but no skill syntax. The docs describe the form typed in the session: launch-argument form not checked live.
    def skill_invocation(self, skill: str, argument: str) -> str:
        return f"/{skill} {argument}"

    def check_skill(self, skill: str, env: Mapping[str, str], project_dirs: Sequence[Path]) -> SkillCheck:
        copilot_home = Path(env.get("COPILOT_HOME") or _home(env) / ".copilot").expanduser()
        roots = [
            *(Path(d) / sub / "skills" for d in project_dirs for sub in (".github", ".agents", ".claude")),
            copilot_home / "skills",
            _home(env) / ".agents" / "skills",
        ]
        return _skills_by_name(skill, roots)


_ADAPTERS: dict[str, AgentAdapter] = {a.name: a for a in (ClaudeCodeAdapter(), CodexCliAdapter(), CopilotCliAdapter())}


def agent_adapter_for(name: str) -> AgentAdapter:
    """The adapter for a harness type's `adapter` value; unknown harnesses get one with no launch form, which `open` refuses."""
    return _ADAPTERS.get(name, AgentAdapter())
