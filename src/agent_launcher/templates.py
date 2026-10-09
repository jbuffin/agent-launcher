"""Prompt templates (SPEC §17): files under `~/.agent-launcher/templates/`, referenced by name.

A template named `review` is the file `templates/review.txt`. It is plain text with `$variable` or `${variable}`
placeholders (`string.Template`; `$$` is a literal dollar). The variables are the ten of `VARIABLES`. Rendering is a
single pass: values are inserted verbatim, so a task title that contains `$(...)`, `${x}` or a backtick is text, never
evaluated and never expanded again.

Everything that can be wrong is an error, found when `config validate`/`doctor`/`workflows` load and again when the
prompt is rendered: a missing or unreadable file, an unknown variable, a malformed placeholder, and a variable that
has no value for the task (`task_url` on a local task). A failed render is never replaced by another prompt.
"""

from collections.abc import Mapping
from string import Template

from agent_launcher.errors import LauncherError
from agent_launcher.paths import templates_dir

EXTENSION = ".txt"
MAX_BYTES = 64 * 1024

VARIABLES: dict[str, str] = {
    "task_id": "the launcher's task ID",
    "task_type": "issue, pr or local",
    "task_url": "the GitHub URL; GitHub tasks only",
    "task_title": "the task's title, verbatim",
    "repository": "owner/name; GitHub tasks, and local tasks whose repository has a known GitHub name",
    "repository_path": "the repository's local checkout",
    "worktree_path": "the task's worktree; only once it exists (not in `workflows test`)",
    "profile": "the repository's profile",
    "agent": "the agent that runs; only once chosen (not in `workflows test` unless one is given)",
    "workflow": "the id of the workflow that chose the template",
    "skill_invocation": "how the workflow's skill is invoked for this agent (`/code-review` for Claude Code); workflows with a skill only",
}
LOCAL_UNAVAILABLE = ("task_url",)
"""Variables a local task can never have, so a workflow that only matches local tasks cannot use them."""


class TemplateError(LauncherError):
    pass


def template_path(name: str):
    return templates_dir() / f"{name}{EXTENSION}"


def _fail(code: str, message: str, **details) -> TemplateError:
    return TemplateError(code, message, **details)


def read_template(name: str) -> str:
    path = template_path(name)
    try:
        if path.stat().st_size > MAX_BYTES:
            raise _fail("template_invalid", f"Template {name!r} ({path}) is larger than {MAX_BYTES // 1024} KiB.", template=name)
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise _fail(
            "template_missing", f"Template {name!r} does not exist: expected {path}. Nothing was substituted.", template=name
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise _fail(
            "template_unreadable", f"Template {name!r} ({path}) cannot be read as UTF-8 text: {exc}", template=name
        ) from exc
    return text


def used_variables(text: str, name: str = "?") -> list[str]:
    """The variables a template refers to, in order of first use. Raises `TemplateError` for a malformed one."""
    template = Template(text)
    if not template.is_valid():
        raise _fail(
            "template_invalid",
            f"Template {name!r} has a malformed placeholder (a lone `$`, or `${{` without `}}`). "
            "Write `$$` for a literal dollar sign.",
            template=name,
        )
    found = template.get_identifiers()
    unknown = [v for v in found if v not in VARIABLES]
    if unknown:
        raise _fail(
            "template_invalid",
            f"Template {name!r} uses unknown variable(s): {', '.join('$' + v for v in unknown)}. "
            f"Available: {', '.join(VARIABLES)}.",
            template=name, unknown=unknown,
        )
    return list(dict.fromkeys(found))


def problem(name: str, *, task_types: tuple[str, ...] | None = None, has_skill: bool = True) -> str | None:
    """Why `name` cannot be used, or None. `task_types` narrows what the template will see: a workflow that only
    matches local tasks cannot use `task_url`. `has_skill` is False for a workflow with no skill."""
    try:
        used = used_variables(read_template(name), name)
    except TemplateError as exc:
        return exc.message
    if not has_skill and "skill_invocation" in used:
        return f"template {name!r} uses $skill_invocation, but this workflow has no skill"
    if task_types == ("local",):
        bad = [v for v in used if v in LOCAL_UNAVAILABLE]
        if bad:
            return f"template {name!r} uses ${bad[0]}, which a local task does not have (this workflow matches only local tasks)"
    return None


def render(name: str, values: Mapping[str, str | None]) -> str:
    """The template with its variables filled in. A variable with no value is an error that says why."""
    text = read_template(name)
    used = used_variables(text, name)
    missing = [v for v in used if not values.get(v)]
    if missing:
        what = ", ".join("$" + v for v in missing)
        task_type = values.get("task_type") or "this"
        raise _fail(
            "template_variable_unavailable",
            f"Template {name!r} needs {what}, which has no value for this {task_type} task ({VARIABLES[missing[0]]}). "
            "The prompt was not built; nothing was substituted.",
            template=name, variables=missing,
        )
    return Template(text).substitute({v: values[v] for v in used})


def config_problem(config) -> str | None:
    """Why the global `prompt_template` cannot be used, or None."""
    name = config.prompt_template
    if name is None:
        return None
    why = problem(name, has_skill=False)  # it applies only to workflows with no skill
    return f"prompt_template: {why}" if why else None


def config_warning(config) -> str | None:
    """A global template that no local task can use."""
    name = config.prompt_template
    if name is None or problem(name, has_skill=False) is not None:
        return None
    if "task_url" in used_variables(read_template(name), name):
        return (
            f"prompt_template {name!r} uses $task_url, which a local task does not have: local tasks "
            "(and any workflow without its own template) cannot be opened until it is changed"
        )
    return None
