"""Workflow routing (ticket #14): `workflows.json`, deterministic matching, selection, prompts and `workflows test`.

Unit tests drive `routing` with `TaskFacts` directly. The end-to-end tests reuse the fake `gh` and real git of
`test_github_pulls`, with the mock terminal: no GitHub, no cmux, no agent.
"""

import json
from pathlib import Path

import pytest

from agent_launcher import state
from agent_launcher.agent_adapters import ClaudeCodeAdapter, AgentAdapter
from agent_launcher.config import Config
from agent_launcher.doctor import CommandResult, run_doctor
from agent_launcher.errors import LauncherError
from agent_launcher.github import GitHub
from agent_launcher.picker import pick_agent
from agent_launcher.prompt import build_prompt, template_variables
from agent_launcher.routing import TaskFacts, preferred_agent, route, select_workflow
from agent_launcher.tasks import Task
from agent_launcher.workflows import (
    Match,
    Workflow,
    WorkflowsError,
    WorkflowsFile,
    example_file,
    load_workflows,
    save_workflows,
    validate_data,
    validate_workflows,
)
from scripted import ScriptedPrompter
from test_github_issues import data, failure, mock_calls, run
from test_github_issues import checkout, env  # noqa: F401  (fixtures)
from test_github_pulls import PR_URL, PullGh, gh, origin  # noqa: F401  (fixtures)


def wf(id, priority=0, **match):
    return Workflow(id=id, priority=priority, match=Match(**match))


def cfg(**routing) -> Config:
    return Config.model_validate({"version": 2, "workflow_routing": routing})


def pr_facts(**kw) -> TaskFacts:
    base = dict(type="pr", repository="acme/widgets", labels=("bug",), author="octo", state="open", draft=False,
                review_requested=False, head_sha="abc1234", number=5, url=PR_URL, viewer_lookup=lambda: "me")
    return TaskFacts(**{**base, **kw})


def winner(workflows, facts, **routing) -> str:
    return route(WorkflowsFile(workflows=workflows), cfg(**routing), facts).winner.id


# --- The file: schema, validation, atomic writes ---------------------------------------------------


def errors(data):
    return {e.field: e.message for e in validate_data(data)}


def test_example_file_is_valid_and_round_trips(launcher_home):
    path = save_workflows(example_file())
    assert path == launcher_home / "workflows.json"
    assert validate_workflows().valid
    assert [w.id for w in load_workflows().workflows] == ["issue-triage", "code-review", "pr-review-fixer"]
    assert not list(launcher_home.glob(".workflows.json.*"))  # the temporary file is gone


def test_a_failed_write_leaves_the_old_file(launcher_home, monkeypatch):
    save_workflows(example_file())
    before = (launcher_home / "workflows.json").read_text()
    monkeypatch.setattr("os.replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        save_workflows(WorkflowsFile())
    assert (launcher_home / "workflows.json").read_text() == before
    assert not list(launcher_home.glob(".workflows.json.*"))


def test_unknown_fields_are_errors_with_suggestions():
    got = errors({"version": 1, "workflows": [{"id": "a", "match": {"label": ["x"], "reviewer": "self"}, "agent": "claude", "skil": "x"}]})
    assert "did you mean 'labels_any'" in got["workflows.0.match.label"]
    assert "did you mean 'review_requested'" in got["workflows.0.match.reviewer"]
    assert "did you mean 'preferred_agent'" in got["workflows.0.agent"]
    assert "did you mean 'skill'" in got["workflows.0.skil"]
    assert "did you mean 'workflows'" in errors({"version": 1, "workflow": []})["workflow"]


def test_a_workflow_cannot_name_a_profile_or_agents():
    got = errors({"version": 1, "workflows": [{"id": "a", "profile": "work", "agents": ["claude"]}]})
    assert "cannot choose a profile or grant agents" in got["workflows.0.profile"]
    assert "cannot choose a profile or grant agents" in got["workflows.0.agents"]


@pytest.mark.parametrize(
    "workflow,field",
    [
        ({"id": "bad id"}, "workflows.0.id"),
        ({"id": "a", "priority": "high"}, "workflows.0.priority"),
        ({"id": "a", "skill": "../../x"}, "workflows.0.skill"),
        ({"id": "a", "match": {"type": "task"}}, "workflows.0.match.type"),
        ({"id": "a", "match": {"ci": "green"}}, "workflows.0.match.ci"),
        ({"id": "a", "match": {"repository": "nowhere"}}, "workflows.0.match.repository"),
        ({"id": "a", "match": {"review_requested": "you"}}, "workflows.0.match.review_requested"),
        ({"id": "a", "template": "no such template"}, "workflows.0.template"),
        ({"id": "a", "template": "absent"}, "workflows.0.template"),
        ({"id": "a", "prompt_execution": "submit"}, "workflows.0.prompt_execution"),
    ],
)
def test_invalid_values_name_their_field(workflow, field):
    assert field in errors({"version": 1, "workflows": [workflow]})


def test_duplicate_ids_and_versions():
    assert "duplicate id" in errors({"version": 1, "workflows": [{"id": "a"}, {"id": "a"}]})["workflows.1.id"]
    assert "newer" in errors({"version": 2})["version"]
    assert "version" in errors({"workflows": []})


def test_load_raises_on_an_invalid_file_and_validate_reports_it(launcher_home):
    launcher_home.mkdir(parents=True)
    (launcher_home / "workflows.json").write_text('{"version": 1, "workflows": [{"id": "a", "match": {"label": ["x"]}}]}')
    with pytest.raises(WorkflowsError) as err:
        load_workflows()
    assert err.value.code == "workflows_invalid" and "labels_any" in err.value.message
    assert not validate_workflows().valid
    (launcher_home / "workflows.json").write_text("{nope")
    assert "not valid JSON" in validate_workflows().errors[0].message


def test_missing_file_means_no_rules(launcher_home):
    assert load_workflows().workflows == []
    assert validate_workflows().exists is False


def test_config_validate_and_doctor_cover_workflows(launcher_home, write_config):
    write_config({"version": 2, "profiles": {"work": {"default_agent": "claude", "agents": {"claude": {}}}}})
    assert run("config", "validate").exit_code == 0
    (launcher_home / "workflows.json").write_text('{"version": 1, "workflows": [{"id": "a", "match": {"labls": []}}]}')
    bad = run("config", "validate", "--json")
    assert bad.exit_code == 1
    assert "labels_any" in json.dumps(json.loads(bad.stdout)["workflows"])
    check = {c.id: c for c in run_doctor(which=lambda n: None).checks}["workflows"]
    assert check.status == "fail" and "labels_any" in check.detail
    save_workflows(example_file())
    assert run("config", "validate").exit_code == 0
    check = {c.id: c for c in run_doctor(which=lambda n: None).checks}["workflows"]
    assert check.status == "pass" and "3 rules" in check.detail


def test_a_fallback_that_is_not_defined_is_an_error(launcher_home, write_config):
    write_config({"version": 2, "workflow_routing": {"fallback": "triage"}})
    assert run("config", "validate").exit_code == 1
    check = {c.id: c for c in run_doctor(which=lambda n: None).checks}["workflows"]
    assert check.status == "fail" and "triage" in check.detail
    save_workflows(WorkflowsFile(workflows=[Workflow(id="triage")]))
    assert run("config", "validate").exit_code == 0


# --- Matching: every condition ---------------------------------------------------------------------


def matches(match: Match, facts: TaskFacts) -> bool:
    return route(WorkflowsFile(workflows=[Workflow(id="w", match=match)]), cfg(), facts).rules[0].matched


def test_type_repository_and_owner():
    assert matches(Match(type="pr"), pr_facts())
    assert not matches(Match(type="issue"), pr_facts())
    assert matches(Match(repository="ACME/Widgets"), pr_facts())
    assert not matches(Match(repository="acme/gadgets"), pr_facts())
    assert matches(Match(owner="acme"), pr_facts())
    assert not matches(Match(owner="other"), pr_facts())
    assert not matches(Match(owner="acme"), TaskFacts("local"))  # repository unknown never matches


def test_labels():
    facts = pr_facts(labels=("bug", "Needs-Triage"))
    assert matches(Match(labels_any=["needs-triage", "x"]), facts)
    assert not matches(Match(labels_any=["x"]), facts)
    assert matches(Match(labels_all=["bug", "needs-triage"]), facts)
    assert not matches(Match(labels_all=["bug", "x"]), facts)


def test_author_and_assignee_with_self():
    assert matches(Match(author="octo"), pr_facts())
    assert matches(Match(author=["x", "OCTO"]), pr_facts())
    assert not matches(Match(author="self"), pr_facts())
    assert matches(Match(author="self"), pr_facts(author="me"))
    assert matches(Match(author="self"), pr_facts(own=True))  # the stored flag wins without asking gh
    assert not matches(Match(author="self"), pr_facts(own=False, author="me"))
    assert matches(Match(assignee="self"), pr_facts(assignees=("x", "me")))
    assert not matches(Match(assignee="self"), pr_facts(assignees=("x",)))
    assert not matches(Match(assignee="x"), pr_facts())


def test_self_is_unknown_without_the_gh_user():
    facts = pr_facts(author="me", viewer_lookup=lambda: None)
    assert not matches(Match(author="self"), facts)
    result = route(WorkflowsFile(workflows=[wf("w", author="self")]), cfg(), facts).rules[0].conditions[0]
    assert "unknown" in result.reason


def test_review_requested_draft_and_state():
    assert matches(Match(review_requested="self"), pr_facts(review_requested=True))
    assert not matches(Match(review_requested="self"), pr_facts(review_requested=False))
    assert not matches(Match(review_requested="self"), pr_facts(review_requested=None))
    assert not matches(Match(review_requested="self"), pr_facts(type="issue", review_requested=None))
    assert matches(Match(draft=True), pr_facts(draft=True))
    assert matches(Match(draft=False), pr_facts(draft=False))
    assert not matches(Match(draft=True), pr_facts(type="issue", draft=None))
    assert matches(Match(state="merged"), pr_facts(state="merged"))
    assert matches(Match(state=["open", "closed"]), pr_facts())
    assert not matches(Match(state="closed"), pr_facts())


def test_conditions_are_anded_and_an_empty_match_matches_everything():
    both = Match(type="pr", labels_any=["bug"])
    assert matches(both, pr_facts()) and not matches(both, pr_facts(labels=()))
    assert matches(Match(), TaskFacts("local"))


def test_local_tasks_route_on_type_and_repository():
    local = TaskFacts("local", "acme/widgets")
    flows = [wf("any-pr", 50, type="pr"), wf("local-widgets", 10, type="local", repository="acme/widgets"), wf("labelled", 90, labels_any=["bug"])]
    assert winner(flows, local) == "local-widgets"
    assert winner(flows, TaskFacts("local", "acme/other")) == "default"


# --- CI status: fetched on demand, soft failure -------------------------------------------------------


def test_ci_is_fetched_only_when_a_rule_uses_it_and_once():
    calls = []

    def lookup():
        calls.append(1)
        return "success"

    facts = pr_facts(ci_lookup=lookup)
    assert winner([wf("a", 5, type="pr")], facts) == "a" and calls == []
    assert winner([wf("a", 5, ci="success"), wf("b", 4, ci=["success", "pending"])], facts) == "a" and calls == [1]


def test_ci_is_not_fetched_when_another_condition_already_fails():
    calls = []
    facts = pr_facts(ci_lookup=lambda: calls.append(1) or "success")
    assert winner([wf("a", 5, type="issue", ci="success")], facts) == "default" and calls == []


def test_ci_is_looked_up_last_even_when_it_is_listed_before_a_failing_condition():
    calls = []
    facts = pr_facts(ci_lookup=lambda: calls.append(1) or "success")
    assert winner([wf("a", 5, ci="success", state="closed")], facts) == "default" and calls == []  # state fails first


def test_ci_unknown_never_matches_success():
    assert winner([wf("a", ci="success")], pr_facts(ci_lookup=lambda: "unknown")) == "default"
    assert winner([wf("a", ci="unknown")], pr_facts(ci_lookup=lambda: "unknown")) == "a"
    assert winner([wf("a", ci="failure")], pr_facts(type="issue", head_sha=None)) == "default"


class CheckGh:
    def __init__(self, runs=None, statuses=None, fail=False):
        self.runs, self.statuses, self.fail, self.calls = runs or [], statuses or [], fail, []

    def __call__(self, argv, timeout, env=None):
        self.calls.append(argv)
        if self.fail:
            return CommandResult(1, "", "gh: HTTP 500")
        if "check-runs" in argv[2]:
            return CommandResult(0, json.dumps({"check_runs": self.runs}), "")
        state_ = "success" if all(s == "success" for s in self.statuses) else "failure"
        return CommandResult(0, json.dumps({"state": state_ if self.statuses else "pending", "statuses": [{"state": s} for s in self.statuses]}), "")


@pytest.mark.parametrize(
    "runs,statuses,expected",
    [
        ([{"status": "completed", "conclusion": "success"}], [], "success"),
        ([{"status": "completed", "conclusion": "skipped"}, {"status": "completed", "conclusion": "success"}], [], "success"),
        ([{"status": "completed", "conclusion": "success"}, {"status": "completed", "conclusion": "failure"}], [], "failure"),
        ([{"status": "completed", "conclusion": "cancelled"}], [], "failure"),
        ([{"status": "in_progress", "conclusion": None}], [], "pending"),
        ([{"status": "completed", "conclusion": "success"}], ["failure"], "failure"),
        ([], ["success"], "success"),
        ([], [], "unknown"),  # nothing reports: not evidence of success
    ],
)
def test_check_status(runs, statuses, expected):
    assert GitHub(runner=CheckGh(runs, statuses)).check_status("acme", "widgets", "abc1234") == expected


def test_check_status_fails_soft():
    fake = CheckGh(fail=True)
    assert GitHub(runner=fake).check_status("acme", "widgets", "abc1234") == "unknown"
    assert GitHub(runner=fake).check_status("acme", "widgets", "--upload-pack=x") == "unknown"
    assert all(c[:2] == ["gh", "api"] for c in fake.calls)  # and only reads


# --- Priority, ties, fallback -----------------------------------------------------------------------


def test_highest_priority_wins():
    flows = [wf("low", 10, type="pr"), wf("high", 90, type="pr"), wf("mid", 50, type="pr"), wf("other", 99, type="issue")]
    assert winner(flows, pr_facts()) == "high"


def test_ties_go_to_the_earlier_rule_in_the_file():
    flows = [wf("first", 50, type="pr"), wf("second", 50, type="pr"), wf("third", 50, type="pr")]
    assert winner(flows, pr_facts()) == "first"
    assert winner(list(reversed(flows)), pr_facts()) == "third"  # file order, not the id, decides
    routing = route(WorkflowsFile(workflows=flows), cfg(), pr_facts())
    assert [r.workflow.id for r in routing.matched] == ["first", "second", "third"]
    assert "3 rules tie at priority 50: first wins because it is earliest" in routing.tie_note()


def test_priority_beats_position_and_negative_priorities_sort_last():
    flows = [wf("early-low", 1, type="pr"), wf("late-high", 2, type="pr"), wf("negative", -5, type="pr")]
    assert [r.workflow.id for r in route(WorkflowsFile(workflows=flows), cfg(), pr_facts()).matched] == ["late-high", "early-low", "negative"]


def test_routing_is_repeatable():
    flows = [wf(f"w{i}", i % 3, type="pr") for i in range(9)]
    results = {winner(flows, pr_facts()) for _ in range(20)}
    assert results == {"w2"}


def test_fallback_applies_when_nothing_matches():
    routing = route(WorkflowsFile(workflows=[wf("a", type="issue")]), cfg(), pr_facts())
    assert routing.fallback_used and routing.winner.id == "default" and routing.winner.skill is None


def test_configured_fallback_is_used_and_must_exist():
    flows = [wf("a", type="issue"), Workflow(id="triage", skill="triage")]
    assert route(WorkflowsFile(workflows=flows), cfg(fallback="triage"), pr_facts()).winner.skill == "triage"
    with pytest.raises(LauncherError) as err:
        route(WorkflowsFile(workflows=flows), cfg(fallback="nope"), pr_facts())
    assert err.value.code == "fallback_missing"


def test_a_defined_default_replaces_the_builtin_fallback():
    flows = [Workflow(id="default", skill="general", match=Match(type="issue"))]
    assert route(WorkflowsFile(workflows=flows), cfg(), pr_facts()).winner.skill == "general"


def test_the_fallback_cannot_change_the_profile_or_grant_agents():
    """Structural: a workflow has no profile or agent list, the fallback included, and the picker only ever sees
    the repository's profile agents."""
    assert not ({"profile", "profiles", "agents", "env"} & set(Workflow.model_fields))
    fallback = Workflow(id="fb", preferred_agent="codex")
    assert preferred_agent(fallback, ["claude"])[0] is None
    pick = pick_agent("work", ["claude"], mode="always_ask", default="claude", last_used=None,
                      prompter=ScriptedPrompter(("select", "agent should run", "claude")), preferred=fallback.preferred_agent)
    assert pick.agent == "claude"
    with pytest.raises(LauncherError):  # and it cannot be forced either
        pick_agent("work", ["claude"], mode="always_ask", default=None, last_used=None, prompter=None, requested="codex")


# --- Selection modes ------------------------------------------------------------------------------------


def routing_of(flows, **kw):
    return route(WorkflowsFile(workflows=flows), cfg(), pr_facts(**kw))


TWO = [wf("review", 80, type="pr"), wf("fix", 60, type="pr"), wf("triage", 90, type="issue")]


def test_automatic_never_asks():
    sel = select_workflow(routing_of(TWO), mode="automatic", prompter=None)
    assert (sel.workflow.id, sel.how) == ("review", "automatic")
    sel = select_workflow(routing_of([wf("x", type="issue")]), mode="automatic", prompter=None)
    assert (sel.workflow.id, sel.how) == ("default", "fallback")


def test_ask_on_multiple_asks_only_with_several_matches():
    one = routing_of([wf("review", 80, type="pr")])
    assert select_workflow(one, mode="ask_on_multiple", prompter=None).workflow.id == "review"
    prompter = ScriptedPrompter(("select", "workflow", "fix"))
    sel = select_workflow(routing_of(TWO), mode="ask_on_multiple", prompter=prompter)
    prompter.done()
    assert (sel.workflow.id, sel.how) == ("fix", "asked")


def test_the_question_offers_the_winner_first_and_defaults_to_it():
    seen = {}

    class Spy(ScriptedPrompter):
        def select(self, message, choices, default=None):
            seen.update(values=[c.value for c in choices], default=default)
            return default

    sel = select_workflow(routing_of(TWO), mode="always_ask", prompter=Spy())
    assert seen["values"] == ["review", "fix", "triage", "default"] and seen["default"] == "review"
    assert sel.workflow.id == "review"
    select_workflow(routing_of(TWO), mode="ask_on_multiple", prompter=Spy())
    assert seen["values"] == ["review", "fix", "default"]


def test_always_ask_asks_even_for_one_match_and_force_ask_overrides_automatic():
    one = routing_of([wf("review", 80, type="pr")])
    prompter = ScriptedPrompter(("select", "workflow", "review"))
    select_workflow(one, mode="always_ask", prompter=prompter)
    prompter.done()
    prompter = ScriptedPrompter(("select", "workflow", "default"))
    sel = select_workflow(one, mode="automatic", prompter=prompter, force_ask=True)
    prompter.done()
    assert sel.workflow.id == "default"


def test_asking_without_a_terminal_names_the_way_out():
    with pytest.raises(LauncherError) as err:
        select_workflow(routing_of(TWO), mode="always_ask", prompter=None)
    assert err.value.code == "workflow_selection_needed" and "--workflow" in err.value.message


def test_explicit_workflow_wins_without_asking_and_unknown_ids_are_refused():
    sel = select_workflow(routing_of(TWO), mode="always_ask", prompter=None, requested="triage", config=cfg())
    assert (sel.workflow.id, sel.how) == ("triage", "override")  # even one whose rule does not match
    with pytest.raises(LauncherError) as err:
        select_workflow(routing_of(TWO), mode="automatic", prompter=None, requested="reveiw", config=cfg())
    assert err.value.code == "workflow_not_found" and "did you mean 'review'" in err.value.message
    with pytest.raises(LauncherError) as err:
        select_workflow(routing_of(TWO), mode="automatic", prompter=None, requested="review", force_ask=True, config=cfg())
    assert err.value.code == "workflow_options_conflict"


# --- Preferred agent ------------------------------------------------------------------------------------


def test_preferred_agent_is_highlighted_first_then_last_used_then_default():
    def highlighted(**kw):
        seen = {}

        class Spy(ScriptedPrompter):
            def select(self, message, choices, default=None):
                seen["default"], seen["labels"] = default, {c.value: c.label for c in choices}
                return default

        pick_agent("work", ["claude", "codex", "copilot"], mode="always_ask", prompter=Spy(), **kw)
        return seen

    assert highlighted(default="claude", last_used="codex", preferred="copilot")["default"] == "copilot"
    assert highlighted(default="claude", last_used="codex", preferred=None)["default"] == "codex"
    assert highlighted(default="claude", last_used=None, preferred=None)["default"] == "claude"
    assert highlighted(default="claude", last_used="codex", preferred="gemini")["default"] == "codex"  # not in the profile
    assert "workflow preference" in highlighted(default="claude", last_used=None, preferred="copilot")["labels"]["copilot"]


def test_a_preference_outside_the_profile_is_ignored_and_said():
    flow = Workflow(id="review", preferred_agent="codex")
    assert preferred_agent(flow, ["claude", "codex"]) == ("codex", None)
    agent, note = preferred_agent(flow, ["claude"])
    assert agent is None and "'codex'" in note and "ignored" in note
    assert preferred_agent(Workflow(id="x"), ["claude"]) == (None, None)


def test_a_preference_never_removes_the_choice_of_other_agents():
    seen = {}

    class Spy(ScriptedPrompter):
        def select(self, message, choices, default=None):
            seen["values"] = [c.value for c in choices]
            return "claude"

    pick_agent("work", ["claude", "codex"], mode="always_ask", default="claude", last_used=None, prompter=Spy(), preferred="codex")
    assert seen["values"] == ["claude", "codex"]


# --- The prompt ------------------------------------------------------------------------------------------


def github_task(url=PR_URL) -> Task:
    return Task("t-x", "A title $(touch pwned)", "", 1, "/r", "work", None, "created", "", "", source="github", url=url)


def test_a_skill_without_a_template_is_the_adapter_invocation_of_the_url():
    flow = Workflow(id="review", skill="code-review")
    assert build_prompt(github_task(), flow, adapter=ClaudeCodeAdapter()) == f"/code-review {PR_URL}"
    assert "$(touch" not in build_prompt(github_task(), flow, adapter=ClaudeCodeAdapter())  # the title is not in it


def test_no_workflow_or_no_skill_keeps_the_plain_prompt():
    assert build_prompt(github_task()) == PR_URL
    assert build_prompt(github_task(), Workflow(id="default"), adapter=ClaudeCodeAdapter()) == PR_URL
    local = Task("t-y", "Fix it", "details", 1, "/r", "work", None, "created", "", "")
    assert build_prompt(local, Workflow(id="d")) == "Fix it\n\ndetails"
    assert build_prompt(local, Workflow(id="s", skill="triage"), adapter=ClaudeCodeAdapter()) == "/triage Fix it\n\ndetails"


def test_a_template_replaces_the_invocation(launcher_home):
    (launcher_home / "templates").mkdir(parents=True)
    (launcher_home / "templates" / "run.txt").write_text("Run on $repository: $task_url")
    flow = Workflow(id="r", skill="code-review", template="run")
    variables = template_variables(github_task(), flow, agent="claude", worktree_path="/w")
    prompt = build_prompt(github_task(), flow, adapter=ClaudeCodeAdapter(), variables=variables)
    assert prompt == f"Run on acme/widgets: {PR_URL}"


def test_skill_invocation_is_per_adapter_and_per_instance():
    flow = Workflow(id="review", skill="code-review")
    with pytest.raises(LauncherError) as err:
        build_prompt(github_task(), flow, adapter=AgentAdapter())  # an agent with no slash syntax (others: #16)
    assert err.value.code == "skill_unsupported"
    assert build_prompt(github_task(), flow, adapter=AgentAdapter(), style="prompt").startswith("Use the code-review skill")
    with pytest.raises(LauncherError) as err:
        build_prompt(github_task(), flow, adapter=ClaudeCodeAdapter(), style="none")
    assert err.value.code == "skill_unsupported"


def test_skill_check_looks_by_name_only(tmp_path):
    adapter = ClaudeCodeAdapter()
    config_dir, project = tmp_path / "cfg", tmp_path / "proj"
    (config_dir / "skills" / "mine").mkdir(parents=True)
    (project / ".claude" / "commands").mkdir(parents=True)
    (project / ".claude" / "commands" / "fix.md").write_text("x")
    env = {"CLAUDE_CONFIG_DIR": str(config_dir), "HOME": str(tmp_path / "home")}
    assert adapter.check_skill("mine", env, [project]).status == "available"
    assert adapter.check_skill("fix", env, [project]).status == "available"
    absent = adapter.check_skill("absent", env, [project])
    # Not found is not proof of absence (bundled, plugin and managed skills are not in these directories).
    assert absent.status == "not_verified" and str(config_dir / "skills") in absent.checked
    assert adapter.check_skill("plugin:thing", env, [project]).status == "not_verified"
    assert AgentAdapter().check_skill("x", env, []).status == "not_verified"
    # Default location: HOME/.claude when CLAUDE_CONFIG_DIR is not set.
    (tmp_path / "home" / ".claude" / "skills" / "home-skill").mkdir(parents=True)
    assert adapter.check_skill("home-skill", {"HOME": str(tmp_path / "home")}, []).status == "available"


# --- End to end: open, store, reopen, test ---------------------------------------------------------------------

REVIEW = {"id": "code-review", "priority": 80, "match": {"type": "pr", "review_requested": "self"}, "preferred_agent": "claude", "skill": "code-review"}
FIXER = {"id": "pr-review-fixer", "priority": 60, "match": {"type": "pr", "author": "self"}, "skill": "pr-review-fixer"}
TRIAGE = {"id": "issue-triage", "priority": 100, "match": {"type": "issue", "labels_any": ["bug"]}, "skill": "issue-triage"}


@pytest.fixture
def skills(tmp_path, monkeypatch):
    """A Claude config directory (not the real one) with the skills the examples use."""
    config_dir = tmp_path / "claude-config"
    for name in ("code-review", "pr-review-fixer", "issue-triage"):
        (config_dir / "skills" / name).mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    return config_dir


def write_flows(launcher_home, *workflows):
    launcher_home.mkdir(parents=True, exist_ok=True)
    (launcher_home / "workflows.json").write_text(json.dumps({"version": 1, "workflows": list(workflows)}))


def review_pr(gh, origin, **extra):
    sha = origin.commit_on("feat/x", "x.txt")
    origin.pull_head(5, sha)
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha, user={"login": "octo"},
                requested_reviewers=[{"login": "me"}], **extra)
    return sha


def test_scenario_e_a_pr_requesting_my_review_gets_the_code_review_skill(env, checkout, gh, origin, skills, launcher_home):
    write_flows(launcher_home, TRIAGE, REVIEW, FIXER)
    review_pr(gh, origin)
    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert result["workflow"] == "code-review"
    assert result["prompt"] == f"/code-review {PR_URL}"
    (create,) = mock_calls(launcher_home)
    assert create["prompt"] == f"/code-review {PR_URL}"
    assert data(run("tasks", "show", result["task"]["id"], "--json"))["task"]["workflow"] == "code-review"


def test_my_own_pr_gets_the_fixer(env, checkout, gh, origin, skills, launcher_home):
    write_flows(launcher_home, TRIAGE, REVIEW, FIXER)
    sha = origin.commit_on("feat/x", "x.txt")
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha)
    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert (result["workflow"], result["prompt"]) == ("pr-review-fixer", f"/pr-review-fixer {PR_URL}")


def test_an_issue_is_routed_by_label(env, checkout, gh, skills, launcher_home):
    write_flows(launcher_home, TRIAGE, REVIEW)
    result = data(run("open", "https://github.com/acme/widgets/issues/7", "--terminal", "mock", "--json"))
    assert (result["workflow"], result["prompt"]) == ("issue-triage", "/issue-triage https://github.com/acme/widgets/issues/7")


def test_no_file_and_no_match_keep_the_url_prompt(env, checkout, gh, origin, launcher_home):
    result = data(run("open", "https://github.com/acme/widgets/issues/7", "--terminal", "mock", "--json"))
    assert (result["workflow"], result["prompt"]) == ("default", "https://github.com/acme/widgets/issues/7")


def test_a_local_task_routes_too(env, checkout, skills, launcher_home):
    write_flows(launcher_home, {"id": "local-work", "match": {"type": "local", "repository": "acme/widgets"}, "skill": "issue-triage"})
    task = data(run("new", "--title", "Tidy up", "--repo", str(checkout), "--json"))["task"]
    result = data(run("open", task["id"], "--terminal", "mock", "--json"))
    assert (result["workflow"], result["prompt"]) == ("local-work", "/issue-triage Tidy up")


def test_reopen_does_not_route_again(env, checkout, gh, origin, skills, launcher_home):
    write_flows(launcher_home, REVIEW, FIXER)
    review_pr(gh, origin)
    first = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    write_flows(launcher_home, {"id": "other", "priority": 999, "skill": "pr-review-fixer"})  # would win now
    again = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert again["action"] == "focused" and again["prompt"] is None
    assert again["task"]["workflow"] == "code-review" == first["task"]["workflow"]
    assert len(mock_calls(launcher_home)) == 1


def test_workflow_options_after_the_first_open_are_refused(env, checkout, gh, origin, skills, launcher_home):
    write_flows(launcher_home, REVIEW, FIXER)
    review_pr(gh, origin)
    data(run("open", PR_URL, "--terminal", "mock", "--json"))
    for option in (["--workflow", "pr-review-fixer"], ["--ask-workflow"]):
        assert failure(run("open", PR_URL, "--terminal", "mock", "--json", *option))["code"] == "workflow_fixed"


def test_the_workflow_option_overrides_routing(env, checkout, gh, origin, skills, launcher_home):
    write_flows(launcher_home, REVIEW, FIXER)
    review_pr(gh, origin)
    result = data(run("open", PR_URL, "--terminal", "mock", "--json", "--workflow", "pr-review-fixer"))
    assert result["workflow"] == "pr-review-fixer" and result["prompt"].startswith("/pr-review-fixer ")


def test_an_unknown_workflow_option_creates_no_session(env, checkout, gh, origin, skills, launcher_home):
    write_flows(launcher_home, REVIEW)
    review_pr(gh, origin)
    assert failure(run("open", PR_URL, "--terminal", "mock", "--json", "--workflow", "nope"))["code"] == "workflow_not_found"
    assert mock_calls(launcher_home) == []


def test_ask_workflow_goes_through_the_prompter(env, checkout, gh, origin, skills, launcher_home):
    from agent_launcher import state as st
    from agent_launcher.config import load_config
    from agent_launcher.github_tasks import open_github
    from test_github_issues import make_adapter

    write_flows(launcher_home, REVIEW, FIXER)
    review_pr(gh, origin)
    prompter = ScriptedPrompter(("select", "workflow", "pr-review-fixer"))
    with st.open_state() as conn:
        result = open_github(conn, PR_URL, config=load_config(), adapter=make_adapter(launcher_home), prompter=prompter,
                             github=GitHub(), ask_workflow=True)
    prompter.done()
    assert result.workflow == "pr-review-fixer" and result.prompt.startswith("/pr-review-fixer")


def test_selection_modes_from_config(env, checkout, gh, origin, skills, launcher_home, write_config):
    from agent_launcher.config import load_config
    from agent_launcher.github_tasks import open_github
    from test_github_issues import make_adapter
    from agent_launcher import state as st

    write_flows(launcher_home, REVIEW, {**FIXER, "match": {"type": "pr"}})
    review_pr(gh, origin)
    raw = json.loads((launcher_home / "config.json").read_text())
    raw["workflow_routing"] = {"selection_mode": "ask_on_multiple"}
    write_config(raw)
    prompter = ScriptedPrompter(("select", "workflow", "pr-review-fixer"))
    with st.open_state() as conn:
        result = open_github(conn, PR_URL, config=load_config(), adapter=make_adapter(launcher_home), prompter=prompter, github=GitHub())
    prompter.done()
    assert result.workflow == "pr-review-fixer"


def enable_require_verified(launcher_home, write_config):
    raw = json.loads((launcher_home / "config.json").read_text())
    raw["workflow_routing"] = {"require_verified_skills": True}
    write_config(raw)


def test_a_skill_not_found_by_name_is_launched_with_a_notice_not_refused(env, checkout, gh, origin, skills, launcher_home):
    """A by-name lookup proves presence, not absence: bundled and plugin skills are not in any directory."""
    (skills / "skills" / "code-review").rmdir()
    write_flows(launcher_home, REVIEW, FIXER)
    review_pr(gh, origin)
    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert result["prompt"] == f"/code-review {PR_URL}"  # the workflow's skill, not another
    assert "not found by name" in result["notice"] and str(skills / "skills") in result["notice"]
    assert len(mock_calls(launcher_home)) == 1


def test_require_verified_skills_makes_it_a_refusal_and_nothing_is_substituted(
    env, checkout, gh, origin, skills, launcher_home, write_config
):
    (skills / "skills" / "code-review").rmdir()
    enable_require_verified(launcher_home, write_config)
    write_flows(launcher_home, REVIEW, FIXER)
    review_pr(gh, origin)
    error = failure(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert error["code"] == "skill_missing" and "code-review" in error["message"] and "--workflow" in error["message"]
    assert mock_calls(launcher_home) == []  # nothing started, and pr-review-fixer was not used instead
    result = data(run("open", PR_URL, "--terminal", "mock", "--json", "--workflow", "pr-review-fixer"))
    assert result["prompt"].startswith("/pr-review-fixer")


def open_with_prompter(launcher_home, prompter):
    from agent_launcher import state as st
    from agent_launcher.config import load_config
    from agent_launcher.github_tasks import open_github
    from test_github_issues import make_adapter

    with st.open_state() as conn:
        return open_github(conn, PR_URL, config=load_config(), adapter=make_adapter(launcher_home), prompter=prompter, github=GitHub())


def test_on_a_terminal_a_missing_skill_offers_the_fallback_defaulting_to_no(
    env, checkout, gh, origin, skills, launcher_home, write_config
):
    (skills / "skills" / "code-review").rmdir()
    enable_require_verified(launcher_home, write_config)
    write_flows(launcher_home, REVIEW)
    review_pr(gh, origin)
    declined = ScriptedPrompter(("confirm", "fallback workflow 'default'", False))
    with pytest.raises(LauncherError) as err:
        open_with_prompter(launcher_home, declined)
    declined.done()
    assert err.value.code == "skill_missing" and mock_calls(launcher_home) == []
    accepted = ScriptedPrompter(("confirm", "fallback workflow 'default'", True))
    result = open_with_prompter(launcher_home, accepted)
    accepted.done()
    assert result.workflow == "default" and result.prompt == PR_URL
    assert "chose the fallback workflow" in result.notice
    assert data(run("tasks", "show", result.task.id, "--json"))["task"]["workflow"] == "default"


def test_the_skill_is_looked_for_in_the_worktree_the_agent_runs_in(env, checkout, gh, origin, skills, launcher_home, write_config):
    (skills / "skills" / "code-review").rmdir()
    enable_require_verified(launcher_home, write_config)
    write_flows(launcher_home, REVIEW)
    sha = origin.commit_on("feat/x", ".claude/commands/code-review.md")  # the PR branch adds the skill
    origin.pull_head(5, sha)
    gh.add_pull("acme/widgets", 5, db_id=7005, head_ref="feat/x", sha=sha, user={"login": "octo"},
                requested_reviewers=[{"login": "me"}])
    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert result["prompt"] == f"/code-review {PR_URL}" and not result["notice"]  # verified, so no refusal


def test_workflow_options_are_refused_when_an_earlier_launch_already_has_a_terminal(env, checkout, skills, launcher_home):
    from agent_launcher import launches, state as st
    from agent_launcher.terminals import TerminalSessionRef

    write_flows(launcher_home, {"id": "local-work", "match": {"type": "local"}, "skill": "issue-triage"})
    task = data(run("new", "--title", "Tidy", "--repo", str(checkout), "--json"))["task"]
    with st.open_state() as conn:
        launches.begin(conn, task["id"])
        launches.note_terminal(conn, task["id"], TerminalSessionRef(adapter="mock", workspace_id="w1", surface_id="s1", created_by_launcher=True))
    error = failure(run("open", task["id"], "--terminal", "mock", "--json", "--workflow", "default"))
    assert error["code"] == "workflow_fixed"
    assert failure(run("open", task["id"], "--terminal", "mock", "--json", "--ask-workflow"))["code"] == "workflow_fixed"


def test_a_plugin_skill_is_not_verified_but_launches_with_a_notice(env, checkout, gh, origin, skills, launcher_home):
    write_flows(launcher_home, {**REVIEW, "skill": "tools:code-review"})
    review_pr(gh, origin)
    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert result["prompt"] == f"/tools:code-review {PR_URL}" and "not found by name" in result["notice"]


def test_a_preference_outside_the_profile_is_ignored_at_open(env, checkout, gh, origin, skills, launcher_home):
    write_flows(launcher_home, {**REVIEW, "preferred_agent": "codex"})
    review_pr(gh, origin)
    result = data(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert result["session"]["agent"] == "claude" and "'codex'" in result["notice"] and "ignored" in result["notice"]
    assert result["task"]["profile"] == "work"


def test_an_invalid_workflows_file_stops_the_open_before_anything_is_created(env, checkout, gh, origin, launcher_home):
    (launcher_home).mkdir(parents=True, exist_ok=True)
    (launcher_home / "workflows.json").write_text('{"version": 1, "workflows": [{"id": "a", "skil": "x"}]}')
    review_pr(gh, origin)
    error = failure(run("open", PR_URL, "--terminal", "mock", "--json"))
    assert error["code"] == "workflows_invalid" and "did you mean 'skill'" in error["message"]
    assert mock_calls(launcher_home) == []


def test_restart_and_prompt_rebuild_the_prompt_from_the_stored_workflow(env, checkout, gh, origin, skills, launcher_home):
    write_flows(launcher_home, REVIEW, FIXER)
    review_pr(gh, origin)
    task = data(run("open", PR_URL, "--terminal", "mock", "--json"))["task"]
    write_flows(launcher_home, REVIEW, {"id": "other", "priority": 999, "skill": "pr-review-fixer"})
    restarted = data(run("restart", task["id"], "--yes", "--terminal", "mock", "--json"))
    assert restarted["prompt"] == f"/code-review {PR_URL}"  # the stored workflow, which is still defined
    write_flows(launcher_home, {"id": "other", "skill": "pr-review-fixer"})  # code-review is gone
    error = failure(run("restart", task["id"], "--yes", "--terminal", "mock", "--json"))
    assert error["code"] == "workflow_missing"


# --- workflows list / test ----------------------------------------------------------------------------------------


def snapshot(home: Path, repo: Path):
    from conftest import git_run

    db = home / "state.db"
    return (
        db.read_bytes(), sorted(p.name for p in home.glob("*") if not p.name.endswith(("-shm", "-wal"))),
        git_run(repo, "worktree", "list"), git_run(repo, "branch", "--list"),
    )


def test_workflows_test_explains_every_rule_and_creates_nothing(env, checkout, gh, origin, skills, launcher_home):
    write_flows(launcher_home, TRIAGE, REVIEW, {**FIXER, "priority": 80})
    review_pr(gh, origin)
    before = snapshot(launcher_home, checkout)
    result = data(run("workflows", "test", PR_URL, "--json"))
    assert snapshot(launcher_home, checkout) == before  # no task, database change, worktree or association
    with state.open_state() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    assert result["winner"]["id"] == "code-review" and result["matched"] == ["code-review"]
    by_id = {r["id"]: r for r in result["rules"]}
    assert [r["id"] for r in result["rules"]] == ["issue-triage", "code-review", "pr-review-fixer"]  # file order
    assert by_id["code-review"]["matched"] and by_id["code-review"]["rank"] == 1
    assert not by_id["issue-triage"]["matched"] and by_id["issue-triage"]["priority"] == 100
    reasons = {c["condition"]: c for c in by_id["pr-review-fixer"]["conditions"]}
    assert reasons["type"]["matched"] and not reasons["author"]["matched"] and "octo" in reasons["author"]["reason"]
    assert result["winner"]["prompt"] is None or result["winner"]["prompt"].startswith("/code-review")
    assert not any(c[:3] == ["gh", "api", "--method"] for c in gh.calls)  # reads only


def test_workflows_test_text_output_and_ties(env, checkout, gh, origin, skills, launcher_home):
    write_flows(launcher_home, REVIEW, {**FIXER, "priority": 80, "match": {"type": "pr"}})
    review_pr(gh, origin)
    out = run("workflows", "test", PR_URL)
    assert out.exit_code == 0, out.output
    text = out.stdout
    assert "#1 code-review  priority 80  MATCHED, rank 1" in text
    assert "#2 pr-review-fixer  priority 80  MATCHED, rank 2" in text
    assert "tie: 2 rules tie at priority 80: code-review wins because it is earliest" in text
    assert "winner: code-review" in text and "preferred agent: claude" in text


def test_workflows_test_says_when_a_preferred_agent_is_ignored(env, checkout, gh, origin, skills, launcher_home):
    from agent_launcher import state as st
    from agent_launcher.config import load_config

    write_flows(launcher_home, {**REVIEW, "preferred_agent": "codex"})
    review_pr(gh, origin)
    data(run("open", PR_URL, "--terminal", "mock", "--json", "--workflow", "code-review"))  # the repository now has a profile
    result = data(run("workflows", "test", PR_URL, "--json"))
    assert result["profile"] == "work"
    assert result["winner"]["highlighted_agent"] is None and "ignored" in result["winner"]["preferred_agent_note"]
    assert "ignored" in run("workflows", "test", PR_URL).stdout


def test_workflows_test_reads_ci_when_a_rule_uses_it(env, checkout, gh, origin, launcher_home, monkeypatch):
    write_flows(launcher_home, {"id": "green", "priority": 5, "match": {"type": "pr", "ci": "success"}, "skill": "x"})
    review_pr(gh, origin)
    called = []
    original = gh.__call__

    def spy(argv, timeout, env=None):
        called.append(argv[2])
        if "check-runs" in argv[2]:
            return CommandResult(0, json.dumps({"check_runs": [{"status": "completed", "conclusion": "success"}]}), "")
        if argv[2].endswith("/status"):
            return CommandResult(0, json.dumps({"state": "pending", "statuses": []}), "")
        return original(argv, timeout, env)

    import agent_launcher.github as gh_module

    monkeypatch.setattr(gh_module, "run_gh", spy)
    result = data(run("workflows", "test", PR_URL, "--json"))
    assert result["winner"]["id"] == "green"
    assert any("check-runs" in c for c in called)
    cond = {c["condition"]: c for c in result["rules"][0]["conditions"]}
    assert cond["ci"]["actual"] == "success"


def test_workflows_test_with_no_rules_shows_the_fallback(env, checkout, gh, origin, launcher_home):
    review_pr(gh, origin)
    result = data(run("workflows", "test", PR_URL, "--json"))
    assert result["rules"] == [] and result["winner"]["fallback"] and result["winner"]["id"] == "default"
    assert result["winner"]["prompt"] == PR_URL


def test_workflows_test_by_task_id_reads_the_stored_task(env, checkout, skills, launcher_home):
    write_flows(launcher_home, {"id": "local-work", "match": {"type": "local"}, "skill": "issue-triage"})
    task = data(run("new", "--title", "Tidy", "--repo", str(checkout), "--json"))["task"]
    result = data(run("workflows", "test", task["id"], "--json"))
    assert result["winner"]["id"] == "local-work" and result["task"]["type"] == "local" and result["profile"] == "work"
    assert failure(run("workflows", "test", "t-nonexist", "--json"))["code"] == "task_not_found"


def test_workflows_list_shows_rules_in_trial_order(launcher_home):
    write_flows(launcher_home, {"id": "low", "priority": 1}, {"id": "high", "priority": 9, "skill": "s"}, {"id": "also-low", "priority": 1})
    listed = data(run("workflows", "list", "--json"))
    assert [w["id"] for w in listed["workflows"]] == ["high", "low", "also-low"]
    assert listed["fallback"]["id"] == "default" and listed["selection_mode"] == "automatic"
    assert run("workflows", "list").stdout.index("high") < run("workflows", "list").stdout.index("also-low")


def test_workflows_init_never_overwrites(launcher_home):
    assert run("workflows", "init").exit_code == 0
    first = (launcher_home / "workflows.json").read_text()
    (launcher_home / "workflows.json").write_text(first + "\n")
    again = run("workflows", "init")
    assert again.exit_code == 1 and (launcher_home / "workflows.json").read_text() == first + "\n"
