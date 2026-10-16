# 22. The harness runs in the terminal `commodore` was run in

Status: accepted (supersedes ADR 0017, the terminal-session parts of ADRs 0005 and 0007, and the cmux and focus parts of ADRs 0016, 0020 and 0021)

## Context

The launcher ran each harness in a new cmux workspace and recorded the workspace and surface. It used cmux to focus the workspace when the same URL was opened again, to read the screen to tell whether the harness had exited or had failed to resume, and to close a workspace a failed launch had left behind. That tied the launcher to one macOS terminal, which accepts commands only from terminals it started. The engineer already opens a fresh terminal (or cmux workspace) for each task before running `commodore open`, so the extra workspace bought nothing. Screen reading was also fragile: it depended on unobserved wording in each harness's output.

## Decision

- `open`, `resume` and `configure` run the harness as a child process of `commodore` in the current terminal. The harness inherits the terminal, and the prompt rides on its command line as before (ADR 0020). `commodore` waits for it and exits with its status. No terminal is recorded.
- **Liveness is a lock.** Each task has a session lock (`<home>/locks/<task>.session.lock`, `flock`). `open` takes it before it starts the harness, and the harness inherits it, so it stays held for as long as the harness runs, even if `commodore` dies first. Opening a task whose lock is held is refused with `harness_running`, because the launcher cannot reach another terminal to focus it. Otherwise the conversation is resumed in place. `resume --force` still starts a second process on purpose.
- **A failed resume is caught before it happens.** The harness adapter checks whether the harness saved the conversation (Claude Code's transcript file, Copilot CLI's session-state directory). If it did not, a fresh conversation starts on the same session (ADR 0005).
- **The seam is a session host** (`session_host.SessionHost`): is the harness running, claim the session, run a harness. `ForegroundHost` is its only implementation.
- `--json` on `open`, `resume` and `configure` needs `--dry-run`: the harness owns the terminal, so there is nothing to print until it exits. `--dry-run` shows the command, directory and environment names and starts nothing.
- The gh-dash integration (`integrate gh-dash`, `open --picker-surface`, the `doctor` check and the `setup` offer) is removed. `--terminal` and the config's `terminal` block are removed (config version 4), and state migration 12 drops `terminal_sessions`.

## Considered options

- **`exec` the harness in place of `commodore`.** It is simpler, but nothing would remain to hold the lock knowingly, to start a fresh conversation after a failed start, or to grow into a host that others can attach to.
- **Keep a terminal adapter and add others (tmux, WezTerm, iTerm).** Every adapter would bring its own focus, screen-reading and close semantics to maintain. The engineer gains nothing from it while each task already gets its own terminal.
- **tmux as the default host.** tmux is a real headless session host: detached sessions, attach from any terminal, `list-sessions`. It is the first candidate for a second `SessionHost` when a dashboard that lists and attaches to sessions is built. It is not the default because it adds a dependency and behaves differently when the engineer is already inside tmux.

## Consequences

Opening a task whose harness is running in another terminal says so (with its pid) instead of switching to it. A process the harness started that outlives it and keeps the inherited lock makes the task look running; `resume --force` is the way past that. The environment is passed straight to the process, so it no longer appears in a shell's scrollback or history. Sessions recorded before the upgrade keep their conversation IDs; the next `open` says once that the cmux workspaces were forgotten.
