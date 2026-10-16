# The session host

`open`, `resume` and `configure` run the harness in the terminal you typed them in. The launcher works in any terminal, and it knows nothing about terminals: it starts a process and waits for it ([ADR 0022](adr/0022-harness-runs-in-the-invoking-terminal.md)).

## Interface (`commodore_launcher/session_host.py`)

The core talks to a `SessionHost`:

- `running(task_id) -> SessionHolder | None`: who runs the task's harness (`pid`), or None when nothing does.
- `claim(task_id) -> SessionClaim | None`: take the task's session lock for a harness about to start, or None when its harness is running.
- `run(claim, HarnessCommand) -> int`: run the harness until it exits and return its exit status. A command is the argv (run directly, never through a shell), its working directory and its environment, plus, for display only, the names of the variables the profile sets and the prompt the argv carries.

`launch.open_task` decides what to run and claims the session while it holds the task's launch lock, records the session, and returns the command and the claim. The CLI then closes the database and calls `run`.

Environment values may hold secrets. A host must not log or record them.

## The session lock

Each task has a lock file, `<home>/locks/<task>.session.lock`, taken with `flock`. `open` takes it before it starts the harness, writes the harness's process ID into it, and hands the open file to the harness. The operating system releases a `flock` when the last process holding it exits, so:

- the lock is held for exactly as long as the harness runs, even if `commodore` is killed first;
- a task's harness is running exactly when its lock is held, and any process can ask (`locks.session_holder`): `open`, `tasks show` and the profile reassignment listing all do;
- nothing needs a PID check, an expiry or a daemon.

A process the harness starts inherits the lock unless the harness closes it, so a background process that outlives the harness makes the task look running. `resume <task> --force` (which asks first, or takes `--yes`) starts the harness anyway, without the lock.

## `ForegroundHost`

The only host. It runs the harness as a child of `commodore`:

- The child inherits the terminal (stdin, stdout, stderr) and the session lock.
- The environment is passed to the process directly: no shell runs first, so start-up files cannot change it and nothing lands in a shell's scrollback or history.
- While it waits, `commodore` ignores Ctrl-C and Ctrl-\, which the terminal sends to both processes: they are the harness's keys.
- A harness that cannot be started (a missing interpreter, a permission error) is `harness_start_failed`; the lock is released.
- `commodore` exits with the harness's status (128 plus the signal number when a signal ended it).

## Dry runs

`open --dry-run`, `resume --dry-run` and `configure --dry-run` run every check of a launch, print the command, the directory and the names of the profile's variables, and start nothing. They take no lock and record no session or launch; `configure --dry-run` records nothing either. A dry run of a URL never opened still finds (or clones, after asking) the repository and registers the task, as `open` does before it launches. With `--json` the plan is one JSON object: `action`, `task`, `session`, `command`, `working_directory`, `profile_env`, `prompt`, `notice`.

## Another host

A host that runs harnesses headless, so that a dashboard or a second terminal can list them and attach, would be a second implementation of `SessionHost`. tmux is the first candidate: it already keeps detached sessions and lets any terminal attach. Such a host would keep the same session lock, with the harness holding it, so liveness is the same for every host.

## Tests

Tests run on `conftest.RecordingHost`, which records each run (environment variable names only), releases the lock as an exited harness would, and saves the conversation where the harness would so the next open resumes it. The tests of the real host (`test_launch_transactions.py`) run `commodore open` processes with a fake harness script. `tests/test_live_host.py` is the opt-in check with real Claude Code in a pseudo-terminal; see [development.md](development.md).
