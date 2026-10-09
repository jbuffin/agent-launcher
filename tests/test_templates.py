"""Prompt templates and execute mode (ticket #15).

Unit tests drive `templates`/`build_prompt`; the end-to-end ones reuse the fake `gh`, real git and the mock terminal
of `test_workflows`. Nothing here needs cmux, an agent or the network. The cmux half of execute mode is in
`test_terminal_cmux`.
"""

import json

import pytest

from agent_launcher import templates
from agent_launcher.agent_adapters import ClaudeCodeAdapter
from agent_launcher.config import Config
from agent_launcher.errors import LauncherError
from agent_launcher.prompt import build_prompt, template_variables
from agent_launcher.tasks import Task
from agent_launcher.workflows import Workflow, validate_data
from test_github_issues import data, failure, mock_calls, run
from test_github_issues import checkout, env  # noqa: F401  (fixtures)
from test_github_pulls import PR_URL, gh, origin  # noqa: F401  (fixtures)
from test_workflows import REVIEW, review_pr, skills, write_flows  # noqa: F401  (fixtures)

EVIL = "Fix ${task_id} $(touch pwned) `id` $$ $x"


def put(launcher_home, name, text):
    (launcher_home / "templates").mkdir(parents=True, exist_ok=True)
    (launcher_home / "templates" / f"{name}.txt").write_text(text)


def github_task(url=PR_URL, title="A title") -> Task:
    return Task("t-abc", title, "body", 1, "/repo", "work", None, "created", "", "", source="github", url=url)


def local_task(title="Tidy up") -> Task:
    return Task("t-loc", title, "", 1, "/repo", "work", None, "created", "", "")


def prompt_of(task, flow, *, default=None, agent="claude", worktree="/wt", repository=None):
    variables = template_variables(
        task, flow, agent=agent, worktree_path=worktree, repository=repository, adapter=ClaudeCodeAdapter()
    )
    return build_prompt(task, flow, adapter=ClaudeCodeAdapter(), default_template=default, variables=variables)


# --- the engine -------------------------------------------------------------------------------------


def test_every_documented_variable_renders(launcher_home):
    put(launcher_home, "all", " ".join(f"{v}=${{{v}}}" for v in templates.VARIABLES))
    flow = Workflow(id="wf", template="all", skill="code-review")
    out = prompt_of(github_task(), flow, repository=None)
    assert out == (
        f"task_id=t-abc task_type=pr task_url={PR_URL} task_title=A title repository=acme/widgets "
        "repository_path=/repo worktree_path=/wt profile=work agent=claude workflow=wf skill_invocation=/code-review"
    )
    assert "task_type=issue" in prompt_of(github_task("https://github.com/acme/widgets/issues/7"), flow)
    # The stored kind wins over the URL's shape.
    variables = template_variables(github_task(), flow, agent="claude", worktree_path="/wt", kind="issue")
    assert variables["task_type"] == "issue"


def test_task_values_are_inserted_verbatim_and_never_evaluated(launcher_home):
    put(launcher_home, "t", "Task: $task_title | again: ${task_id}")
    out = prompt_of(github_task(title=EVIL), Workflow(id="wf", template="t"))
    assert out == f"Task: {EVIL} | again: t-abc"  # `${task_id}` in the title was not expanded a second time


def test_a_literal_dollar_is_written_twice(launcher_home):
    put(launcher_home, "t", "costs $$5 for $task_id")
    assert prompt_of(github_task(), Workflow(id="wf", template="t")) == "costs $5 for t-abc"


@pytest.mark.parametrize(
    "text,fragment",
    [("Use $nonsense here", "$nonsense"), ("price: $", "malformed"), ("open ${task_id", "malformed"), ("$task-id", "$task")],
)
def test_bad_templates_are_errors(launcher_home, text, fragment):
    put(launcher_home, "bad", text)
    with pytest.raises(templates.TemplateError) as err:
        prompt_of(github_task(), Workflow(id="wf", template="bad"))
    assert err.value.code == "template_invalid" and fragment in err.value.message


def test_a_missing_or_unreadable_file_is_an_error(launcher_home):
    with pytest.raises(templates.TemplateError) as err:
        prompt_of(github_task(), Workflow(id="wf", template="absent"))
    assert err.value.code == "template_missing" and str(launcher_home / "templates" / "absent.txt") in err.value.message
    (launcher_home / "templates").mkdir(parents=True, exist_ok=True)
    (launcher_home / "templates" / "binary.txt").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(templates.TemplateError) as err:
        prompt_of(github_task(), Workflow(id="wf", template="binary"))
    assert err.value.code == "template_unreadable"
    (launcher_home / "templates" / "huge.txt").write_text("x" * (templates.MAX_BYTES + 1))
    with pytest.raises(templates.TemplateError) as err:
        prompt_of(github_task(), Workflow(id="wf", template="huge"))
    assert err.value.code == "template_invalid"


def test_a_variable_the_task_lacks_is_an_error_not_a_blank(launcher_home):
    put(launcher_home, "needs-url", "Look at $task_url")
    put(launcher_home, "needs-repo", "In $repository")
    put(launcher_home, "no-worktree", "At $worktree_path")
    with pytest.raises(templates.TemplateError) as err:
        prompt_of(local_task(), Workflow(id="wf", template="needs-url"))
    assert err.value.code == "template_variable_unavailable" and "$task_url" in err.value.message
    assert "local" in err.value.message and "nothing was substituted" in err.value.message
    with pytest.raises(templates.TemplateError):
        prompt_of(local_task(), Workflow(id="wf", template="needs-repo"))  # no known GitHub name
    assert prompt_of(local_task(), Workflow(id="wf", template="needs-repo"), repository="acme/widgets") == "In acme/widgets"
    with pytest.raises(templates.TemplateError):
        prompt_of(github_task(), Workflow(id="wf", template="no-worktree"), worktree=None)


# --- precedence (SPEC §17) --------------------------------------------------------------------------


def test_precedence_is_template_then_skill_then_url_then_local_reference(launcher_home):
    put(launcher_home, "own", "OWN $task_id")
    put(launcher_home, "global", "GLOBAL $task_id")
    task = github_task()
    skill = Workflow(id="s", skill="code-review")
    assert prompt_of(task, Workflow(id="s", skill="code-review", template="own"), default="global") == "OWN t-abc"
    # The global template is for workflows with neither a template nor a skill: it never replaces a skill invocation.
    assert prompt_of(task, skill, default="global") == f"/code-review {PR_URL}"
    assert prompt_of(task, Workflow(id="plain"), default="global") == "GLOBAL t-abc"
    assert prompt_of(task, skill) == f"/code-review {PR_URL}"
    assert prompt_of(task, Workflow(id="plain")) == PR_URL
    assert prompt_of(local_task(), Workflow(id="plain")) == "Tidy up"
    # No workflow at all (a task opened before workflows existed): the plain prompt, no template.
    assert build_prompt(task, None, default_template="global") == PR_URL


def test_skill_invocation_lets_a_template_name_the_skill(launcher_home):
    put(launcher_home, "t", "$skill_invocation $task_url, carefully")
    assert prompt_of(github_task(), Workflow(id="wf", skill="code-review", template="t")) == f"/code-review {PR_URL}, carefully"
    with pytest.raises(templates.TemplateError) as err:
        prompt_of(github_task(), Workflow(id="wf", template="t"))  # no skill: empty is an error
    assert "$skill_invocation" in err.value.message
    assert "no skill" in errors({"id": "a", "template": "t"})["workflows.0.template"]
    assert errors({"id": "a", "template": "t", "skill": "code-review"}) == {}


def test_no_github_body_diff_or_comments_reach_a_prompt(launcher_home):
    put(launcher_home, "t", "Review $task_url")
    task = github_task()
    task = Task(**{**task.__dict__, "description": "HUGE BODY " * 500})
    assert prompt_of(task, Workflow(id="wf", template="t")) == f"Review {PR_URL}"
    assert "HUGE" not in prompt_of(task, Workflow(id="wf", skill="code-review"))


# --- validation -------------------------------------------------------------------------------------


def errors(*flows, home=None):
    return {e.field: e.message for e in validate_data({"version": 1, "workflows": list(flows)})}


def test_workflows_validation_checks_the_template_files(launcher_home):
    assert "workflows.0.template" in errors({"id": "a", "template": "absent"})
    put(launcher_home, "unknown", "$title")
    put(launcher_home, "urlish", "see $task_url")
    put(launcher_home, "fine", "see $task_id")
    assert "$title" in errors({"id": "a", "template": "unknown"})["workflows.0.template"]
    assert errors({"id": "a", "template": "fine"}) == {}
    assert errors({"id": "a", "template": "urlish", "match": {"type": "pr"}}) == {}
    assert "local" in errors({"id": "a", "template": "urlish", "match": {"type": "local"}})["workflows.0.template"]


def test_an_old_inline_template_string_gets_a_hint(launcher_home):
    message = errors({"id": "a", "template": "Review {url} now"})["workflows.0.template"]
    assert "templates/" in message and "file" in message


def test_doctor_warns_about_a_global_template_local_tasks_cannot_use(launcher_home):
    from agent_launcher.doctor import check_workflows

    put(launcher_home, "g", "Look at $task_url")
    check = check_workflows(Config(prompt_template="g"))
    assert check.status == "warn" and "$task_url" in check.detail
    put(launcher_home, "g", "Task $task_id")
    assert check_workflows(Config(prompt_template="g")).status == "pass"
    put(launcher_home, "g", "$skill_invocation")
    assert check_workflows(Config(prompt_template="g")).status == "fail"  # it never applies to a workflow with a skill


def test_config_validate_reports_template_problems(launcher_home, write_config):
    write_flows(launcher_home, {"id": "a", "template": "absent"})
    result = run("config", "validate")
    assert result.exit_code == 1 and "workflows.0.template" in result.output and "absent" in result.output
    write_flows(launcher_home, {"id": "a"})
    write_config({"version": 2, "prompt_template": "missing"})
    result = run("config", "validate")
    assert result.exit_code == 1 and "prompt_template" in result.output
    put(launcher_home, "missing", "hi $task_id")
    assert run("config", "validate").exit_code == 0


def test_a_broken_templates_file_stops_workflows_loading(launcher_home):
    write_flows(launcher_home, {"id": "a", "template": "absent"})
    result = run("workflows", "list")
    assert result.exit_code != 0 and "absent" in result.output


# --- end to end -------------------------------------------------------------------------------------


def test_a_workflow_template_is_the_prompt_sent_to_the_terminal(env, checkout, gh, origin, skills, launcher_home):
    put(launcher_home, "review", "Review $task_type $repository ($task_url) in $worktree_path as $agent/$profile [$workflow]")
    write_flows(launcher_home, {**REVIEW, "template": "review"})
    review_pr(gh, origin)
    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    (create,) = mock_calls(launcher_home)
    assert create["prompt"] == result["prompt"]
    assert result["prompt"].startswith(f"Review pr acme/widgets ({PR_URL}) in ")
    assert result["prompt"].endswith(" as claude/work [code-review]")
    assert create["working_directory"] in result["prompt"]


def test_a_global_default_template_applies_to_workflows_without_one(env, checkout, gh, origin, skills, launcher_home, write_config):
    put(launcher_home, "everywhere", "Global: $task_url")
    write_flows(launcher_home, {"id": "plain", "match": {"type": "pr"}})
    raw = json.loads((launcher_home / "config.json").read_text())
    write_config({**raw, "prompt_template": "everywhere"})
    review_pr(gh, origin)
    assert data(run("open", PR_URL, "--terminal", "mock", "--json"))["prompt"] == f"Global: {PR_URL}"


def test_a_failed_render_aborts_before_anything_starts(env, checkout, gh, origin, skills, launcher_home):
    put(launcher_home, "bad", "Hello $task_title and $nope")
    write_flows(launcher_home, {**REVIEW, "template": "bad"})
    review_pr(gh, origin)
    bad = failure(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert bad["code"] == "workflows_invalid" and "$nope" in bad["message"]  # caught when the workflows load
    assert mock_calls(launcher_home) == []  # not retried with the skill or the URL
    # Once it is fixed the same open succeeds, with the template.
    put(launcher_home, "bad", "Hello $task_id")
    assert data(run("open", PR_URL, "--terminal", "mock", "--json"))["prompt"].startswith("Hello t-")


def test_a_broken_global_template_aborts_the_open_and_changes_nothing(env, checkout, gh, origin, skills, launcher_home, write_config):
    put(launcher_home, "broken", "Hello $nope")
    raw = json.loads((launcher_home / "config.json").read_text())
    write_config({**raw, "prompt_template": "broken"})
    write_flows(launcher_home, {"id": "plain", "match": {"type": "pr"}})
    review_pr(gh, origin)
    err = failure(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert err["code"] == "template_invalid" and "$nope" in err["message"]
    assert mock_calls(launcher_home) == []
    assert data(run("worktrees", "list", "--json"))["worktrees"] == []  # refused before the launch began


def test_a_local_task_needing_the_url_fails_clearly_and_leaves_nothing(env, checkout, skills, launcher_home):
    put(launcher_home, "needs-url", "Look at $task_url")
    write_flows(launcher_home, {"id": "any", "template": "needs-url"})  # matches every type: only known at render
    task = data(run("new", "--title", "Tidy up", "--repo", str(checkout), "--json"))["task"]
    err = failure(run("open", task["id"], "--terminal", "mock", "--json"))
    assert err["code"] == "template_variable_unavailable" and "$task_url" in err["message"]
    assert mock_calls(launcher_home) == []
    assert data(run("worktrees", "list", "--json"))["worktrees"] == []  # no worktree was made
    from agent_launcher import state, launches

    with state.open_state() as conn:
        assert launches.get_launch(conn, task["id"]) is None  # and no launch was begun
        assert conn.execute("SELECT state FROM tasks WHERE id = ?", (task["id"],)).fetchone()[0] == "created"


def test_restart_that_cannot_build_its_prompt_leaves_the_old_session_untouched(env, checkout, skills, launcher_home, write_config):
    write_flows(launcher_home, {"id": "any"})
    task = data(run("new", "--title", "Tidy up", "--repo", str(checkout), "--json"))["task"]
    first = data(run("open", task["id"], "--terminal", "mock", "--json"))
    # Later the global template is changed to one this local task cannot render.
    put(launcher_home, "needs-url", "Look at $task_url")
    raw = json.loads((launcher_home / "config.json").read_text())
    write_config({**raw, "prompt_template": "needs-url"})
    err = failure(run("restart", task["id"], "--yes", "--terminal", "mock", "--json"))
    assert err["code"] == "template_variable_unavailable"
    assert mock_calls(launcher_home, "close_session") == [] and len(mock_calls(launcher_home)) == 1
    shown = data(run("tasks", "show", task["id"], "--json"))
    assert shown["sessions"][0]["terminal"] == first["session"]["terminal"]


def test_execute_is_refused_before_anything_is_asked_when_the_adapter_cannot_submit(env, checkout, gh, origin, skills, launcher_home, monkeypatch):
    from agent_launcher.terminal_mock import MockTerminalAdapter

    monkeypatch.setattr(MockTerminalAdapter, "capabilities", lambda self: {"create_session", "prepare_prompt"})
    write_flows(launcher_home, REVIEW)
    review_pr(gh, origin)
    # No terminal for the picker: had it been reached the error would be about selection, not capability.
    assert "submit_prompt" in json.dumps(failure(run("open", PR_URL, "--terminal", "mock", "--json", "--execute")))


def test_workflows_test_previews_the_template_and_its_failures(env, checkout, gh, origin, skills, launcher_home):
    put(launcher_home, "review", "Review $task_url in $worktree_path")
    write_flows(launcher_home, {**REVIEW, "template": "review"})
    review_pr(gh, origin)
    out = data(run("workflows", "test", PR_URL, "--json"))
    assert out["winner"]["prompt"] == f"Review {PR_URL} in <worktree_path>"
    put(launcher_home, "review", "Review $task_url in $nope")
    write_flows(launcher_home, REVIEW)  # the file no longer names the broken template
    assert data(run("workflows", "test", PR_URL, "--json"))["winner"]["prompt"] == f"/code-review {PR_URL}"


# --- execute mode -----------------------------------------------------------------------------------


def submitted(launcher_home):
    return [c["submit_prompt"] for c in mock_calls(launcher_home)]


def test_the_default_is_prepare(env, checkout, gh, origin, skills, launcher_home):
    write_flows(launcher_home, REVIEW)
    review_pr(gh, origin)
    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert submitted(launcher_home) == [False] and result["prompt_submitted"] is False


def test_execute_flag_submits(env, checkout, gh, origin, skills, launcher_home):
    write_flows(launcher_home, REVIEW)
    review_pr(gh, origin)
    result = data(run("open", PR_URL, "--terminal", "mock", "--json", "--execute"))
    assert submitted(launcher_home) == [True] and result["prompt_submitted"] is True


def test_global_setting_and_per_workflow_override_and_flags(env, checkout, gh, origin, skills, launcher_home, write_config):
    raw = json.loads((launcher_home / "config.json").read_text())
    write_config({**raw, "prompt_execution": "execute"})
    write_flows(launcher_home, REVIEW)
    review_pr(gh, origin)
    data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert submitted(launcher_home) == [True]  # global execute


def test_a_workflow_can_override_the_global_mode_either_way(env, checkout, gh, origin, skills, launcher_home, write_config):
    raw = json.loads((launcher_home / "config.json").read_text())
    write_config({**raw, "prompt_execution": "execute"})
    write_flows(launcher_home, {**REVIEW, "prompt_execution": "prepare"})
    review_pr(gh, origin)
    data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert submitted(launcher_home) == [False]
    write_config({**raw, "prompt_execution": "prepare"})
    other = "https://github.com/acme/widgets/issues/7"
    write_flows(launcher_home, {"id": "x", "prompt_execution": "execute"})
    data(run("open", other, "--terminal", "mock", "--json"))
    assert submitted(launcher_home) == [False, True]


def test_the_flags_beat_both_settings(env, checkout, gh, origin, skills, launcher_home, write_config):
    raw = json.loads((launcher_home / "config.json").read_text())
    write_config({**raw, "prompt_execution": "execute"})
    write_flows(launcher_home, {**REVIEW, "prompt_execution": "execute"})
    review_pr(gh, origin)
    data(run("open", PR_URL, "--terminal", "mock", "--json", "--prepare"))
    assert submitted(launcher_home) == [False]


def test_both_flags_together_are_refused(env, checkout, gh, origin, skills, launcher_home):
    result = run("open", PR_URL, "--terminal", "mock", "--execute", "--prepare")
    assert result.exit_code != 0 and mock_calls(launcher_home) == []


def test_execute_with_an_adapter_that_cannot_submit_fails_explicitly(env, checkout, gh, origin, skills, launcher_home, monkeypatch):
    from agent_launcher.terminal_mock import MockTerminalAdapter

    monkeypatch.setattr(MockTerminalAdapter, "capabilities", lambda self: {"create_session", "prepare_prompt"})
    write_flows(launcher_home, REVIEW)
    review_pr(gh, origin)
    err = failure(run("open", PR_URL, "--terminal", "mock", "--json", "--execute"))
    assert "submit_prompt" in json.dumps(err) and mock_calls(launcher_home) == []


def test_workflow_prompt_execution_is_validated(launcher_home):
    assert "workflows.0.prompt_execution" in errors({"id": "a", "prompt_execution": "now"})
    assert errors({"id": "a", "prompt_execution": "execute"}) == {}
    assert Config(prompt_template="x").prompt_template == "x"
