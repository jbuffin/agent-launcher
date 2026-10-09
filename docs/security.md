# Security model: repository profile associations

Agent Launcher must never run a task under the wrong profile (SPEC §6). Which profile a repository uses is therefore an explicit, stored decision, not something worked out each time.

## The association

Every repository has at most one profile, stored in `state.db` (under `~/.agent-launcher/`, or `$AGENT_LAUNCHER_HOME`), separate from `config.json`. The rules:

- **Explicit only.** An association exists because you chose it: by answering the prompt for a new repository, or with `agent-launcher profile set`. Nothing is assigned from a default profile, from the repository's owner or organisation, or from another repository.
- **Rules may suggest, never assign.** A routing rule can name a profile; it is highlighted in the prompt and reported as `suggested_profile` in the error. It is not stored and does not skip the question.
- **Authoritative until you change it.** A rename, a transfer, a changed config default, an unavailable agent or a different terminal never changes an association.
- **Unknown repositories stop.** On a terminal you are shown the repository and the available profiles and must pick one. Without a terminal (or with `--json`) the command fails with `unknown_repository_profile`, listing the available profiles. Nothing is launched and nothing is saved.
- **A removed profile is not replaced.** If the associated profile no longer exists in `config.json`, the result is `associated_profile_missing`; the repository is not moved to another profile.
- **Changing needs `--force`.** `profile set` refuses to overwrite a different profile. `--force` overwrites it, but it does **not** deal with existing sessions or worktrees under the old profile. The safe reassignment flow that does is a later ticket; until then treat `--force` as an expert escape hatch.

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

## What this does not protect against

Profiles choose which executable, arguments and environment an agent starts with. They are **not** operating-system isolation: any process you start runs as your user and can read what your account can read, including other profiles' configuration directories. See [profiles.md](profiles.md).

The state database is a plain SQLite file with your user's permissions. Anyone who can edit it can change associations.

## Related

- State store design: [ADR 0003](adr/0003-state-database.md).
- Commands: [profiles.md](profiles.md).
