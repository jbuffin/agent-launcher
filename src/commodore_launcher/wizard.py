"""The setup wizard: gather answers, show a diff, apply it in one atomic write.

Two ways to get answers, one way to apply them:

- interactive: `gather_answers` asks through a `Prompter`, offering detected values as
  suggestions that the user confirms;
- non-interactive: `load_answers` reads a JSON file. Nothing is detected or guessed; a value
  that is missing or ambiguous fails with a `SetupError`.

`apply_answers` only ever adds to or updates the raw config object. It never removes a key, a
profile, a harness or a search root, so running setup again cannot reset what is there. It never
touches a harness's own configuration directory.
"""

import copy
import difflib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, field_validator

from commodore_launcher import doctor
from commodore_launcher.agents import instance_home, normalize_path
from commodore_launcher.config import (
    CURRENT_VERSION,
    NAME_PATTERN,
    AgentSelection,
    AgentType,
    Config,
    ConfigError,
    ConfigOutdatedError,
    outdated_message,
    outdated_version,
    Name,
    PathStr,
    builtin_harness_types,
    load_config,
    read_raw,
    update_config,
    validate_data,
)
from commodore_launcher.detect import IDENTITY_ENV, Detection
from commodore_launcher.doctor import Which
from commodore_launcher.interaction import Choice, Prompter, SetupCancelled
from commodore_launcher.logs import trace
from commodore_launcher.paths import config_path
from commodore_launcher.profiles import InstanceEdit, merge_profile

__all__ = ["SetupCancelled", "SetupError", "SetupAnswers", "SetupResult", "run_setup"]

TERMINAL_ADAPTERS = ("cmux",)
AGENT_SELECTIONS = {
    "always_ask": "always_ask: always ask which harness",
    "use_default": "use_default: use the profile's default harness",
    "ask_if_multiple": "ask_if_multiple: ask only when the profile has several harnesses",
}


class SetupError(Exception):
    """A setup problem the caller can act on. `code` is stable; `field` names the answer at fault."""

    def __init__(self, code: str, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "field": self.field}}


# --- answers ---------------------------------------------------------------------------------


class AgentAnswer(BaseModel):
    """One harness in a profile. Fields left out are not changed."""

    model_config = ConfigDict(extra="forbid")

    executable: StrictStr | None = Field(default=None, min_length=1)
    args: list[StrictStr] | None = None
    env: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    working_directory: StrictStr | None = Field(default=None, min_length=1)
    resume_args: list[StrictStr] | None = None

    @field_validator("env")
    @classmethod
    def _env_names(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            if not key or "=" in key or "\0" in key:
                raise ValueError(f"invalid environment variable name {key!r}")
        if "HOME" in value and not value["HOME"]:
            raise ValueError("HOME must not be empty")
        return value


class ProfileAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    harnesses: dict[Name, AgentAnswer] = Field(default_factory=dict)
    default_harness: Name | None = None


class SetupAnswers(BaseModel):
    """Everything the wizard can set. A field that is `None` (or absent) is left as it is."""

    model_config = ConfigDict(extra="forbid")

    profiles: list[ProfileAnswer] = Field(default_factory=list)
    terminal_adapter: Name | None = None
    search_roots: list[PathStr] | None = None
    """Roots to add. Existing roots are kept."""
    harness_selection: AgentSelection | None = None


def _describe(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()
    )


def load_answers(path: Path) -> SetupAnswers:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SetupError("invalid_answers", f"answers file {path} does not exist", "--answers") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SetupError("invalid_answers", f"answers file {path} cannot be read as JSON: {exc}", "--answers") from exc
    try:
        return SetupAnswers.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or None
        raise SetupError("invalid_answers", f"{path}: {_describe(exc)}", where) from exc


# --- checks on answers ------------------------------------------------------------------------


def _profile_edits(profile: ProfileAnswer) -> list[InstanceEdit]:
    return [
        InstanceEdit(
            agent=agent,
            executable=a.executable,
            args=a.args,
            env=dict(a.env),
            working_directory=a.working_directory,
            resume_args=a.resume_args,
        )
        for agent, a in profile.harnesses.items()
    ]


def _identity_key(env: dict[str, str], agent: str) -> tuple[str, str] | None:
    """The variable and normalised directory that decide which identity an instance runs as."""
    caller_home = str(Path.home())
    home = instance_home(env, caller_home)
    for name in (n for n in (IDENTITY_ENV.get(agent), "HOME") if n):
        if env.get(name):
            return name, normalize_path(env[name], caller_home if name == "HOME" else home)
    return None


def identity_problems(profiles: dict[str, Any], touched: set[str]) -> list[tuple[str, str]]:
    """Profiles that would share a harness identity: (`profile.harnesses.<harness>`, explanation).

    Two profiles using the same harness must each point it at a different configuration
    (`CLAUDE_CONFIG_DIR`, `CODEX_HOME` or `HOME`). Otherwise both would run as one identity, and
    the wizard will not choose for you. Only profiles in `touched` are reported.
    """
    problems: list[tuple[str, str]] = []
    by_agent: dict[str, list[tuple[str, tuple[str, str] | None]]] = {}
    for pname, profile in profiles.items():
        agents = profile.get("harnesses", {}) if isinstance(profile, dict) else {}
        for aname, inst in agents.items():
            env = inst.get("env", {}) if isinstance(inst, dict) else {}
            by_agent.setdefault(aname, []).append((pname, _identity_key(env if isinstance(env, dict) else {}, aname)))
    for aname, users in by_agent.items():
        if len(users) < 2 or aname not in IDENTITY_ENV:
            continue
        for pname, key in users:
            if pname not in touched:
                continue
            where = f"profiles.{pname}.harnesses.{aname}"
            if key is None:
                problems.append(
                    (where, f"{aname} is used by several profiles but this one sets no {IDENTITY_ENV[aname]}; "
                            "it would run as whichever identity the caller has")
                )
                continue
            twins = sorted(o for o, k in users if o != pname and k == key)
            if twins:
                problems.append((where, f"{key[0]}={key[1]} is also used by profile {', '.join(twins)}"))
    return problems


def executable_problem(executable: str, env: dict[str, str], which: Which) -> str | None:
    """Why `executable` cannot be found, or None. Skipped when the instance sets its own PATH."""
    if "PATH" in env:
        return None
    if "/" in executable:
        home = instance_home(env)
        path = Path(executable)
        if executable == "~" or executable.startswith("~/"):
            path = Path(home + executable[1:])
        if not path.is_absolute():
            return f"{executable!r} is a relative path; use an absolute path or a bare command name"
        if not (path.is_file() and os.access(path, os.X_OK)):
            return f"{executable!r} does not exist or is not executable"
        return None
    return None if which(executable) else f"{executable!r} was not found on PATH"


def check_answers(
    raw: dict[str, Any] | None, answers: SetupAnswers, which: Which, harness_types: Mapping[str, AgentType] | None = None
) -> None:
    """Non-interactive validation: raises SetupError for anything missing or ambiguous.

    `harness_types` is the effective config's (default: the built-ins); a type's `executable` is
    what runs when an instance does not set its own.
    """
    harness_types = harness_types if harness_types is not None else builtin_harness_types()
    types = set(harness_types)
    seen: set[str] = set()
    for i, profile in enumerate(answers.profiles):
        where = f"profiles[{i}]"
        if profile.name in seen:
            raise SetupError("invalid_answers", f"profile {profile.name!r} is listed twice", f"{where}.name")
        seen.add(profile.name)
        if not profile.harnesses:
            existing = ((raw or {}).get("profiles") or {}).get(profile.name)
            if not existing:
                raise SetupError("invalid_answers", f"profile {profile.name!r} lists no harnesses", f"{where}.harnesses")
        for aname, agent in profile.harnesses.items():
            if aname not in types:
                raise SetupError(
                    "invalid_answers",
                    f"profile {profile.name!r}: no harness type named {aname!r} (harness_types: {', '.join(sorted(types))})",
                    f"{where}.harnesses.{aname}",
                )
            exe = agent.executable or harness_types[aname].executable
            existing_exe = (
                (((raw or {}).get("profiles") or {}).get(profile.name, {}).get("harnesses", {}).get(aname, {}) or {})
                .get("executable")
            )
            if agent.executable is None and existing_exe:
                continue
            problem = executable_problem(exe, agent.env, which)
            if problem:
                raise SetupError(
                    "executable_not_found",
                    f"profile {profile.name!r}, harness {aname!r}: {problem}; give an explicit executable",
                    f"{where}.harnesses.{aname}.executable",
                )
        if profile.default_harness is not None:
            have = set(profile.harnesses) | set(
                (((raw or {}).get("profiles") or {}).get(profile.name, {}) or {}).get("harnesses", {}) or {}
            )
            if profile.default_harness not in have:
                raise SetupError(
                    "invalid_answers",
                    f"profile {profile.name!r}: default_harness {profile.default_harness!r} is not one of its harnesses",
                    f"{where}.default_harness",
                )
        elif len(profile.harnesses) > 1 and profile.name not in ((raw or {}).get("profiles") or {}):
            raise SetupError(
                "ambiguous_default",
                f"profile {profile.name!r} has several harnesses and no default_harness; setup will not choose one",
                f"{where}.default_harness",
            )
    if answers.terminal_adapter and answers.terminal_adapter not in TERMINAL_ADAPTERS:
        raise SetupError(
            "invalid_answers",
            f"terminal_adapter {answers.terminal_adapter!r} is not implemented (available: {', '.join(TERMINAL_ADAPTERS)})",
            "terminal_adapter",
        )
    merged = apply_answers(raw, answers)
    problems = identity_problems(merged.get("profiles", {}), seen)
    if problems:
        field, message = problems[0]
        raise SetupError("ambiguous_identity", message, field)


# --- applying answers -------------------------------------------------------------------------


def _section(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.setdefault(key, {})
    if not isinstance(value, dict):
        raise SetupError("config_invalid", f"{key} in config.json must be an object; fix it by hand first", key)
    return value


def apply_answers(raw: dict[str, Any] | None, answers: SetupAnswers) -> dict[str, Any]:
    """The config that results from `answers`. Pure: `raw` is not modified and nothing is written."""
    new: dict[str, Any] = copy.deepcopy(raw) if raw else {"version": CURRENT_VERSION}
    try:
        profiles = _section(new, "profiles") if answers.profiles else new.get("profiles")
        for profile in answers.profiles:
            merge_profile(profiles, profile.name, _profile_edits(profile), profile.default_harness)
    except ConfigError as exc:
        raise SetupError("config_invalid", str(exc)) from exc
    if answers.terminal_adapter is not None:
        _section(new, "terminal")["adapter"] = answers.terminal_adapter
    if answers.search_roots is not None:
        repos = _section(new, "repositories")
        if answers.search_roots is not None:
            roots = repos.setdefault("search_roots", [])
            if not isinstance(roots, list):
                raise SetupError("config_invalid", "repositories.search_roots must be a list", "repositories.search_roots")
            roots.extend(r for r in answers.search_roots if r not in roots)
    if answers.harness_selection is not None:
        new["harness_selection"] = answers.harness_selection
    version = outdated_version(new)
    if new != raw and version is not None:
        raise SetupError(ConfigOutdatedError.code, outdated_message(version), "version")
    return new


def render_diff(old: dict[str, Any] | None, new: dict[str, Any]) -> str:
    before = json.dumps(old, indent=2).splitlines() if old is not None else []
    after = json.dumps(new, indent=2).splitlines()
    return "\n".join(
        difflib.unified_diff(
            before, after, "config.json (current)" if old is not None else "/dev/null", "config.json (proposed)", lineterm=""
        )
    )


# --- interactive gathering --------------------------------------------------------------------


def render_detection(det: Detection) -> str:
    marks = {"pass": "ok  ", "warn": "warn", "fail": "FAIL"}
    lines = ["What I found (a guide only; you will confirm each choice):", f"  [ok  ] Operating system: {det.os}"]
    for c in det.checks:
        lines.append(f"  [{marks[c.status]}] {c.name}: {c.detail}")
    if det.wrappers:
        lines.append(f"  Harness wrappers on PATH: {', '.join(sorted(det.wrappers))}")
    for agent, names in sorted(det.config_dirs.items()):
        lines.append(f"  Existing {agent} config directories in your home (names only, never read): {', '.join(names)}")
    lines.append("  Note: `gh auth status` shows only the active GitHub account; setup does not choose accounts for profiles.")
    return "\n".join(lines)


def _path_error(value: str) -> str | None:
    if value == "~" or value.startswith("~/") or value.startswith("/"):
        return None
    return "enter an absolute path or one starting with ~/"


def _env_error(value: str) -> str | None:
    if not value:
        return None
    name, sep, _ = value.partition("=")
    return None if sep and name and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else "use NAME=VALUE"


def _roots_error(value: str) -> str | None:
    for part in (p.strip() for p in value.split(",")):
        if part and (err := _path_error(part)):
            return f"{part}: {err}"
    return None


def _choices(options: dict[str, str]) -> list[Choice]:
    return [Choice(value, label) for value, label in options.items()]


def _ask_profile(
    p: Prompter, det: Detection, config: Config, taken: set[str], which: Which
) -> ProfileAnswer:
    def name_error(value: str) -> str | None:
        if not re.fullmatch(NAME_PATTERN, value):
            return "use letters, digits, '-' and '_'"
        return "that profile already exists" if value in taken else None

    name = p.text("Profile name (for example work or personal)", validate=name_error)
    types = sorted(config.harness_types)
    while True:
        picked = p.checkbox(
            f"Which harnesses does profile {name!r} use?",
            [
                Choice(t, f"{t} (found at {det.agent_paths.get(t)})" if det.agent_paths.get(t) else f"{t} (not found on PATH)")
                for t in types
            ],
            defaults=[t for t in types if det.agent_paths.get(t)],
        )
        if picked:
            break
        p.say("A profile needs at least one harness.")
    agents: dict[str, AgentAnswer] = {}
    for agent in picked:
        answer = AgentAnswer()
        found = det.agent_paths.get(agent)
        if found and p.confirm(f"Use {found} for {agent}?"):
            pass
        else:
            if det.wrappers:
                p.say(f"Wrappers on PATH: {', '.join(sorted(det.wrappers))}")
            answer.executable = p.text(
                f"Executable or wrapper for {agent} (name on PATH, or absolute path)",
                validate=lambda v: None if v.strip() else "required",
            ).strip()
            if (problem := executable_problem(answer.executable, {}, which)):
                p.say(f"Warning: {problem}. It is saved anyway; fix it later with `profile edit`.")
        var = IDENTITY_ENV.get(agent)
        if var:
            options = [Choice(f"~/{n}", f"~/{n}") for n in det.config_dirs.get(agent, [])]
            options += [Choice("__path__", "Enter a path..."), Choice("__none__", "Don't set it (inherit the caller's environment)")]
            chosen = p.select(f"Which directory should {var} point to for {name}/{agent}?", options, default="__none__")
            if chosen == "__path__":
                chosen = p.text(f"{var} for {name}/{agent}", validate=lambda v: _path_error(v))
            if chosen != "__none__":
                answer.env[var] = chosen
        while (extra := p.text(f"Extra environment variable for {name}/{agent} as NAME=VALUE (blank to finish)", validate=_env_error)):
            key, _, value = extra.partition("=")
            answer.env[key] = value
        agents[agent] = answer
    default = None
    if len(agents) > 1:
        default = p.select(f"Default harness for {name}", [Choice(a, a) for a in agents])
    return ProfileAnswer(name=name, harnesses=agents, default_harness=default)


def gather_answers(p: Prompter, det: Detection, config: Config, raw: dict[str, Any] | None, which: Which) -> SetupAnswers:
    p.say(render_detection(det))
    existing = set(config.profiles)
    if existing:
        p.say(f"Existing profiles (left as they are): {', '.join(sorted(existing))}")
    else:
        p.say("No profiles yet. Let's create the first one.")
    added: list[ProfileAnswer] = []
    while True:
        if (existing or added) and not p.confirm("Add a profile?", default=False):
            break
        profile = _ask_profile(p, det, config, existing | {a.name for a in added}, which)
        candidate = apply_answers(raw, SetupAnswers(profiles=[*added, profile]))
        problems = identity_problems(candidate["profiles"], {profile.name})
        if problems:
            for where, message in problems:
                p.say(f"Ambiguous identity at {where}: {message}.")
            if not p.confirm("Keep this profile anyway?", default=False):
                continue
        added.append(profile)

    c = config
    adapter = p.select("Terminal adapter", [Choice(a, a) for a in TERMINAL_ADAPTERS], default=c.terminal.adapter)
    roots = p.text(
        "Repository search roots to add, comma-separated (blank for none; repositories can be set up later)"
        + (f". Kept: {', '.join(c.repositories.search_roots)}" if c.repositories.search_roots else ""),
        validate=_roots_error,
    )
    selection = p.select("Harness selection", _choices(AGENT_SELECTIONS), default=c.harness_selection)
    return SetupAnswers(
        profiles=added,
        terminal_adapter=adapter,
        search_roots=[r.strip() for r in roots.split(",") if r.strip()],
        harness_selection=selection,
    )


# --- running ----------------------------------------------------------------------------------


@dataclass
class SetupResult:
    path: Path
    changed: bool
    applied: bool
    diff: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "changed": self.changed, "applied": self.applied, "diff": self.diff}


def run_setup(
    *,
    prompter: Prompter | None = None,
    answers: SetupAnswers | None = None,
    detection: Detection | None = None,
    assume_yes: bool = False,
    dry_run: bool = False,
    which: Which | None = None,
) -> SetupResult:
    """Run the wizard. With `answers` nothing is asked; otherwise `prompter` is required.

    Raises SetupError, or SetupCancelled when the user backs out. Nothing is written unless the
    result says `applied`.
    """
    which = which or doctor._default_which
    path = config_path()
    try:
        raw = read_raw(path)
        try:
            config = load_config(path)
        except ConfigOutdatedError:
            config = Config()  # an old file is only refused if setup has something to write (below)
    except ConfigError as exc:
        raise SetupError("config_invalid", f"{exc}; setup will not touch an unusable config, fix it first") from exc
    trace("setup started", interactive=answers is None, exists=raw is not None)

    if answers is None:
        if prompter is None:
            raise SetupError("needs_input", "no prompter and no answers file; use --answers FILE --yes")
        if (version := outdated_version(raw)) is not None:
            raise SetupError(ConfigOutdatedError.code, outdated_message(version), "version")  # before any question
        if detection is None:
            from commodore_launcher.detect import detect

            detection = detect(config, which=which)
        answers = gather_answers(prompter, detection, config, raw, which)
    else:
        check_answers(raw, answers, which, config.harness_types)

    new = apply_answers(raw, answers)
    errors, _ = validate_data(new)
    if errors:
        raise SetupError("invalid_config", "; ".join(f"{e.field}: {e.message}" for e in errors), errors[0].field)
    changed = new != (raw or {}) or raw is None
    diff = render_diff(raw, new) if changed else ""

    def say(text: str) -> None:
        if prompter is not None:
            prompter.say(text)

    if not changed:
        say("Nothing to change: your configuration already matches these answers.")
        return SetupResult(path, False, False, "")
    say(f"Proposed changes to {path}:\n{diff}")
    if dry_run:
        return SetupResult(path, True, False, diff)
    if not assume_yes:
        if prompter is None:
            raise SetupError("needs_confirmation", "refusing to write without confirmation; pass --yes")
        if not prompter.confirm("Apply these changes?"):
            say("Nothing was written.")
            return SetupResult(path, True, False, diff)
    try:
        update_config({k: v for k, v in new.items() if raw is None or raw.get(k) != v}, path)
    except ConfigError as exc:
        raise SetupError("invalid_config", str(exc)) from exc
    trace("setup applied", path=str(path))
    say(f"Wrote {path}. Run `commodore doctor` to check it.")
    return SetupResult(path, True, True, diff)
