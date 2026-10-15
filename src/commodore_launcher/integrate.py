"""`commodore integrate gh-dash` (SPEC §22, ADR 0017): `configure` with an integration request.

Commodore Launcher writes no gh-dash configuration. The harness does, guided by the request below, with the management
skill, started and submitted at launch. Nothing in this module reads or writes a gh-dash path.
"""

from commodore_launcher import doctor
from commodore_launcher.doctor import Runner, Which

GH_DASH_TASK_TITLE = "Integrate Commodore Launcher with gh-dash"

# What `gh dash` hands a custom command, checked against gh-dash v4.26.1 (b1376625b2e4d50f2b2d761bf2fa9ec543e79380)
# and https://www.gh-dash.dev/configuration/keybindings/: `{{.RepoName}}` is `owner/name`; `{{.PrNumber}}` and
# `{{.IssueNumber}}` are numbers. There is no URL field (and templates fail on a missing key), so the URL is built
# from these. The command runs through `$SHELL -c` with the
# terminal's real stdin and stdout (`tea.ExecProcess`), so the interactive picker works as it is.
ISSUE_COMMAND = "commodore open https://github.com/{{.RepoName}}/issues/{{.IssueNumber}}"
PR_COMMAND = "commodore open https://github.com/{{.RepoName}}/pull/{{.PrNumber}}"

INTEGRATION_PROMPT = f"""Set up the gh-dash integration for Commodore Launcher. Use the commodore-launcher skill's rules. Do not write a script or tool that edits YAML: make the edit yourself, with care.

Goal: from gh-dash, highlight an issue or a PR, press a shortcut, and Commodore Launcher opens that issue or PR as a task and harness session in cmux.

Do these in order, and stop and ask me if anything is unclear:
1. Inspect the installed gh-dash version (`gh extension list`, or `gh-dash --version` for a standalone binary).
2. Locate its existing configuration (usually ~/.config/gh-dash/config.yml; check $GH_DASH_CONFIG and the version's documentation).
3. Inspect its current keybindings: universal, issues and prs, including the defaults for that version.
4. Read the current gh-dash keybinding documentation for that version (https://www.gh-dash.dev/configuration/keybindings/) so the YAML matches the installed version.
5. Preserve all existing configuration. Make a dated backup copy of the file first, and change only the `keybindings` entries below.
6. Add a shortcut for issues and one for PRs that run Commodore Launcher with the selected item. Choose a key that no existing universal, issues or prs binding and no gh-dash default uses (suggest `b`: in gh-dash 4.26.1's defaults it is free in the issues and PRs views but is `toggleBookmark` in the notifications view, which these shortcuts do not touch; if it is taken, pick another and tell me). Never overwrite or remove a conflicting binding.
   - issues: command `{ISSUE_COMMAND}`
   - prs: command `{PR_COMMAND}`
   gh-dash runs the command in the terminal with a real TTY, so Commodore Launcher's picker can ask questions (harness, profile) there.
7. Validate the result: the file is still valid YAML, every earlier key and binding is still present, and no key is bound twice in one view.
8. Test where possible without changing anything else: run `commodore doctor`, run `commodore open --help`, and check the command renders with a real repository and number. Do not open real issues or PRs without my say-so. If gh-dash itself cannot be driven from here, tell me the one thing to try by hand.
9. Report exactly what you changed (file, lines, key chosen), where the backup is, and how to undo it.

Rules: do not touch any other tool's configuration; do not change `state.db`, profiles or associations."""


def integration_prompt() -> str:
    return INTEGRATION_PROMPT


def gh_dash_detected(which: Which | None = None, runner: Runner | None = None) -> bool:
    """Is gh-dash installed, as a standalone binary or a `gh` extension? The same probe `doctor` uses."""
    return doctor.check_gh_dash(which or doctor._default_which, runner or doctor.run_command).status == "pass"
