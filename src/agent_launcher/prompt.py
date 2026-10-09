"""The prompt sent to the agent. For a local task: the task reference and nothing else.

A GitHub task's prompt is its URL, exactly (SPEC §17). The issue title and body are never part of it: they are
untrusted text the agent can read through the URL if it wants.

A workflow (`workflows`) changes it in one of two ways. With a `template`, the prompt is the template with `{url}`,
`{skill}`, `{repository}` and `{number}` filled in. With a `skill` and no template, it is the agent adapter's skill
invocation of the task text (`/<skill> <url>` for Claude Code). The instance's `skill_invocation` setting decides how:
`slash` (the adapter's syntax), `prompt` (a plain sentence naming the skill) or `none` (the agent cannot use skills:
an error, never a substitute).
"""

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


def build_prompt(
    task: Task, workflow: Workflow | None = None, *, adapter: AgentAdapter | None = None, style: str = "slash"
) -> str:
    text = task_text(task)
    if workflow is None:
        return text
    if workflow.template is not None:
        number = task.url.rsplit("/", 1)[-1] if task.url else ""
        return workflow.template.format(
            url=task.url or text, skill=workflow.skill or "", number=number,
            repository=repository_full_name(task.url) or "",
        )
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
