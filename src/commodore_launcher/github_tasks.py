"""GitHub issue and pull request tasks: `commodore open <issue-or-pr-url>` (Scenario A).

Pipeline: parse the URL, fetch the issue through `gh` (`github.py`), find the task by GitHub's stable IDs, or else
find the repository (`repo_locator`), give it its profile association (`associations.ensure_profile`), create the
task and hand over to `launch.open_task`, which starts the one session in that checkout. Opening the same
issue again, from any URL spelling that reaches it, finds the same task and focuses it.

A pull request takes the same road (`open_pull_request`), with `GitHub.pull` for the fetch. It also stores the head
repository, ref and SHA, the base ref, draft, review-requested and whether it is the user's own. Nothing is checked
out: the harness starts in the repository's checkout, and Commodore does the rest (ADR 0021).

Nothing else creates a task: `open` takes a GitHub issue or PR URL, or the ID of a task made from one (ADR 0021).

The task is identified by the issue's node ID and database ID plus the repository's ID, never by `owner/repo#N`.
"""

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone

from commodore_launcher.associations import ensure_profile
from commodore_launcher.config import Config
from commodore_launcher.errors import LauncherError
from commodore_launcher.github import (
    GitHub,
    GitHubError,
    IssueMetadata,
    IssueRef,
    parse_github_url,
    parse_issue_url,
    parse_pull_url,
)
from commodore_launcher.interaction import Prompter
from commodore_launcher.launch import OpenResult, open_task, require_launch_form
from commodore_launcher.locks import task_lock
from commodore_launcher.picker import pick_agent
from commodore_launcher.logs import trace
from commodore_launcher.repo_locator import locate
from commodore_launcher.repositories import identify_reference
from commodore_launcher.sessions import last_used_agent
from commodore_launcher.state import transaction
from commodore_launcher.tasks import TASK_ARCHIVED, TASK_CREATED, Task, get_task, new_task_id
from commodore_launcher.terminals import TerminalAdapter

OFFLINE_ERRORS = ("github_unreachable", "gh_unavailable")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_issue_reference(text: str) -> bool:
    return text.strip().lower().startswith(("http://", "https://"))


_TASK_ID_SHAPE = re.compile(r"t-[a-z0-9]{1,8}")


def is_task_id_shaped(text: str) -> bool:
    """`t-` and up to eight characters: a task ID or a prefix of one that starts `t-`. Not a guess at a match."""
    return _TASK_ID_SHAPE.fullmatch(text.strip().lower()) is not None


def needs_issue_or_pr_url_error(text: str) -> LauncherError:
    return LauncherError(
        "needs_issue_or_pr_url",
        f"{text.strip()!r} is not a GitHub issue or PR URL, or the ID of a task opened from one. `commodore open` "
        "needs a GitHub issue or pull request URL (https://github.com/owner/repo/issues/N or /pull/N). "
        "Nothing was created.",
        input=text.strip(),
    )


def find_task(conn: sqlite3.Connection, meta: IssueMetadata) -> Task | None:
    """The task for this issue or pull request, by its stable IDs. The node ID is unique across kinds; a database ID
    is only unique within its kind (an issue and a PR can share one), so it matches only the same kind."""
    row = conn.execute(
        "SELECT task_id FROM task_github WHERE node_id = ? OR (kind = ? AND database_id = ?)",
        (meta.node_id, meta.kind, meta.database_id),
    ).fetchone()
    return get_task(conn, row[0]) if row else None


def find_task_by_url(conn: sqlite3.Connection, ref: IssueRef) -> Task | None:
    """Only for when GitHub cannot be asked. A URL is a weaker key than the IDs, and a renamed repository's
    old URL will not match here. The URL is rebuilt from the parsed reference, so a query string, fragment or
    trailing slash on what was typed makes no difference."""
    segment = "pull" if ref.kind == "pull_request" else "issues"
    url = f"https://github.com/{ref.owner}/{ref.name}/{segment}/{ref.number}"
    rows = conn.execute("SELECT task_id FROM task_github WHERE lower(url) = lower(?)", (url,)).fetchall()
    return get_task(conn, rows[0][0]) if len(rows) == 1 else None


_BASE_COLUMNS = (
    "kind", "node_id", "database_id", "repository_github_id", "repository_node_id", "number", "title", "state",
    "labels", "author", "assignees", "url", "fetched_at",
)
_PULL_COLUMNS = (
    "pr_head_repo_owner", "pr_head_repo_name", "pr_head_repo_id", "pr_head_fork", "pr_head_ref", "pr_head_sha",
    "pr_base_ref", "pr_draft", "pr_review_requested", "pr_own",
)
_ALL_COLUMNS = _BASE_COLUMNS + _PULL_COLUMNS


def _flag(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _github_values(meta: IssueMetadata) -> tuple:
    pr = meta.pull
    return (
        meta.kind, meta.node_id, meta.database_id, meta.repository.github_id, meta.repository.node_id, meta.number,
        meta.title, meta.state, json.dumps(list(meta.labels)), meta.author, json.dumps(list(meta.assignees)),
        meta.url, _now(),
        *(
            (
                pr.head_repo_owner, pr.head_repo_name, pr.head_repo_id, int(pr.head_fork), pr.head_ref, pr.head_sha,
                pr.base_ref, int(pr.draft), _flag(pr.review_requested), _flag(pr.own),
            )
            if pr
            else (None,) * len(_PULL_COLUMNS)
        ),
    )


def create_github_task(
    conn: sqlite3.Connection, meta: IssueMetadata, repository_id: int, repo_path: str, profile: str
) -> tuple[Task, bool]:
    """Create the task for an issue, or return the one that exists. One transaction, so two racing creators
    end with one task (the IDs are also UNIQUE)."""
    now = _now()
    with transaction(conn):
        existing = find_task(conn, meta)
        if existing is not None:
            return existing, False
        while True:
            task_id = new_task_id()
            if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
                break
        conn.execute(
            """INSERT INTO tasks (id, title, description, repository_id, repo_path, profile, agent, state,
                                  created_at, updated_at, source)
               VALUES (?, ?, '', ?, ?, ?, NULL, ?, ?, ?, 'github')""",
            (task_id, meta.title, repository_id, repo_path, profile, TASK_CREATED, now, now),
        )
        conn.execute(
            f"INSERT INTO task_github ({', '.join(_ALL_COLUMNS)}, task_id) VALUES ({', '.join('?' * (len(_ALL_COLUMNS) + 1))})",
            (*_github_values(meta), task_id),
        )
    trace("github task created", task=task_id, kind=meta.kind, id=meta.database_id)
    return get_task(conn, task_id), True


def refresh_github(conn: sqlite3.Connection, task: Task, meta: IssueMetadata) -> None:
    """Keep the stored copy current (title, state, labels, head SHA, the canonical URL). Identity never changes."""
    with transaction(conn):
        conn.execute(
            f"UPDATE task_github SET {', '.join(f'{c} = ?' for c in _ALL_COLUMNS)} WHERE task_id = ?",
            (*_github_values(meta), task.id),
        )
        conn.execute("UPDATE tasks SET title = ?, updated_at = ? WHERE id = ?", (meta.title, _now(), task.id))


def refresh_task(
    conn: sqlite3.Connection, task: Task, github: GitHub | None = None, *, archived_ok: bool = False
) -> None:
    """Re-read the item's GitHub metadata (title, state, head SHA) and keep the stored copy current. Best effort: a task
    that cannot be fetched is left as it is, never an error. Archived tasks (unless `archived_ok`) and non-GitHub tasks are
    left alone."""
    if task.source != "github" or (task.state == TASK_ARCHIVED and not archived_ok) or not task.url:
        return
    github = github or GitHub()
    try:
        ref = parse_github_url(task.url)
        meta = github.pull(ref) if ref.kind == "pull_request" else github.issue(ref)
    except (GitHubError, LauncherError):
        return
    refresh_github(conn, task, meta)


def github_details(conn: sqlite3.Connection, task_id: str) -> dict | None:
    keys = ("kind", "node_id", "database_id", "repository_github_id", "repository_node_id", "number", "state",
            "labels", "author", "assignees", "url", "fetched_at")
    row = conn.execute(
        f"SELECT {', '.join(keys)}, {', '.join(_PULL_COLUMNS)} FROM task_github WHERE task_id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return None
    data = dict(zip(keys, row))
    data["labels"], data["assignees"] = json.loads(data["labels"]), json.loads(data["assignees"])
    if data["kind"] == "pull_request":
        pr = dict(zip(_PULL_COLUMNS, row[len(keys):]))
        data["pull"] = {
            "head_repo_owner": pr["pr_head_repo_owner"], "head_repo_name": pr["pr_head_repo_name"],
            "head_repo_id": pr["pr_head_repo_id"], "head_fork": bool(pr["pr_head_fork"]),
            "head_ref": pr["pr_head_ref"], "head_sha": pr["pr_head_sha"], "base_ref": pr["pr_base_ref"],
            "draft": bool(pr["pr_draft"]), "review_requested": None if pr["pr_review_requested"] is None else bool(pr["pr_review_requested"]),
            "own": None if pr["pr_own"] is None else bool(pr["pr_own"]),
        }
    return data


@contextmanager
def _issue_lock(database_id: int, reference: str, kind: str = "issue") -> Iterator[None]:
    try:
        with task_lock(f"{'pr' if kind == 'pull_request' else 'issue'}-{database_id}"):
            yield
    except LauncherError as exc:
        if exc.code != "task_busy":
            raise
        raise LauncherError(
            "task_busy",
            f"Another launch of this {'pull request' if kind == 'pull_request' else 'issue'} is in progress (possibly cloning the repository). Nothing was changed; "
            "run the command again when it finishes.",
            url=reference,
        ) from None


def open_github(conn: sqlite3.Connection, reference: str, **kwargs) -> OpenResult:
    """`open <url>` for an issue or a pull request URL."""
    try:
        kind = parse_github_url(reference).kind
    except GitHubError as exc:
        if exc.code == "unsupported_url":
            raise needs_issue_or_pr_url_error(reference) from exc
        raise
    if kind == "pull_request":
        return open_pull_request(conn, reference, **kwargs)
    return open_issue(conn, reference, **kwargs)


def open_pull_request(conn: sqlite3.Connection, reference: str, **kwargs) -> OpenResult:
    return open_issue(conn, reference, _kind="pull_request", **kwargs)


def open_issue(
    conn: sqlite3.Connection,
    reference: str,
    *,
    config: Config,
    adapter: TerminalAdapter,
    prompter: Prompter | None,
    agent: str | None = None,
    offline: bool = False,
    base_env: Mapping[str, str] | None = None,
    github: GitHub | None = None,
    _kind: str = "issue",
) -> OpenResult:
    github = github or GitHub()
    ref = parse_pull_url(reference) if _kind == "pull_request" else parse_issue_url(reference)
    common = dict(
        config=config, adapter=adapter, prompter=prompter, agent=agent, offline=offline, base_env=base_env,
        github=github,
    )
    # A harness asked for by name that cannot be given a prompt at launch is refused before a task or a clone exists.
    harness_type = config.harness_types.get(agent) if agent else None
    if harness_type is not None:
        require_launch_form(agent, harness_type.adapter)

    meta: IssueMetadata | None = None
    unreachable: GitHubError | None = None
    if not offline:
        try:
            meta = github.pull(ref) if _kind == "pull_request" else github.issue(ref)
        except GitHubError as exc:
            if exc.code not in OFFLINE_ERRORS:
                raise
            unreachable = exc
    if meta is None:
        known = find_task_by_url(conn, ref)
        if known is None:
            raise LauncherError(
                "github_required",
                f"Opening {'a pull request' if _kind == 'pull_request' else 'an issue'} for the first time needs GitHub: "
                + (str(unreachable) if unreachable else "--offline was given")
                + ". Nothing was created.",
                url=reference,
            )
        result = open_task(conn, known.id, **common)
        stale = "GitHub could not be reached; the task was found by its URL and its details were not refreshed."
        return replace(result, notice=" ".join(n for n in (stale, result.notice) if n))

    # One issue is set up by one process at a time, so two simultaneous opens create one task (and clone once).
    with _issue_lock(meta.database_id, reference, meta.kind):
        task = find_task(conn, meta)
        cloned_note = None
        if task is not None:
            refresh_github(conn, task, meta)
        else:
            located = locate(
                conn, config, meta.repository, names=(ref.full_name,), github=github, prompter=prompter, offline=offline
            )
            # The checkout was proven to be this repository, so the ID GitHub gave is its identity.
            identity = replace(
                identify_reference(located.path, fetch_github=False),
                github_id=meta.repository.github_id,
                node_id=meta.repository.node_id,
                full_name=meta.repository.full_name,
            )
            # The association exists before any harness can launch, for a fresh clone as for an old checkout.
            resolved = ensure_profile(conn, identity, sorted(config.profiles), prompter)
            assert identity.path is not None
            # The harness is settled here, once the profile is known and before the task row exists, so one with no
            # launch form for a prompt is refused without creating a task. `open_task` is then given it as asked for.
            profile = config.profiles.get(resolved.profile)
            if profile is not None and profile.harnesses:
                agent = pick_agent(
                    resolved.profile, list(profile.harnesses), mode=config.harness_selection,
                    default=profile.default_harness, last_used=last_used_agent(conn, resolved.profile),
                    prompter=prompter, requested=agent,
                ).agent
                harness_type = config.harness_types.get(agent)
                if harness_type is not None:
                    require_launch_form(agent, harness_type.adapter)
                common["agent"] = agent
            task, _ = create_github_task(conn, meta, resolved.repository_id, identity.path, resolved.profile)
            if located.cloned:
                cloned_note = f"Cloned {meta.repository.full_name} to {located.path}."
    result = open_task(conn, task.id, **common, refresh=False)  # refreshed just above
    notice = " ".join(n for n in (cloned_note, result.notice) if n) or None
    return replace(result, notice=notice) if notice != result.notice else result
