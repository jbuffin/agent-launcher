"""Redaction for logs and diagnostic exports (SPEC §28).

Everything that leaves the process as text (log lines, `doctor` details, exported
bundles) goes through here. Redaction is deliberately greedy: a false positive hides
a harmless value, a false negative leaks a credential.

What is redacted:
- values of secret-looking names (environment variables, JSON keys, `--token`-style flags)
- well-known token shapes wherever they appear (GitHub, OpenAI-style, Slack, AWS, JWT, Bearer)
- credentials embedded in URLs (`https://user:pass@host`)
- home directories (shown as `~`) and credential locations such as `~/.ssh`
- argument values that look like prompts (multi-line, or very long) and `--prompt` values
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"
REDACTED_PATH = "[REDACTED-PATH]"
PROMPT_OMITTED_LENGTH = 120
"""An argument longer than this (or containing a newline) is treated as prompt text and omitted."""

_SECRET_WORDS = (
    r"token|secret|passw(?:or)?d|passwd|pwd|api[_-]?key|apikey|auth(?!or(?!ization))|credential|"
    r"private[_-]?key|access[_-]?key|session[_-]?key|cookie|bearer"
    r"|(?<![a-z0-9])(?:key|pass|pat)(?![a-z0-9])"
)
SECRET_NAME = re.compile(_SECRET_WORDS, re.IGNORECASE)

_PROMPT_FLAGS = {"--prompt", "-p"}

_URL_CREDENTIALS = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s@]+@", re.IGNORECASE)
_KEY_VALUE = re.compile(
    rf"(?<![\w-])([\w.-]*(?:{_SECRET_WORDS})[\w.-]*)(\"?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)",
    re.IGNORECASE,
)
_SECRET_FLAG = re.compile(
    rf"(?<![\w-])(--?[\w-]*(?:{_SECRET_WORDS})[\w-]*)(\s+)(\"[^\"]*\"|'[^']*'|[^\s\"'-][^\s\"']*)",
    re.IGNORECASE,
)
_AUTH_HEADER = re.compile(r"\b((?:Proxy-)?Authorization)(\"?\s*[:=]\s*)[^\r\n]*", re.IGNORECASE)
_BEARER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_TOKEN_SHAPES = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
)
_HOME = re.compile(r"(?<![\w.~-])/(?:Users|home)/[^/\s\"']+")
_SENSITIVE_PATH = re.compile(
    r"((?:~|/)[^\s\"',]*?)/?(?:\.ssh|\.aws|\.gnupg|\.kube|\.config/gh|\.netrc|\.npmrc|\.pypirc|\.git-credentials"
    r"|\.env(?:\.[\w-]+)?)(?=/|\s|\"|'|,|$)(?:/[^\s\"',]*)?"
)


def _mask_value(match: re.Match[str]) -> str:
    value = match.group(3)
    if value.strip("\"'") == REDACTED:
        return match.group(0)
    return f"{match.group(1)}{match.group(2)}{REDACTED}"


def redact_text(text: str, home: str | None = None) -> str:
    """Redact secrets, credentials in URLs and sensitive paths in free text."""
    if home:
        text = text.replace(home, "~")
    text = _URL_CREDENTIALS.sub(lambda m: f"{m.group(1)}{REDACTED}@", text)
    text = _HOME.sub("~", text)
    text = _SENSITIVE_PATH.sub(lambda m: f"{m.group(1)}/{REDACTED_PATH}".replace("//", "/"), text)
    text = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _KEY_VALUE.sub(_mask_value, text)
    text = _SECRET_FLAG.sub(_mask_value, text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    return _TOKEN_SHAPES.sub(REDACTED, text)


def is_secret_name(name: str) -> bool:
    return SECRET_NAME.search(name) is not None


def redact_env(env: Mapping[str, str], home: str | None = None) -> dict[str, str]:
    """Environment variables: secret-looking names lose their value, the rest are scrubbed."""
    return {
        key: REDACTED if is_secret_name(key) else redact_text(str(value), home)
        for key, value in env.items()
    }


SAFE_ENV_NAMES = frozenset(
    {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "SHELL", "USER", "TMPDIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "COPILOT_HOME"}
)
"""Environment variable names whose values are kept in a config export. Every other value is hidden."""


def redact_env_allowlist(env: Mapping[str, str], home: str | None = None) -> dict[str, str]:
    """Instance environments in an export: only known-safe names keep their (scrubbed) value."""
    return {
        key: redact_text(str(value), home) if key in SAFE_ENV_NAMES and not is_secret_name(key) else REDACTED
        for key, value in env.items()
    }


def redact_args(args: Sequence[str], home: str | None = None) -> list[str]:
    """Command-line arguments: `--token X`, `--token=X`, `KEY=secret`, URLs, and prompt text."""
    out: list[str] = []
    hide_next = False
    omit_next = False
    for arg in args:
        if hide_next:
            out.append(REDACTED)
            hide_next = False
            continue
        if omit_next:
            out.append(f"[PROMPT OMITTED: {len(arg)} chars]")
            omit_next = False
            continue
        if arg in _PROMPT_FLAGS:
            out.append(arg)
            omit_next = True
            continue
        if arg.startswith("--prompt="):
            out.append(f"--prompt=[PROMPT OMITTED: {len(arg) - 9} chars]")
            continue
        if "\n" in arg or len(arg) > PROMPT_OMITTED_LENGTH:
            out.append(f"[PROMPT OMITTED: {len(arg)} chars]")
            continue
        if arg.startswith("-") and "=" not in arg:
            out.append(arg)
            hide_next = is_secret_name(arg)
            continue
        name, sep, value = arg.partition("=")
        if sep and is_secret_name(name):
            out.append(f"{name}={REDACTED}")
            continue
        out.append(redact_text(arg, home))
    return out


_ARG_LIST_KEYS = {"args", "resume_args", "argv"}


def redact_value(value: Any, home: str | None = None, key: str = "") -> Any:
    """Redact a JSON-like structure: secret-named keys, `env` maps, argument lists and all strings."""
    if isinstance(value, Mapping):
        if key == "env":
            return redact_env({str(k): v for k, v in value.items()}, home)
        return {str(k): redact_value(v, home, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        if key in _ARG_LIST_KEYS and all(isinstance(v, str) for v in value):
            return redact_args(value, home)
        return [redact_value(v, home, key) for v in value]
    if value is None or isinstance(value, bool):
        return value
    if key and is_secret_name(key):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value, home)
    return value
