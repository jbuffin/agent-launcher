# 17. gh-dash integration is `configure` with a prepared prompt

Status: accepted

## Decision

`agent-launcher integrate gh-dash` (SPEC §22) reuses the ADR 0016 machinery. It is a maintenance task with its own title (`Integrate Agent Launcher with gh-dash`), the built-in `agent-launcher-configure` workflow (so the management skill) and, as the task description the prompt is built from, the integration prompt in `integrate.py`. `find_maintenance_task` now also matches the title, so `configure` and `integrate` get separate tasks in the same maintenance repository; the prompt is the default request, so running it again focuses the open session instead of tripping `configure_request_ignored`.

- **No YAML editing.** Nothing in the package writes gh-dash configuration or depends on a YAML library. A test enforces it. The agent edits, guided by the prompt, and the user reviews the prompt before it is sent.
- **Detection** is `doctor`'s `check_gh_dash` (a `gh-dash` binary or a `gh` extension). `setup` offers the integration only when it passes, default No, after setup has finished; a failed launch does not fail setup. `integrate gh-dash` without gh-dash is refused (`gh_dash_not_found`); `--print-prompt` always works.
- **Terminal.** gh-dash 4.26.0 runs custom commands with `tea.ExecProcess`, so they get the real TTY and the picker works unchanged. The fallback exists anyway because it is cheap and the engineer asked for it: a new terminal capability `run_interactive` (cmux: a new workspace running only the given argv, the same `new-workspace` call as a launch; mock: recorded). `open --picker-surface` uses it when there is no terminal and the refusal is one of a fixed set that mean "a person must choose". It is opt-in per command so a plain `open` never opens surfaces on its own, and the child command omits the flag.

## Consequences

The `run_interactive` workspace is not recorded as a task session. It closes itself by ending its shell (`<command>; exit`, a `CreateSessionRequest.exit_when_done` flag), which is checked against the fake only; whether cmux then removes the surface is a live check. Recording it and closing it after the child returns was rejected: the launcher process that started it has already returned. The suggested key (`b`) is in the prompt text and docs only; the agent verifies it against the installed version.
