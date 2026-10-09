"""`workflows test <url-or-task>`: what routing would do, and why. Read only.

It creates no task, worktree, association or database, and writes nothing. The pull request or issue is read from
GitHub (or, if GitHub cannot be reached, from the stored copy of an existing task); a task reference is read from the
state database opened read-only. CI status is fetched only if a rule tests it.
"""

import sqlite3
from typing import Any

from agent_launcher.agent_adapters import agent_adapter_for
from agent_launcher.config import Config
from agent_launcher.errors import LauncherError
from agent_launcher.github import GitHub, GitHubError, IssueMetadata, parse_github_url
from agent_launcher.github_tasks import OFFLINE_ERRORS, find_task_by_url, is_issue_reference
from agent_launcher.prompt import build_prompt, template_variables
from agent_launcher.routing import (
    Routing,
    TaskFacts,
    facts_from_task,
    preferred_agent,
    repository_full_name,
    route,
    would_ask,
)
from agent_launcher.state import SCHEMA_VERSION, state_path
from agent_launcher.tasks import Task, get_task
from agent_launcher.workflows import load_workflows


def _readonly_state() -> sqlite3.Connection | None:
    """The state database if it exists and is current, opened so that nothing can be written."""
    path = state_path()
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            conn.close()
            return None
    except sqlite3.Error:
        return None
    return conn


def _facts_from_metadata(meta: IssueMetadata, github: GitHub) -> TaskFacts:
    pull = meta.pull
    sha = pull.head_sha if pull else None
    owner, name = meta.repository.full_name.split("/")
    return TaskFacts(
        "pr" if pull else "issue", meta.repository.full_name, meta.labels, meta.author, meta.assignees,
        pull.review_requested if pull else None, pull.own if pull else None, pull.draft if pull else None,
        meta.state, sha, meta.number, meta.url, github.viewer,
        (lambda: github.check_status(owner, name, sha)) if sha else (lambda: "unknown"),
    )


def _profile_for(conn: sqlite3.Connection | None, github_id: int | None, repository_id: int | None) -> str | None:
    if conn is None:
        return None
    if repository_id is None and github_id is not None:
        row = conn.execute("SELECT id FROM repositories WHERE github_id = ?", (github_id,)).fetchone()
        repository_id = row[0] if row else None
    if repository_id is None:
        return None
    row = conn.execute("SELECT profile FROM profile_associations WHERE repository_id = ?", (repository_id,)).fetchone()
    return row[0] if row else None


def explain(
    reference: str, *, config: Config, github: GitHub | None = None, offline: bool = False
) -> dict[str, Any]:
    """The routing decision for a GitHub URL or an existing task, as plain data (the CLI renders it)."""
    github = github or GitHub()
    file = load_workflows()
    conn = _readonly_state()
    source = "github"
    profile: str | None = None
    task: Task | None = None
    try:
        if is_issue_reference(reference):
            ref = parse_github_url(reference)
            meta: IssueMetadata | None = None
            problem: GitHubError | None = None
            if not offline:
                try:
                    meta = github.pull(ref) if ref.kind == "pull_request" else github.issue(ref)
                except GitHubError as exc:
                    if exc.code not in OFFLINE_ERRORS:
                        raise
                    problem = exc
            if meta is not None:
                facts = _facts_from_metadata(meta, github)
                profile = _profile_for(conn, meta.repository.github_id, None)
            else:
                known = find_task_by_url(conn, ref) if conn is not None else None
                if known is None:
                    raise LauncherError(
                        "github_required",
                        "Testing a URL needs GitHub: " + (str(problem) if problem else "--offline was given")
                        + ", and no task for it is stored.",
                        url=reference,
                    )
                task, source = known, "stored task (GitHub not asked)"
        else:
            if conn is None:
                raise LauncherError("task_not_found", "There is no task database yet, so there is no such task.")
            task, source = get_task(conn, reference), "stored task"
        if task is not None:
            assert conn is not None
            facts = facts_from_task(
                conn, task, viewer=(lambda: None) if offline else github.viewer,
                ci=(lambda o, n, s: "unknown") if offline else github.check_status,
            )
            profile = task.profile
    finally:
        if conn is not None:
            conn.close()

    routing = route(file, config, facts)
    mode = config.workflow_routing.selection_mode
    winner = routing.winner
    agents = list(config.profiles[profile].agents) if profile in config.profiles else None
    highlighted, note = (None, None) if agents is None else preferred_agent(winner, agents)
    if agents is None and winner.preferred_agent:
        note = (
            f"Workflow {winner.id!r} prefers {winner.preferred_agent!r}; the repository's profile is not known "
            "yet, so whether it is offered cannot be checked."
        )
    return {
        "reference": reference,
        "source": source,
        "task": facts.describe(),
        "profile": profile,
        "selection_mode": mode,
        "would_ask": would_ask(mode, routing),
        "rules": [_rule(routing, r) for r in routing.rules],
        "matched": [r.workflow.id for r in routing.matched],
        "tie": routing.tie_note(),
        "winner": {
            "id": winner.id,
            "priority": winner.priority,
            "fallback": routing.fallback_used,
            "skill": winner.skill,
            "template": winner.template,
            "preferred_agent": winner.preferred_agent,
            "highlighted_agent": highlighted,
            "preferred_agent_note": note,
            "prompt": _preview(config, profile, winner, highlighted, facts),
        },
        "fallback": routing.fallback.id,
    }


def _rule(routing: Routing, rule) -> dict[str, Any]:
    return {
        "position": rule.position + 1,
        "id": rule.workflow.id,
        "priority": rule.workflow.priority,
        "matched": rule.matched,
        "rank": routing.rank(rule),
        "conditions": [c.to_dict() for c in rule.conditions],
    }


def _preview(config: Config, profile: str | None, workflow, agent: str | None, facts: TaskFacts) -> str | None:
    """The prompt the workflow would produce, when the agent is settled enough to say. For a workflow without a
    skill or a template it is the task text (not previewed: it is the URL, or the local title). What is only known
    at launch (task ID, worktree, an agent not yet chosen) shows as `<name>`; a variable the task cannot have
    (`task_url` of a local task) is reported as an error, exactly as `open` would fail."""
    template = workflow.template or (config.prompt_template if workflow.skill is None else None)
    if workflow.skill is None and template is None:
        return facts.url
    agent = agent or (config.profiles[profile].default_agent if profile in config.profiles else None)
    if template is None and (
        agent is None or profile not in config.profiles or agent not in config.profiles[profile].agents
    ):
        return None  # which agent runs is chosen at open
    known = profile in config.profiles and agent in config.profiles[profile].agents
    adapter = agent_adapter_for(config.agent_types[agent].adapter) if agent in config.agent_types else None
    style = config.profiles[profile].agents[agent].skill_invocation if known else "slash"
    stand_in = Task(
        "t-preview", "<task_title>", "", 0, "<repository_path>", profile or "", None, "created", "", "",
        source="github" if facts.url else "local", url=facts.url,
    )
    if template is None and facts.url is None:
        return None
    variables = None
    if template is not None:
        variables = template_variables(
            stand_in, workflow, agent=agent, worktree_path=None, repository=facts.repository, adapter=adapter, style=style
        )
        variables["task_type"] = facts.type
        for name, value in (("task_id", "<task_id>"), ("worktree_path", "<worktree_path>"), ("agent", "<agent>")):
            variables[name] = variables[name] or value
    try:
        return build_prompt(
            stand_in, workflow, adapter=adapter, style=style, default_template=config.prompt_template,
            variables=variables,
        )
    except LauncherError as exc:
        return f"(not possible: {exc.message})"
