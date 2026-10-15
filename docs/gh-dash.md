# gh-dash integration

[gh-dash](https://www.gh-dash.dev) (`gh extension install dlvhdr/gh-dash`, or a standalone `gh-dash`) is a good place to pick an issue or PR. Commodore Launcher works without it; nothing in setup requires it.

The goal: highlight an issue or PR in gh-dash, press a key, and `commodore open <url>` starts (or focuses) its task and harness session.

## Harness-assisted setup

```bash
commodore integrate gh-dash [--repo PATH] [--profile NAME] [--harness A] [--terminal mock] [--json]
commodore integrate gh-dash --print-prompt      # just print the instructions, to use with any harness by hand
```

`commodore setup` offers the same at its end when gh-dash is detected (as a binary or a `gh` extension, the check `doctor` runs). The answer defaults to No.

It is `configure` with a fixed request ([management skill](management-skill.md), [ADR 0016](adr/0016-configure-maintenance-task.md), [ADR 0017](adr/0017-gh-dash-integration.md)): a maintenance session titled `Integrate Commodore Launcher with gh-dash`, launched through the shared path with the `commodore-launcher` skill, started on the integration request and submitted at launch (nothing is held back for you to review: read the request with `--print-prompt` first). The request tells the harness to: inspect the gh-dash version, find its configuration, inspect the current keybindings, read the keybinding documentation for that version, keep everything that is there (backing the file up first), add one shortcut for issues and one for PRs on a key nothing else uses, never overwrite a conflicting binding, validate the result, test what it can, and report what it changed and how to undo it.

**Commodore Launcher writes no gh-dash configuration.** There is no YAML editor in it, and a test checks the package for gh-dash config paths and YAML libraries. The harness you launch does the edit, and you review it.

Not installed: `integrate gh-dash` refuses with `gh_dash_not_found`; `--print-prompt` and the manual steps below still work. One maintenance session per repository and profile: running it again focuses the same session.

## Manual integration

Add to gh-dash's `config.yml` (usually `~/.config/gh-dash/config.yml`; `gh dash --help` shows the flag for another path), under `keybindings:`, keeping what is already there. Pick a key that is free in the view; `b` is unused by gh-dash 4.26.1's defaults in the issues and PRs views, but it is `toggleBookmark` in the notifications view (these shortcuts do not touch that view). Press `?` in gh-dash to see the bindings in effect.

```yaml
keybindings:
  issues:
    - key: b
      name: commodore launcher
      command: commodore open https://github.com/{{.RepoName}}/issues/{{.IssueNumber}}
  prs:
    - key: b
      name: commodore launcher
      command: commodore open https://github.com/{{.RepoName}}/pull/{{.PrNumber}}
```

If you already have `issues:` or `prs:` lists, add the entry to them rather than a second list. `commodore` must be on the `PATH` gh-dash runs with. Run gh-dash inside a cmux terminal: cmux only accepts commands from terminals it started.

`{{.RepoName}}` is `owner/name`. The URL is built from these fields because gh-dash passes no URL field to a command and fails a template that names a missing one. The first time, `open` asks to clone or locate the repository and to choose a profile or harness, as it does anywhere (see [GitHub tasks](github-integration.md)).

## Does a gh-dash command get a terminal?

Checked against gh-dash v4.26.1's source (`b1376625b2e4d50f2b2d761bf2fa9ec543e79380`) and [keybinding docs](https://www.gh-dash.dev/configuration/keybindings/) (not by driving the TUI). A custom command is run as `$SHELL -c <command>` (`sh -c` if unset) through Bubble Tea's `tea.ExecProcess`, which suspends the TUI and gives the command the real terminal as stdin, stdout and stderr. gh-dash's own docs rely on it (`gum input` in a `prs` command). So `commodore open` can ask its questions (profile, harness, clone confirmation) right there, and returns to gh-dash when it ends. **Live check for you:** press the key on an item whose repository has no profile yet and confirm the picker appears and gh-dash comes back afterwards.

### Fallback: a temporary cmux surface

If your gh-dash (or another launcher) does not give a terminal, `commodore open` cannot ask and refuses with a structured error (`unknown_repository_profile`, `harness_selection_needed`, `ambiguous_repository_location`, `confirmation_required`). Add `--picker-surface`:

```yaml
      command: commodore open --picker-surface https://github.com/{{.RepoName}}/pull/{{.PrNumber}}
```

With it, when there is no terminal and one of those refusals happens, `open` asks the terminal adapter's `run_interactive` capability to start the same command (without `--picker-surface`, so it cannot loop) in a new cmux workspace named `Commodore Launcher: choose`. The picker runs there and the task opens as usual. The command is typed as `<command>; exit`, so the workspace's shell ends when the picker (and the `open` it runs) finishes and cmux should close the surface; that last part is a live check for you (if it stays open, close it). An adapter without `run_interactive` leaves the original refusal. `--json` never uses it. This path is tested against a fake cmux and the mock terminal only; the `new-workspace` call it makes is the one every launch uses, but the whole flow has not been run live.
