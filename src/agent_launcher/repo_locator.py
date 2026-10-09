"""Find the local checkout of a GitHub repository (SPEC §11).

Order, stopping at the first checkout that is proven to be the repository:

1. an explicit mapping in `repositories.mappings`;
2. a path remembered from earlier (`repository_paths`), validated again before reuse;
3. the configured `search_roots` (and `clone_root` if you set it), two levels deep and never recursive, matching on
   `origin`; the place a clone would go is also checked, so a clone from an earlier, interrupted launch is reused;
4. remote inspection: checkouts in those roots whose `origin` is a different GitHub name are asked about by
   repository ID (bounded), which finds a repository that was renamed or transferred;
5. an offer to clone into `clone_root` (default answer No; `auto_clone` skips the question).

A checkout counts only if its primary remote is the repository. The filesystem is never searched outside the
configured directories, linked worktrees are not repositories here, and nothing is cloned silently unless
`auto_clone` is on.
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_launcher.config import DEFAULT_CLONE_ROOT, Config
from agent_launcher.doctor import CommandError
from agent_launcher.errors import LauncherError
from agent_launcher.github import GitHub, GitHubError, RepoMetadata, parse_full_name
from agent_launcher.interaction import Choice, Prompter
from agent_launcher.logs import trace
from agent_launcher.repositories import (
    Runner,
    RepositoryError,
    github_name,
    local_identity,
    normalize_remote,
    run_command,
)
from agent_launcher.state import transaction

MAX_ENTRIES = 5000
"""Directory entries looked at while scanning, so a huge root cannot make `open` hang."""
MAX_ID_LOOKUPS = 25
"""Renamed-repository checks (one `gh api` call each) per `open`."""
ID_CACHE_DAYS = 7
"""How long a remote URL's repository ID is trusted from `github_remote_ids`. Short, because a name can be reused."""


@dataclass(frozen=True)
class Located:
    path: str
    how: str
    """`mapping`, `cached`, `search_root`, `remote_inspection` or `cloned`."""
    cloned: bool = False


def _expand(path: str) -> Path:
    return Path(path).expanduser()


def _origin_url(checkout: Path) -> str | None:
    """`origin`'s URL straight from `.git/config`: no process per directory while scanning."""
    try:
        text = (checkout / ".git" / "config").read_text(errors="replace")
    except OSError:
        return None
    in_origin = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_origin = re.fullmatch(r'\[remote\s+"origin"\]', stripped) is not None
        elif in_origin and re.match(r"url\s*=", stripped):
            return stripped.split("=", 1)[1].strip()
    return None


class _Checker:
    def __init__(
        self, conn: sqlite3.Connection, github: GitHub, repo: RepoMetadata, offline: bool, runner: Runner | None
    ) -> None:
        self.conn, self.github, self.repo, self.offline = conn, github, repo, offline
        self.runner = runner or run_command
        self.want = normalize_remote(f"https://github.com/{repo.full_name}")
        self.lookups = 0

    def checkout(self, path: Path) -> Path | None:
        """The main checkout (top level of the repository, not a subdirectory or a linked worktree) at `path`
        if its primary remote is this repository, else None."""
        if not path.is_dir():
            return None
        try:
            identity = local_identity(path, self.runner)
            common = self.runner(
                ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"], 10.0
            )
        except (RepositoryError, CommandError):
            return None
        git_dir = Path(common.stdout.strip()) if common.returncode == 0 and common.stdout.strip() else None
        if not identity.remotes or git_dir is None or git_dir.name != ".git":
            return None  # no remote, or a bare repository
        remote = identity.remotes[0]
        if not self._same_id(remote, default=remote == self.want):
            return None
        return git_dir.parent.resolve()

    def _same_id(self, remote: str, *, default: bool) -> bool:
        """Does GitHub say `remote` is this repository's ID? `default` when we cannot ask (offline, or a lookup
        failed): a name match stands, a different name does not."""
        name = github_name(remote)
        if name is None:
            return False
        if remote == self.want and self.offline:
            return True
        cached = self._cached_id(remote)
        if cached is not None:
            return cached == self.repo.github_id
        if self.offline or self.lookups >= MAX_ID_LOOKUPS:
            return default
        self.lookups += 1
        try:
            found = self.github.repository(name).github_id
        except GitHubError:
            return default
        self._store_id(remote, found)
        return found == self.repo.github_id

    def _cached_id(self, remote: str) -> int | None:
        row = self.conn.execute("SELECT github_id, checked_at FROM github_remote_ids WHERE url = ?", (remote,)).fetchone()
        if row is None:
            return None
        try:
            fresh = datetime.fromisoformat(row[1]) > datetime.now(timezone.utc) - timedelta(days=ID_CACHE_DAYS)
        except ValueError:
            fresh = False
        return row[0] if fresh else None

    def _store_id(self, remote: str, github_id: int) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR REPLACE INTO github_remote_ids (url, github_id, checked_at) VALUES (?, ?, ?)",
                (remote, github_id, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )


def _scan(config: Config) -> list[Path]:
    """Checkouts (a `.git` directory, so not linked worktrees) one or two levels below each configured root."""
    settings = config.repositories
    roots = [_expand(r) for r in (*settings.search_roots, *([settings.clone_root] if settings.clone_root else []))]
    skip = _expand(config.repositories.worktree_root).resolve()
    found: dict[str, Path] = {}
    seen = 0
    for root in roots:
        try:
            children = sorted(p for p in root.iterdir() if not p.name.startswith(".") and p.is_dir())
        except OSError:
            continue
        for child in children:
            seen += 1
            if seen > MAX_ENTRIES:
                break
            if child.resolve() == skip:
                continue
            if (child / ".git").is_dir():
                found.setdefault(str(child.resolve()), child)
                continue
            try:
                grandchildren = sorted(p for p in child.iterdir() if not p.name.startswith(".") and p.is_dir())
            except OSError:
                continue
            for grand in grandchildren:
                seen += 1
                if seen > MAX_ENTRIES:
                    break
                if (grand / ".git").is_dir():
                    found.setdefault(str(grand.resolve()), grand)
    return list(found.values())


def _cached_paths(conn: sqlite3.Connection, repo: RepoMetadata) -> list[str]:
    rows = conn.execute(
        """SELECT p.path FROM repository_paths p JOIN repositories r ON r.id = p.repository_id
           WHERE r.github_id = ? ORDER BY p.path""",
        (repo.github_id,),
    ).fetchall()
    return [r[0] for r in rows]


def locate(
    conn: sqlite3.Connection,
    config: Config,
    repo: RepoMetadata,
    *,
    names: tuple[str, ...] = (),
    github: GitHub,
    prompter: Prompter | None,
    offline: bool = False,
    runner: Runner | None = None,
) -> Located:
    """The checkout of `repo`, cloning it (after asking) when there is none. `names` are other `owner/name`
    spellings to look up in `mappings` (the one in the URL, after a rename)."""
    checker = _Checker(conn, github, repo, offline, runner)
    wanted = {n.lower() for n in (repo.full_name, *names)}

    for key, value in config.repositories.mappings.items():
        if key.lower() in wanted:
            path = _expand(value)
            root = checker.checkout(path)
            if root is None:
                raise LauncherError(
                    "mapping_invalid",
                    f"repositories.mappings maps {key} to {path}, which is not a checkout of {repo.full_name} "
                    "(missing, not a git repository, or its origin is a different repository). "
                    "Fix or remove the mapping; nothing else is tried.",
                    repository=repo.full_name,
                    path=str(path),
                )
            return Located(str(root), "mapping")

    for cached in _cached_paths(conn, repo):
        root = checker.checkout(Path(cached))
        if root is not None:
            return Located(str(root), "cached")
        trace("cached repository path rejected", path=cached)

    scanned = _scan(config)
    want_url = checker.want
    by_name = [p for p in scanned if (u := _origin_url(p)) and normalize_remote(u) == want_url]
    verified = [r for p in by_name if (r := checker.checkout(p))]
    how = "search_root"
    if not verified and not offline:
        others = [p for p in scanned if p not in by_name and (u := _origin_url(p)) and github_name(normalize_remote(u))]
        verified = [r for p in others if (r := checker.checkout(p))]
        how = "remote_inspection"
    if len(verified) == 1:
        return Located(str(verified[0]), how)
    if len(verified) > 1:
        paths = [str(p) for p in verified]
        if prompter is None:
            raise LauncherError(
                "ambiguous_repository_location",
                f"{repo.full_name} has more than one checkout in your search roots: {', '.join(paths)}. "
                "Pick one with a `repositories.mappings` entry.",
                repository=repo.full_name,
                paths=paths,
            )
        chosen = prompter.select(
            f"Which checkout of {repo.full_name} should be used?", [Choice(p, p) for p in paths], default=paths[0]
        )
        return Located(chosen, how)
    return _clone(config, repo, github=github, prompter=prompter, checker=checker)


def _clone(config: Config, repo: RepoMetadata, *, github: GitHub, prompter: Prompter | None, checker: _Checker) -> Located:
    settings = config.repositories
    owner, name = parse_full_name(repo.full_name)
    root = _expand(settings.clone_root or DEFAULT_CLONE_ROOT)
    target = root / name
    # Where a clone would go may already hold one: a clone from an earlier launch that stopped (at the profile
    # question, say). Only these exact paths are looked at, wherever `clone_root` points.
    for spot in (root / name, root / f"{owner}-{name}"):
        found = checker.checkout(spot) if spot.is_dir() else None
        if found is not None:
            return Located(str(found), "search_root")
    if target.exists() or target.is_symlink():
        target = root / f"{owner}-{name}"
    if not settings.auto_clone:
        hint = {"repository": repo.full_name, "clone_root": str(root), "target": str(target)}
        if prompter is None:
            raise LauncherError(
                "repository_not_found",
                f"No local checkout of {repo.full_name} was found in the mappings, the remembered paths or the "
                f"search roots. It was not cloned: ask on a terminal, set repositories.auto_clone, or add a mapping. "
                f"It would be cloned to {target}.",
                **hint,
            )
        if not prompter.confirm(f"{repo.full_name} is not on this machine. Clone it into {target}?", default=False):
            raise LauncherError(
                "repository_not_found", f"{repo.full_name} has no local checkout and was not cloned.", **hint
            )
    if target.exists() or target.is_symlink():
        raise LauncherError(
            "clone_target_exists",
            f"{target} already exists and is not a checkout of {repo.full_name}; nothing was cloned. "
            "Move it, or map the repository to a checkout with `repositories.mappings`.",
            repository=repo.full_name,
            target=str(target),
        )
    root.mkdir(parents=True, exist_ok=True)
    github.clone(repo.full_name, str(target))
    cloned = checker.checkout(target)
    if cloned is None:
        raise LauncherError(
            "clone_mismatch",
            f"{target} was cloned but its origin does not match {repo.full_name}. It was left in place.",
            repository=repo.full_name,
            target=str(target),
        )
    return Located(str(cloned), "cloned", cloned=True)
