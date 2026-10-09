"""Choosing a workflow for a task (SPEC §14). Deterministic: no model is involved, only the task's metadata.

Inputs are the metadata stored when the task was opened (`task_github`, #7, #12, #13) and, only for a rule that
uses it, the pull request's check status, fetched on demand and `unknown` when it cannot be. `self` means the
authenticated `gh` user.

Order is total: the highest `priority` wins, and equal priorities go to the rule earlier in `workflows.json`.
With no matching rule the configured fallback applies. A workflow has no profile or agent list, so routing cannot
change a task's profile or grant an agent; its `preferred_agent` only decides what the picker highlights.

Modes (`workflow_routing.selection_mode`): `automatic` takes the winner; `ask_on_multiple` asks when more than one
rule matches; `always_ask` always asks. `--workflow <id>` names one and `--ask-workflow` forces the question.
"""

import difflib
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_launcher.config import Config
from agent_launcher.errors import LauncherError
from agent_launcher.interaction import Choice, Prompter
from agent_launcher.logs import trace
from agent_launcher.workflows import (
    CONDITIONS,
    Workflow,
    WorkflowsFile,
    find_workflow,
)


@dataclass
class TaskFacts:
    """What rules can test. `None` means not known, which never matches a condition on it."""

    type: str
    repository: str | None = None
    labels: tuple[str, ...] = ()
    author: str | None = None
    assignees: tuple[str, ...] = ()
    review_requested: bool | None = None
    own: bool | None = None
    """The author is the authenticated user, if that was settled when the pull request was read."""
    draft: bool | None = None
    state: str | None = None
    head_sha: str | None = None
    number: int | None = None
    url: str | None = None
    viewer_lookup: Callable[[], str | None] = lambda: None
    ci_lookup: Callable[[], str] = lambda: "unknown"
    _viewer: tuple[str | None] | None = field(default=None, repr=False)
    _ci: str | None = field(default=None, repr=False)

    @property
    def owner(self) -> str | None:
        return self.repository.split("/")[0] if self.repository else None

    def viewer(self) -> str | None:
        if self._viewer is None:
            self._viewer = (self.viewer_lookup(),)
        return self._viewer[0]

    def ci(self) -> str:
        if self._ci is None:
            self._ci = self.ci_lookup() if self.type == "pr" and self.head_sha else "unknown"
        return self._ci

    def describe(self) -> dict[str, Any]:
        return {
            "type": self.type, "repository": self.repository, "owner": self.owner, "labels": list(self.labels),
            "author": self.author, "assignees": list(self.assignees), "review_requested": self.review_requested,
            "draft": self.draft, "state": self.state, "number": self.number, "url": self.url,
        }


@dataclass(frozen=True)
class ConditionResult:
    condition: str
    expected: Any
    actual: Any
    matched: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition": self.condition, "expected": self.expected, "actual": self.actual,
            "matched": self.matched, "reason": self.reason,
        }


@dataclass(frozen=True)
class RuleResult:
    position: int
    """0-based place in `workflows.json`."""
    workflow: Workflow
    conditions: tuple[ConditionResult, ...]

    @property
    def matched(self) -> bool:
        return all(c.matched for c in self.conditions)


def _as_list(value: Any) -> list[Any]:
    return [] if value is None else value if isinstance(value, list) else [value]


def _is_viewer(login: str | None, facts: TaskFacts) -> bool | None:
    viewer = facts.viewer()
    return None if viewer is None or login is None else viewer.lower() == login.lower()


def _people(name: str, wanted: list[str], people: Sequence[str | None], facts: TaskFacts) -> ConditionResult:
    """`author` and `assignee`: any wanted login is among the people; `self` is the authenticated user."""
    present = [p for p in people if p]
    shown = list(present)
    if not present:
        return ConditionResult(name, wanted, shown, False, f"the task has no {name}")
    unknown = False
    for want in wanted:
        if want == "self":
            for person in present:
                verdict = facts.own if name == "author" and facts.own is not None else _is_viewer(person, facts)
                if verdict:
                    return ConditionResult(name, wanted, shown, True, f"{person} is you (the gh user)")
                unknown = unknown or verdict is None
        elif any(want.lower() == p.lower() for p in present):
            return ConditionResult(name, wanted, shown, True, f"{want} is among the {name} values")
    if unknown:
        return ConditionResult(name, wanted, shown, False, "unknown: the gh user could not be determined")
    return ConditionResult(name, wanted, shown, False, f"none of {', '.join(shown)} is {' or '.join(wanted)}")


def _test(name: str, expected: Any, facts: TaskFacts) -> ConditionResult:
    if name == "type":
        ok = facts.type == expected
        return ConditionResult(name, expected, facts.type, ok, f"the task is {expected}" if ok else f"the task is {facts.type}, not {expected}")
    if name == "repository":
        if facts.repository is None:
            return ConditionResult(name, expected, None, False, "unknown: the repository's GitHub name is not known")
        ok = facts.repository.lower() == expected.lower()
        return ConditionResult(name, expected, facts.repository, ok, "same repository" if ok else f"the repository is {facts.repository}")
    if name == "owner":
        if facts.owner is None:
            return ConditionResult(name, expected, None, False, "unknown: the repository's GitHub name is not known")
        ok = facts.owner.lower() == expected.lower()
        return ConditionResult(name, expected, facts.owner, ok, "same owner" if ok else f"the owner is {facts.owner}")
    if name in ("labels_any", "labels_all"):
        if facts.type == "local":
            return ConditionResult(name, expected, [], False, "not applicable: a local task has no labels")
        have = {l.lower() for l in facts.labels}
        wanted = [l.lower() for l in expected]
        hit = [l for l in wanted if l in have]
        ok = bool(hit) if name == "labels_any" else len(hit) == len(wanted)
        why = f"has {', '.join(hit)}" if ok else f"missing {', '.join(l for l in wanted if l not in have) if name == 'labels_all' else ', '.join(wanted)}"
        return ConditionResult(name, expected, list(facts.labels), ok, why)
    if name == "author":
        if facts.type == "local":
            return ConditionResult(name, expected, None, False, "not applicable: a local task has no author")
        return _people(name, _as_list(expected), [facts.author], facts)
    if name == "assignee":
        if facts.type == "local":
            return ConditionResult(name, expected, [], False, "not applicable: a local task has no assignee")
        return _people(name, _as_list(expected), list(facts.assignees), facts)
    if name == "review_requested":
        if facts.type != "pr":
            return ConditionResult(name, expected, None, False, f"not applicable: a {facts.type} task has no reviewers")
        if facts.review_requested is None:
            return ConditionResult(name, expected, None, False, "unknown: the gh user could not be determined")
        ok = facts.review_requested
        return ConditionResult(name, expected, ok, ok, "your review is requested" if ok else "your review is not requested")
    if name == "draft":
        if facts.type != "pr":
            return ConditionResult(name, expected, None, False, f"not applicable: a {facts.type} task is not a draft")
        ok = facts.draft == expected
        return ConditionResult(name, expected, facts.draft, ok, f"draft is {facts.draft}")
    if name == "ci":
        if facts.type != "pr":
            return ConditionResult(name, expected, None, False, f"not applicable: a {facts.type} task has no checks")
        status = facts.ci()
        wanted = _as_list(expected)
        ok = status in wanted
        return ConditionResult(name, wanted, status, ok, f"checks are {status}" + (" (could not be read)" if status == "unknown" else ""))
    if name == "state":
        if facts.type == "local":
            return ConditionResult(name, expected, None, False, "not applicable: a local task has no GitHub state")
        wanted = _as_list(expected)
        ok = facts.state in wanted
        return ConditionResult(name, wanted, facts.state, ok, f"the state is {facts.state}")
    raise AssertionError(name)  # pragma: no cover


def evaluate_rule(position: int, workflow: Workflow, facts: TaskFacts) -> RuleResult:
    """Every condition the rule gives, in `CONDITIONS` order. All are evaluated (for the explanation), except the
    one that costs a network call: `ci` is looked up last, and only if every other condition held."""
    given = {n: getattr(workflow.match, n) for n in CONDITIONS if getattr(workflow.match, n) not in (None, [])}
    results = {n: _test(n, v, facts) for n, v in given.items() if n != "ci"}
    if "ci" in given:
        if all(r.matched for r in results.values()):
            results["ci"] = _test("ci", given["ci"], facts)
        else:
            results["ci"] = ConditionResult("ci", _as_list(given["ci"]), None, False, "not checked: another condition already failed")
    return RuleResult(position, workflow, tuple(results[n] for n in given))


@dataclass(frozen=True)
class Routing:
    rules: tuple[RuleResult, ...]
    matched: tuple[RuleResult, ...]
    """Matching rules, best first: priority descending, then position in the file."""
    fallback: Workflow
    file: WorkflowsFile

    @property
    def winner(self) -> Workflow:
        return self.matched[0].workflow if self.matched else self.fallback

    @property
    def fallback_used(self) -> bool:
        return not self.matched

    def rank(self, rule: RuleResult) -> int | None:
        return self.matched.index(rule) + 1 if rule in self.matched else None

    def tie_note(self) -> str | None:
        if len(self.matched) > 1 and self.matched[0].workflow.priority == self.matched[1].workflow.priority:
            same = [r for r in self.matched if r.workflow.priority == self.matched[0].workflow.priority]
            return (
                f"{len(same)} rules tie at priority {same[0].workflow.priority}: "
                f"{same[0].workflow.id} wins because it is earliest in workflows.json"
            )
        return None


def resolve_fallback(file: WorkflowsFile, config: Config) -> Workflow:
    wanted = config.workflow_routing.fallback
    found = find_workflow(file, wanted)
    if found is None:
        raise LauncherError(
            "fallback_missing",
            f"workflow_routing.fallback is {wanted!r} but workflows.json defines no workflow with that id "
            f"(defined: {', '.join(w.id for w in file.workflows) or 'none'}).",
            fallback=wanted,
        )
    return found


def route(file: WorkflowsFile, config: Config, facts: TaskFacts) -> Routing:
    rules = tuple(evaluate_rule(i, w, facts) for i, w in enumerate(file.workflows))
    matched = tuple(sorted((r for r in rules if r.matched), key=lambda r: (-r.workflow.priority, r.position)))
    routing = Routing(rules, matched, resolve_fallback(file, config), file)
    trace(
        "workflow routed", winner=routing.winner.id, fallback=routing.fallback_used,
        matched=[r.workflow.id for r in matched],
    )
    return routing


# --- Selection ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Selection:
    workflow: Workflow
    how: str
    """`override` (--workflow), `automatic`, `fallback`, `asked`."""
    routing: Routing | None = None


def lookup(file: WorkflowsFile, config: Config, workflow_id: str) -> Workflow:
    found = find_workflow(file, workflow_id)
    if found is None:
        ids = [w.id for w in file.workflows]
        close = difflib.get_close_matches(workflow_id, ids, n=1)
        raise LauncherError(
            "workflow_not_found",
            f"No workflow {workflow_id!r} in workflows.json" + (f"; did you mean {close[0]!r}?" if close else f" (defined: {', '.join(ids) or 'none'}).")
            + " Nothing was changed.",
            workflow=workflow_id,
        )
    return found


def would_ask(mode: str, routing: Routing, force_ask: bool = False) -> bool:
    return force_ask or mode == "always_ask" or (mode == "ask_on_multiple" and len(routing.matched) > 1)


def select_workflow(
    routing: Routing,
    *,
    mode: str,
    prompter: Prompter | None,
    requested: str | None = None,
    force_ask: bool = False,
    config: Config | None = None,
    title: str = "Which workflow should run this task?",
) -> Selection:
    if requested is not None and force_ask:
        raise LauncherError("workflow_options_conflict", "--workflow and --ask-workflow cannot be used together.")
    if requested is not None:
        assert config is not None
        chosen = lookup(routing.file, config, requested)
        trace("workflow chosen", workflow=chosen.id, how="override")
        return Selection(chosen, "override", routing)
    if not would_ask(mode, routing, force_ask):
        how = "fallback" if routing.fallback_used else "automatic"
        return Selection(routing.winner, how, routing)

    # Matching rules best first (the winner is the default), then the rest of the file, then the fallback.
    ordered: list[tuple[Workflow, str]] = [(r.workflow, "matches") for r in routing.matched]
    if force_ask or mode != "ask_on_multiple":  # ask_on_multiple: only the matches are in doubt
        ordered += [(r.workflow, "no match") for r in routing.rules if not r.matched]
    if not any(w.id == routing.fallback.id for w, _ in ordered):
        ordered.append((routing.fallback, "fallback"))
    if prompter is None:
        raise LauncherError(
            "workflow_selection_needed",
            f"This task needs a workflow choice (mode {'ask' if force_ask else mode}) but cannot ask here; "
            f"pass --workflow ({', '.join(w.id for w, _ in ordered)}).",
            workflows=[w.id for w, _ in ordered],
            suggested_workflow=routing.winner.id,
        )
    labels = {w.id: f"{w.id} (priority {w.priority}, {tag})" if tag != "fallback" else f"{w.id} (fallback)" for w, tag in ordered}
    picked = prompter.select(title, [Choice(w.id, labels[w.id]) for w, _ in ordered], default=routing.winner.id)
    chosen = next(w for w, _ in ordered if w.id == picked)
    trace("workflow chosen", workflow=chosen.id, how="asked")
    return Selection(chosen, "asked", routing)


# --- The preferred agent -----------------------------------------------------------------------


def preferred_agent(workflow: Workflow, profile_agents: Sequence[str]) -> tuple[str | None, str | None]:
    """`(agent to highlight, note)`. A preference for an agent the repository's profile lacks is ignored, never
    granted: the note says so."""
    wanted = workflow.preferred_agent
    if wanted is None:
        return None, None
    if wanted in profile_agents:
        return wanted, None
    return None, (
        f"Workflow {workflow.id!r} prefers {wanted!r}, which this repository's profile does not have "
        f"(it has: {', '.join(sorted(profile_agents)) or 'none'}); the preference is ignored."
    )


# --- Facts from stored metadata ---------------------------------------------------------------


def repository_full_name(url: str | None) -> str | None:
    if not url:
        return None
    parts = url.split("/")
    return f"{parts[3]}/{parts[4]}" if len(parts) > 4 and parts[2] == "github.com" else None


def facts_from_task(
    conn: sqlite3.Connection,
    task: Any,
    *,
    viewer: Callable[[], str | None] = lambda: None,
    ci: Callable[[str, str, str], str] | None = None,
) -> TaskFacts:
    """Facts from what is stored for `task` (no network, except `viewer` and `ci`, called only if a rule needs them).
    `ci(owner, name, sha)` returns a check status."""
    from agent_launcher.github_tasks import github_details  # imported late: github_tasks imports launch

    details = github_details(conn, task.id)
    if details is None:
        row = conn.execute("SELECT full_name FROM repositories WHERE id = ?", (task.repository_id,)).fetchone()
        return TaskFacts("local", row[0] if row and row[0] else None, viewer_lookup=viewer)
    pull = details.get("pull")
    repo = repository_full_name(details["url"])
    sha = pull["head_sha"] if pull else None
    lookup_ci: Callable[[], str] = lambda: "unknown"  # noqa: E731
    if ci is not None and repo and sha:
        owner, name = repo.split("/")
        lookup_ci = lambda: ci(owner, name, sha)  # noqa: E731
    return TaskFacts(
        "pr" if pull else "issue", repo, tuple(details["labels"]), details["author"], tuple(details["assignees"]),
        pull["review_requested"] if pull else None, pull["own"] if pull else None, pull["draft"] if pull else None,
        details["state"], sha, details["number"], details["url"], viewer, lookup_ci,
    )
