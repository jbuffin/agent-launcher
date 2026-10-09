"""Repository identity (SPEC §6).

A repository is identified by what we can observe about it:

- the canonical local path (symlinks resolved, the checkout's top level),
- its normalised remote URLs (`git remote -v`),
- GitHub's immutable repository ID and node ID, when `gh` can tell us.

The ID is what survives a rename or transfer, so it is the preferred key. Fetching it is
optional: offline, unauthenticated or without `gh`, identification still succeeds with
path and remotes only. Nothing here reads or writes the state database.
"""

import json
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from agent_launcher.doctor import CommandError, CommandResult
from agent_launcher.doctor import run_command as _real_run_command
from agent_launcher.logs import trace

GIT_TIMEOUT_SECONDS = 10.0
GH_TIMEOUT_SECONDS = 10.0

Runner = Callable[[Sequence[str], float], CommandResult]

GITHUB_HOST = "github.com"
_OWNER_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SCP_LIKE = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+):(?!//)(.+)$")


def run_command(argv: Sequence[str], timeout: float) -> CommandResult:
    """The default runner. Tests replace this module attribute (or pass `runner=`)."""
    return _real_run_command(argv, timeout)


class RepositoryError(Exception):
    """A repository reference could not be understood. `code` is stable for JSON consumers."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RepositoryIdentity:
    path: str | None
    """Canonical top-level path of a local checkout; None when only a name or URL was given."""
    remotes: tuple[str, ...]
    """The one primary remote (normalised), or empty. Only this ever identifies the repository."""
    other_remotes: tuple[str, ...] = ()
    """Further remotes of a checkout, for display only. Never stored and never used to match."""
    github_id: int | None = None
    node_id: str | None = None
    full_name: str | None = None
    """`owner/name` as last seen (from GitHub if reachable, else from the primary github.com remote)."""

    def describe(self) -> str:
        parts = [self.full_name or (self.remotes[0] if self.remotes else "unknown repository")]
        if self.path:
            parts.append(self.path)
        if self.github_id is not None:
            parts.append(f"GitHub ID {self.github_id}")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "remotes": list(self.remotes),
            "other_remotes": list(self.other_remotes),
            "github_id": self.github_id,
            "node_id": self.node_id,
            "full_name": self.full_name,
        }


def normalize_remote(url: str) -> str:
    """`git@GitHub.com:Owner/Repo.git`, `https://user:tok@github.com/owner/repo/` and
    `ssh://git@github.com/owner/repo` all become `github.com/owner/repo`. Credentials are dropped."""
    text = url.strip()
    if "://" in text:
        parts = urlsplit(text)
        host, path = (parts.hostname or "").lower(), parts.path
        if parts.scheme == "file" or not host:
            return text.rstrip("/")
    else:
        match = _SCP_LIKE.match(text)
        if not match:
            return text.rstrip("/")  # a plain filesystem path remote
        host, path = match.group(1).lower(), match.group(2)
    path = path.strip("/")
    path = path.removesuffix(".git").rstrip("/")
    if host == GITHUB_HOST:
        path = path.lower()
    return f"{host}/{path}"


def _is_owner_name(text: str) -> bool:
    return bool(_OWNER_NAME.match(text)) and not any(part in (".", "..") for part in text.split("/"))


def github_name(normalized_remote: str) -> str | None:
    """`owner/name` for a normalised github.com remote, else None."""
    host, _, rest = normalized_remote.partition("/")
    if host == GITHUB_HOST and _is_owner_name(rest):
        return rest
    return None


def _git(runner: Runner, cwd: Path, *args: str) -> CommandResult:
    return runner(["git", "-C", str(cwd), *args], GIT_TIMEOUT_SECONDS)


def local_identity(path: Path, runner: Runner) -> RepositoryIdentity:
    """Path and remotes of the checkout containing `path`. No network."""
    try:
        top = _git(runner, path, "rev-parse", "--show-toplevel")
    except CommandError as exc:
        raise RepositoryError("git_unavailable", f"cannot run git: {exc}") from exc
    if top.returncode != 0 or not top.stdout.strip():
        raise RepositoryError("not_a_repository", f"{path} is not inside a git repository")
    canonical = os.path.realpath(top.stdout.strip())
    named: dict[str, str] = {}
    try:
        listing = _git(runner, Path(canonical), "remote", "-v")
        if listing.returncode == 0:
            for line in listing.stdout.splitlines():
                fields = line.split()
                if len(fields) >= 2:
                    named.setdefault(fields[0], normalize_remote(fields[1]))
        branch = _git(runner, Path(canonical), "symbolic-ref", "--short", "-q", "HEAD")
        upstream = ""
        if branch.returncode == 0 and branch.stdout.strip():
            configured = _git(runner, Path(canonical), "config", "--get", f"branch.{branch.stdout.strip()}.remote")
            upstream = configured.stdout.strip() if configured.returncode == 0 else ""
    except CommandError as exc:
        raise RepositoryError("git_unavailable", f"cannot run git: {exc}") from exc
    # The primary remote is `origin`; failing that, the current branch's upstream remote;
    # failing that there is no remote identity and the checkout is known by path alone.
    # Other remotes (forks, `upstream`) never identify the repository.
    primary_name = "origin" if "origin" in named else upstream if upstream in named else None
    primary = named[primary_name] if primary_name else None
    others = sorted({url for name, url in named.items() if name != primary_name and url != primary})
    return RepositoryIdentity(
        path=canonical,
        remotes=(primary,) if primary else (),
        other_remotes=tuple(others),
        full_name=github_name(primary) if primary else None,
    )


def fetch_github_identity(full_name: str, runner: Runner) -> tuple[int, str | None, str] | None:
    """`(id, node_id, owner/name)` from `gh api repos/{owner}/{name}`, or None for any failure."""
    try:
        result = runner(
            ["gh", "api", f"repos/{full_name}", "--jq", "{id: .id, node_id: .node_id, full_name: .full_name}"],
            GH_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            trace("github id unavailable", repository=full_name, reason=result.stderr.strip()[:200])
            return None
        data = json.loads(result.stdout)
        repo_id = data["id"]
        if isinstance(repo_id, bool) or not isinstance(repo_id, int):
            return None
        node_id = data.get("node_id")
        name = data.get("full_name")
        return repo_id, node_id if isinstance(node_id, str) else None, name if isinstance(name, str) else full_name
    except (CommandError, ValueError, KeyError, TypeError, AttributeError) as exc:
        trace("github id unavailable", repository=full_name, reason=str(exc))
        return None


def _with_github(identity: RepositoryIdentity, runner: Runner) -> RepositoryIdentity:
    names = [n for n in (github_name(r) for r in identity.remotes) if n]
    if not names:
        return identity
    fetched = fetch_github_identity(names[0], runner)
    if fetched is None:
        return identity
    repo_id, node_id, full_name = fetched
    return replace(identity, github_id=repo_id, node_id=node_id, full_name=full_name)


def identify_reference(reference: str, *, fetch_github: bool = True, runner: Runner | None = None) -> RepositoryIdentity:
    """Identify what the user typed: a path, `owner/name`, or a GitHub URL.

    A path is identified by the checkout it belongs to. A name or URL has no local path.
    An argument that is an existing directory is always treated as a path.
    """
    runner = runner or run_command
    text = reference.strip()
    if not text:
        raise RepositoryError("invalid_repository", "repository reference is empty")
    expanded = Path(text).expanduser()
    if expanded.exists() or text.startswith(("/", ".", "~")):
        if not expanded.is_dir():
            raise RepositoryError("not_a_repository", f"{text} is not a directory")
        identity = local_identity(expanded, runner)
    elif "://" in text or _SCP_LIKE.match(text):
        remote = normalize_remote(text)
        identity = RepositoryIdentity(path=None, remotes=(remote,), full_name=github_name(remote))
    elif _is_owner_name(text):
        remote = normalize_remote(f"https://{GITHUB_HOST}/{text}")
        identity = RepositoryIdentity(path=None, remotes=(remote,), full_name=github_name(remote))
    else:
        raise RepositoryError(
            "invalid_repository", f"{text!r} is not a path, an owner/name or a GitHub URL"
        )
    return _with_github(identity, runner) if fetch_github else identity
