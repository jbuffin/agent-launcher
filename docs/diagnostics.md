# Diagnostics, logging and debug mode

## `agent-launcher doctor`

```bash
agent-launcher doctor [--json]
```

Runs read-only checks and prints each as `ok`, `warn` or `FAIL` with a remediation hint. Exit code 1 if any check fails; warnings do not change the exit code. `--json` prints `{"ok", "summary": {"passed","warnings","failures"}, "checks": [{"id","name","status","detail","remediation"}]}`.

| Check | Fails when | Warns when |
| --- | --- | --- |
| Python | older than 3.11 | |
| Configuration | `config.json` is unreadable or invalid (including any bad profile) | unknown top-level fields |
| git | not on PATH | |
| GitHub CLI (`gh`), cmux | | not on PATH |
| `gh auth status` | | not authenticated, or the probe timed out |
| gh-dash | | not found as a `gh-dash` binary or a `gh` extension (`gh dash`) |
| Agent types (claude, codex, copilot, and any you add) | | executable not on PATH |
| Profiles | a profile's agent instance does not resolve (same rules as `profile check`) | no profiles; a profile with no agents or no default; not checked because the config is invalid |
| Database (`state.db`) | exists but is unreadable or fails `PRAGMA quick_check` (opened read-only) | |

The profile checks call the same `resolve_agent` that launching uses, so they report exactly what a launch would hit. An agent type missing from PATH is only a warning because a profile may point at a wrapper elsewhere; the profile check is what fails.

External probes are `<tool> --version`, `gh auth status` and `gh extension list`, run as argv lists with a 5 second timeout and no stdin. They read state and never write it. Skill integration and repository mapping checks will be added with the features they check.

## Debug mode

`agent-launcher --debug <command>` (the flag goes before the command; `agent-launcher doctor --debug` is a usage error) (or `"debug": true` in `config.json`) prints decision traces to stderr and records them in the log, for example:

```text
debug: agent resolved profile="work" agent="claude" executable="/usr/local/bin/claude" ...
```

Traces so far cover command start, `doctor`, and agent resolution; later tickets add repository, workflow, worktree, session and terminal decisions through `agent_launcher.logs.trace()`.

## Logs

JSON lines in `~/.agent-launcher/logs/agent-launcher.log` (under `AGENT_LAUNCHER_HOME` if set). The directory is created (mode 0700) only when the first record is written. Without `--debug`, only INFO and above is written; traces are DEBUG.

Rotation is by size and configurable:

```json
{ "logs": { "max_bytes": 1000000, "backup_count": 5 } }
```

The active file rolls over at `max_bytes` (at least 1024); `backup_count` rolled files (`agent-launcher.log.1` ...) are kept and older ones deleted. `backup_count: 0` keeps only the active file.

## Redaction and privacy

Every log record, `doctor` detail and exported file is redacted (`agent_launcher/redact.py`):

- environment variables and JSON keys whose names look secret (`token`, `secret`, `password`, `api_key`, `auth`, `credential`, `private_key`, `cookie`, ...) lose their value
- `--token X`, `--token=X`, `KEY=secret` arguments
- well-known token shapes anywhere in text (GitHub, `sk-`, Slack, AWS, JWT, `Bearer ...`)
- credentials in URLs (`https://user:pass@host`)
- home directories shown as `~`; credential locations (`~/.ssh`, `~/.aws`, `~/.gnupg`, `.netrc`, `.env` files, ...) shown as `[REDACTED-PATH]`
- instance environment variables in the exported `config.json`: only known-safe names (`PATH`, `HOME`, `LANG`, `TERM`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, ...) keep their value; every other value is hidden
- the GitHub account name from `gh auth status` (visible in interactive `doctor`, hidden in exports)
- `Authorization` and `Proxy-Authorization` header values, to the end of the line
- prompts: arguments after `--prompt`/`-p`, and any argument that is multi-line or over 120 characters, are replaced by `[PROMPT OMITTED: N chars]`

Redaction errs on the side of hiding too much. Complete prompts and conversations are never logged by default; code that logs command lines must pass them through `redact_args`. Redaction is best effort, so read an export before sharing it.

## `agent-launcher diagnostics export`

```bash
agent-launcher diagnostics export [--output FILE.tar.gz]
```

Writes (mode 0600; refuses to overwrite) a `.tar.gz` with `system.json` (versions, platform), `doctor.json`, `config.json` (redacted) and `logs/` (redacted). The database, prompts and conversations are never included. Default file: `agent-launcher-diagnostics-<timestamp>.tar.gz` in the current directory.
