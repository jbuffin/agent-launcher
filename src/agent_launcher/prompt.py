"""The prompt sent to the agent. For a local task: the task reference and nothing else.

A GitHub task's prompt is its URL, exactly (SPEC §17); skills and workflows add to it in a later ticket. The issue
title and body are never part of it: they are untrusted text the agent can read through the URL if it wants.
"""

from agent_launcher.tasks import Task


def build_prompt(task: Task) -> str:
    if task.source == "github" and task.url:
        return task.url
    if task.description:
        return f"{task.title}\n\n{task.description}"
    return task.title
