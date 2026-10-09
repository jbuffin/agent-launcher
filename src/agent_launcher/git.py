"""Git access for worktrees (SPEC §12): argv lists, timeouts, checked exit codes, machine-readable output only.

Nothing here resets, stashes, cleans, force-checks-out, pushes or removes a worktree. The only writes are
`git worktree add`, a read-only-in-effect `git fetch` (of a branch, or of a pull request head into a launcher-owned
ref under `refs/agent-launcher/`), and setting a new local branch's upstream.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent_launcher.errors import LauncherError
from agent_launcher.logs import trace

GIT_TIMEOUT_SECONDS = 15.0
FETCH_TIMEOUT_SECONDS = 30.0


class GitError(LauncherError):
    """A git command failed, timed out, or is not installed."""


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C", GCM_INTERACTIVE="never")
    return env


def run_git(
    repo: str | Path, args: list[str], *, timeout: float = GIT_TIMEOUT_SECONDS, ok: tuple[int, ...] = (0,)
) -> GitResult:
    """`git -C <repo> <args>`. An exit code not in `ok` raises `GitError`; the caller decides what the others mean."""
    argv = ["git", "-C", str(repo), *args]
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, check=False, env=_env()
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError("git_timeout", f"`git {args[0]}` timed out after {timeout:g}s.", command=args[0]) from exc
    except OSError as exc:
        raise GitError("git_unavailable", f"Cannot run git: {exc.strerror or exc}.") from exc
    if done.returncode not in ok:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise GitError(
            "git_failed",
            f"`git {args[0]}` failed (exit {done.returncode}): {detail[-1] if detail else 'no output'}",
            command=args[0],
            exit_code=done.returncode,
        )
    return GitResult(done.returncode, done.stdout, done.stderr)


@dataclass(frozen=True)
class WorktreeEntry:
    path: str
    head: str | None = None
    branch: str | None = None
    """Full ref, for example `refs/heads/task/t-abc`; None when detached or bare."""
    bare: bool = False
    detached: bool = False
    locked: bool = False
    prunable: bool = False

    @property
    def branch_name(self) -> str | None:
        return self.branch.removeprefix("refs/heads/") if self.branch else None


def parse_worktree_list(output: str) -> list[WorktreeEntry]:
    """Parse `git worktree list --porcelain -z`: NUL-terminated fields, an empty field ends a record."""
    entries: list[WorktreeEntry] = []
    current: dict[str, object] = {}

    def flush() -> None:
        if current:
            entries.append(WorktreeEntry(**current))  # type: ignore[arg-type]
            current.clear()

    for field in output.split("\0"):
        if not field:
            flush()
            continue
        key, _, value = field.partition(" ")
        if key == "worktree":
            flush()
            current["path"] = value
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value
        elif key in ("bare", "detached", "locked", "prunable"):
            current[key] = True
    flush()
    return entries


def list_worktrees(repo: str | Path) -> list[WorktreeEntry]:
    """Every worktree of the repository; the first entry is the main one."""
    return parse_worktree_list(run_git(repo, ["worktree", "list", "--porcelain", "-z"]).stdout)


def canonical(path: str | Path) -> str:
    return str(Path(path).resolve())


def is_dirty(path: str | Path) -> bool:
    """Uncommitted or untracked changes (`status --porcelain=v1 -z`)."""
    return bool(run_git(path, ["status", "--porcelain=v1", "-z", "--untracked-files=normal"]).stdout)


def branch_exists(repo: str | Path, branch: str) -> bool:
    return run_git(repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], ok=(0, 1)).returncode == 0


def ref_exists(repo: str | Path, ref: str) -> bool:
    return run_git(repo, ["show-ref", "--verify", "--quiet", ref], ok=(0, 1)).returncode == 0


def valid_branch_name(repo: str | Path, branch: str) -> bool:
    return run_git(repo, ["check-ref-format", "--branch", branch], ok=(0, 1, 128)).returncode == 0


def origin_head(repo: str | Path) -> str | None:
    """The branch `origin/HEAD` points at (`main`), or None when it is not set."""
    done = run_git(repo, ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], ok=(0, 1))
    ref = done.stdout.strip()
    prefix = "refs/remotes/origin/"
    return ref[len(prefix):] if done.returncode == 0 and ref.startswith(prefix) else None


def current_branch(repo: str | Path) -> str | None:
    done = run_git(repo, ["symbolic-ref", "--quiet", "HEAD"], ok=(0, 1))
    ref = done.stdout.strip()
    return ref.removeprefix("refs/heads/") if done.returncode == 0 and ref.startswith("refs/heads/") else None


def configured_default_branch(repo: str | Path) -> str | None:
    done = run_git(repo, ["config", "--get", "init.defaultBranch"], ok=(0, 1))
    return done.stdout.strip() or None


def has_remote(repo: str | Path, name: str = "origin") -> bool:
    return name in run_git(repo, ["remote"]).stdout.split()


def fetch_branch(repo: str | Path, branch: str) -> str | None:
    """Fetch one branch from `origin`. Returns a warning when it fails; the network is never required."""
    try:
        run_git(repo, ["fetch", "--quiet", "--no-tags", "origin", "--", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], timeout=FETCH_TIMEOUT_SECONDS)
    except GitError as exc:
        trace("fetch failed", branch=branch, error=exc.message)
        return f"Could not fetch {branch!r} from origin ({exc.message}); using what is already local."
    return None


def head_commit(path: str | Path) -> str | None:
    done = run_git(path, ["rev-parse", "--verify", "--quiet", "HEAD"], ok=(0, 1))
    return done.stdout.strip() or None


def add_worktree(repo: str | Path, path: str | Path, branch: str, start_ref: str) -> None:
    """Create `branch` at `start_ref` in a new worktree at `path`. Never overwrites: git refuses a non-empty path
    and an existing branch. `--` keeps every value from being read as an option; `--no-track` keeps the new
    branch from tracking (and so pushing to) the base."""
    run_git(repo, ["worktree", "add", "--no-track", "-b", branch, "--", str(path), start_ref])


def add_worktree_tracking(repo: str | Path, path: str | Path, branch: str, remote_ref: str) -> None:
    """Create the local `branch` from `remote_ref` (`refs/remotes/origin/<branch>`) in a new worktree, tracking it."""
    run_git(repo, ["worktree", "add", "--track", "-b", branch, "--", str(path), remote_ref])


def add_worktree_existing(repo: str | Path, path: str | Path, branch: str) -> None:
    """Check out the existing local `branch` in a new worktree. Git refuses if it is checked out elsewhere; there is
    no `--force`."""
    run_git(repo, ["worktree", "add", "--", str(path), branch])


def rev_parse(repo: str | Path, ref: str) -> str | None:
    done = run_git(repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], ok=(0, 1))
    return done.stdout.strip() or None


def branch_upstream(repo: str | Path, branch: str) -> str | None:
    done = run_git(repo, ["for-each-ref", "--format=%(upstream)", f"refs/heads/{branch}"])
    return done.stdout.strip() or None


def set_upstream(repo: str | Path, branch: str, remote_branch: str) -> None:
    run_git(repo, ["branch", f"--set-upstream-to=origin/{remote_branch}", "--", branch])


def pull_ref(number: int) -> str:
    """The launcher-owned ref a pull request's head is fetched into. Nothing else writes under it."""
    return f"refs/agent-launcher/pr-{number}"


def fetch_pull(repo: str | Path, number: int) -> str | None:
    """Fetch `refs/pull/<N>/head` (which GitHub keeps for forks too) from `origin` into `pull_ref(number)`. Returns
    a warning when it fails; the network is never required."""
    try:
        run_git(
            repo,
            ["fetch", "--quiet", "--no-tags", "origin", "--", f"+refs/pull/{number}/head:{pull_ref(number)}"],
            timeout=FETCH_TIMEOUT_SECONDS,
        )
    except GitError as exc:
        trace("pull fetch failed", number=number, error=exc.message)
        return f"Could not fetch pull request #{number} from origin ({exc.message}); using what is already local."
    return None


def has_changes(path: str | Path) -> bool:
    """Any uncommitted change or untracked file, every untracked file listed (`--untracked-files=all`). Ignored
    files do not count."""
    return bool(run_git(path, ["status", "--porcelain=v1", "-z", "--untracked-files=all"]).stdout)


def ignored_entries(path: str | Path, limit: int = 8) -> list[str]:
    """Top-level ignored files and directories (for example `.env`, `node_modules/`), which removal deletes too.
    At most `limit`; best effort, empty when git cannot say."""
    try:
        out = run_git(path, ["status", "--porcelain=v1", "-z", "--ignored=matching", "--untracked-files=normal"]).stdout
    except LauncherError:
        return []
    return [e[3:] for e in out.split("\0") if e.startswith("!! ")][:limit]


def unpushed_commits(path: str | Path, branch: str | None, base_ref: str | None) -> int:
    """Commits on HEAD (and `branch`) that no remote-tracking ref, no fetched pull-request ref and not the base has.

    With no upstream this is every commit the base lacks, so "no upstream" is unpushed unless the branch's commits
    are all in the base."""
    tips = ["HEAD"]
    if branch and branch_exists(path, branch):
        tips.append(f"refs/heads/{branch}")
    exclude = ["--remotes", "--glob=refs/agent-launcher/*"]
    if base_ref and rev_parse(path, base_ref):
        exclude.append(base_ref)
    out = run_git(path, ["rev-list", "--count", *tips, "--not", *exclude]).stdout.strip()
    return int(out or 0)


def remove_worktree(repo: str | Path, path: str | Path) -> None:
    """`git worktree remove` without `--force`: git itself refuses a dirty or locked worktree."""
    run_git(repo, ["worktree", "remove", "--", str(path)])


def delete_branch_if_merged(repo: str | Path, branch: str) -> bool:
    """`git branch -d` (never `-D`): git refuses an unmerged branch. True when it was deleted."""
    return run_git(repo, ["branch", "-d", "--", branch], ok=(0, 1)).returncode == 0
