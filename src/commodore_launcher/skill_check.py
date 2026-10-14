"""Whether the skills Commodore needs are installed for a harness, and how to install them (ADR 0021).

A profile with its own configuration directory does not get skills from `npx skills`, so `doctor` checks each profile's
harness and `open` the one it is about to start. Where each harness loads skills from is the adapter's
`check_skill`; this module asks it for each skill and words the result.
"""

import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from commodore_launcher.agent_adapters import agent_adapter_for
from commodore_launcher.agents import _expand

COMMODORE_SKILLS = ("commodore", "agent-teammates", "run-preflight", "target-evaluation")
"""What a task on a GitHub URL needs: `commodore` and the skills it calls."""
MANAGEMENT_SKILLS = ("commodore-launcher",)
"""What `configure` and `integrate gh-dash` start."""

# Where the four skills are published is not decided yet: fill this in (and nowhere else) when it is.
COMMODORE_SKILLS_SOURCE = "<source>"
MANAGEMENT_SKILLS_SOURCE = "jbuffin/agent-launcher"

_SKILLS_CLI_AGENT = {"claude-code": "claude-code", "codex-cli": "codex", "github-copilot-cli": "github-copilot"}
"""The harness adapter's name to the `-a` id of `npx skills`."""


_DIRECTORY_VARIABLES = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_PLUGIN_CACHE_DIR", "CODEX_HOME", "COPILOT_HOME")


def _expanded(env: Mapping[str, str]) -> dict[str, str]:
    """`env` with the variables that name a skills location expanded as the instance expands them (`~`, `$HOME`)."""
    home = env.get("HOME") or str(Path.home())
    return {k: _expand(v, home) if k in _DIRECTORY_VARIABLES else v for k, v in env.items()}


@dataclass(frozen=True)
class SkillReport:
    missing: tuple[str, ...] = ()
    not_verified: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    """Why skills could not be verified, without repeats."""
    checked: tuple[str, ...] = ()
    """The places looked in, without repeats."""


def check_skills(
    adapter_name: str, env: Mapping[str, str], skills: Sequence[str], project_dirs: Sequence[Path] = ()
) -> SkillReport:
    adapter, env = agent_adapter_for(adapter_name), _expanded(env)
    missing, unverified, checked, reasons = [], [], dict.fromkeys(()), dict.fromkeys(())
    for skill in skills:
        result = adapter.check_skill(skill, env, project_dirs)
        checked.update(dict.fromkeys(result.checked))
        if result.status == "missing":
            missing.append(skill)
        elif result.status == "not_verified":
            unverified.append(skill)
            reasons[result.reason] = None
    return SkillReport(tuple(missing), tuple(unverified), tuple(reasons), tuple(checked))


def user_skills_dir(adapter_name: str, env: Mapping[str, str]) -> str:
    """Where the harness loads the user's skills from, for the hint."""
    env = _expanded(env)
    home = Path(env.get("HOME") or Path.home())
    if adapter_name == "claude-code":
        return str(Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser() / "skills")
    if adapter_name == "github-copilot-cli":
        return str(home / ".copilot" / "skills")  # where `npx skills -a github-copilot -g` writes
    return str(home / ".agents" / "skills")


def _shell_path(path: str, env: Mapping[str, str]) -> str:
    """`path` as a shell word. One that needs quoting and sits under HOME is written `"$HOME/..."`, because a quoted
    `~` is not expanded and `doctor` shows the home directory as `~` (it would otherwise be pasted literally)."""
    quoted = shlex.quote(path)
    home = env.get("HOME") or str(Path.home())
    if quoted == path or not path.startswith(home + "/"):
        return quoted
    rest = path[len(home):].replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return f'"$HOME{rest}"'


def install_hint(adapter_name: str, env: Mapping[str, str], names: Sequence[str], source: str) -> str:
    """How to install `names` where `adapter_name` loads skills, given the profile's environment."""
    env = _expanded(env)
    where = user_skills_dir(adapter_name, env)
    cli_agent = _SKILLS_CLI_AGENT.get(adapter_name)
    if cli_agent is None:
        return f"Install {', '.join(names)} where {adapter_name} loads skills."
    add = f"npx skills add {source} -g -a {cli_agent} --skill {' '.join(shlex.quote(n) for n in names)}"
    hint = f"Install {', '.join(names)} for {adapter_name} where it loads skills ({where}): `{add}`."
    if adapter_name == "claude-code" and env.get("CLAUDE_CONFIG_DIR"):
        config = str(Path(env["CLAUDE_CONFIG_DIR"]).expanduser())
        hint += (
            f" This profile's configuration directory is {config}, which `npx skills` only uses when it is told: "
            f"run `CLAUDE_CONFIG_DIR={_shell_path(config, env)} {add}`, or copy each skill's directory into "
            f"{_shell_path(where, env)}/."
        )
    if adapter_name == "github-copilot-cli" and env.get("COPILOT_HOME"):
        hint = (
            f"Install {', '.join(names)} for {adapter_name} where it loads skills ($COPILOT_HOME/skills, or "
            f"~/.agents/skills, which it also reads): copy each skill's directory there. "
            "`npx skills` writes to ~/.copilot/skills, which a profile that sets COPILOT_HOME does not read."
        )
    return hint


def problem(report: SkillReport, adapter_name: str, env: Mapping[str, str], source: str) -> str | None:
    """A sentence naming the skills not found, the places looked in and how to install them, or None."""
    if not report.missing:
        return None
    return (
        f"{', '.join(report.missing)} not installed for {adapter_name} (looked in {', '.join(report.checked)}). "
        + install_hint(adapter_name, env, report.missing, source)
    )
