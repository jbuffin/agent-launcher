# Agent Launcher

A Python CLI that launches AI coding agents against GitHub issues, PRs and local tasks. The full design is in [SPEC.md](SPEC.md). This repo is at the foundation stage: only the commands below work so far.

## Install

Requires Python 3.11+.

```bash
pipx install .        # regular install
pipx install -e .     # editable, for development
agent-launcher version
```

## Commands

| Command | What it does |
| --- | --- |
| `agent-launcher version [--json]` | Print the installed version. |
| `agent-launcher config show [--json]` | Show the effective config: defaults overlaid with `config.json`, unknown fields included. |
| `agent-launcher config validate [--json] [--strict]` | Check `config.json`. Errors name the field and exit 1. Unknown fields are reported as warnings; `--strict` makes them fail. |
| `agent-launcher profile list\|add\|edit\|check` | Manage profiles and their agent instances. See [docs/profiles.md](docs/profiles.md) and [docs/agents.md](docs/agents.md). |
| `agent-launcher doctor [--json]` | Health checks with pass/warn/fail and remediation hints. Exit 1 if any check fails. |
| `agent-launcher diagnostics export [-o FILE]` | Write a sanitised `.tar.gz` for bug reports. |
| `agent-launcher --debug <command>` | Trace decisions to stderr and the log. Works with every command. |

See [docs/diagnostics.md](docs/diagnostics.md) for checks, logs and redaction.

## Configuration

Config lives in `~/.agent-launcher/config.json` (set `AGENT_LAUNCHER_HOME` to use a different root). It is plain JSON you can edit by hand. A missing file is fine: defaults apply and nothing is created.

```json
{
  "version": 2,
  "debug": false
}
```

- `version`: schema version (currently 2; version 1 files are still accepted). Required; a version newer than the installed release supports is rejected.
- `debug`: boolean, default `false`. `true` behaves like always passing `--debug`.
- `logs`: `{"max_bytes": 1000000, "backup_count": 5}`, log rotation. See [docs/diagnostics.md](docs/diagnostics.md).
- `agent_types`, `profiles`: see [docs/agents.md](docs/agents.md) and [docs/profiles.md](docs/profiles.md).

Fields the installed version doesn't recognise are never dropped: `validate` reports them and programmatic updates keep them. Do not put tokens or credentials in config.

## Development

See [docs/development.md](docs/development.md).
