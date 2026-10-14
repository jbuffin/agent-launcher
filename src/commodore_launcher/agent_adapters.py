"""Harness adapters: what a harness's TUI needs, apart from where it is displayed (SPEC §19).

A harness adapter knows how its harness is started on a first prompt (`launch_prompt_args`), how a skill is invoked
(`skill_invocation`) and how a conversation is resumed. Terminal adapters (workspaces, surfaces) know nothing of that
and take it as data.
The argv itself comes from the profile's instance (`agents.resolve_agent`), not from here.
"""

import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from commodore_launcher.terminals import PROMPT_MARKER, ResumeCheck


@dataclass(frozen=True)
class SkillCheck:
    status: str
    """`available`, `missing` (not found in the places checked) or `not_verified` (not found, and some place that
    could hold it cannot be read)."""
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
        """Whether `skill` is installed, by name only, in the places this harness loads skills from (ADR 0021): the
        user's, the project's from each of `project_dirs` up to its repository root, and plugins where they can be
        read. It never reads or runs a skill. `missing` is "not found there"; bundled, enterprise and managed skills
        are out of scope. `not_verified` when it cannot look (a name that is not a plain directory name, or plugins
        it cannot read) and the skill was not found elsewhere."""
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

    # Syntax checked against Claude Code's skills documentation, https://code.claude.com/docs/en/skills (a skill is
    # run as `/skill-name`), and `claude --version` 2.1.296: launch-argument form not checked live.
    def skill_invocation(self, skill: str, argument: str) -> str:
        return f"/{skill} {argument}"

    # Where Claude Code loads skills, checked against https://code.claude.com/docs/en/skills and
    # https://code.claude.com/docs/en/env-vars and Claude Code 2.1.296: `<config>/skills/<n>/SKILL.md` with `<config>`
    # = `CLAUDE_CONFIG_DIR` or `~/.claude`, the project's `.claude/skills` from the working directory up to the
    # repository root, and the skills of enabled plugins. It does not read `~/.agents/skills`.
    def check_skill(self, skill: str, env: Mapping[str, str], project_dirs: Sequence[Path]) -> SkillCheck:
        if not _PLAIN_NAME.fullmatch(skill):
            return SkillCheck("not_verified")
        config = Path(env.get("CLAUDE_CONFIG_DIR") or _home(env) / ".claude").expanduser()
        walked = [d for project in project_dirs for d in _up_to_repository_root(project)]
        roots = [config / "skills", *(d / ".claude" / "skills" for d in walked)]
        checked = tuple(str(r) for r in roots)
        if any((r / skill / "SKILL.md").is_file() for r in roots):
            return SkillCheck("available", checked)
        plugins_root = Path(env.get("CLAUDE_CODE_PLUGIN_CACHE_DIR") or config / "plugins").expanduser()
        if not plugins_root.is_dir():
            return SkillCheck("missing", checked)
        checked += (str(plugins_root),)
        # The installed plugin record says which version dir is current: the cache keeps stale versions for days, so
        # it is never searched as a whole. An unreadable record cannot rule a plugin out.
        record = _read_json(plugins_root / "installed_plugins.json")
        installed = record.get("plugins") if isinstance(record, dict) else None
        if not isinstance(installed, dict):
            return SkillCheck("not_verified", checked)
        enabled = _enabled_plugins(config / "settings.json", walked)
        for plugin_id, entries in installed.items():
            if not enabled.get(plugin_id) or not isinstance(entries, list):
                continue
            for entry in entries:
                path = entry.get("installPath") if isinstance(entry, dict) else None
                if isinstance(path, str) and _has_skill_below(Path(path), skill):
                    return SkillCheck("available", checked)
        return SkillCheck("missing", checked)

    def has_exited(self, screen: str) -> bool:
        # Claude Code's exit text offering to resume (unobserved wording): only that counts as an exit.
        return bool(re.search(r"Resume this (conversation|session) with", screen))


_PLAIN_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or Path.home())


def _up_to_repository_root(directory: Path) -> list[Path]:
    """`directory` and its parents up to the one holding `.git`. Without a repository, only `directory`."""
    chain = [directory, *directory.parents]
    for i, d in enumerate(chain):
        if (d / ".git").exists():
            return chain[: i + 1]
    return [directory]


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _enabled_plugins(user_settings: Path, project_dirs: Sequence[Path]) -> dict[str, bool]:
    """`enabledPlugins` as the user's settings and then the project's (`settings.json`, `settings.local.json`) set it:
    the last word on a plugin wins."""
    files = [user_settings]
    for d in reversed(project_dirs):  # the repository root first, the checkout last
        files += [d / ".claude" / "settings.json", d / ".claude" / "settings.local.json"]
    enabled: dict[str, bool] = {}
    for f in files:
        content = _read_json(f)
        found = content.get("enabledPlugins") if isinstance(content, dict) else None
        if isinstance(found, dict):
            enabled.update({k: v for k, v in found.items() if isinstance(v, bool)})
    return enabled


def _has_skill_below(root: Path, skill: str) -> bool:
    """A `<skill>/SKILL.md` anywhere below `root`, whatever the plugin's own nesting; dependencies are skipped."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "node_modules"]
        if Path(dirpath).name == skill and "SKILL.md" in filenames:
            return True
    return False


def _skills_by_name(skill: str, roots: Sequence[Path], *, unverifiable: bool = False) -> SkillCheck:
    """`available` when `<root>/<skill>/SKILL.md` exists under one of `roots`, else `missing`, or `not_verified` when
    `unverifiable` (the harness has plugin skills in a place it cannot read). A name that is not a plain directory
    name is never looked up."""
    if not _PLAIN_NAME.fullmatch(skill):
        return SkillCheck("not_verified")
    checked = tuple(str(r) for r in roots)
    if any((root / skill / "SKILL.md").is_file() for root in roots):
        return SkillCheck("available", checked)
    return SkillCheck("not_verified" if unverifiable else "missing", checked)


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

    # Where Codex loads skills (https://learn.chatgpt.com/docs/build-skills, codex-cli 0.162.1): `.agents/skills` from
    # the working directory up to the repository root, `~/.agents/skills` and `/etc/codex/skills`. `CODEX_HOME/skills`
    # is not documented: it is looked in as a harmless extra. Plugin skills have no documented place and `codex plugin
    # list` is not run, so any installed plugin makes a skill not found `not_verified`.
    def check_skill(self, skill: str, env: Mapping[str, str], project_dirs: Sequence[Path]) -> SkillCheck:
        codex_home = Path(env.get("CODEX_HOME") or _home(env) / ".codex").expanduser()
        project = [d / ".agents" / "skills" for p in project_dirs for d in _up_to_repository_root(p)]
        roots = [*project, _home(env) / ".agents" / "skills", Path("/etc/codex/skills"), codex_home / "skills"]
        plugins = codex_home / "plugins"
        return _skills_by_name(skill, roots, unverifiable=plugins.is_dir() and any(plugins.iterdir()))


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

    # Where Copilot loads skills (`copilot skill --help`, GitHub Copilot CLI 1.0.97): project `.github/skills`,
    # `.agents/skills` and `.claude/skills` from the working directory up to the repository root; personal
    # `~/.copilot/skills` and `~/.agents/skills`. `$COPILOT_HOME/skills` is inferred, not documented.
    def check_skill(self, skill: str, env: Mapping[str, str], project_dirs: Sequence[Path]) -> SkillCheck:
        copilot_home = Path(env.get("COPILOT_HOME") or _home(env) / ".copilot").expanduser()
        roots = [
            *(d / sub / "skills" for p in project_dirs for d in _up_to_repository_root(p)
              for sub in (".github", ".agents", ".claude")),
            copilot_home / "skills",
            _home(env) / ".copilot" / "skills",
            _home(env) / ".agents" / "skills",
        ]
        return _skills_by_name(skill, roots)


_ADAPTERS: dict[str, AgentAdapter] = {a.name: a for a in (ClaudeCodeAdapter(), CodexCliAdapter(), CopilotCliAdapter())}


def agent_adapter_for(name: str) -> AgentAdapter:
    """The adapter for a harness type's `adapter` value; unknown harnesses get one with no launch form, which `open` refuses."""
    return _ADAPTERS.get(name, AgentAdapter())
