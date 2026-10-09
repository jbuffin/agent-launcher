# Agent Launcher — Master Implementation Prompt

## 1. Role and Objective

You are a senior software architect and principal engineer responsible for designing and implementing **Agent Launcher**, a portable, extensible, self-hosted command-line application for launching AI coding agents against development tasks.

Your responsibility is to deliver a functional, maintainable, well-tested V1 application—not merely a design, prototype, or scaffold.

You have latitude to make reasonable implementation decisions within the constraints below. Prefer established libraries, simple abstractions, and conventional software engineering practices over unnecessary frameworks.

Before implementing external integrations, inspect the current installed tools and verify their supported interfaces against their official documentation.

Do not assume that example commands, CLI flags, or API contracts in this specification are necessarily supported by the installed versions.

### Primary objective

Enable a developer to select a GitHub issue, pull request, or local task and automatically:

1. Resolve its repository and associated development profile.
2. Select an available AI coding agent.
3. Determine the appropriate workflow and skill.
4. Find or establish an appropriate Git worktree.
5. Create or resume a persistent task association.
6. Open the agent in a supported terminal environment.
7. Prepare an editable prompt containing the selected skill and task reference.
8. Leave the user in control of when the prompt executes.

The primary interaction should require very little effort.

For example:

```text
gh-dash
   |
   v
Select PR #291
   |
   v
Press configured shortcut
   |
   v
Agent Launcher
   |
   +-- Resolve repository profile
   |
   +-- Select agent
   |
   +-- Resolve workflow
   |
   +-- Locate or create worktree
   |
   +-- Create cmux workspace
   |
   +-- Launch selected agent
   |
   v
Prepared, editable prompt
```

For tasks with an existing session, the launcher should simply return the user to that session.

### Guiding principles

The application must be:

- Portable.
- Configurable.
- Profile-aware.
- Agent-agnostic.
- Terminal-agnostic.
- GitHub-first but not GitHub-dependent.
- Safe by default.
- Easy to install.
- Easy to extend.
- Reliable under concurrent invocation.
- Maintainable by one developer.
- Suitable for eventual distribution through a private GitHub repository and private Homebrew tap.

Do not build a large orchestration framework.

Do not introduce a background service unless a documented technical limitation makes one unavoidable. The V1 design should not require one.

Do not introduce Docker or container execution.

Do not couple the core architecture to cmux.

---

# 2. Technology Stack

## Language

Use **Python 3.11 or newer**.

Prefer standard-library functionality wherever reasonable.

Relevant standard-library modules include:

```python
argparse
dataclasses
json
pathlib
subprocess
sqlite3
logging
shutil
tempfile
threading
typing
uuid
```

You may select limited external dependencies when they materially improve implementation quality.

Examples include:

- Typer for CLI development.
- Rich or prompt_toolkit for interactive terminal selection.
- Pydantic or another well-supported validator for configuration.
- pytest for testing.

These are suggestions, not mandated dependencies.

Evaluate them before making selections.

Avoid introducing a heavyweight application framework.

## Packaging

Use a conventional Python package:

```text
pyproject.toml
src/agent_launcher/
tests/
```

Expose a console entry point:

```bash
agent-launcher
```

The project must support installation using pipx.

It should also be structured for eventual Homebrew distribution.

Do not implement a custom software updater.

Installation and upgrades will be managed by pipx, Homebrew, or another external package manager.

---

# 3. Architecture

Use clear separation between the following components:

```text
                         Task Sources
                              |
                 +------------+------------+
                 |                         |
               GitHub                    Local
                 |                         |
                 +------------+------------+
                              |
                       Task Resolver
                              |
                       Profile Resolver
                              |
                       Workflow Router
                              |
                        Agent Picker
                              |
                       Prompt Builder
                              |
                        Task Manager
                              |
                       Worktree Manager
                              |
                       Session Manager
                              |
                       Terminal Adapter
                              |
                            cmux
                              |
                       Selected Agent
```

The core application must not depend on one specific terminal application or AI agent.

Implement interfaces for:

- Task providers.
- Agent adapters.
- Terminal adapters.
- Profile resolution.
- Workflow resolution.
- Repository resolution.
- Session management.

Keep interfaces small and purpose-driven.

Avoid unnecessary abstractions that exist solely to accommodate speculative future requirements.

## Identity separation

The system must distinguish between:

**Task identity**

The durable identity of the work being performed.

**Agent conversation identity**

The conversation or session maintained by Claude, Codex, Copilot, or another agent.

**Terminal session identity**

The workspace, pane, or terminal instance displaying the agent.

These are separate concepts.

A task may retain its identity even when its terminal session no longer exists.

The internal task ID must remain stable when a local task becomes associated with a GitHub issue.

---

# 4. Configuration

All user configuration must live under:

```text
~/.agent-launcher/
```

Use human-editable JSON.

Proposed structure:

```text
~/.agent-launcher/
├── config.json
├── workflows.json
├── repositories.json
├── templates/
├── state.db
├── logs/
└── cache/
```

This is a recommended structure. Minor adjustments are permitted when justified.

Separate configuration from runtime state.

Do not store authentication tokens or credentials in configuration files.

## Configuration requirements

Implement:

- Schema validation.
- Helpful validation errors.
- Atomic configuration writes.
- Versioned configuration schemas.
- Safe migration procedures.
- Backup and rollback support.
- Unknown-field detection where appropriate.
- Clear precedence rules for defaults and overrides.

Provide commands equivalent to:

```bash
agent-launcher config show
agent-launcher config edit
agent-launcher config validate
agent-launcher config migrate --dry-run
agent-launcher config migrate
```

Configuration editing must preserve unrelated settings.

Do not silently discard unknown configuration fields during migration.

---

# 5. Profiles and Agent Instances

Profiles represent distinct development identities.

Examples:

```text
work
personal
opensource
```

Profiles are user-defined.

Never hardcode `work` and `personal` as the only available profiles.

Each profile defines:

- Available agent instances.
- Default agent.
- Agent-specific executable or wrapper configuration.
- Environment variables.
- Optional GitHub authentication context.
- Profile-specific behavior where necessary.

## Global agent types

Define supported agent types globally.

Example:

```json
{
  "agent_types": {
    "claude": {
      "adapter": "claude-code",
      "executable": "claude"
    },
    "codex": {
      "adapter": "codex-cli",
      "executable": "codex"
    },
    "copilot": {
      "adapter": "github-copilot-cli",
      "executable": "copilot"
    }
  }
}
```

## Profile-specific instances

Profiles configure instances of those agent types.

Example:

```json
{
  "profiles": {
    "work": {
      "default_agent": "claude",
      "agents": {
        "claude": {
          "environment": {
            "CLAUDE_CONFIG_DIR": "~/.claude-work"
          }
        },
        "codex": {
          "command": "codex-work"
        },
        "copilot": {
          "command": "copilot"
        }
      }
    },
    "personal": {
      "default_agent": "codex",
      "agents": {
        "claude": {
          "environment": {
            "CLAUDE_CONFIG_DIR": "~/.claude-personal"
          }
        },
        "codex": {
          "command": "codex-personal"
        }
      }
    }
  }
}
```

The exact JSON schema may differ, but the architecture must preserve the separation between agent types and configured instances.

Agent instances must support configurable:

- Executable.
- Command arguments.
- Environment variables.
- Working directory.
- Session-resumption mechanism.
- Prompt-submission behavior.
- Skill invocation behavior.

Support shell wrappers and executable scripts.

Do not assume that every agent can be launched using the same arguments.

Do not assume that every wrapper is safe to invoke through a shell. Prefer structured argument arrays and explicit executable resolution. Where a shell function or alias cannot be invoked directly, provide a documented supported wrapper mechanism.

---

# 6. Strict Profile Safety

This section contains mandatory security invariants.

**The launcher must never accidentally execute a task under the wrong profile.**

## Repository associations

Every repository must have an explicit, persistent profile association.

When an unknown repository is encountered:

1. Identify the repository.
2. Display available profiles.
3. Require the user to select a profile.
4. Save the association.
5. Reuse the association indefinitely.

Do not assign unknown repositories to a default profile.

Do not infer an authoritative profile association solely from repository ownership or organization names.

Rules may suggest a profile, but they must not silently assign one.

## Persistent identity

Once a repository is associated with a profile, that association remains authoritative until the user explicitly changes it.

The launcher must never silently reassign a repository because:

- Its organization changed.
- Its name changed.
- A default changed.
- A configuration rule changed.
- An agent became unavailable.
- The current terminal environment changed.

Store immutable GitHub repository IDs when available.

Use those identifiers to handle renames and transfers safely.

## No cross-profile fallback

If a work agent is unavailable, do not substitute a personal agent.

If a configured executable fails, do not launch a different executable associated with another identity.

If the selected agent instance cannot be verified or resolved, stop and report the problem.

## No cross-profile session reuse

Existing sessions remain associated with their original profiles.

Changing a repository's profile must explicitly address any existing sessions and worktrees.

Do not silently reinterpret historical session records under a new profile.

## Important security boundary

The launcher is responsible for strict identity selection and configuration separation.

It does not provide operating-system-level isolation between processes running under the same user account.

Document this limitation accurately.

Do not claim that profile selection prevents agents from accessing files outside their repository.

---

# 7. GitHub Authentication

Assume that most users, including the initial developer, use one GitHub account across multiple profiles.

Support shared GitHub authentication by default.

Use the GitHub CLI where appropriate.

Do not require separate GitHub accounts for work and personal profiles.

However, design the GitHub authentication interface so profile-specific authentication can be supported later.

GitHub identity must not determine repository profile identity.

The persistent repository association remains authoritative.

Never modify the globally active GitHub account merely to launch a task.

---

# 8. Task Model

Support two task sources in V1:

1. GitHub.
2. Local tasks.

Use a source-independent internal task model.

## GitHub tasks

Support:

- Issues.
- Pull requests.
- Assigned issues.
- Authored PRs.
- PRs requesting review.
- Relevant GitHub labels.
- Open, closed, and merged states.

Use stable task identities.

Do not rely exclusively on mutable `owner/repo` names.

## Local tasks

Allow creating arbitrary tasks associated with a local repository or working directory.

Example:

```bash
agent-launcher new \
  --title "Investigate startup performance" \
  --repo ~/Projects/my-app
```

Local tasks must support:

- Title.
- Optional description.
- Repository association.
- Profile association.
- Workflow.
- Agent.
- Worktree.
- Persistent session.
- Lifecycle state.

## Local-to-GitHub conversion

Support linking an existing local task to a GitHub issue or PR.

Example:

```bash
agent-launcher tasks link <task-id> <github-url>
```

This must preserve:

- Internal task ID.
- Agent identity.
- Profile.
- Worktree association.
- Session history.
- Existing agent conversation.

Do not create duplicate sessions when linking.

If the GitHub identity already belongs to another task, stop and require explicit conflict resolution.

---

# 9. One Task, One Primary Session

This is a deliberate architectural constraint.

**Each task has one canonical primary agent session and one associated worktree.**

Do not implement a general-purpose multi-agent orchestration framework.

Multi-agent workflows are the responsibility of the selected agent and its skills.

The launcher does not need to manage individual subagents.

## Opening an existing task

When the user opens a task that already has an active session:

1. Locate its persisted session.
2. Validate the terminal association.
3. Focus the existing session.
4. Do not display an agent picker.
5. Do not regenerate the initial prompt.
6. Do not create another worktree.

If the agent process has exited, use the agent adapter's supported resumption mechanism where possible.

If the session cannot be restored automatically, present a safe recovery option.

## Additional commands

Support equivalents of:

```bash
agent-launcher open <task>
agent-launcher resume <task>
agent-launcher prompt <task>
agent-launcher restart <task>
```

Restart must require confirmation and preserve the existing worktree.

Do not create multiple primary sessions for the same task.

---

# 10. Session Registry

Use SQLite for runtime state.

Store information including:

- Internal task ID.
- Task source.
- External task identifiers.
- Repository identity.
- Profile identity.
- Selected agent.
- Workflow.
- Worktree path.
- Worktree ownership.
- Agent conversation identifier.
- Terminal adapter.
- Terminal workspace identifier.
- Terminal pane or surface identifier.
- Session lifecycle state.
- Creation and update timestamps.
- Completion and archive metadata.

Do not store full agent conversations.

Avoid storing full generated prompts unless an explicit feature requires it and the user enables that behavior.

## Concurrent launches

The application must safely support concurrent invocations.

Use SQLite transactions and per-task locking.

Two processes opening the same task must not create two independent primary sessions.

Different tasks should be able to launch concurrently.

Locks must support safe recovery after crashes.

Do not use a persistent background daemon in V1.

---

# 11. Repository Resolution

Use the following resolution order:

1. Explicit repository mapping.
2. Previously discovered repository locations.
3. Configured repository search roots.
4. Git remote inspection.
5. GitHub metadata.
6. Offer to clone if no suitable repository exists.

Example configuration:

```json
{
  "repositories": {
    "search_roots": [
      "~/Projects",
      "~/Developer"
    ],
    "clone_root": "~/Projects",
    "auto_clone": false,
    "worktree_root": "~/.agent-launcher/worktrees",
    "mappings": {}
  }
}
```

Do not recursively search the entire filesystem.

Search only configured directories.

Cache discoveries where appropriate.

Validate cached entries before reuse.

## Missing repositories

Do not automatically clone repositories by default.

Ask first.

Clone using the selected repository identity and appropriate GitHub authentication.

Ensure that the repository receives its correct persistent profile association before launching an agent.

---

# 12. Git Worktree Management

Worktree management is a core V1 feature.

Do not defer it.

## Existing worktree discovery

Before creating a worktree:

1. Check the task registry.
2. Inspect existing Git worktrees using a machine-readable Git interface.
3. Inspect associated branches and remotes.
4. Inspect relevant PR metadata.
5. Identify likely existing checkouts.
6. Offer to adopt a suitable existing worktree when appropriate.

Use:

```bash
git worktree list --porcelain
```

Do not parse human-formatted Git output when a machine-readable alternative exists.

## Adoption

If a matching worktree exists, allow the user to associate it with the task.

Remember that association.

Do not repeatedly ask about an already adopted worktree.

Matching branch names alone are insufficient proof that a worktree belongs to a particular task.

## Issues

For an issue without an existing worktree, create an appropriately named task branch and dedicated worktree.

Use the repository's configured base branch.

Do not assume every repository uses `main`.

## Pull requests

For the user's own PRs, prefer the actual PR branch when it can be checked out safely.

For someone else's PR, use a dedicated review checkout or appropriately isolated worktree.

Never modify the contributor's branch automatically.

Handle forks and remote branches correctly.

Do not assume that a PR head branch exists in the local repository.

## Safety rules

Never automatically:

- Reset a branch.
- Stash changes.
- Clean untracked files.
- Delete uncommitted changes.
- Force-checkout another branch.
- Delete an externally adopted worktree.
- Push changes.
- Modify an existing user's checkout destructively.

Detect dirty worktrees and branch conflicts.

When necessary, present explicit choices.

Track whether a worktree was created by Agent Launcher or adopted from an external location.

---

# 13. Agent Selection

Support three modes:

```text
always_ask
use_default
ask_if_multiple
```

Default to:

```text
always_ask
```

For new tasks, display only agents available in the selected repository profile.

## Selection precedence

When highlighting a preferred agent:

1. Workflow's preferred agent.
2. Last-used agent within the profile.
3. Profile's default agent.

A workflow's preferred agent is advisory.

It must never override profile restrictions.

## Example interaction

```text
PR #291 — Update dependency injection

Repository: company/platform
Profile: Work
Workflow: Code Review

Select agent:

  > Claude Code (Work)
    Codex (Work)
    GitHub Copilot

Enter: Select
Esc: Cancel
```

For existing sessions, skip selection and focus the existing agent.

---

# 14. Workflow Routing

Implement deterministic, JSON-configured workflow routing.

The launcher should use GitHub metadata to select appropriate workflows.

Supported matching conditions should include, where applicable:

- Task type.
- Repository.
- Repository owner.
- Labels.
- Author.
- Assignee.
- Review requested.
- PR draft status.
- CI/check status.
- Issue or PR state.

## Example

```json
{
  "workflows": [
    {
      "id": "issue-triage",
      "priority": 100,
      "match": {
        "type": "issue",
        "labels_any": [
          "needs-triage"
        ]
      },
      "skill": "issue-triage"
    },
    {
      "id": "code-review",
      "priority": 80,
      "match": {
        "type": "pr",
        "review_requested": "self"
      },
      "preferred_agent": "claude",
      "skill": "code-review"
    },
    {
      "id": "pr-review-fixer",
      "priority": 60,
      "match": {
        "type": "pr",
        "author": "self"
      },
      "skill": "pr-review-fixer"
    }
  ]
}
```

Evaluate the highest-priority matching workflow.

Use deterministic tie handling.

Do not select a workflow using an LLM.

## Selection modes

Support:

```text
automatic
ask_on_multiple
always_ask
```

Default to:

```text
automatic
```

Support explicit CLI overrides:

```bash
agent-launcher open <url> --workflow code-review
agent-launcher open <url> --ask-workflow
```

Provide an explanation command:

```bash
agent-launcher workflows test <url>
```

This should display matching rules, priorities, and the selected workflow.

## No matching rule

Use a configurable fallback workflow.

The fallback must not introduce unauthorized agent access or change the repository's profile.

---

# 15. Skills

Agent Launcher must support existing agent skills without copying or redefining them.

Users may maintain their own skills for:

- Issue triage.
- Code review.
- PR fixes.
- Implementation.
- Security review.
- Architecture review.
- Other custom workflows.

The application should support skill discovery where reliable, but must not assume every agent implements skills identically.

Each agent adapter must define how skills are invoked.

If a required skill is unavailable, clearly report the condition.

Do not silently substitute a different skill.

## Skill management

Prefer using the existing `npx skills` ecosystem rather than implementing another skill package manager.

Investigate its current supported commands and installation behavior.

The application itself must not require Node.js during normal operation.

---

# 16. Bundled Management Skill

Ship one management skill named:

```text
agent-launcher
```

Its purpose is to teach coding agents how to configure, troubleshoot, maintain, and extend Agent Launcher.

It should understand:

- Configuration schema.
- Profiles.
- Agent instances.
- Workflow routing.
- Repository associations.
- Prompt templates.
- Session management.
- Terminal adapters.
- Diagnostics.
- Configuration migrations.
- Integration setup.

The skill must instruct agents to use supported CLI operations for configuration changes whenever possible.

Agents should not directly modify SQLite state.

They must not bypass profile safety checks.

## Skill distribution

Bundle the skill with the Python package.

Support installation through `npx skills`.

Use conventional global skill installation locations.

Do not implement special handling for custom Claude configuration directories.

The initial developer already uses an external synchronization mechanism to distribute globally installed Claude skills to separate Claude environments.

Do not interfere with that mechanism.

---

# 17. Prompt Construction

Prompts may use optional templates.

Templates are stored under:

```text
~/.agent-launcher/templates/
```

## Prompt precedence

Apply these rules exactly.

**Template available**

Render the template using available task variables.

**No template, but skill assigned**

Generate only the skill invocation and task URL.

Example:

```text
/code-review https://github.com/owner/repo/pull/291
```

**No template or skill**

Generate only the task URL.

Example:

```text
https://github.com/owner/repo/issues/142
```

For local tasks without URLs, use a minimal task reference or description.

Do not invent lengthy instructions.

Do not automatically append large GitHub descriptions, diffs, or comment histories.

The agent's skill should be responsible for substantive investigation.

## Template variables

Support useful variables such as:

```text
task_id
task_type
task_url
task_title
repository
repository_path
worktree_path
profile
agent
workflow
```

Clearly document variable availability.

Validate templates.

If a configured template fails to render, report the error rather than silently replacing it with an unrelated prompt.

---

# 18. Prompt Execution Modes

Support two modes:

```text
prepare
execute
```

Default:

```text
prepare
```

## Prepare mode

1. Launch the selected agent.
2. Wait until its interactive interface is ready.
3. Insert the prepared prompt.
4. Do not submit it.
5. Allow the user to edit it.
6. Leave the cursor ready for interaction.

## Execute mode

Launch the agent and submit the prepared prompt automatically.

Support:

```bash
agent-launcher open <url> --execute
```

Allow execution mode to be configured globally and per workflow.

## Critical behavior

Do not assume that sending multiline text to a terminal is equivalent to safely preparing a prompt.

Agent adapters must account for their specific terminal input behavior.

Avoid accidentally sending Enter.

Test this against actual installed agent versions.

If safe prompt preparation cannot be guaranteed, report the limitation and provide a safe alternative rather than accidentally executing the prompt.

---

# 19. Terminal Adapter Architecture

The application must be terminal-independent.

V1 will implement **cmux only** as a full terminal adapter.

However, the adapter system must be designed to allow adding:

- Ghostty.
- Herdr.
- tmux.
- macOS Terminal.
- Windows Terminal.
- Other terminal or session backends.

without rewriting core task management.

## Common adapter responsibilities

Define operations equivalent to:

```python
class TerminalAdapter:
    def available(self) -> bool:
        ...

    def capabilities(self) -> set[str]:
        ...

    def create_session(self, request):
        ...

    def focus_session(self, session):
        ...

    def send_text(self, session, text):
        ...

    def discover_sessions(self):
        ...

    def close_session(self, session):
        ...
```

Use proper typed request and result models in the implementation.

Do not expose cmux-specific types throughout the application.

## Capabilities

Adapters must explicitly report supported capabilities.

For example:

```text
create_session
focus_session
discover_sessions
prepare_prompt
submit_prompt
restore_session
close_session
```

Unsupported operations must fail explicitly.

Do not silently switch terminal providers.

## Future adapters

Document how to create an additional terminal adapter.

Include a mock terminal adapter for testing.

The core application must pass its tests using the mock adapter without cmux installed.

---

# 20. cmux Integration

Implement a complete cmux adapter in V1.

Inspect the installed cmux version and current CLI documentation before implementation.

Do not assume commands from older versions remain valid.

The integration should support:

- Detecting cmux.
- Creating workspaces.
- Setting working directories.
- Starting agent processes.
- Preparing prompts.
- Focusing existing workspaces.
- Focusing the correct terminal surface.
- Discovering candidate sessions.
- Recording terminal identifiers.
- Recovering from unavailable or stale terminal identifiers.

## Workspace naming

Use meaningful names including repository and task information.

For example:

```text
app-ios — Issue #142
platform — PR #291
```

Avoid ambiguous workspace names when multiple repositories contain tasks with the same number.

## Existing sessions

Automatically manage sessions created by Agent Launcher.

Support conservative discovery of externally created cmux sessions.

If an external session appears to match a task, offer explicit adoption.

Do not automatically assume that a Claude process running in a repository belongs to a particular GitHub issue.

Do not move or terminate adopted sessions automatically.

## Session resumption

If the terminal session is active, focus it.

If the terminal session has exited, use the selected agent adapter's supported conversation-resumption mechanism where available.

Never claim that a conversation has been restored unless the underlying agent confirms successful restoration.

---

# 21. Interactive User Interface

Provide a lightweight terminal-based interactive interface.

Do not build a full-screen dashboard in V1.

The interactive selection interface must remain independent of cmux.

Use the originating terminal when it supports interactive input.

If gh-dash cannot reliably provide the required interactive environment, launch the picker in an appropriate temporary cmux surface.

Do not assume how gh-dash custom actions handle standard input.

Verify actual behavior.

## Noninteractive support

All relevant operations should have a noninteractive mode.

If a required decision cannot be resolved safely, return a structured error.

Never silently select a repository profile merely to satisfy noninteractive execution.

Support JSON output for automation.

---

# 22. gh-dash Integration

gh-dash is the initial task-discovery interface.

Agent Launcher must remain usable without gh-dash.

## Desired workflow

From gh-dash:

1. Highlight an issue or PR.
2. Press the configured shortcut.
3. Invoke Agent Launcher with the selected task.
4. Resolve the repository profile if necessary.
5. Select an agent.
6. Resolve the workflow.
7. Create or resume the task session.
8. Open the agent in cmux.

## Critical requirement: Agent-assisted integration

**Do not build a specialized gh-dash YAML modification engine.**

Instead, Agent Launcher should bootstrap its gh-dash integration by creating a local maintenance task.

After initial setup:

1. Detect whether gh-dash is installed.
2. Offer to configure the integration.
3. Create an Agent Launcher local task.
4. Launch the selected agent through the normal task-launch infrastructure.
5. Prepare an integration prompt.
6. Allow the user to edit and submit it.

The integration prompt should instruct the agent to:

- Inspect the installed gh-dash version.
- Locate its existing configuration.
- Inspect current keybindings.
- Read current integration documentation.
- Preserve all existing configuration.
- Add an Agent Launcher shortcut for issues and PRs.
- Avoid overwriting conflicting shortcuts.
- Validate the resulting configuration.
- Test the integration where possible.
- Report changes.

The setup task must use the bundled `agent-launcher` management skill.

Do not require gh-dash to be installed to complete core setup.

The launcher should also provide enough documentation for manual integration.

---

# 23. Initial Setup Wizard

Implement progressive first-run setup.

Running:

```bash
agent-launcher
```

without an existing configuration should offer to initialize the application.

Also support:

```bash
agent-launcher setup
```

## Initial discovery

Detect:

- Operating system.
- Python version.
- Git.
- GitHub CLI.
- GitHub authentication.
- cmux.
- gh-dash.
- Claude Code.
- Codex CLI.
- GitHub Copilot CLI.
- Available agent wrappers.
- Existing agent configurations, where discoverable.

Do not assume detection results are authoritative when multiple identities are possible.

Ask for confirmation where appropriate.

## Required initial configuration

The wizard should establish:

1. At least one profile.
2. Available agents for each profile.
3. Agent executable and environment settings.
4. Default agent preferences.
5. Terminal adapter.
6. Repository search roots.
7. Worktree root.
8. Basic workflow-routing preferences.
9. Prompt execution default.
10. Agent-selection behavior.

Do not require users to configure every repository immediately.

When a new repository is encountered, request a profile association and persist it.

## Safety

Setup must be idempotent.

Running it again must not destroy or reset existing configuration.

Show proposed changes before applying them.

Do not overwrite existing agent configuration files.

---

# 24. Agent-Assisted Configuration

Support a command equivalent to:

```bash
agent-launcher configure
```

This creates a local Agent Launcher maintenance task and launches a configured agent with the management skill.

The agent may assist with:

- Adding workflow rules.
- Configuring agents.
- Creating profiles.
- Editing templates.
- Adding terminal adapters.
- Troubleshooting integrations.
- Preparing configuration migrations.

Deterministic configuration validation and persistence remain the responsibility of the Python application.

Agents should use documented CLI commands whenever possible.

Do not allow agent-assisted configuration to bypass profile safety controls.

---

# 25. Task Completion and Cleanup

Detect GitHub issue closure and PR closure or merging.

Mark corresponding tasks as eligible for cleanup.

Do not automatically terminate sessions or delete worktrees.

Provide:

```bash
agent-launcher tasks completed
agent-launcher tasks archive <task>
agent-launcher cleanup
```

## Cleanup requirements

Before offering to delete a worktree, check:

- Whether Agent Launcher created it.
- Whether it contains uncommitted changes.
- Whether it contains untracked files.
- Whether it contains unpushed commits.
- Whether a running process is using it.
- Whether the associated task is still active.

Never automatically delete externally adopted worktrees.

Require explicit confirmation before deleting launcher-managed worktrees.

Archiving task metadata must be possible without deleting files.

Preserve historical task records.

---

# 26. Configuration Migrations

Use versioned configuration schemas.

Support controlled migrations.

Migration operations must:

1. Detect the current version.
2. Validate existing configuration.
3. Create a backup.
4. Determine necessary changes.
5. Request confirmation for significant changes.
6. Write atomically.
7. Validate the result.
8. Restore the backup if validation fails.

Support dry-run mode.

Known migrations should be deterministic Python operations.

Do not require an AI agent for routine migrations.

If a migration requires interpretation of custom configuration, offer an agent-assisted maintenance task.

Never silently change repository-to-profile associations during migration.

---

# 27. Transactional Task Creation

Task creation consists of multiple operations that may fail.

Treat launches as recoverable transactions.

Possible stages:

```text
resolve task
resolve repository
resolve profile
resolve workflow
select agent
resolve worktree
register task
create terminal session
start agent
prepare prompt
mark ready
```

Record sufficient information to recover from partial failure.

Do not claim that a task is running before the agent session is successfully established.

## Failure handling

If worktree creation succeeds but cmux creation fails:

- Preserve the worktree.
- Record the incomplete operation.
- Display the error.
- Offer retry.
- Do not create a duplicate worktree on retry.

Never roll back by deleting resources that existed before the launch attempt.

Automatically remove newly created resources only when their ownership and unused state can be verified safely.

Use explicit lifecycle states.

---

# 28. Logging and Diagnostics

Provide structured application logging.

Default location:

```text
~/.agent-launcher/logs/
```

Support log rotation and configurable retention.

## Debug mode

Support:

```bash
agent-launcher --debug open <url>
```

Debug output should make it possible to understand:

- Repository resolution.
- Profile selection.
- Agent selection.
- Workflow matching.
- Worktree decisions.
- Session resolution.
- Terminal operations.
- Recovery failures.

## Diagnostic commands

Implement:

```bash
agent-launcher doctor
agent-launcher doctor --json
agent-launcher diagnostics export
```

The diagnostic command should inspect:

- Configuration validity.
- Python compatibility.
- External CLI availability.
- Agent executables.
- Profile configuration.
- GitHub authentication.
- cmux availability.
- Skill integration.
- Repository mappings.
- Database health.

## Privacy

Never log:

- Authentication tokens.
- API keys.
- Passwords.
- Full agent conversations.
- Complete prompts by default.
- Private repository content unnecessarily.

Diagnostic exports must be sanitized.

Redaction must apply to environment variables, arguments, and potentially sensitive paths.

---

# 29. Public CLI Contract

Design a coherent CLI with commands resembling:

```bash
agent-launcher
agent-launcher setup
agent-launcher doctor
agent-launcher version
```

Task operations:

```bash
agent-launcher open <github-url>
agent-launcher new --title "Task" --repo <path>
agent-launcher resume <task>
agent-launcher prompt <task>
agent-launcher restart <task>
agent-launcher tasks list
agent-launcher tasks show <task>
agent-launcher tasks archive <task>
agent-launcher tasks link <task> <github-url>
```

Configuration:

```bash
agent-launcher config show
agent-launcher config edit
agent-launcher config validate
agent-launcher config migrate
```

Profiles:

```bash
agent-launcher profile list
agent-launcher profile add
agent-launcher profile edit
agent-launcher profile set <repo> <profile>
```

Workflows:

```bash
agent-launcher workflows list
agent-launcher workflows test <github-url>
```

Worktrees:

```bash
agent-launcher worktrees list
agent-launcher worktrees inspect <task>
agent-launcher worktrees associate <task> <path>
```

Maintenance:

```bash
agent-launcher configure
agent-launcher cleanup
agent-launcher diagnostics export
```

Use consistent argument naming and error behavior.

Support machine-readable JSON output for commands where it is useful.

Do not implement every command merely as an empty stub.

Adjust command naming where justified, but document any departures from this specification.

---

# 30. Installation and Distribution

The application must be installable through pipx.

Support editable installation during development.

Provide clear documentation.

Prepare the project for eventual publication to a private GitHub repository.

Homebrew distribution is a future objective.

Do not require a working Homebrew tap in V1.

However:

- Keep package metadata conventional.
- Avoid installation-time assumptions about the source checkout.
- Ensure bundled skill resources are included in the package.
- Ensure configuration remains outside the installed package.
- Do not write mutable state into the package installation directory.
- Make clean upgrades possible.

Do not create a custom self-updater.

---

# 31. Testing Requirements

Use pytest or an equivalently appropriate Python testing framework.

Unit tests must cover:

- Configuration validation.
- Configuration migrations.
- Profile persistence.
- Profile mismatch rejection.
- Agent resolution.
- Workflow matching and priorities.
- Prompt construction.
- Repository discovery.
- Worktree association.
- Task identity.
- Local-to-GitHub linking.
- Session state transitions.
- Concurrency control.
- Failure recovery.
- Cleanup protections.

## Safety-critical tests

Explicitly test that:

1. Unknown repositories never receive an automatic profile assignment.
2. A work task never falls back to a personal agent.
3. An unavailable agent causes a safe failure.
4. Profile associations survive application restarts.
5. Profile associations survive configuration migration.
6. Existing task launches do not create duplicate sessions.
7. Dirty worktrees are never deleted automatically.
8. Externally adopted worktrees are never deleted automatically.
9. Failed launches remain recoverable.
10. Concurrent requests for the same task produce only one primary session.

## Terminal abstraction tests

Use a mock terminal adapter.

Verify that core task management operates without cmux installed.

The core must not depend on cmux-specific identifiers or commands.

## Integration tests

Test against actual installed versions of:

- Git.
- GitHub CLI.
- cmux.
- Claude Code.
- Codex CLI.
- Copilot CLI, where available.

Do not perform destructive integration tests against production repositories.

Use isolated temporary repositories and temporary configuration directories.

Avoid invoking billable AI operations unnecessarily during automated testing.

---

# 32. Documentation

Provide a useful README containing:

- Project overview.
- Installation.
- Quick start.
- Initial setup.
- Creating profiles.
- Configuring agents.
- Launching GitHub tasks.
- Launching local tasks.
- Configuring workflows.
- Managing worktrees.
- Resuming sessions.
- Troubleshooting.

Provide additional documentation for:

```text
docs/
├── architecture.md
├── configuration.md
├── profiles.md
├── workflows.md
├── agents.md
├── terminal-adapters.md
├── github-integration.md
├── sessions.md
├── security.md
└── development.md
```

These filenames are suggestions.

Documentation must describe actual behavior, not planned or nonexistent functionality.

Include a practical guide to implementing additional terminal adapters.

Explain clearly that profile separation does not constitute OS-level sandboxing.

---

# 33. Implementation Milestones

Implement the application incrementally.

Do not stop after producing an architecture document.

Maintain a durable implementation plan in the repository.

Each milestone must have executable acceptance tests.

## Milestone 1 — Foundation

Deliver:

- Python package.
- CLI entry point.
- Configuration schema.
- Initial setup wizard.
- Profile management.
- Global agent definitions.
- Profile-specific agent instances.
- Validation.
- Initial diagnostic commands.

**Acceptance:** The application installs cleanly, initializes configuration, and correctly resolves configured agent identities.

## Milestone 2 — Task and Workflow System

Deliver:

- GitHub task provider.
- Local task provider.
- Stable task identities.
- Repository discovery.
- Persistent profile associations.
- Workflow routing.
- Agent selection.
- Prompt templates and fallbacks.

**Acceptance:** GitHub and local tasks resolve deterministically to profiles, workflows, agents, and prepared prompts.

## Milestone 3 — Worktrees and Persistence

Deliver:

- Git worktree discovery.
- Worktree creation.
- Existing worktree adoption.
- SQLite task/session registry.
- Task locking.
- Recovery states.
- Concurrent launch protections.

**Acceptance:** Multiple tasks can safely coexist, and duplicate launches converge on the same canonical task.

## Milestone 4 — cmux and Agent Execution

Deliver:

- Terminal adapter interface.
- Mock adapter.
- Full cmux adapter.
- Agent startup.
- Editable prompt preparation.
- Execute mode.
- Session focusing.
- Session restoration where supported.
- External session adoption where reliable.

**Acceptance:** Selecting a task launches the correct agent in cmux and prepares the expected prompt. Reopening the task focuses its existing session.

## Milestone 5 — Agent-Assisted Configuration

Deliver:

- Bundled management skill.
- `npx skills` integration.
- Local maintenance tasks.
- Agent-assisted configuration command.
- Agent-assisted gh-dash integration task.
- Documentation for gh-dash integration.

**Acceptance:** The user can complete initial setup, launch a maintenance task, and use an agent to configure gh-dash integration without requiring a custom YAML editor in Agent Launcher.

## Milestone 6 — Lifecycle, Reliability, and Distribution

Deliver:

- Completion detection.
- Archiving.
- Safe cleanup.
- Configuration migrations.
- Full diagnostics.
- Sanitized diagnostic export.
- Comprehensive tests.
- Installation documentation.
- Packaging validation.

**Acceptance:** The application can be installed, configured, used repeatedly, upgraded, diagnosed, and maintained without corrupting repository state or silently crossing profile boundaries.

---

# 34. Required End-to-End Acceptance Scenarios

The completed implementation must demonstrate the following scenarios.

### Scenario A: First launch from an unknown repository

A GitHub issue is selected.

The launcher has never encountered the repository.

Expected result:

1. Repository identified.
2. Profile selection requested.
3. Association persisted.
4. Agent picker displayed.
5. Workflow resolved.
6. Worktree established.
7. cmux workspace created.
8. Agent launched.
9. Prompt prepared but not executed.

### Scenario B: Reopen existing issue

The same issue is selected again.

Expected result:

- Existing task found.
- Existing session focused.
- No duplicate worktree.
- No new agent selection.
- No repeated prompt.

### Scenario C: Work/personal isolation

A repository assigned to `work` requests Claude.

The work Claude executable is unavailable.

Expected result:

- Launch rejected.
- No personal Claude fallback.
- No profile reassignment.
- Clear diagnostic error.

### Scenario D: Existing worktree

A PR already has an appropriate worktree.

Expected result:

- Existing worktree discovered.
- User offered adoption.
- No unnecessary worktree created.
- Association persisted.

### Scenario E: Workflow routing

A PR requesting the user's review matches the code-review workflow.

Expected result:

- Correct rule selected.
- Appropriate agent preference highlighted.
- Skill invocation prepared.
- User can edit prompt before submission.

### Scenario F: Local task

The user creates a local task.

Expected result:

- Stable local task ID.
- Repository and profile resolved.
- Agent launched.
- Task persists between application invocations.

### Scenario G: Local task linked to GitHub

An existing local task is linked to a newly created GitHub issue.

Expected result:

- Internal identity preserved.
- Existing worktree preserved.
- Existing session preserved.
- GitHub identity associated.
- No duplicate task.

### Scenario H: Concurrent launch

Two processes attempt to open the same task simultaneously.

Expected result:

- One canonical task.
- One primary session.
- One associated worktree.
- No corrupted database state.

### Scenario I: Partial launch failure

Worktree creation succeeds, but cmux fails.

Expected result:

- Recoverable task state.
- Worktree preserved.
- Clear error.
- Retry succeeds without creating duplicates.

### Scenario J: gh-dash bootstrap

Initial setup completes.

The user chooses to configure gh-dash.

Expected result:

- Local maintenance task created.
- Management skill selected.
- Agent launched in cmux.
- Integration prompt prepared.
- Existing gh-dash configuration preserved.

---

# 35. Engineering Quality Standards

Apply the following standards throughout implementation.

### Maintainability

Favor straightforward, conventional Python.

Avoid complicated metaprogramming, unnecessary dependency injection frameworks, and excessive inheritance.

Use type annotations consistently.

Separate business logic from CLI presentation.

### Reliability

Use structured subprocess invocation.

Handle subprocess exit codes and timeouts.

Avoid shell interpolation of untrusted repository names, titles, URLs, and paths.

Validate external inputs.

Use transactions and explicit state transitions.

### Security

Do not execute untrusted task content as shell commands.

Treat GitHub issue titles, descriptions, comments, and repository metadata as untrusted input.

Do not embed arbitrary task content into executable command strings.

Protect credentials.

Enforce profile boundaries.

### User experience

Prefer sensible defaults.

Avoid redundant prompts.

Remember durable decisions.

Present useful errors.

Do not ask users to configure information that can be reliably discovered.

Do not guess when identity selection is ambiguous.

### Extensibility

Adding a new agent type should not require changing workflow routing.

Adding a new terminal adapter should not require changing task management.

Adding a new task provider should not require changing session persistence.

Adding a new workflow should generally require only configuration.

---

# 36. Implementation Instructions

Begin by inspecting the development environment.

Determine:

- Operating system and architecture.
- Python versions.
- Available package managers.
- Installed Git and GitHub CLI versions.
- Installed cmux version and API capabilities.
- Installed Claude Code version.
- Installed Codex CLI version.
- Installed Copilot CLI version.
- Existing relevant skill infrastructure.
- Existing gh-dash installation.

Do not modify existing application configurations during this inspection.

Next:

1. Review this specification completely.
2. Identify implementation dependencies and technical risks.
3. Verify external APIs and CLI behavior.
4. Produce a concise architectural design.
5. Create a milestone-based implementation plan in the repository.
6. Implement the system incrementally.
7. Run tests after each milestone.
8. Resolve failures before proceeding.
9. Verify the end-to-end workflows.
10. Complete installation and operational documentation.

Keep the implementation plan updated throughout development.

Do not stop after scaffolding.

Do not treat mocked tests as proof that real terminal and agent integration works.

Do not silently omit difficult requirements.

If a documented requirement is technically infeasible with the installed tools, investigate alternatives and document the limitation. Preserve the intended architecture wherever possible.

If a decision is not specified, choose the simplest solution consistent with the architecture and safety requirements.

Only request user input when a decision materially affects security, data preservation, or a major product behavior that cannot be reasonably inferred from this specification.

---

# 37. Definition of Done

V1 is complete when:

- The Python package installs successfully.
- First-run setup works.
- Profiles and agents are configurable.
- Repository profile associations persist.
- Work/personal identity fallback is prevented.
- GitHub issues and PRs can be launched.
- Local tasks can be launched.
- Local tasks can be linked to GitHub.
- Workflow rules select the correct skills.
- Agent selection works.
- Existing worktrees are discovered.
- Task worktrees are safely managed.
- cmux sessions are created and focused.
- Existing task sessions are reused.
- Prompts are prepared without accidental execution.
- Optional automatic execution works.
- The management skill is bundled.
- gh-dash integration can be bootstrapped through an agent task.
- Concurrent task launches are safe.
- Partial failures are recoverable.
- Configuration migrations are supported.
- Cleanup is conservative.
- Diagnostics are available.
- Automated tests pass.
- Documentation matches the implemented behavior.

The application must be usable in a normal development environment without running a persistent server.

**The ultimate success criterion is that a developer can select a GitHub issue or PR, choose an agent, and begin working in an isolated, correctly configured, resumable session with minimal interaction.**

Build the complete V1 to this standard.

---
