# Security model: repository profile associations

Commodore Launcher must never run a task under the wrong profile. Which profile a repository uses is therefore an explicit, stored decision, not something worked out each time.

## The association

Every repository has at most one profile, stored in `state.db` (under `~/.commodore-launcher/`, or `$COMMODORE_LAUNCHER_HOME`), separate from `config.json`. The rules:

- **Explicit only.** An association exists because you chose it: by answering the prompt for a new repository, or with `commodore profile set`. Nothing is assigned from a default profile, from the repository's owner or organisation, or from another repository.
- **Authoritative until you change it.** A rename, a transfer, a changed config default, an unavailable harness or a different terminal never changes an association.
- **Unknown repositories stop.** On a terminal you are shown the repository and the available profiles and must pick one. Without a terminal (or with `--json`) the command fails with `unknown_repository_profile`, listing the available profiles. Nothing is launched and nothing is saved.
- **A removed profile is not replaced.** If the associated profile no longer exists in `config.json`, the result is `associated_profile_missing`; the repository is not moved to another profile.
- **Changing needs a resolution when tasks exist.** `profile set` on a repository with tasks lists them (profile, session, conversation) and requires `--archive-tasks`, `--keep-tasks` or `--cancel`; without one it fails with `reassignment_requires_resolution` and changes nothing. It never closes sessions, touches checkouts or rewrites the profile stored on tasks and sessions, and an old session is never reused under the new profile. An org/name change, a default, harness availability and the terminal environment never change an association (`tests/test_association_safety.py`). See [profiles.md](profiles.md) and [ADR 0014](adr/0014-profile-reassignment.md).

## How a repository is recognised

Identity is built from the checkout's **one primary remote**, never from all of its remotes:

- the primary remote is `origin` if the checkout has one; otherwise the remote the current branch tracks (`branch.<name>.remote`); otherwise there is none and the checkout is known by its path alone;
- other remotes (a fork's `upstream`, for instance) are shown as `other_remotes` but are never stored and never used to match. A personal fork of a work repository is therefore its own, unknown repository and asks which profile to use.

Lookup order:

1. **GitHub repository ID** (immutable, from `gh api repos/{owner}/{name}` for the primary remote only), plus the node ID. If the ID is already stored, that repository matches, whatever its name, owner or path now are. This is what makes renames and transfers keep their association; the new name and remote are recorded against the same repository.
2. **Canonical local path** (the checkout's top level, symlinks resolved) and the **normalised primary remote** (`git@github.com:Owner/Repo.git`, `https://github.com/owner/repo` and the like all become `github.com/owner/repo`; credentials are dropped).

Details that protect against mix-ups:

- A path or remote that belongs to a stored repository with a **different** GitHub ID is not a match. A repository created later under a freed name does not inherit the old repository's profile.
- When GitHub reports an ID, a stored repository **without** an ID is not matched by path or remote either, because it cannot be proven to be the same repository. You are asked again, and the new association is stored with the ID. A stored ID is never filled in from a lookup.
- **A path match is accepted only when the remote state agrees:** both the checkout and the stored repository have no remote, or the checkout's primary remote is one the stored repository has had. So a local-only repository replaced by a clone at the same path, a clone replaced by a fresh `git init`, and a checkout whose `origin` was removed are all unknown and ask again, online or offline.
- If path and remote point at more than one stored repository, the result is `ambiguous_repository`. Set the profile explicitly (online, where IDs tell them apart).
- `profile set`/`profile which` accept a path, `owner/name` or a GitHub URL. An argument that is an existing directory is always read as a path. `.` and `..` are not valid owner or name parts.

**Offline caveat.** The ID is optional. Offline, without `gh`, with `--offline`, or for a non-GitHub remote, matching uses the primary remote and path only. Then a repository created under a name that was freed since the association was stored would match the old association, because nothing offline can tell the two apart. Associations made while online store the ID and do not have this problem. Prefer online use for repositories whose names may be reused.

## Other safety rules, in one place

- **No fallback.** If the profile's harness executable is missing, the launch is refused with a diagnostic naming the profile and the executable. No other profile's harness is tried, and nothing is reassigned (`harness_unresolved`). The same holds for a different terminal adapter: a session recorded by one is never looked at through another.
- **One identity per harness.** Two profiles using the same harness must set different identity directories (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `COPILOT_HOME`); setup refuses otherwise. Nothing is read from or written into a harness's own configuration directory.
- **GitHub text is untrusted.** Titles and bodies never reach a command line, a branch name or the harness's prompt; only validated owner, name and number do. Subprocesses use argument lists and timeouts, never a shell with task content.
- **GitHub login is read, not managed.** The launcher uses the `gh` login as it is. It never logs in, out or refreshes; `doctor` runs `gh auth status` read-only to report whether you are logged in.
- **The prompt is fixed.** It is the harness's invocation of Commodore on the task's URL, passed on the command line through a private temp file the shell deletes, and never includes an issue's title or body.
- **Destructive actions are narrow.** The launcher removes no worktree, branch or checkout (it creates none), and never closes a session it did not create.
- **Secrets stay out.** Config holds no credentials; logs and exports are redacted ([diagnostics.md](diagnostics.md)).

## What this does not protect against

**Profile separation is not an operating-system sandbox.** Profiles choose which executable, arguments and environment a harness starts with. They are **not** isolation: any process you start, including the harnesses, runs as your user and can read what your account can read, including other profiles' configuration directories, your SSH keys and your shell history. A misbehaving or prompt-injected harness in a `personal` profile can read `work` files if your account can. If you need that boundary, use separate operating-system accounts, containers or machines; Commodore Launcher does not provide it. See [profiles.md](profiles.md).

The state database is a plain SQLite file with your user's permissions. Anyone who can edit it can change associations.

## Related

- State store design: [ADR 0003](adr/0003-state-database.md).
- Commands: [profiles.md](profiles.md).
