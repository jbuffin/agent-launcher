import json
import os
import re
import subprocess
import sys
from dataclasses import replace

import pytest

from commodore_launcher import cli
from conftest import make_github_task
from test_tasks_flow import fake_agents  # noqa: F401  (fixture)

from commodore_launcher.agent_adapters import ClaudeCodeAdapter, agent_adapter_for
from commodore_launcher.terminal_cmux import CmuxAdapter, CmuxResult
from commodore_launcher.terminal_cmux import run_process as REAL_RUN_PROCESS  # conftest replaces the module's own
from commodore_launcher.terminal_select import select_adapter
from commodore_launcher.terminals import (
    CLOSE_SESSION,
    CREATE_SESSION,
    FOCUS_SESSION,
    PROMPT_MARKER,
    RUN_INTERACTIVE,
    CreateSessionRequest,
    ResumeCheck,
    StaleTerminalSession,
    TerminalError,
    TerminalSessionRef,
    UnsupportedCapability,
)

WS = "11111111-2222-3333-4444-555555555555"
SF = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
IDLE = "╭────╮\n│ >  │\n╰────╯\n  ? for shortcuts\n"


REAL_UUID_LINE = '* B1C2D3E4-F5A6-4B7C-8D9E-0F1A2B3C4D5E  terminal  [focused]  "gh dash"'
REAL_REF_LINE = '* surface:1000000000  terminal  [focused]  "title"'
"""`cmux list-panels --workspace W` as observed on cmux 0.65.0: refs by default, UUIDs with `--id-format uuids`."""


def split_id_format(argv):
    """(argv without the global `--id-format X`, X or None)."""
    argv = list(argv)
    if "--id-format" in argv:
        i = argv.index("--id-format")
        return argv[:i] + argv[i + 2 :], argv[i + 1]
    return argv, None


class FakeCmux:
    """A scripted cmux. `screens` are returned by successive read-screen calls (the last repeats)."""

    def __init__(self, screens=(IDLE,), ping_rc=0):
        self.calls: list[tuple[list[str], str | None]] = []
        self.screens = list(screens)
        self.ping_rc = ping_rc
        self.env_file_text: str | None = None

    def __call__(self, argv, timeout, stdin=None):
        argv, id_format = split_id_format(argv)
        self.id_formats = [*getattr(self, "id_formats", []), (argv[1] if len(argv) > 1 else None, id_format)]
        self.calls.append((argv, stdin))
        cmd = argv[1] if len(argv) > 1 else argv[0]
        if cmd == "ping":
            return CmuxResult(self.ping_rc, "PONG" if not self.ping_rc else "", "Access denied" if self.ping_rc else "")
        if cmd == "new-workspace":
            if "--env-file" in argv:
                with open(argv[argv.index("--env-file") + 1]) as f:
                    self.env_file_text = f.read()
            return CmuxResult(0, f"OK {WS}\n", "")
        if cmd == "list-panels":
            line = f'* {SF}  terminal  [focused]  "zsh"' if id_format == "uuids" else REAL_REF_LINE
            return CmuxResult(0, line + "\n", "")
        if cmd == "read-screen":
            screen = self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
            return CmuxResult(0, screen, "")
        return CmuxResult(0, "OK\n", "")

    def commands(self, name):
        return [a for a, _ in self.calls if len(a) > 1 and a[1] == name]


def adapter(fake, **kw):
    clock = iter(range(0, 10_000))
    return CmuxAdapter(fake, cli="/bin/cmux", sleep=lambda s: None, clock=lambda: float(next(clock)), base_env={"PATH": "/usr/bin"}, **kw)


def request(prompt=None, **kw):
    return CreateSessionRequest(
        title="platform — Fix login",
        working_directory="/work/platform",
        command=["/bin/claude", "--model", "a b"],
        env={"PATH": "/usr/bin", "CLAUDE_CONFIG_DIR": "/home/u/.claude-personal"},
        pinned_env=("CLAUDE_CONFIG_DIR",),
        prompt=prompt,
        **kw,
    )


def test_capabilities_are_honest():
    caps = adapter(FakeCmux()).capabilities()
    assert caps == {CREATE_SESSION, FOCUS_SESSION, CLOSE_SESSION, RUN_INTERACTIVE}


def test_run_interactive_opens_a_workspace_that_runs_only_the_command():
    fake = FakeCmux()
    ref = adapter(fake).run_interactive("Commodore Launcher: choose", "/work", ["/py", "-m", "commodore_launcher", "open", "https://x/o r/issues/1"])
    (argv,) = fake.commands("new-workspace")
    assert argv[argv.index("--cwd") + 1] == "/work"
    assert argv[argv.index("--command") + 1] == " /usr/bin/env /py -m commodore_launcher open 'https://x/o r/issues/1'; exit"
    assert not fake.commands("paste") and not fake.commands("send-key")  # nothing is typed into it
    assert ref == TerminalSessionRef("cmux", WS, SF, created_by_launcher=True)


def test_availability_and_reasons():
    assert adapter(FakeCmux()).available()
    denied = adapter(FakeCmux(ping_rc=1))
    assert not denied.available() and "from a cmux terminal" in denied.unavailable_reason()
    missing = CmuxAdapter(FakeCmux(), cli=None)
    assert not missing.available() and "not found" in missing.unavailable_reason()


def test_creates_named_workspace_with_cwd_quoted_command_and_env_outside_argv():
    fake = FakeCmux()
    result = adapter(fake).create_session(request())
    (argv,) = fake.commands("new-workspace")
    assert argv[argv.index("--name") + 1] == "platform — Fix login"
    assert argv[argv.index("--cwd") + 1] == "/work/platform"
    assert argv[argv.index("--command") + 1] == " /usr/bin/env CLAUDE_CONFIG_DIR=/home/u/.claude-personal /bin/claude --model 'a b'"
    assert argv[argv.index("--command") + 1].startswith(" ")  # leading space for HIST_IGNORE_SPACE
    # Only the variable that differs from the caller's goes to cmux, and not on the command line.
    assert fake.env_file_text == "CLAUDE_CONFIG_DIR=/home/u/.claude-personal\n"
    assert "--env-file" in argv and "claude-personal" not in argv[argv.index("--env-file") + 1]
    assert result.session == TerminalSessionRef("cmux", WS, SF, created_by_launcher=True)


def test_profile_env_is_passed_even_when_the_caller_already_has_it():
    fake = FakeCmux()
    a = CmuxAdapter(
        fake, cli="/bin/cmux", sleep=lambda s: None, base_env={"PATH": "/usr/bin", "CLAUDE_CONFIG_DIR": "/home/u/.claude-personal"}
    )
    a.create_session(request(prompt=None))
    (argv,) = fake.commands("new-workspace")
    assert fake.env_file_text == "CLAUDE_CONFIG_DIR=/home/u/.claude-personal\n"
    assert argv[argv.index("--command") + 1].startswith(" /usr/bin/env CLAUDE_CONFIG_DIR=/home/u/.claude-personal /bin/claude")


def test_awkward_values_round_trip_through_the_shell_quoting():
    import shlex

    value = "/a b/it's \"q\" $HOME;x"
    fake = FakeCmux()
    req = CreateSessionRequest(
        title="t", working_directory="/w", command=["/bin/claude", "x y"], env={"K": value}, pinned_env=("K",)
    )
    adapter(fake).create_session(req)
    (argv,) = fake.commands("new-workspace")
    assert shlex.split(argv[argv.index("--command") + 1]) == ["/usr/bin/env", f"K={value}", "/bin/claude", "x y"]


def test_env_file_is_removed_afterwards(tmp_path):
    fake = FakeCmux()
    adapter(fake).create_session(request())
    (argv,) = fake.commands("new-workspace")
    import os

    assert not os.path.exists(argv[argv.index("--env-file") + 1])


TRICKY = "- starts with a dash\nhas 'quotes', \"doubles\", $HOME, `ticks` and $(echo no)\n\n  last line"


@pytest.mark.parametrize("form", [("--", PROMPT_MARKER), (f"--interactive={PROMPT_MARKER}",)])
@pytest.mark.parametrize("shell", ["/bin/sh", "/bin/zsh", "/bin/bash"])
def test_execute_at_launch_hands_the_prompt_to_the_agent_without_typing_it(shell, form, tmp_path):  # 15.1, 15.2
    if not os.path.exists(shell):
        pytest.skip(f"{shell} not installed")
    fake = FakeCmux()
    echo = [sys.executable, "-c", "import json, sys; print(json.dumps(sys.argv[1:]))"]
    result = adapter(fake).create_session(
        replace(request(TRICKY, submit_prompt=True, prompt_args=form), command=echo)
    )
    assert result.prompt_submitted and result.notice is None
    assert not fake.commands("paste") and not fake.commands("send-key") and not fake.commands("read-screen")
    (argv,) = fake.commands("new-workspace")
    typed = argv[argv.index("--command") + 1]
    assert "starts with a dash" not in typed and "$(cat -- " in typed  # never echoed by the shell or kept in history
    # The workspace's shell runs the typed line: the agent gets the prompt as one argument, byte for byte
    # (bar trailing newlines), and the file is gone afterwards.
    ran = subprocess.run([shell, "-c", typed], capture_output=True, text=True, cwd=tmp_path)
    assert json.loads(ran.stdout) == [a.replace(PROMPT_MARKER, TRICKY) for a in form], ran.stderr
    assert not [p for p in re.findall(r"-- '?(/[^ ')]+commodore-launcher-prompt-[^ ')]+)", typed) if os.path.exists(p)]


def test_execute_at_launch_takes_exactly_one_prompt_argument():
    for args in ((PROMPT_MARKER, PROMPT_MARKER), ("--",)):
        with pytest.raises(TerminalError, match="Exactly one"):
            request("x", submit_prompt=True, prompt_args=args)


def test_a_prompt_without_launch_arguments_is_refused_not_pasted():
    fake = FakeCmux()
    with pytest.raises(TerminalError, match="launch arguments"):
        adapter(fake).create_session(request("Fix the login bug", submit_prompt=True))
    assert not fake.commands("new-workspace") and not fake.commands("paste")


def test_a_failed_workspace_creation_leaves_no_prompt_file_behind(tmp_path, monkeypatch):
    import glob
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))  # this test's own temp dir: nothing else can add files

    class Down(FakeCmux):
        def __call__(self, argv, timeout, stdin=None):
            if "new-workspace" in argv:
                return CmuxResult(1, "", "cmux is not responding")
            return super().__call__(argv, timeout, stdin)

    pattern = os.path.join(tempfile.gettempdir(), "commodore-launcher-prompt-*")
    before = set(glob.glob(pattern))
    with pytest.raises(TerminalError):
        adapter(Down()).create_session(request("Fix it", submit_prompt=True, prompt_args=("--", PROMPT_MARKER)))
    assert set(glob.glob(pattern)) == before


def test_execute_at_launch_is_only_for_a_new_prompt():
    fake = FakeCmux()
    result = adapter(fake).create_session(request(prompt=None, submit_prompt=False, prompt_args=("--", PROMPT_MARKER)))
    (argv,) = fake.commands("new-workspace")
    assert "$(cat" not in argv[argv.index("--command") + 1] and not result.prompt_submitted


def test_no_workspace_id_in_output_is_an_error():
    class Odd(FakeCmux):
        def __call__(self, argv, timeout, stdin=None):
            if split_id_format(argv)[0][1] == "new-workspace":
                return CmuxResult(0, "OK\n", "")
            return super().__call__(argv, timeout, stdin)

    with pytest.raises(TerminalError) as err:
        adapter(Odd()).create_session(request())
    assert err.value.code == "cmux_unexpected_output"


def test_cmux_failure_on_create_is_an_error():
    class Broken(FakeCmux):
        def __call__(self, argv, timeout, stdin=None):
            if split_id_format(argv)[0][1] == "new-workspace":
                return CmuxResult(1, "", "boom")
            return super().__call__(argv, timeout, stdin)

    with pytest.raises(TerminalError) as err:
        adapter(Broken()).create_session(request())
    assert err.value.code == "cmux_failed"


def test_focus_and_close_target_the_workspace():
    fake = FakeCmux()
    a = adapter(fake)
    ref = TerminalSessionRef("cmux", WS, SF)
    a.focus_session(ref)
    a.close_session(ref)
    assert fake.commands("workspace")[0][1:] == ["workspace", "select", WS]
    assert not fake.commands("close-workspace") and not fake.commands("select-workspace")


def test_close_forces_only_a_workspace_the_launcher_created():
    fake = FakeCmux()
    a = adapter(fake)
    made = a.create_session(request()).session
    assert made.created_by_launcher
    a.close_session(made)
    a.close_session(TerminalSessionRef("cmux", "workspace:7", None))  # adopted or loaded from the registry
    forced, adopted = [c for c in fake.commands("workspace") if c[2] == "close"]
    assert forced == ["/bin/cmux", "workspace", "close", WS, "--force"]
    assert adopted == ["/bin/cmux", "workspace", "close", "workspace:7"]


def test_real_id_shapes_are_accepted():
    class Real(FakeCmux):
        def __call__(self, argv, timeout, stdin=None):
            if split_id_format(argv)[0][1] == "new-workspace":
                return CmuxResult(0, "OK workspace:1000000054\n", "")
            if split_id_format(argv)[0][1] == "list-panels":
                return CmuxResult(0, "* D2E3F4A5-1B2C-4D3E-8F90-A1B2C3D4E5F6  [terminal]\n", "")
            return super().__call__(argv, timeout, stdin)

    ref = adapter(Real()).create_session(request(prompt=None)).session
    assert ref.workspace_id == "workspace:1000000054" and ref.surface_id == "D2E3F4A5-1B2C-4D3E-8F90-A1B2C3D4E5F6"


def test_several_panels_record_no_surface():
    class Many(FakeCmux):
        def __call__(self, argv, timeout, stdin=None):
            if split_id_format(argv)[0][1] == "list-panels":
                return CmuxResult(0, f"{SF}\nsurface:2\n", "")
            return super().__call__(argv, timeout, stdin)

    assert adapter(Many()).create_session(request(prompt=None)).session.surface_id is None


def test_selectable_by_name():
    assert isinstance(select_adapter("cmux"), CmuxAdapter)


# --- focus, screen, resume and prompt on an existing session (ticket #9) -----------------


class StaleCmux(FakeCmux):
    """A cmux whose workspace is gone (`list-panels` fails) or whose surface is no longer listed."""

    def __init__(self, *args, panels_rc=1, panels_out="", **kw):
        super().__init__(*args, **kw)
        self.panels_rc, self.panels_out = panels_rc, panels_out

    def __call__(self, argv, timeout, stdin=None):
        if "list-panels" in argv:
            self.calls.append((split_id_format(argv)[0], stdin))
            missing = "Error: Workspace ref not found: workspace:999999999"
            return CmuxResult(self.panels_rc, self.panels_out, missing if self.panels_rc else "")
        return super().__call__(argv, timeout, stdin)


def test_focus_selects_the_workspace_only_after_checking_it_exists():
    fake = FakeCmux()
    adapter(fake).focus_session(TerminalSessionRef("cmux", WS, SF))
    names = [a[1] for a, _ in fake.calls]
    assert names == ["list-panels", "workspace"]


def test_focus_of_a_vanished_workspace_is_a_stale_error_and_selects_nothing():
    fake = StaleCmux()
    with pytest.raises(StaleTerminalSession) as err:
        adapter(fake).focus_session(TerminalSessionRef("cmux", WS, SF))
    assert err.value.code == "terminal_session_stale"
    assert not fake.commands("workspace")


def test_focus_of_a_workspace_whose_surface_is_gone_is_stale():
    fake = StaleCmux(panels_rc=0, panels_out="* some-other-surface  [terminal]\n")
    with pytest.raises(StaleTerminalSession):
        adapter(fake).focus_session(TerminalSessionRef("cmux", WS, SF))
    assert not fake.commands("workspace")


def test_read_screen_is_none_for_a_missing_session_and_text_otherwise():
    ref = TerminalSessionRef("cmux", WS, SF)
    assert adapter(StaleCmux()).read_screen(ref) is None
    assert "? for shortcuts" in adapter(FakeCmux()).read_screen(ref)


def resume_request(check):
    return CreateSessionRequest(
        title="t", working_directory="/w", command=["/bin/claude", "--resume", "abc"], prompt=None, resume_check=check
    )


def test_resume_is_confirmed_only_on_a_confirming_screen():
    check = ResumeCheck(confirmed=(r"Resumed conversation",), failed=(r"No conversation found",))
    done = adapter(FakeCmux(screens=["booting\n", "Resumed conversation abc\n"])).create_session(resume_request(check))
    assert done.resume_state == "confirmed"
    failed = adapter(FakeCmux(screens=["No conversation found with session ID: abc\n"])).create_session(resume_request(check))
    assert failed.resume_state == "failed"


def test_resume_with_nothing_recognised_is_unconfirmed_and_types_nothing():
    fake = FakeCmux(screens=[IDLE])
    result = adapter(fake).create_session(resume_request(ClaudeCodeAdapter().resume_check()))
    assert result.resume_state == "unconfirmed" and not result.prompt_submitted
    assert not fake.commands("paste") and not fake.commands("send")
    (argv,) = fake.commands("new-workspace")
    assert argv[argv.index("--command") + 1].endswith("/bin/claude --resume abc")


def test_claude_adapter_fixes_and_resumes_conversations_by_id():
    claude = ClaudeCodeAdapter()
    cid = claude.new_conversation_id()
    assert claude.conversation_args(cid) == ("--session-id", cid)
    assert claude.resume_args(cid) == ("--resume", cid)
    assert agent_adapter_for("generic").new_conversation_id() is None
    assert agent_adapter_for("generic").resume_args("x") is None
    assert not claude.has_exited(IDLE)  # an idle agent is never mistaken for an exited one


def test_reopening_a_task_through_cmux_focuses_and_creates_nothing(write_config, fake_agents, make_repo, monkeypatch):
    from test_tasks_flow import run

    write_config(
        {
            "version": 3,
            "terminal": {"adapter": "cmux"},
            "harness_selection": "use_default",
            "profiles": {"work": {"default_harness": "claude", "harnesses": {"claude": {"executable": str(fake_agents / "claude")}}}},
        }
    )
    repo = make_repo("reopen")
    assert run("profile", "set", str(repo), "work", "--offline").exit_code == 0
    fake = FakeCmux(screens=[IDLE])
    monkeypatch.setattr(cli, "select_adapter", lambda name: adapter(fake))
    task = make_github_task(repo)
    assert run("open", task["id"], "--offline", "--json").exit_code == 0
    assert len(fake.commands("new-workspace")) == 1
    again = run("open", task["id"], "--offline", "--json")
    assert json.loads(again.stdout)["action"] == "focused"
    assert len(fake.commands("new-workspace")) == 1
    assert fake.commands("workspace")[-1][1:] == ["workspace", "select", WS]
    # The workspace disappears: the stale IDs are recovered from by resuming in a new workspace.
    stale = StaleCmux(panels_rc=1)
    monkeypatch.setattr(cli, "select_adapter", lambda name: adapter(stale))
    lost = json.loads(run("open", task["id"], "--offline", "--json").stdout)
    assert lost["action"] == "resumed" and lost["resume_state"] == "unconfirmed"
    (created,) = stale.commands("new-workspace")
    assert "--resume" in created[created.index("--command") + 1]
    assert not stale.commands("paste")


class BrokenCmux(FakeCmux):
    """A cmux that fails `list-panels` (or `read-screen`) for a reason that says nothing about the workspace."""

    def __init__(self, *args, command="list-panels", **kw):
        super().__init__(*args, **kw)
        self.command = command

    def __call__(self, argv, timeout, stdin=None):
        if self.command in argv:
            self.calls.append((split_id_format(argv)[0], stdin))
            return CmuxResult(1, "", "Access denied - only processes started inside cmux can connect")
        return super().__call__(argv, timeout, stdin)


@pytest.mark.parametrize("command", ["list-panels", "read-screen"])
def test_an_unrelated_cmux_failure_is_an_error_not_a_gone_session(command):
    ref = TerminalSessionRef("cmux", WS, SF)
    with pytest.raises(TerminalError) as err:
        adapter(BrokenCmux(command=command)).read_screen(ref)
    assert err.value.code == "cmux_failed" and "not treated as gone" in err.value.message
    with pytest.raises(TerminalError):
        adapter(BrokenCmux(command="list-panels")).focus_session(ref)


def test_open_over_an_unrelated_cmux_failure_resumes_nothing(write_config, fake_agents, make_repo, monkeypatch):
    from test_tasks_flow import run

    write_config(
        {
            "version": 3,
            "terminal": {"adapter": "cmux"},
            "harness_selection": "use_default",
            "profiles": {"work": {"default_harness": "claude", "harnesses": {"claude": {"executable": str(fake_agents / "claude")}}}},
        }
    )
    repo = make_repo("hiccup")
    assert run("profile", "set", str(repo), "work", "--offline").exit_code == 0
    first = FakeCmux(screens=[IDLE])
    monkeypatch.setattr(cli, "select_adapter", lambda name: adapter(first))
    task = make_github_task(repo)
    assert run("open", task["id"], "--offline", "--json").exit_code == 0
    broken = BrokenCmux()
    monkeypatch.setattr(cli, "select_adapter", lambda name: adapter(broken))
    out = run("open", task["id"], "--offline", "--json")
    assert out.exit_code == 1 and json.loads(out.stdout)["error"]["code"] == "cmux_failed"
    assert not broken.commands("new-workspace") and not broken.commands("workspace")


def test_close_forces_only_a_recorded_owned_workspace_whose_surface_is_still_listed():
    def forced(ref, fake=None):
        fake = fake or FakeCmux()
        adapter(fake).close_session(ref)
        return "--force" in fake.commands("workspace")[0]

    assert forced(TerminalSessionRef("cmux", WS, SF, created_by_launcher=True))
    assert not forced(TerminalSessionRef("cmux", WS, SF))  # not recorded as ours (older rows)
    assert not forced(TerminalSessionRef("cmux", WS, None, created_by_launcher=True))  # nothing to identify it by
    other = StaleCmux(panels_rc=0, panels_out="* some-other-surface  [terminal]\n")  # the ref now names another workspace
    assert not forced(TerminalSessionRef("cmux", WS, SF, created_by_launcher=True), other)


def test_every_command_asks_for_uuids_and_a_live_surface_is_found_in_the_real_listing():
    fake = FakeCmux()
    a = adapter(fake)
    created = a.create_session(request(prompt=None))
    # The surface recorded at create time is a UUID, and so is what list-panels prints, so a live session
    # is present. (cmux prints `surface:N` refs by default: the flag is what makes the two comparable.)
    assert created.session.surface_id == SF
    a.focus_session(created.session)
    assert a.read_screen(created.session) is not None
    assert {fmt for _, fmt in fake.id_formats} == {"uuids"}


def test_the_two_real_listing_formats_differ_and_only_uuids_match_a_recorded_uuid():
    ref = TerminalSessionRef("cmux", WS, "B1C2D3E4-F5A6-4B7C-8D9E-0F1A2B3C4D5E")

    class Listing(FakeCmux):
        def __call__(self, argv, timeout, stdin=None):
            argv2, fmt = split_id_format(argv)
            if "list-panels" in argv2:
                return CmuxResult(0, (REAL_UUID_LINE if fmt == "uuids" else REAL_REF_LINE) + "\n", "")
            return super().__call__(argv, timeout, stdin)

    assert adapter(Listing()).read_screen(ref) is not None  # the adapter asks for uuids
    assert "B1C2D3E4-F5A6-4B7C-8D9E-0F1A2B3C4D5E" not in REAL_REF_LINE  # what the default would have printed


def test_only_the_observed_not_found_wording_means_gone():
    ref = TerminalSessionRef("cmux", WS, SF)

    class Says(FakeCmux):
        def __init__(self, text):
            super().__init__()
            self.text = text

        def __call__(self, argv, timeout, stdin=None):
            if "list-panels" in argv:
                return CmuxResult(1, "", self.text)
            return super().__call__(argv, timeout, stdin)

    assert adapter(Says("Error: Workspace ref not found: workspace:999999999")).read_screen(ref) is None
    assert adapter(Says("Error: not_found: Workspace not found")).read_screen(ref) is None
    for unrelated in ("Error: socket not found at /tmp/cmux.sock", "cmux: command not found"):
        with pytest.raises(TerminalError):
            adapter(Says(unrelated)).read_screen(ref)


def test_read_screen_of_a_non_terminal_surface_is_an_error_not_gone():
    ref = TerminalSessionRef("cmux", WS, SF)

    class NotATerminal(FakeCmux):
        def __call__(self, argv, timeout, stdin=None):
            if "read-screen" in argv:
                return CmuxResult(1, "", "Error: invalid_params: Surface is not a terminal")
            return super().__call__(argv, timeout, stdin)

    with pytest.raises(TerminalError):
        adapter(NotATerminal()).read_screen(ref)


def test_cmux_runs_with_quiet_set_so_stderr_stays_clean(monkeypatch):
    import subprocess

    from commodore_launcher import terminal_cmux

    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(terminal_cmux.subprocess, "run", fake_run)
    REAL_RUN_PROCESS(["cmux", "ping"], 5)
    assert seen["CMUX_QUIET"] == "1"


class Live0065(FakeCmux):
    """cmux 0.65.0 as observed: `new-workspace` prints a REF even with uuids; `list-panels` prints the surface UUID."""

    def __call__(self, argv, timeout, stdin=None):
        plain, _ = split_id_format(argv)
        if plain[1:2] == ["new-workspace"]:
            self.calls.append((plain, stdin))
            return CmuxResult(0, "OK workspace:1000000058\n", "")
        if plain[1:2] == ["list-panels"]:
            self.calls.append((plain, stdin))
            return CmuxResult(0, '* C7D8E9F0-A1B2-4C3D-9E4F-5A6B7C8D9E0F  terminal  [focused]  "Terminal"\n', "")
        return super().__call__(argv, timeout, stdin)


def test_create_records_the_workspace_ref_as_printed_and_the_surface_uuid():
    fake = Live0065()
    ref = adapter(fake).create_session(request(prompt=None)).session
    assert (ref.workspace_id, ref.surface_id) == ("workspace:1000000058", "C7D8E9F0-A1B2-4C3D-9E4F-5A6B7C8D9E0F")
    assert fake.commands("list-panels")[0][2:] == ["--workspace", "workspace:1000000058"]
    # With that surface UUID the session is verifiable: focus, screen and a forced close all work.
    a = adapter(fake)
    a.focus_session(ref)
    assert a.read_screen(ref) is not None
    a.close_session(ref)
    assert "--force" in fake.commands("workspace")[-1]


def test_a_session_without_a_surface_uuid_is_unverifiable():
    ref = TerminalSessionRef("cmux", "workspace:1000000058", None, True)
    fake = Live0065()
    a = adapter(fake)
    a.focus_session(ref)  # by ref is allowed
    with pytest.raises(UnsupportedCapability):
        a.read_screen(ref)  # nothing read is attributed to it, so a reopen never decides it has exited
    a.close_session(ref)
    assert "--force" not in fake.commands("workspace")[-1]


def test_adopted_sessions_are_never_force_closed():
    fake = FakeCmux()
    adopted = TerminalSessionRef("cmux", WS, SF, created_by_launcher=False)
    adapter(fake).close_session(adopted)
    (argv,) = fake.commands("workspace")
    assert "--force" not in argv
