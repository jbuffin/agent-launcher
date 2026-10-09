"""The prompt sent to the agent. For a local task: the task reference and nothing else."""

from agent_launcher.tasks import Task


def build_prompt(task: Task) -> str:
    if task.description:
        return f"{task.title}\n\n{task.description}"
    return task.title
