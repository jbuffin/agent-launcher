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

## Configuration

Config lives in `~/.agent-launcher/config.json` (set `AGENT_LAUNCHER_HOME` to use a different root). It is plain JSON you can edit by hand. A missing file is fine: defaults apply and nothing is created.

```json
{
  "version": 1,
  "debug": false
}
```

- `version`: schema version. Required; a version newer than the installed release supports is rejected.
- `debug`: boolean, default `false`.

Fields the installed version doesn't recognise are never dropped: `validate` reports them and programmatic updates keep them. Do not put tokens or credentials in config.

## Development

See [docs/development.md](docs/development.md).
