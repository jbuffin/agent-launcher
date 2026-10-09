"""Adopting a terminal session the launcher did not create (SPEC §20, ticket #19).

Discovery is conservative and explicit. The adapter lists what exists (`discover_sessions`, read-only, no screen
reads); this module keeps only sessions with concrete evidence for one task: the terminal's working directory is
the task's own worktree, or its title carries the task ID, the issue or PR reference with the repository name, or
the launcher's own `<repo> — <title>` form. "An agent is running in this repository" is never evidence (the
repository directory is not even supporting evidence), and free-text title matches are not used: a short title
such as "Fix" would match unrelated workspaces.

Adoption records the session with `created_by_launcher = false`. The adapters never force-close such a session,
and `restart` leaves it open (see `launch._restart_locked`). Nothing here moves, closes or types into it.
"""

import os
import re
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agent_launcher.agents import AgentResolutionError, resolve_agent
from agent_launcher.config import Config
from agent_launcher.github_tasks import github_details

from agent_launcher.errors import LauncherError
from agent_launcher.sessions import SessionRecord, primary_session, record_session
from agent_launcher.tasks import TASK_LAUNCH_FAILED, TASK_LAUNCHING, Task, require_not_archived
from agent_launcher.terminals import (
    DISCOVER_SESSIONS,
    ExternalSession,
    TerminalAdapter,
    TerminalSessionRef,
)
from agent_launcher.worktrees import get_worktree


@dataclass(frozen=True)
class Candidate:
    external: ExternalSession
    evidence: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.external.session.adapter,
            "workspace_id": self.external.session.workspace_id,
            "surface_id": self.external.session.surface_id,
            "title": self.external.title,
            "cwd": self.external.cwd,
            "evidence": list(self.evidence),
        }


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _clean(text: str | None) -> str | None:
    """External text is printed to the terminal: control characters (escape sequences) are removed."""
    return None if text is None else _CONTROL.sub("", text)


def _same_dir(a: str, b: str) -> bool:
    return os.path.realpath(os.path.expanduser(a)) == os.path.realpath(os.path.expanduser(b))


def _evidence(conn: sqlite3.Connection, task: Task, external: ExternalSession) -> list[str]:
    found: list[str] = []
    worktree = get_worktree(conn, task.id)
    if external.cwd and worktree is not None and _same_dir(external.cwd, worktree.path):
        found.append(f"working directory is the task's worktree ({worktree.path})")
    title = external.title.lower()
    repo = Path(task.repo_path).name.lower()
    if re.search(rf"(?<![0-9a-z]){re.escape(task.id)}(?![0-9a-z])", title):
        found.append(f"title names the task ({task.id})")
    details = github_details(conn, task.id)
    number = details.get("number") if details else None
    if number and repo and re.search(rf"(?<![0-9a-z]){re.escape(repo)}\b.*#{number}(?!\d)|#{number}(?!\d).*(?<![0-9a-z]){re.escape(repo)}\b", title):
        found.append(f"title names the issue or pull request ({repo}#{number})")
    if repo and task.title.strip() and title.strip() == f"{repo} — {task.title.strip().lower()}":
        found.append("title is the one the launcher gives its own workspace for this task")
    return found


def _recorded(conn: sqlite3.Connection, ref: TerminalSessionRef) -> bool:
    """Any session already points at this workspace or surface (the surface UUID outlives a reused ref)."""
    args = (ref.adapter, ref.workspace_id, ref.surface_id)
    # A terminal held by an unfinished launch is the launcher's own, not an external one.
    queries = (
        "SELECT 1 FROM terminal_sessions WHERE adapter = ? AND (workspace_id = ? OR (surface_id IS NOT NULL AND surface_id IS ?))",
        "SELECT 1 FROM launches WHERE terminal_adapter = ? AND (terminal_workspace_id = ? "
        "OR (terminal_surface_id IS NOT NULL AND terminal_surface_id IS ?))",
    )
    return any(conn.execute(q, args).fetchone() is not None for q in queries)


def find_candidates(conn: sqlite3.Connection, task: Task, adapter: TerminalAdapter) -> list[Candidate]:
    """Sessions that may be this task's, each with its evidence. Empty when the adapter cannot discover."""
    adapter.require(DISCOVER_SESSIONS)
    found: list[Candidate] = []
    for external in adapter.discover_sessions():
        ref = external.session
        if ref.created_by_launcher or ref.surface_id is None or _recorded(conn, ref):
            continue
        evidence = _evidence(conn, task, external)
        if evidence:
            shown = replace(external, title=_clean(external.title) or "", cwd=_clean(external.cwd))
            found.append(Candidate(shown, tuple(evidence)))
    return found


def adopt_session(
    conn: sqlite3.Connection,
    task: Task,
    adapter: TerminalAdapter,
    workspace: str,
    config: Config,
    *,
    agent: str | None = None,
) -> SessionRecord:
    """Record the picked session as the task's one session. `workspace` is a candidate's workspace or surface ID
    exactly as `find_candidates` printed it. The picked session is checked again (still there, still matching,
    still unrecorded) and only then confirmed on its screen: the one read of a screen discovery makes."""
    require_not_archived(task)
    if task.state in (TASK_LAUNCHING, TASK_LAUNCH_FAILED):
        raise LauncherError(
            "launch_incomplete",
            f"Task {task.id} has a launch that did not finish. Run `agent-launcher open {task.id}` to retry it; "
            "nothing was adopted.",
            task=task.id,
        )
    existing = primary_session(conn, task.id)
    if existing is not None:
        raise LauncherError(
            "session_exists",
            f"Task {task.id} already has a session ({existing.id}). A task never gets a second one, so nothing was adopted.",
            task=task.id,
        )
    picked = [c for c in find_candidates(conn, task, adapter) if workspace in (c.external.session.workspace_id, c.external.session.surface_id)]
    if len(picked) != 1:
        raise LauncherError(
            "not_a_candidate",
            f"{workspace!r} is not a session with evidence for task {task.id}. "
            f"List them with `agent-launcher sessions candidates {task.id}`.",
            task=task.id,
        )
    ref = picked[0].external.session
    chosen = agent or task.agent
    if not chosen:
        raise LauncherError(
            "agent_required", f"Say which agent runs in it: `--agent <name>`. Task {task.id} has none yet.", task=task.id
        )
    try:
        resolve_agent(task.profile, chosen, config=config)  # an agent of the task's own profile, or nothing is written
    except AgentResolutionError as exc:
        raise LauncherError(
            "agent_unresolved", f"Cannot adopt as agent {chosen!r}: {exc}. Nothing was adopted.", task=task.id
        ) from exc
    if adapter.read_screen(ref) is None:
        raise LauncherError("not_a_candidate", f"{workspace!r} no longer exists. Nothing was adopted.", task=task.id)
    return record_session(conn, task.id, task.profile, chosen, ref)
