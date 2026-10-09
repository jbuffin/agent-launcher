import pytest

from agent_launcher.agent_adapters import ClaudeCodeAdapter, agent_adapter_for
from agent_launcher.terminal_cmux import CmuxAdapter, CmuxResult
from agent_launcher.terminal_select import select_adapter
from agent_launcher.terminals import (
    CLOSE_SESSION,
    CREATE_SESSION,
    FOCUS_SESSION,
    PREPARE_PROMPT,
    SUBMIT_PROMPT,
    CreateSessionRequest,
    TerminalError,
    TerminalSessionRef,
    UnsupportedCapability,
)

WS = "11111111-2222-3333-4444-555555555555"
SF = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
IDLE = "╭────╮\n│ >  │\n╰────╯\n  ? for shortcuts\n"


class FakeCmux:
    """A scripted cmux. `screens` are returned by successive read-screen calls (the last repeats)."""

    def __init__(self, screens=(IDLE,), paste_rc=0, ping_rc=0, after_paste=None):
        self.calls: list[tuple[list[str], str | None]] = []
        self.screens = list(screens)
        self.paste_rc = paste_rc
        self.ping_rc = ping_rc
        self.after_paste = after_paste
        self.pasted = False
        self.env_file_text: str | None = None

    def __call__(self, argv, timeout, stdin=None):
        self.calls.append((list(argv), stdin))
        cmd = argv[1] if len(argv) > 1 else argv[0]
        if argv[0] == "pbcopy":
            self.clipboard = stdin
            return CmuxResult(0, "", "")
        if cmd == "ping":
            return CmuxResult(self.ping_rc, "PONG" if not self.ping_rc else "", "Access denied" if self.ping_rc else "")
        if cmd == "new-workspace":
            if "--env-file" in argv:
                with open(argv[argv.index("--env-file") + 1]) as f:
                    self.env_file_text = f.read()
            return CmuxResult(0, f"OK {WS}\n", "")
        if cmd == "list-panels":
            return CmuxResult(0, f"* {SF}  [terminal]  \"zsh\"\n", "")
        if cmd == "read-screen":
            if self.pasted and self.after_paste is not None:
                return CmuxResult(0, self.after_paste, "")
            screen = self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
            return CmuxResult(0, screen, "")
        if cmd == "paste":
            if self.paste_rc == 0:
                self.pasted = True
            return CmuxResult(self.paste_rc, "", "refused" if self.paste_rc else "")
        return CmuxResult(0, "OK\n", "")

    def commands(self, name):
        return [a for a, _ in self.calls if len(a) > 1 and a[1] == name]


def adapter(fake, **kw):
    clock = iter(range(0, 10_000))
    return CmuxAdapter(fake, cli="/bin/cmux", sleep=lambda s: None, clock=lambda: float(next(clock)), base_env={"PATH": "/usr/bin"}, **kw)


def request(prompt="Fix the login bug", **kw):
    kw.setdefault("prompt_input", ClaudeCodeAdapter().prompt_input())
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
    assert caps == {CREATE_SESSION, FOCUS_SESSION, CLOSE_SESSION, PREPARE_PROMPT}
    with pytest.raises(UnsupportedCapability):
        adapter(FakeCmux()).require(SUBMIT_PROMPT)


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


def test_a_cmux_error_while_preparing_becomes_a_notice_not_an_orphan():
    class Flaky(FakeCmux):
        def __call__(self, argv, timeout, stdin=None):
            if len(argv) > 1 and argv[1] == "read-screen":
                raise TerminalError("terminal_timeout", "cmux timed out after 20s.")
            return super().__call__(argv, timeout, stdin)

    result = adapter(Flaky()).create_session(request())
    assert result.session.workspace_id == WS and not result.prompt_prepared
    assert "timed out" in result.notice and "clipboard" in result.notice


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


def test_prompt_is_pasted_once_never_submitted_and_multiline_is_one_paste():
    fake = FakeCmux(screens=[IDLE], after_paste="│ > line one\n│   line two\n  ? for shortcuts\n")
    prompt = "line one\n\nline two"
    result = adapter(fake).create_session(request(prompt))
    assert result.prompt_prepared and not result.prompt_submitted and result.notice is None
    (paste,) = fake.commands("paste")
    assert paste[-1] == "-" and "--submit" not in paste and "--force" not in paste
    assert [stdin for a, stdin in fake.calls if a[1:2] == ["paste"]] == [prompt]
    assert not fake.commands("send") and not fake.commands("send-key")


def test_waits_for_the_input_box_before_pasting():
    fake = FakeCmux(screens=["", "Welcome to Claude Code\n", IDLE, IDLE], after_paste="Fix the login bug")
    assert adapter(fake).create_session(request()).prompt_prepared
    order = [a[1] for a, _ in fake.calls]
    assert order.index("paste") > max(i for i, c in enumerate(order) if c == "read-screen" and i < order.index("paste")) >= 3


def test_trust_dialog_blocks_paste_and_falls_back_to_clipboard():
    fake = FakeCmux(screens=["Do you trust the files in this folder?\n"])
    result = adapter(fake).create_session(request())
    assert not result.prompt_prepared and not fake.commands("paste")
    assert "dialog" in result.notice and "clipboard" in result.notice
    assert fake.clipboard == "Fix the login bug"
    assert result.session.workspace_id == WS


def test_timeout_never_pastes():
    fake = FakeCmux(screens=["booting...\n"])
    result = adapter(fake).create_session(request())
    assert not result.prompt_prepared and not fake.commands("paste")
    assert "input box" in result.notice


def test_unknown_agent_has_no_prompt_input_so_nothing_is_typed():
    fake = FakeCmux()
    result = adapter(fake).create_session(request(prompt_input=None))
    assert not result.prompt_prepared and not fake.commands("paste") and not fake.commands("read-screen")
    assert fake.clipboard == "Fix the login bug"
    assert agent_adapter_for("codex-cli").prompt_input() is None


def test_cmux_refusing_the_paste_is_reported_not_forced():
    fake = FakeCmux(paste_rc=1)
    result = adapter(fake).create_session(request())
    assert not result.prompt_prepared and "refused" in result.notice
    assert len(fake.commands("paste")) == 1 and "--force" not in fake.commands("paste")[0]


def test_agent_working_after_paste_is_reported_as_possibly_submitted():
    fake = FakeCmux(after_paste="Fix the login bug\n  esc to interrupt\n")
    result = adapter(fake).create_session(request())
    assert not result.prompt_prepared and not result.prompt_submitted and "submitted" in result.notice


def test_prompt_not_visible_afterwards_is_not_claimed_prepared():
    result = adapter(FakeCmux(after_paste=IDLE)).create_session(request())
    assert not result.prompt_prepared and "could not be seen" in result.notice


def test_long_paste_collapsed_by_the_tui_counts():
    fake = FakeCmux(after_paste="│ > [Pasted text #1 +40 lines]\n  ? for shortcuts\n")
    assert adapter(fake).create_session(request("x\n" * 500)).prompt_prepared


def test_submit_is_refused_up_front():
    with pytest.raises(UnsupportedCapability):
        adapter(FakeCmux()).create_session(request(submit_prompt=True))


def test_no_workspace_id_in_output_is_an_error():
    class Odd(FakeCmux):
        def __call__(self, argv, timeout, stdin=None):
            if argv[1] == "new-workspace":
                return CmuxResult(0, "OK\n", "")
            return super().__call__(argv, timeout, stdin)

    with pytest.raises(TerminalError) as err:
        adapter(Odd()).create_session(request())
    assert err.value.code == "cmux_unexpected_output"


def test_cmux_failure_on_create_is_an_error():
    class Broken(FakeCmux):
        def __call__(self, argv, timeout, stdin=None):
            if argv[1] == "new-workspace":
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
            if argv[1] == "new-workspace":
                return CmuxResult(0, "OK workspace:1000000054\n", "")
            if argv[1] == "list-panels":
                return CmuxResult(0, "* D2E3F4A5-1B2C-4D3E-8F90-A1B2C3D4E5F6  [terminal]\n", "")
            return super().__call__(argv, timeout, stdin)

    ref = adapter(Real()).create_session(request(prompt=None)).session
    assert ref.workspace_id == "workspace:1000000054" and ref.surface_id == "D2E3F4A5-1B2C-4D3E-8F90-A1B2C3D4E5F6"


def test_several_panels_record_no_surface():
    class Many(FakeCmux):
        def __call__(self, argv, timeout, stdin=None):
            if argv[1] == "list-panels":
                return CmuxResult(0, f"{SF}\nsurface:2\n", "")
            return super().__call__(argv, timeout, stdin)

    assert adapter(Many()).create_session(request(prompt=None)).session.surface_id is None


def test_selectable_by_name():
    assert isinstance(select_adapter("cmux"), CmuxAdapter)
