"""Task worktrees (SPEC §12): create, discover, adopt, and track who owns what.

Each task runs in its own Git worktree under `repositories.worktree_root`:

    <root>/<repo-name>-<repository-id>/<task-id>

The task ID is opaque and the repository name is sanitised, so the path is safe for any title and cannot
collide across repositories. The branch is `task/<task-id>-<title-slug>`; the title only ever appears inside a
ref name that git has validated, and every git call passes values after `--`.

Ownership is recorded per worktree: `created` (the launcher made it) or `adopted` (the user said it belongs to
this task, through the Prompter or `worktrees associate`). A branch-name match is never proof of either.

A pull request task (ticket #13) is checked out differently. The user's own PR with a head branch in the same
repository uses that very branch, tracking `origin/<head>`, when it is not checked out anywhere else (if it is, that
worktree is offered for adoption, never taken over). Anyone else's PR, a fork's, or a head branch that cannot be had,
gets an isolated review worktree on `review/pr-<N>`, cut from `refs/agent-launcher/pr-<N>` (a ref the launcher
fetches `refs/pull/<N>/head` into) with no upstream, so nothing can be pushed to the contributor's branch. A head
branch that is not present locally is normal: it is created from `origin/<head>`.

Nothing here resets, stashes, cleans, force-checks-out, pushes or removes. Dirty trees and branch conflicts
are presented as choices (interactive) or a structured error (otherwise).
"""

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_launcher import git, launches
from agent_launcher.config import Config
from agent_launcher.errors import LauncherError
from agent_launcher.interaction import Choice, Prompter
from agent_launcher.logs import trace
from agent_launcher.sessions import primary_session
from agent_launcher.state import transaction
from agent_launcher.tasks import Task, record_event

CREATED = "created"
ADOPTED = "adopted"

_SLUG_MAX = 40


class WorktreeError(LauncherError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class WorktreeRecord:
    task_id: str
    repository_id: int
    path: str
    branch: str | None
    ownership: str
    base_ref: str | None
    created_at: str
    removed_at: str | None = None
    """Set when `cleanup` removed the directory (ticket #22). The row stays as history."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "repository_id": self.repository_id,
            "path": self.path,
            "branch": self.branch,
            "ownership": self.ownership,
            "base_ref": self.base_ref,
            "created_at": self.created_at,
            "removed_at": self.removed_at,
        }


_COLUMNS = "task_id, repository_id, path, branch, ownership, base_ref, created_at"
_SELECT = _COLUMNS + ", removed_at"


def _require_on_disk(record: WorktreeRecord) -> None:
    if record.removed_at:
        raise WorktreeError(
            "worktree_removed",
            f"The worktree {record.path} for task {record.task_id} was removed by `agent-launcher cleanup` on "
            f"{record.removed_at}. Nothing was started and nothing is recreated over it; the task record, session "
            "record and branch history are kept. To work on the task again, add a worktree yourself "
            f"(`git worktree add <path> <branch>` in the repository) and run "
            f"`agent-launcher worktrees associate {record.task_id} <path> --force`.",
            path=record.path,
            task=record.task_id,
        )
    if not Path(record.path).is_dir():
        raise WorktreeError(
            "worktree_missing",
            f"The worktree {record.path} for task {record.task_id} no longer exists on disk. Nothing was closed or "
            f"started, and nothing is recreated over it; restore it, or see "
            f"`agent-launcher worktrees inspect {record.task_id}`.",
            path=record.path,
            task=record.task_id,
        )


def get_worktree(conn: sqlite3.Connection, task_id: str) -> WorktreeRecord | None:
    row = conn.execute(f"SELECT {_SELECT} FROM worktrees WHERE task_id = ?", (task_id,)).fetchone()
    return WorktreeRecord(*row) if row else None


def list_worktree_records(conn: sqlite3.Connection) -> list[WorktreeRecord]:
    rows = conn.execute(f"SELECT {_SELECT} FROM worktrees ORDER BY created_at, task_id").fetchall()
    return [WorktreeRecord(*r) for r in rows]


def _owner_of(conn: sqlite3.Connection, path: str) -> str | None:
    row = conn.execute("SELECT task_id FROM worktrees WHERE path = ?", (path,)).fetchone()
    return row[0] if row else None


def _record(
    conn: sqlite3.Connection, task: Task, path: str, branch: str | None, ownership: str, base_ref: str | None
) -> WorktreeRecord:
    try:
        with transaction(conn):
            conn.execute(
                f"INSERT INTO worktrees ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task.id, task.repository_id, path, branch, ownership, base_ref, _now()),
            )
    except sqlite3.IntegrityError as exc:
        # Another process recorded this task's worktree, or this path, first.
        raise WorktreeError(
            "worktree_conflict",
            f"A worktree for task {task.id} or at {path} was recorded by another process first. Nothing was changed; "
            "run the command again.",
            path=path,
            task=task.id,
        ) from exc
    trace("worktree recorded", task=task.id, ownership=ownership)
    record = get_worktree(conn, task.id)
    assert record is not None
    return record


# --- naming ---------------------------------------------------------------------------------------------


def slugify(text: str, limit: int = _SLUG_MAX) -> str:
    """Lower-case ASCII letters, digits and single dashes. Anything else (including a leading `-`) is dropped."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].strip("-")


def branch_name_for(task: Task, repo: str) -> str:
    slug = slugify(task.title)
    candidate = f"task/{task.id}-{slug}" if slug else f"task/{task.id}"
    return candidate if git.valid_branch_name(repo, candidate) else f"task/{task.id}"


def worktree_path_for(task: Task, config: Config) -> Path:
    root = Path(config.repositories.worktree_root).expanduser()
    name = slugify(Path(task.repo_path).name, 60) or "repo"
    return root / f"{name}-{task.repository_id}" / task.id


# --- base branch ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BaseRef:
    branch: str
    source: str
    """`config`, `origin/HEAD` or `local default`."""
    start_ref: str
    warning: str | None = None

    @property
    def guessed(self) -> bool:
        return self.source == "local default"


def _configured_base(config: Config, repo: str) -> str | None:
    for key, branch in config.repositories.base_branches.items():
        if git.canonical(Path(key).expanduser()) == repo:
            return branch
    return None


def resolve_base(repo: str, config: Config, *, offline: bool = False) -> BaseRef:
    """Configured base branch, then `origin/HEAD`, then the local default. `main` is never assumed."""
    source = "config"
    branch = _configured_base(config, repo)
    if branch is None:
        source, branch = "origin/HEAD", git.origin_head(repo)
    if branch is None:
        source = "local default"
        default = git.configured_default_branch(repo)
        if default and git.branch_exists(repo, default):
            branch = default
        else:
            branch = git.current_branch(repo)
    if branch is None:
        raise WorktreeError(
            "base_branch_unknown",
            f"Cannot tell which branch to cut task worktrees from in {repo}: no configured base branch, no "
            "origin/HEAD, and no local default branch. Set repositories.base_branches for it in config.json.",
            repo_path=repo,
        )
    warning = None
    if not offline and git.has_remote(repo):
        warning = git.fetch_branch(repo, branch)
    remote_ref, local_ref = f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"
    # Origin's copy is the base when there is one, even where `origin/HEAD` was never set (only `clone` sets it).
    if git.ref_exists(repo, remote_ref):
        start = remote_ref
    elif git.ref_exists(repo, local_ref):
        start = local_ref
    else:
        raise WorktreeError(
            "base_branch_missing",
            f"The base branch {branch!r} ({source}) does not exist in {repo}, locally or on origin. "
            "A repository with no commits has none yet.",
            repo_path=repo,
            branch=branch,
            source=source,
        )
    return BaseRef(branch, source, start, warning)


# --- discovery -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    entry: git.WorktreeEntry
    dirty: bool
    branch_matches: bool
    """The branch name looks like this task's. A hint for ordering the choices, never proof."""

    def label(self) -> str:
        what = self.entry.branch_name or ("detached HEAD" if self.entry.detached else "no branch")
        notes = [what] + (["has uncommitted changes"] if self.dirty else []) + (
            ["branch name looks like this task's"] if self.branch_matches else []
        )
        return f"{self.entry.path} ({', '.join(notes)})"


def discover(conn: sqlite3.Connection, task: Task, branch: str | None = None) -> list[Candidate]:
    """Worktrees of the task's repository that could be adopted: not the main checkout, not bare or prunable,
    not already some task's. Matching branches sort first but are not trusted."""
    entries = git.list_worktrees(task.repo_path)
    main = git.canonical(entries[0].path) if entries else None
    found = []
    for entry in entries[1:]:
        path = git.canonical(entry.path)
        if path == main or entry.bare or entry.prunable or _owner_of(conn, path) is not None:
            continue
        if not Path(path).is_dir():
            continue
        name = entry.branch_name
        matches = bool(name) and (name == branch or name.startswith(f"task/{task.id}"))  # type: ignore[union-attr]
        found.append(Candidate(entry, git.is_dirty(path), matches))
    return sorted(found, key=lambda c: (not c.branch_matches, c.entry.path))


def _check_adoptable(conn: sqlite3.Connection, task: Task, path: str) -> git.WorktreeEntry:
    entries = git.list_worktrees(task.repo_path)
    match = next((e for e in entries if git.canonical(e.path) == path), None)
    if match is None:
        raise WorktreeError(
            "not_a_worktree",
            f"{path} is not a worktree of {task.repo_path}. `git worktree list` in that repository shows its worktrees.",
            path=path,
        )
    if match is entries[0]:
        raise WorktreeError(
            "main_worktree", f"{path} is the repository's main checkout; a task needs its own worktree.", path=path
        )
    if match.bare or match.prunable or not Path(path).is_dir():
        raise WorktreeError("worktree_unusable", f"{path} is bare, missing or prunable and cannot be adopted.", path=path)
    owner = _owner_of(conn, path)
    if owner is not None:
        raise WorktreeError(
            "worktree_taken", f"{path} already belongs to task {owner}.", path=path, task=owner
        )
    return match


def associate(
    conn: sqlite3.Connection, task: Task, path: str | Path, *, force: bool = False
) -> tuple[WorktreeRecord, bool]:
    """Adopt `path` as the task's worktree, explicitly. Returns the record and whether the tree is dirty.
    Adopting changes nothing in the tree. A task keeps one worktree: associating another is refused. A task that
    already has a session is refused unless `force`: its conversation is tied to the directory it ran in."""
    resolved = git.canonical(Path(path).expanduser())
    existing = get_worktree(conn, task.id)
    if existing is not None and existing.removed_at:
        # `cleanup` removed it: the explicit way back is to adopt another one. The old row goes into the history.
        entry = _check_adoptable(conn, task, resolved)
        if not force and primary_session(conn, task.id) is not None:
            raise WorktreeError(
                "session_exists",
                f"Task {task.id} already has a session, and its agent conversation is tied to the directory it ran in "
                f"({task.repo_path}). Pass --force to associate anyway.",
                task=task.id, path=resolved,
            )
        with transaction(conn):
            conn.execute("DELETE FROM worktrees WHERE task_id = ?", (task.id,))
            record_event(conn, task.id, "worktree_replaced", f"{existing.path} (removed {existing.removed_at})")
        return _record(conn, task, resolved, entry.branch_name, ADOPTED, None), git.is_dirty(resolved)
    if existing is not None:
        if existing.path == resolved:
            return existing, git.is_dirty(resolved) if Path(resolved).is_dir() else False
        raise WorktreeError(
            "worktree_already_set",
            f"Task {task.id} already has the worktree {existing.path} ({existing.ownership}). "
            "It is not replaced.",
            path=existing.path,
        )
    entry = _check_adoptable(conn, task, resolved)
    if not force and primary_session(conn, task.id) is not None:
        raise WorktreeError(
            "session_exists",
            f"Task {task.id} already has a session, and its agent conversation is tied to the directory it ran in "
            f"({task.repo_path}). Resuming from {resolved} would not find it. Pass --force to associate anyway.",
            task=task.id,
            path=resolved,
        )
    record = _record(conn, task, resolved, entry.branch_name, ADOPTED, None)
    return record, git.is_dirty(resolved)


# --- the entry point `open` uses --------------------------------------------------------------------------


@dataclass(frozen=True)
class WorktreeResult:
    record: WorktreeRecord
    action: str
    """`existing` (already recorded), `created` or `adopted`."""
    notices: tuple[str, ...] = field(default_factory=tuple)


def _free_branch(repo: str, branch: str) -> str:
    for n in range(2, 100):
        if not git.branch_exists(repo, f"{branch}-{n}"):
            return f"{branch}-{n}"
    raise WorktreeError("branch_conflict", f"No free suffix for the branch {branch!r}.", branch=branch)


def _choose(
    task: Task, candidates: list[Candidate], new_branch: str, taken: str | None, prompter: Prompter
) -> Candidate | None:
    """Ask once. Returns the adopted candidate, or None for "create a new worktree on `new_branch`"."""
    choices = [Choice(f"wt:{i}", c.label()) for i, c in enumerate(candidates)]
    choices.append(
        Choice("new", f"Create a new worktree for this task on branch {new_branch}")
        if taken is None
        else Choice("new", f"Create a new worktree on a new branch {new_branch} (the branch {taken} already exists)")
    )
    choices.append(Choice("cancel", "Cancel; change nothing"))
    picked = prompter.select(
        f"Task {task.id} has no worktree yet. Adopt an existing one, or create a new one?",
        choices,
        default="new" if taken is None else "cancel",
    )
    if picked == "cancel":
        raise WorktreeError("declined", "Cancelled. Nothing was changed.", task=task.id)
    if picked == "new":
        return None
    chosen = candidates[int(picked.removeprefix("wt:"))]
    if chosen.dirty and not prompter.confirm(
        f"{chosen.entry.path} has uncommitted changes. Adopting does not touch them; the agent will see them. Adopt it?",
        default=False,
    ):
        raise WorktreeError("declined", "Cancelled. Nothing was changed.", task=task.id)
    return chosen


def ensure_worktree(
    conn: sqlite3.Connection, task: Task, config: Config, prompter: Prompter | None, *, offline: bool = False
) -> WorktreeResult:
    """The task's worktree: the recorded one, an adopted one, or a new one cut from the base branch."""
    existing = get_worktree(conn, task.id)
    if existing is not None:
        _require_on_disk(existing)
        return WorktreeResult(existing, "existing")

    repo = task.repo_path
    recovered = _recover_interrupted(conn, task)
    if recovered is not None:
        return recovered
    pull = _pull_checkout(conn, task)
    if pull is not None:
        return _ensure_pull_worktree(conn, task, config, prompter, pull, offline=offline)
    wanted = branch_name_for(task, repo)
    branch = wanted
    branch_taken = git.branch_exists(repo, wanted)
    candidates = discover(conn, task, wanted)
    if prompter is not None and (candidates or branch_taken):
        if branch_taken:
            branch = _free_branch(repo, wanted)
        chosen = _choose(task, candidates, branch, wanted if branch_taken else None, prompter)
        if chosen is not None:
            path = git.canonical(chosen.entry.path)
            record = _record(conn, task, path, chosen.entry.branch_name, ADOPTED, None)
            notice = (f"Adopted {path}, which has uncommitted changes.",) if chosen.dirty else ()
            return WorktreeResult(record, "adopted", notice)
    elif branch_taken:
        holder = next((c for c in candidates if c.entry.branch_name == branch), None)
        raise WorktreeError(
            "branch_conflict",
            f"The branch {branch!r} already exists in {repo}, and it is not recorded as belonging to task "
            f"{task.id}. Nothing was created. "
            + (
                f"It is checked out at {holder.entry.path}: if that is this task's worktree, run "
                f"`agent-launcher worktrees associate {task.id} {holder.entry.path}`. "
                if holder
                else "Rename or delete that branch yourself, or adopt a worktree with "
                f"`agent-launcher worktrees associate {task.id} <path>`. "
            ),
            branch=branch,
            task=task.id,
            worktree=holder.entry.path if holder else None,
        )
    else:
        lookalikes = [c for c in candidates if c.branch_matches]
        if lookalikes:
            paths = ", ".join(c.entry.path for c in lookalikes)
            raise WorktreeError(
                "worktree_candidate_exists",
                f"{paths} has a branch that looks like task {task.id}'s. It is not assumed to be this task's, and "
                f"a second worktree is not created beside it without asking. If it is, run "
                f"`agent-launcher worktrees associate {task.id} <path>`; otherwise run `open` on a terminal to choose.",
                task=task.id,
                worktrees=[c.entry.path for c in lookalikes],
            )

    path = _free_path(task, config)
    base = resolve_base(repo, config, offline=offline)
    path.parent.mkdir(parents=True, exist_ok=True)
    launches.note_worktree_intent(conn, task.id, str(path), branch, base.start_ref)
    git.add_worktree(repo, path, branch, base.start_ref)
    record = _record(conn, task, git.canonical(path), branch, CREATED, base.start_ref)
    trace("worktree created", task=task.id, base=base.start_ref)
    notices = [base.warning] if base.warning else []
    if base.guessed:
        notices.append(
            f"Cut from {base.start_ref} (guessed: no configured base branch and no origin/HEAD). Set "
            "repositories.base_branches in config.json to choose it."
        )
    return WorktreeResult(record, "created", tuple(notices))


def _free_path(task: Task, config: Config) -> Path:
    path = worktree_path_for(task, config)
    if path.exists() or path.is_symlink():
        raise WorktreeError(
            "worktree_path_exists",
            f"{path} already exists and is not recorded as task {task.id}'s worktree. It is left alone. If it is this task's worktree, run "
            f"`agent-launcher worktrees associate {task.id} {path}`.",
            path=str(path),
            task=task.id,
        )
    return path


# --- pull requests ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PullCheckout:
    number: int
    own: bool
    """The author is the authenticated user (decided when the task was opened)."""
    head_fork: bool
    head_ref: str
    head_sha: str


def _pull_checkout(conn: sqlite3.Connection, task: Task) -> PullCheckout | None:
    row = conn.execute(
        """SELECT number, pr_own, pr_head_fork, pr_head_ref, pr_head_sha FROM task_github
           WHERE task_id = ? AND kind = 'pull_request'""",
        (task.id,),
    ).fetchone()
    if row is None or not row[3] or not row[4]:
        return None
    return PullCheckout(row[0], bool(row[1]), row[2] is None or bool(row[2]), row[3], row[4])


def review_branch_name(number: int) -> str:
    return f"review/pr-{number}"


def _ensure_pull_worktree(
    conn: sqlite3.Connection, task: Task, config: Config, prompter: Prompter | None, pr: PullCheckout, *, offline: bool
) -> WorktreeResult:
    repo = task.repo_path
    review = review_branch_name(pr.number)
    notices: list[str] = []
    entries = git.list_worktrees(repo)
    use_head = pr.own and not pr.head_fork and git.valid_branch_name(repo, pr.head_ref)
    if use_head:
        holder_entry = next((e for e in entries if e.branch_name == pr.head_ref), None)
        why = None
        if holder_entry is not None:
            if holder_entry is entries[0] or _owner_of(conn, git.canonical(holder_entry.path)) is not None:
                why = f"already checked out in {holder_entry.path}, which cannot be adopted"
            elif holder_entry.prunable or holder_entry.bare or not Path(holder_entry.path).is_dir():
                why = f"held by the worktree {holder_entry.path}, which is missing or prunable"
        if why:
            notices.append(f"The head branch {pr.head_ref!r} is {why}, so this is an isolated review worktree on {review} instead.")
            use_head = False
    wanted = pr.head_ref if use_head else review
    candidates = discover(conn, task, wanted)
    holder = next((c for c in candidates if c.entry.branch_name == wanted), None)
    lookalikes = [c for c in candidates if c.branch_matches]
    if prompter is not None and candidates:
        # Scenario D: an existing worktree may already be this PR's. Offer it; a new one is the other choice.
        if use_head and holder is None:
            fallback = pr.head_ref  # what "create a new worktree" would use
        else:
            fallback = _free_branch(repo, review) if git.branch_exists(repo, review) else review
        chosen = _choose(task, candidates, fallback, wanted if holder is not None else None, prompter)
        if chosen is not None:
            path = git.canonical(chosen.entry.path)
            record = _record(conn, task, path, chosen.entry.branch_name, ADOPTED, None)
            note = (f"Adopted {path}, which has uncommitted changes.",) if chosen.dirty else ()
            return WorktreeResult(record, "adopted", (*notices, *note))
        if holder is not None:
            use_head = False
    elif lookalikes:
        paths = ", ".join(c.entry.path for c in lookalikes)
        raise WorktreeError(
            "worktree_candidate_exists",
            f"{paths} has the branch {wanted!r}, which looks like pull request #{pr.number}'s. It is not assumed to be "
            f"this task's, and a second worktree is not created beside it without asking. If it is, run "
            f"`agent-launcher worktrees associate {task.id} <path>`; otherwise run `open` on a terminal to choose.",
            task=task.id,
            worktrees=[c.entry.path for c in lookalikes],
        )

    path = _free_path(task, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    if use_head:
        done = _add_head_worktree(conn, task, path, pr, offline=offline, notices=notices)
        if done is not None:
            return done
    return _add_review_worktree(conn, task, path, pr, offline=offline, notices=notices)


def _add_head_worktree(
    conn: sqlite3.Connection, task: Task, path: Path, pr: PullCheckout, *, offline: bool, notices: list[str]
) -> WorktreeResult | None:
    """The user's own PR: its head branch, tracking `origin/<head>`. None when the branch cannot be had here."""
    repo, head = task.repo_path, pr.head_ref
    if not offline and git.has_remote(repo):
        warning = git.fetch_branch(repo, head)
        if warning:
            notices.append(warning)
    remote_ref = f"refs/remotes/origin/{head}"
    has_remote_ref, has_local = git.ref_exists(repo, remote_ref), git.branch_exists(repo, head)
    if not has_remote_ref and not has_local:
        notices.append(
            f"The head branch {head!r} is neither local nor on origin, so this is an isolated review worktree instead."
        )
        return None
    launches.note_worktree_intent(conn, task.id, str(path), head, remote_ref if not has_local else f"refs/heads/{head}")
    if has_local:
        git.add_worktree_existing(repo, path, head)
        if has_remote_ref and git.branch_upstream(repo, head) is None:
            git.set_upstream(repo, head, head)
            notices.append(f"Set the upstream of your local {head!r} to origin/{head} (it had none).")
        if has_remote_ref and git.rev_parse(repo, f"refs/heads/{head}") != git.rev_parse(repo, remote_ref):
            notices.append(f"Your local {head!r} and origin/{head} are at different commits; neither was changed.")
        start = f"refs/heads/{head}"
    else:
        git.add_worktree_tracking(repo, path, head, remote_ref)
        start = remote_ref
    record = _record(conn, task, git.canonical(path), head, CREATED, start)
    trace("pull worktree created", task=task.id, mode="head")
    return WorktreeResult(record, "created", tuple(notices))


def _add_review_worktree(
    conn: sqlite3.Connection, task: Task, path: Path, pr: PullCheckout, *, offline: bool, notices: list[str]
) -> WorktreeResult:
    """An isolated worktree on `review/pr-<N>`, cut from the launcher's own copy of the PR head, with no upstream."""
    repo = task.repo_path
    if not offline and git.has_remote(repo):
        warning = git.fetch_pull(repo, pr.number)
        if warning:
            notices.append(warning)
    ref = git.pull_ref(pr.number)
    if git.ref_exists(repo, ref):
        start = ref
    elif git.rev_parse(repo, pr.head_sha):
        start = pr.head_sha
    else:
        raise WorktreeError(
            "pr_head_unavailable",
            f"Pull request #{pr.number}'s head ({pr.head_sha[:12]}) is not in {repo} and could not be fetched"
            + (" (offline)" if offline else "")
            + ". Nothing was created; run `open` again when GitHub is reachable.",
            task=task.id,
        )
    if git.rev_parse(repo, start) != pr.head_sha:
        notices.append(f"The fetched head differs from the one GitHub reported ({pr.head_sha[:12]}); the PR may have moved.")
    branch = review_branch_name(pr.number)
    if git.branch_exists(repo, branch):
        branch = _free_branch(repo, branch)
    launches.note_worktree_intent(conn, task.id, str(path), branch, start)
    git.add_worktree(repo, path, branch, start)  # --no-track: no upstream, so no push to the contributor's branch
    record = _record(conn, task, git.canonical(path), branch, CREATED, start)
    trace("pull worktree created", task=task.id, mode="review")
    return WorktreeResult(record, "created", tuple(notices))


def _recover_interrupted(conn: sqlite3.Connection, task: Task) -> WorktreeResult | None:
    """A launch died between `git worktree add` and recording the tree. The launcher wrote its intent (path and
    branch) first, so a tree git lists at exactly that path on exactly that branch, unowned, is the launcher's own:
    record it as `created`. Anything that does not match is left alone and reported by the normal checks."""
    intent = launches.get_launch(conn, task.id)
    if intent is None or not intent.worktree_path or not intent.worktree_branch:
        return None
    path = Path(intent.worktree_path)
    if not path.is_dir():
        return None  # git never created it: start over
    canonical = git.canonical(path)
    entries = git.list_worktrees(task.repo_path)
    entry = next((e for e in entries[1:] if git.canonical(e.path) == canonical), None)
    if (
        entry is None
        or entry.prunable
        or entry.branch_name != intent.worktree_branch
        or _owner_of(conn, canonical) is not None
    ):
        return None
    record = _record(conn, task, canonical, intent.worktree_branch, CREATED, intent.worktree_base_ref)
    trace("worktree recovered", task=task.id)
    return WorktreeResult(
        record, "recovered", (f"Recovered the worktree {canonical}, created by an earlier launch that was interrupted.",)
    )


def working_directory(conn: sqlite3.Connection, task: Task) -> str:
    """Where the task's agent runs: its worktree. A task opened before worktrees existed keeps running in
    its repository directory, because its conversation lives there."""
    record = get_worktree(conn, task.id)
    if record is None:
        return task.repo_path
    _require_on_disk(record)
    return record.path


def inspect_worktree(record: WorktreeRecord, repo_path: str) -> dict[str, Any]:
    """The recorded worktree next to what git says about it now. Read-only."""
    info = record.to_dict()
    exists = Path(record.path).is_dir()
    info.update(exists=exists, listed=False, branch_now=None, head=None, dirty=None, locked=False, prunable=False)
    if Path(repo_path).is_dir():
        entry = next((e for e in git.list_worktrees(repo_path) if git.canonical(e.path) == record.path), None)
        if entry is not None:
            info.update(
                listed=True, branch_now=entry.branch_name, head=entry.head, locked=entry.locked, prunable=entry.prunable
            )
    if exists:
        info["dirty"] = git.is_dirty(record.path)
    return info
