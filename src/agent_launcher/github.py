"""The one place that talks to GitHub: the `gh` CLI, as an argv list, with a timeout (SPEC §7).

- `gh api` only reads. Nothing here runs `gh auth` anything, so the globally active account is never switched.
- Authentication is shared by every profile for now (`SharedAuth`). `GitHubAuth` is the seam where a per-profile
  token or `GH_CONFIG_DIR` can be supplied later without touching callers. GitHub identity never decides a
  repository's profile.
- Issue titles are untrusted. Only validated `owner`, `name` and number reach argv, and the title is stored
  after control characters are removed; it never appears in a command.
- Failures are `GitHubError` with a stable code.
"""

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from agent_launcher.doctor import CommandError, CommandResult
from agent_launcher.errors import LauncherError
from agent_launcher.logs import trace

GH_TIMEOUT_SECONDS = 20.0
CLONE_TIMEOUT_SECONDS = 600.0
MAX_TITLE = 200

_NAME_PART = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"
_OWNER_NAME = re.compile(rf"^({_NAME_PART})/({_NAME_PART})$")
_PATH = re.compile(rf"^/({_NAME_PART})/({_NAME_PART})/(issues|pull)/([1-9][0-9]{{0,9}})/?$")


class GitHubError(LauncherError):
    pass


class GitHubAuth(Protocol):
    def env(self, profile: str | None) -> Mapping[str, str] | None:
        """Extra environment for `gh` on behalf of `profile`; None keeps the caller's environment."""


class SharedAuth:
    """Every profile uses the account `gh` is already logged in with. It is read, never changed."""

    def env(self, profile: str | None) -> Mapping[str, str] | None:
        return None


Runner = Callable[[Sequence[str], float, Mapping[str, str] | None], CommandResult]


def run_gh(argv: Sequence[str], timeout: float, env: Mapping[str, str] | None = None) -> CommandResult:
    """The default runner: an argv list (never a shell), a timeout, no stdin. Tests replace this attribute."""
    full_env = {**os.environ, **env} if env else None
    try:
        done = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
            check=False, env=full_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise CommandError(exc.strerror or str(exc)) from exc
    return CommandResult(done.returncode, done.stdout or "", done.stderr or "")


@dataclass(frozen=True)
class IssueRef:
    """What an issue URL says. Mutable names; the IDs come from GitHub (`IssueMetadata`)."""

    owner: str
    name: str
    number: int

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class RepoMetadata:
    github_id: int
    node_id: str | None
    full_name: str
    """Current `owner/name`, which may differ from the one in the URL after a rename or transfer."""
    private: bool = False


@dataclass(frozen=True)
class IssueMetadata:
    kind: str
    node_id: str
    database_id: int
    number: int
    title: str
    state: str
    labels: tuple[str, ...]
    author: str | None
    assignees: tuple[str, ...]
    url: str
    repository: RepoMetadata = field(compare=False)


def parse_issue_url(text: str) -> IssueRef:
    """`https://github.com/owner/repo/issues/N`. Pull requests and other hosts are refused for now."""
    raw = text.strip()
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() != "github.com":
        raise GitHubError("unsupported_url", f"{raw!r} is not a github.com URL.")
    if parts.username or parts.password or parts.port:
        raise GitHubError("unsupported_url", "A GitHub URL with credentials or a port is not accepted.")
    match = _PATH.match(parts.path)
    if not match:
        raise GitHubError("unsupported_url", f"{raw!r} is not a GitHub issue URL (https://github.com/owner/repo/issues/N).")
    owner, name, kind, number = match.groups()
    if kind == "pull":
        raise GitHubError("unsupported_url", "Pull request URLs are not supported yet; only issues.", url=raw)
    if any(part in (".", "..") for part in (owner, name)):
        raise GitHubError("unsupported_url", f"{raw!r} is not a GitHub issue URL.")
    return IssueRef(owner, name, int(number))


def parse_full_name(text: str) -> tuple[str, str]:
    match = _OWNER_NAME.match(text)
    if not match or ".." in text or text.startswith(".") or "/." in text:
        raise GitHubError("invalid_repository", f"{text!r} is not an owner/name.")
    return match.group(1), match.group(2)


def clean_text(text: str, limit: int = MAX_TITLE) -> str:
    """Untrusted one-line text: control characters (including newlines and escapes) become spaces."""
    flat = "".join(" " if (not ch.isprintable() and ch != " ") else ch for ch in text)
    return " ".join(flat.split())[:limit]


def _classify(stderr: str) -> tuple[str, str]:
    low = stderr.lower()
    if "http 404" in low or "not found" in low:
        return "not_found", "GitHub does not know it, or this account cannot see it."
    if "gh auth login" in low or "http 401" in low or "authentication" in low or "bad credentials" in low:
        return "github_not_authenticated", "gh is not logged in. Run `gh auth login` (the launcher never does)."
    if "rate limit" in low or "http 403" in low:
        return "github_forbidden", "GitHub refused the request (rate limit or permissions)."
    return "github_error", "gh failed."


class GitHub:
    def __init__(self, runner: Runner | None = None, auth: GitHubAuth | None = None) -> None:
        self._runner = runner
        self._auth = auth or SharedAuth()

    def _run(self, argv: Sequence[str], timeout: float, profile: str | None = None) -> CommandResult:
        runner = self._runner or run_gh
        try:
            return runner(argv, timeout, self._auth.env(profile))
        except CommandError as exc:
            missing = "no such file" in str(exc).lower()
            raise GitHubError(
                "gh_unavailable" if missing else "github_unreachable",
                "The GitHub CLI (`gh`) is not installed." if missing else f"Could not reach GitHub through gh: {exc}",
            ) from exc

    def _api(self, path: str) -> dict[str, Any]:
        result = self._run(["gh", "api", path], GH_TIMEOUT_SECONDS)
        if result.returncode != 0:
            code, why = _classify(result.stderr)
            trace("gh api failed", path=path, reason=result.stderr.strip()[:200])
            raise GitHubError(code, f"{why} (gh api {path})", path=path, detail=result.stderr.strip()[:300])
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise GitHubError("github_error", f"gh api {path} returned something that is not JSON.") from exc
        if not isinstance(data, dict):
            raise GitHubError("github_error", f"gh api {path} returned an unexpected shape.")
        return data

    def repository(self, full_name: str) -> RepoMetadata:
        owner, name = parse_full_name(full_name)
        data = self._api(f"repos/{owner}/{name}")
        repo_id = data.get("id")
        if isinstance(repo_id, bool) or not isinstance(repo_id, int):
            raise GitHubError("github_error", f"GitHub gave no repository ID for {full_name}.")
        node = data.get("node_id")
        current = data.get("full_name")
        return RepoMetadata(
            repo_id, node if isinstance(node, str) else None, current if isinstance(current, str) else full_name,
            bool(data.get("private")),
        )

    def issue(self, ref: IssueRef) -> IssueMetadata:
        data = self._api(f"repos/{ref.owner}/{ref.name}/issues/{ref.number}")
        if "pull_request" in data:
            raise GitHubError("unsupported_url", "That is a pull request, not an issue; pull requests are not supported yet.")
        db_id, node, number = data.get("id"), data.get("node_id"), data.get("number")
        if isinstance(db_id, bool) or not isinstance(db_id, int) or not isinstance(node, str) or not isinstance(number, int):
            raise GitHubError("github_error", "GitHub's issue response had no stable IDs.")
        repo = self.repository(ref.full_name)
        names = lambda items, key: tuple(  # noqa: E731
            clean_text(str(i[key]), 100) for i in items or [] if isinstance(i, dict) and i.get(key)
        )
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        # Prefer the canonical URL GitHub reports (it follows a rename) if it is a valid issue URL.
        url = f"https://github.com/{repo.full_name}/issues/{number}"
        reported = data.get("html_url")
        if isinstance(reported, str):
            try:
                if parse_issue_url(reported).number == number:
                    url = reported
            except GitHubError:
                pass  # not an issue URL for this number: keep the one built from the IDs
        return IssueMetadata(
            kind="issue",
            node_id=node,
            database_id=db_id,
            number=number,
            title=clean_text(str(data.get("title") or "")) or f"Issue #{number}",
            state=str(data.get("state") or "unknown"),
            labels=names(data.get("labels"), "name"),
            author=clean_text(str(user.get("login")), 100) if user.get("login") else None,
            assignees=names(data.get("assignees"), "login"),
            url=url,
            repository=repo,
        )

    def clone(self, full_name: str, target: str) -> None:
        """`gh repo clone owner/name <absolute target>`. The target is an absolute path, so never an option."""
        owner, name = parse_full_name(full_name)
        if not os.path.isabs(target):
            raise GitHubError("invalid_clone_target", "The clone target must be an absolute path.")
        result = self._run(["gh", "repo", "clone", f"{owner}/{name}", target], CLONE_TIMEOUT_SECONDS)
        if result.returncode != 0:
            code, why = _classify(result.stderr)
            raise GitHubError(
                "clone_failed" if code in ("github_error", "not_found") else code,
                f"Cloning {full_name} failed: {result.stderr.strip()[:300] or why}",
                repository=full_name,
            )
