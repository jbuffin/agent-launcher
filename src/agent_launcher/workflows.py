"""`workflows.json`: the rules that pick a workflow for a task (SPEC §14). Schema, validation and file access.

The file lives under the launcher home next to `config.json`. It is versioned, validated with Pydantic, and
unknown fields are errors with a suggestion (a misspelt condition would otherwise silently never match). Writes are
atomic. Matching and selection are in `routing`.

A workflow carries a skill, a prompt template and an advisory preferred agent. It has no profile, agent list or
environment, and the schema refuses them: a workflow can never change a task's profile or grant an agent.
"""

import difflib
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, StringConstraints, ValidationError

from agent_launcher.config import (
    ConfigError,
    FieldIssue,
    Name,
    PromptExecution,
    ValidationReport,
    read_raw,
    write_json_atomic,
)
from agent_launcher import templates
from agent_launcher.errors import LauncherError
from agent_launcher.paths import workflows_path

CURRENT_VERSION = 1

TASK_TYPES = ("issue", "pr", "local")
CI_STATUSES = ("success", "failure", "pending", "unknown")
GITHUB_STATES = ("open", "closed", "merged")

SkillName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$", max_length=100)]
Login = Annotated[str, StringConstraints(pattern=r"^([A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?|self)$", max_length=100)]
OwnerName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*$")]
Owner = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")]
Label = Annotated[str, StringConstraints(min_length=1, max_length=100)]


class Match(BaseModel):
    """Every condition given must hold (AND). A condition left out is not tested. Names and logins compare
    without regard to case; `self` in `author`, `assignee` and `review_requested` is the authenticated `gh` user."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["issue", "pr", "local"] | None = None
    repository: OwnerName | None = None
    owner: Owner | None = None
    labels_any: list[Label] = Field(default_factory=list)
    """At least one of these labels is on the issue or pull request."""
    labels_all: list[Label] = Field(default_factory=list)
    author: Login | list[Login] | None = None
    assignee: Login | list[Login] | None = None
    review_requested: Literal["self"] | None = None
    draft: StrictBool | None = None
    ci: Literal["success", "failure", "pending", "unknown"] | list[Literal["success", "failure", "pending", "unknown"]] | None = None
    """Check status of the pull request's head commit. Fetched only when a rule uses it; `unknown` when it cannot be."""
    state: Literal["open", "closed", "merged"] | list[Literal["open", "closed", "merged"]] | None = None


CONDITIONS = tuple(Match.model_fields)
"""The order conditions are tested and explained in."""


class Workflow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Name
    priority: StrictInt = 0
    """Higher wins. Equal priorities go to the rule earlier in the file."""
    match: Match = Field(default_factory=Match)
    description: StrictStr = ""
    preferred_agent: Name | None = None
    """Highlighted first in the agent picker. Advisory: ignored when the repository's profile lacks that agent."""
    skill: SkillName | None = None
    template: Name | None = None
    """The name of a prompt template (`templates/<name>.txt`, see `templates`), which is then the whole prompt. Without
    it, the global `prompt_template` applies if set; without that and with a skill, the prompt is the agent's skill
    invocation of the task's URL (`/<skill> <url>` for Claude Code)."""
    prompt_execution: PromptExecution | None = None
    """`prepare` or `execute` for tasks this workflow handles, instead of the global `prompt_execution`."""


class WorkflowsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: StrictInt = CURRENT_VERSION
    workflows: list[Workflow] = Field(default_factory=list)


BUILTIN_FALLBACK_ID = "default"


def builtin_fallback() -> Workflow:
    """No skill, no template, no preferred agent: the prompt is the task's URL, as before workflows existed."""
    return Workflow(id=BUILTIN_FALLBACK_ID, description="Built-in fallback: no skill.")


BUILTIN_CONFIGURE_ID = "agent-launcher-configure"
MANAGEMENT_SKILL = "agent-launcher"


def builtin_configure() -> Workflow:
    """The workflow `agent-launcher configure` launches with: the bundled management skill, nothing else. It is never
    routed to (no rule can match it); `workflows.json` may define the same id, and then the file wins, as for
    `default`."""
    return Workflow(
        id=BUILTIN_CONFIGURE_ID, skill=MANAGEMENT_SKILL, description="Built-in: the Agent Launcher management skill."
    )


_ALIASES = {
    "label": "labels_any", "labels": "labels_any", "tags": "labels_any",
    "assignees": "assignee", "authors": "author", "reviewer": "review_requested", "review": "review_requested",
    "reviews": "review_requested", "is_draft": "draft", "checks": "ci", "ci_status": "ci", "status": "state",
    "repo": "repository", "kind": "type", "task_type": "type",
    "agent": "preferred_agent", "preferred": "preferred_agent", "prio": "priority", "name": "id",
    "prompt": "template", "prompt_template": "template", "skills": "skill",
}
_PROFILE_FIELDS = {"profile", "profiles", "agents", "env", "environment", "executable", "args", "allowed_agents"}


class WorkflowsError(LauncherError):
    pass


def _unknown_field(loc: tuple[Any, ...]) -> FieldIssue:
    key = str(loc[-1])
    path = ".".join(str(p) for p in loc)
    if len(loc) >= 2 and loc[-2] == "match":
        valid, kind = list(Match.model_fields), "condition"
    elif len(loc) >= 2 and loc[0] == "workflows":
        valid, kind = list(Workflow.model_fields), "field"
        if key.lower() in _PROFILE_FIELDS:
            return FieldIssue(
                path,
                f"unknown {kind} {key!r}: a workflow cannot choose a profile or grant agents; the repository's "
                "profile decides. Use 'preferred_agent' to highlight one of the profile's agents",
            )
    else:
        valid, kind = list(WorkflowsFile.model_fields), "field"
    guess = _ALIASES.get(key.lower())
    if guess not in valid:
        close = difflib.get_close_matches(key, valid, n=1)
        guess = close[0] if close else None
    hint = f"; did you mean {guess!r}?" if guess else f"; valid: {', '.join(valid)}"
    return FieldIssue(path, f"unknown {kind} {key!r}{hint}")


def cross_check(model: WorkflowsFile) -> list[FieldIssue]:
    issues: list[FieldIssue] = []
    seen: dict[str, int] = {}
    for index, workflow in enumerate(model.workflows):
        if workflow.id in seen:
            issues.append(
                FieldIssue(f"workflows.{index}.id", f"duplicate id {workflow.id!r} (also workflows.{seen[workflow.id]})")
            )
        seen.setdefault(workflow.id, index)
        if workflow.template is not None:
            types = (workflow.match.type,) if workflow.match.type else None
            why = templates.problem(workflow.template, task_types=types, has_skill=workflow.skill is not None)
            if why:
                issues.append(FieldIssue(f"workflows.{index}.template", why))
    return issues


def validate_data(data: dict[str, Any]) -> list[FieldIssue]:
    """Every problem with a parsed `workflows.json`. Unknown fields are errors here."""
    errors: list[FieldIssue] = []
    version = data.get("version")
    if "version" not in data:
        errors.append(FieldIssue("version", f'missing; add "version": {CURRENT_VERSION}'))
    elif isinstance(version, int) and not isinstance(version, bool) and version > CURRENT_VERSION:
        errors.append(
            FieldIssue("version", f"{version} is newer than this release supports (max {CURRENT_VERSION}); upgrade agent-launcher")
        )
    elif isinstance(version, int) and not isinstance(version, bool) and version < 1:
        errors.append(FieldIssue("version", "must be 1 or greater"))
    try:
        model = WorkflowsFile.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            loc = _field_loc(err["loc"])
            name = ".".join(str(p) for p in loc) or "(root)"
            if err["type"] != "extra_forbidden" and any(e.field == name for e in errors):
                continue  # a union (one value or a list) reports once per branch; the first message says enough
            if err["type"] == "extra_forbidden":
                errors.append(_unknown_field(loc))
            elif name == "version" and any(e.field == "version" for e in errors):
                continue
            elif name.endswith(".template") and err["type"] == "string_pattern_mismatch":
                errors.append(
                    FieldIssue(
                        name,
                        "template is now the name of a file in templates/ (\"review\" for templates/review.txt), "
                        "not the prompt text; move the text into that file",
                    )
                )
            else:
                errors.append(FieldIssue(name, _plain(err)))
    else:
        errors.extend(cross_check(model))
    return errors


def _field_loc(loc: tuple[Any, ...]) -> tuple[Any, ...]:
    """Pydantic appends the name of each union branch (`list[literal[...]]`, `constrained-str`) to a location: drop them."""
    out: list[Any] = []
    for part in loc:
        if isinstance(part, str) and (any(c in part for c in "[]") or part in ("str", "constrained-str", "literal", "function-after")):
            break
        out.append(part)
    return tuple(out)


def _plain(err: Any) -> str:
    """Pydantic's message, with a literal's allowed values spelt out (it lists them already) and no jargon."""
    return str(err["msg"]).removeprefix("Value error, ")


def validate_workflows(path: Path | None = None) -> ValidationReport:
    path = path or workflows_path()
    try:
        data = read_raw(path)
    except ConfigError as exc:
        return ValidationReport(path, True, errors=[FieldIssue("(file)", str(exc))])
    if data is None:
        return ValidationReport(path, False)
    return ValidationReport(path, True, validate_data(data))


def load_workflows(path: Path | None = None) -> WorkflowsFile:
    """The validated file, or an empty one when it does not exist. Raises `WorkflowsError` on any problem."""
    path = path or workflows_path()
    try:
        data = read_raw(path)
    except ConfigError as exc:
        raise WorkflowsError("workflows_invalid", str(exc), path=str(path)) from exc
    if data is None:
        return WorkflowsFile()
    errors = validate_data(data)
    if errors:
        raise WorkflowsError(
            "workflows_invalid",
            f"{path}: " + "; ".join(f"{e.field}: {e.message}" for e in errors)
            + ". Fix it (see `agent-launcher config validate`); nothing was routed.",
            path=str(path),
        )
    return WorkflowsFile.model_validate(data)


def save_workflows(file: WorkflowsFile, path: Path | None = None) -> Path:
    """Write the file atomically: readers see the old one or the new one, never half."""
    path = path or workflows_path()
    write_json_atomic(path, json.loads(file.model_dump_json(exclude_defaults=False)))
    return path


def example_file() -> WorkflowsFile:
    """The example from the spec: what `workflows init` writes."""
    return WorkflowsFile(
        workflows=[
            Workflow(id="issue-triage", priority=100, match=Match(type="issue", labels_any=["needs-triage"]), skill="issue-triage"),
            Workflow(
                id="code-review", priority=80, match=Match(type="pr", review_requested="self"),
                preferred_agent="claude", skill="code-review",
            ),
            Workflow(id="pr-review-fixer", priority=60, match=Match(type="pr", author="self"), skill="pr-review-fixer"),
        ]
    )


def find_workflow(file: WorkflowsFile, workflow_id: str) -> Workflow | None:
    """A defined workflow by id; the built-in fallback (`default`) or `configure` workflow when the file does not define it."""
    for workflow in file.workflows:
        if workflow.id == workflow_id:
            return workflow
    if workflow_id == BUILTIN_FALLBACK_ID:
        return builtin_fallback()
    return builtin_configure() if workflow_id == BUILTIN_CONFIGURE_ID else None
