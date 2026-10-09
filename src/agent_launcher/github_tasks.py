"""GitHub issue tasks: `agent-launcher open <issue-url>` (Scenario A).

Pipeline: parse the URL, fetch the issue through `gh` (`github.py`), find the task by GitHub's stable IDs, or else
find the repository (`repo_locator`), give it its profile association (`associations.ensure_profile`), create the
task and hand over to `launch.open_task`, which creates the worktree and the one session. Opening the same
issue again, from any URL spelling that reaches it, finds the same task and focuses it.

The task is identified by the issue's node ID and database ID plus the repository's ID, never by `owner/repo#N`.
"""

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone

from agent_launcher.associations import ensure_profile
from agent_launcher.config import Config
from agent_launcher.errors import LauncherError
from agent_launcher.github import GitHub, GitHubError, IssueMetadata, IssueRef, parse_issue_url
from agent_launcher.interaction import Prompter
from agent_launcher.launch import OpenResult, open_task
from agent_launcher.locks import task_lock
from agent_launcher.logs import trace
from agent_launcher.repo_locator import locate
from agent_launcher.repositories import identify_reference
from agent_launcher.state import transaction
from agent_launcher.tasks import TASK_CREATED, Task, get_task, new_task_id
from agent_launcher.terminals import TerminalAdapter

OFFLINE_ERRORS = ("github_unreachable", "gh_unavailable")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_issue_reference(text: str) -> bool:
    return text.strip().lower().startswith(("http://", "https://"))


def find_task(conn: sqlite3.Connection, meta: IssueMetadata) -> Task | None:
    """The task for this issue, by its stable IDs."""
    row = conn.execute(
        "SELECT task_id FROM task_github WHERE node_id = ? OR database_id = ?", (meta.node_id, meta.database_id)
    ).fetchone()
    return get_task(conn, row[0]) if row else None


def find_task_by_url(conn: sqlite3.Connection, ref: IssueRef) -> Task | None:
    """Only for when GitHub cannot be asked. A URL is a weaker key than the IDs, and a renamed repository's
    old URL will not match here. The URL is rebuilt from the parsed reference, so a query string, fragment or
    trailing slash on what was typed makes no difference."""
    url = f"https://github.com/{ref.owner}/{ref.name}/issues/{ref.number}"
    rows = conn.execute("SELECT task_id FROM task_github WHERE lower(url) = lower(?)", (url,)).fetchall()
    return get_task(conn, rows[0][0]) if len(rows) == 1 else None


def _github_values(meta: IssueMetadata) -> tuple:
    return (
        meta.kind, meta.node_id, meta.database_id, meta.repository.github_id, meta.repository.node_id, meta.number,
        meta.title, meta.state, json.dumps(list(meta.labels)), meta.author, json.dumps(list(meta.assignees)),
        meta.url, _now(),
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
            """INSERT INTO task_github (kind, node_id, database_id, repository_github_id, repository_node_id,
                                        number, title, state, labels, author, assignees, url, fetched_at, task_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (*_github_values(meta), task_id),
        )
    trace("github task created", task=task_id, issue=meta.database_id)
    return get_task(conn, task_id), True


def refresh_github(conn: sqlite3.Connection, task: Task, meta: IssueMetadata) -> None:
    """Keep the stored copy current (title, state, labels, the canonical URL). Identity never changes."""
    with transaction(conn):
        conn.execute(
            """UPDATE task_github SET kind = ?, node_id = ?, database_id = ?, repository_github_id = ?,
                   repository_node_id = ?, number = ?, title = ?, state = ?, labels = ?, author = ?,
                   assignees = ?, url = ?, fetched_at = ? WHERE task_id = ?""",
            (*_github_values(meta), task.id),
        )
        conn.execute("UPDATE tasks SET title = ?, updated_at = ? WHERE id = ?", (meta.title, _now(), task.id))


def github_details(conn: sqlite3.Connection, task_id: str) -> dict | None:
    row = conn.execute(
        """SELECT kind, node_id, database_id, repository_github_id, repository_node_id, number, state, labels,
                  author, assignees, url, fetched_at FROM task_github WHERE task_id = ?""",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    keys = ("kind", "node_id", "database_id", "repository_github_id", "repository_node_id", "number", "state",
            "labels", "author", "assignees", "url", "fetched_at")
    data = dict(zip(keys, row))
    data["labels"], data["assignees"] = json.loads(data["labels"]), json.loads(data["assignees"])
    return data


@contextmanager
def _issue_lock(database_id: int, reference: str) -> Iterator[None]:
    try:
        with task_lock(f"issue-{database_id}"):
            yield
    except LauncherError as exc:
        if exc.code != "task_busy":
            raise
        raise LauncherError(
            "task_busy",
            "Another launch of this issue is in progress (possibly cloning the repository). Nothing was changed; "
            "run the command again when it finishes.",
            url=reference,
        ) from None


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
) -> OpenResult:
    github = github or GitHub()
    ref = parse_issue_url(reference)
    common = dict(config=config, adapter=adapter, prompter=prompter, agent=agent, offline=offline, base_env=base_env)

    meta: IssueMetadata | None = None
    unreachable: GitHubError | None = None
    if not offline:
        try:
            meta = github.issue(ref)
        except GitHubError as exc:
            if exc.code not in OFFLINE_ERRORS:
                raise
            unreachable = exc
    if meta is None:
        known = find_task_by_url(conn, ref)
        if known is None:
            raise LauncherError(
                "github_required",
                "Opening an issue for the first time needs GitHub: "
                + (str(unreachable) if unreachable else "--offline was given")
                + ". Nothing was created.",
                url=reference,
            )
        result = open_task(conn, known.id, **common)
        stale = "GitHub could not be reached; the task was found by its URL and its details were not refreshed."
        return replace(result, notice=" ".join(n for n in (stale, result.notice) if n))

    # One issue is set up by one process at a time, so two simultaneous opens create one task (and clone once).
    with _issue_lock(meta.database_id, reference):
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
            # The association exists before any agent can launch, for a fresh clone as for an old checkout.
            resolved = ensure_profile(conn, identity, sorted(config.profiles), prompter)
            assert identity.path is not None
            task, _ = create_github_task(conn, meta, resolved.repository_id, identity.path, resolved.profile)
            if located.cloned:
                cloned_note = f"Cloned {meta.repository.full_name} to {located.path}."
    result = open_task(conn, task.id, **common)
    notice = " ".join(n for n in (cloned_note, result.notice) if n) or None
    return replace(result, notice=notice) if notice != result.notice else result
