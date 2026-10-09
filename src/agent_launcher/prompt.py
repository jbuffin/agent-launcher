"""The prompt sent to the agent. For a local task: the task reference and nothing else.

A GitHub task's prompt is its URL, exactly (SPEC §17). The issue title and body are never part of it: they are
untrusted text the agent can read through the URL if it wants.

Precedence, exactly (SPEC §17): a template (the workflow's; the global `prompt_template` only for a workflow with no
skill), else the workflow's
skill invocation of the task text (`/<skill> <url>` for Claude Code), else the task text. The instance's
`skill_invocation` setting decides how a skill is invoked: `slash` (the adapter's syntax), `prompt` (a plain sentence
naming the skill) or `none` (the agent cannot use skills: an error, never a substitute). A template that cannot be
rendered is an error; no other prompt takes its place.
"""

from collections.abc import Mapping

from agent_launcher import templates
from agent_launcher.agent_adapters import AgentAdapter
from agent_launcher.errors import LauncherError
from agent_launcher.routing import repository_full_name
from agent_launcher.tasks import Task
from agent_launcher.workflows import Workflow


def task_text(task: Task) -> str:
    if task.source == "github" and task.url:
        return task.url
    if task.description:
        return f"{task.title}\n\n{task.description}"
    return task.title


def task_type(task: Task) -> str:
    """`issue`, `pr` or `local`, from the task's URL. `template_variables` takes the stored kind when it has one."""
    if task.source != "github" or not task.url:
        return "local"
    return "pr" if "/pull/" in task.url else "issue"


def skill_phrase(workflow: Workflow, adapter: AgentAdapter, style: str) -> str | None:
    """How the workflow's skill is named to this agent without a task attached (`/code-review`), or None."""
    if workflow.skill is None or style == "none":
        return None
    if style == "prompt":
        return f"Use the {workflow.skill} skill"
    invoked = adapter.skill_invocation(workflow.skill, "")
    return invoked.strip() if invoked else None


def template_variables(
    task: Task, workflow: Workflow, *, agent: str | None, worktree_path: str | None, repository: str | None = None,
    kind: str | None = None, adapter: AgentAdapter | None = None, style: str = "slash",
) -> dict[str, str | None]:
    """The values a template can use. `None` is 'not available for this task' (see `templates.VARIABLES`); a
    template that needs one fails to render rather than getting an empty string."""
    return {
        "task_id": task.id,
        "task_type": kind or task_type(task),
        "task_url": task.url if task.source == "github" else None,
        "task_title": task.title,
        "repository": (repository_full_name(task.url) if task.url else None) or repository,
        "repository_path": task.repo_path,
        "worktree_path": worktree_path,
        "profile": task.profile,
        "agent": agent,
        "workflow": workflow.id,
        "skill_invocation": skill_phrase(workflow, adapter or AgentAdapter(), style),
    }


def build_prompt(
    task: Task,
    workflow: Workflow | None = None,
    *,
    adapter: AgentAdapter | None = None,
    style: str = "slash",
    default_template: str | None = None,
    variables: Mapping[str, str | None] | None = None,
) -> str:
    """`variables` are those of `template_variables`; a task with a template must be given them."""
    text = task_text(task)
    if workflow is None:
        return text
    # The global template is for workflows with neither a template nor a skill: it never replaces a skill invocation.
    name = workflow.template or (default_template if workflow.skill is None else None)
    if name is not None:
        if variables is None:
            raise LauncherError("template_unrendered", f"Template {name!r} was not given any variables.", template=name)
        return templates.render(name, variables)
    if workflow.skill is None:
        return text
    if style == "none":
        raise LauncherError(
            "skill_unsupported",
            f"Workflow {workflow.id!r} uses the skill {workflow.skill!r}, but this agent instance has "
            "skill_invocation 'none'. Nothing was substituted; use another agent or change the setting.",
            workflow=workflow.id, skill=workflow.skill,
        )
    if style == "prompt":
        return f"Use the {workflow.skill} skill on this: {text}"
    invoked = (adapter or AgentAdapter()).skill_invocation(workflow.skill, text)
    if invoked is None:
        raise LauncherError(
            "skill_unsupported",
            f"Workflow {workflow.id!r} uses the skill {workflow.skill!r}, but this agent has no supported way to "
            "invoke a skill by name. Nothing was substituted; set skill_invocation to 'prompt' for the instance, or "
            "use another agent.",
            workflow=workflow.id, skill=workflow.skill,
        )
    return invoked
