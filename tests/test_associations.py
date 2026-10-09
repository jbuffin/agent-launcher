import subprocess

import pytest

from agent_launcher import state
from agent_launcher.associations import AssociationError, ensure_profile, find_repository, get_association, set_profile
from agent_launcher.repositories import identify_reference
from scripted import CANCEL, ScriptedPrompter

PROFILES = ["personal", "work"]


@pytest.fixture
def conn():
    connection = state.connect()
    yield connection
    connection.close()


def test_unknown_repository_has_no_profile_and_nothing_is_assigned(conn, make_repo):
    identity = identify_reference(str(make_repo("one", "https://github.com/o/one")))
    assert get_association(conn, identity) is None
    assert conn.execute("SELECT count(*) FROM profile_associations").fetchone()[0] == 0


def test_unknown_repository_non_interactive_is_a_structured_error(conn, make_repo):
    identity = identify_reference(str(make_repo("one", "https://github.com/o/one")))
    with pytest.raises(AssociationError) as exc:
        ensure_profile(conn, identity, PROFILES, prompter=None)
    error = exc.value.to_dict()["error"]
    assert error["code"] == "unknown_repository_profile"
    assert error["available_profiles"] == PROFILES
    assert error["suggested_profile"] is None
    assert conn.execute("SELECT count(*) FROM profile_associations").fetchone()[0] == 0


def test_suggestion_is_reported_but_never_assigned(conn, make_repo):
    identity = identify_reference(str(make_repo("one", "https://github.com/work-org/one")))
    with pytest.raises(AssociationError) as exc:
        ensure_profile(conn, identity, PROFILES, prompter=None, suggested="work")
    assert exc.value.details["suggested_profile"] == "work"
    assert get_association(conn, identity) is None


def test_suggestion_for_a_missing_profile_is_dropped(conn, make_repo):
    identity = identify_reference(str(make_repo("one")))
    with pytest.raises(AssociationError) as exc:
        ensure_profile(conn, identity, PROFILES, prompter=None, suggested="ghost")
    assert exc.value.details["suggested_profile"] is None


def test_interactive_choice_is_saved_and_reused_without_asking(conn, make_repo):
    identity = identify_reference(str(make_repo("one")))
    prompter = ScriptedPrompter(("select", "Which profile", "work"))
    resolved = ensure_profile(conn, identity, PROFILES, prompter)
    prompter.done()
    assert (resolved.profile, resolved.newly_associated) == ("work", True)
    again = ensure_profile(conn, identity, PROFILES, ScriptedPrompter())  # any prompt would fail
    assert (again.profile, again.newly_associated) == ("work", False)


def test_prompt_marks_the_suggestion_and_offers_every_profile(conn, make_repo):
    seen = {}

    class Spy(ScriptedPrompter):
        def select(self, message, choices, default=None):
            seen["labels"] = [c.label for c in choices]
            seen["default"] = default
            return super().select(message, choices, default)

    identity = identify_reference(str(make_repo("one")))
    ensure_profile(conn, identity, PROFILES, Spy(("select", "profile", "personal")), suggested="work")
    assert seen == {"labels": ["personal", "work (suggested)"], "default": "work"}


def test_no_default_when_nothing_is_suggested(conn, make_repo):
    seen = {}

    class Spy(ScriptedPrompter):
        def select(self, message, choices, default=None):
            seen["default"] = default
            return super().select(message, choices, default)

    ensure_profile(conn, identify_reference(str(make_repo("one"))), PROFILES, Spy(("select", "profile", "work")))
    assert seen["default"] is None


def test_no_profiles_configured_is_an_error_not_a_default(conn, make_repo):
    with pytest.raises(AssociationError) as exc:
        ensure_profile(conn, identify_reference(str(make_repo("one"))), [], prompter=None)
    assert exc.value.code == "no_profiles"


def test_association_survives_a_restart(launcher_home, make_repo):
    identity = identify_reference(str(make_repo("one", "https://github.com/o/one")))
    with state.open_state() as conn:
        set_profile(conn, identity, "work")
    with state.open_state() as conn:  # a new connection, as after a restart
        assert get_association(conn, identity).profile == "work"


def test_set_profile_refuses_to_change_without_force(conn, make_repo):
    identity = identify_reference(str(make_repo("one")))
    set_profile(conn, identity, "work")
    with pytest.raises(AssociationError) as exc:
        set_profile(conn, identity, "personal")
    assert exc.value.code == "association_exists"
    assert "later release" in exc.value.message
    assert get_association(conn, identity).profile == "work"


def test_set_profile_same_profile_is_a_no_op(conn, make_repo):
    identity = identify_reference(str(make_repo("one")))
    set_profile(conn, identity, "work")
    assert set_profile(conn, identity, "work").changed is False


def test_force_changes_the_association(conn, make_repo):
    identity = identify_reference(str(make_repo("one")))
    set_profile(conn, identity, "work")
    result = set_profile(conn, identity, "personal", force=True)
    assert (result.previous, result.changed) == ("work", True)
    assert get_association(conn, identity).profile == "personal"


def test_association_whose_profile_was_removed_is_not_reassigned(conn, make_repo):
    identity = identify_reference(str(make_repo("one")))
    set_profile(conn, identity, "work")
    with pytest.raises(AssociationError) as exc:
        ensure_profile(conn, identity, ["personal"], ScriptedPrompter())
    assert exc.value.code == "associated_profile_missing"
    assert get_association(conn, identity).profile == "work"


# --- identity: renames, transfers, lookup order -------------------------------------------


def test_rename_keeps_the_association_by_github_id(conn, make_repo, fake_github):
    fake_github.add("owner/old", 42)
    repo = make_repo("one", "git@github.com:owner/old.git")
    set_profile(conn, identify_reference(str(repo)), "work")

    # GitHub rename + transfer: same ID, new owner/name. The checkout's remote is updated.
    fake_github.rename("owner/old", "neworg/new")
    subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin", "https://github.com/neworg/new"], check=True)
    renamed = identify_reference(str(repo))
    assert renamed.remotes == ("github.com/neworg/new",)
    assert get_association(conn, renamed).profile == "work"

    # A different clone path, found only by name and ID, is the same repository too.
    by_name = identify_reference("neworg/new")
    assert by_name.path is None
    assert get_association(conn, by_name).profile == "work"
    assert conn.execute("SELECT count(*) FROM repositories").fetchone()[0] == 1
    assert conn.execute("SELECT full_name FROM repositories").fetchone()[0] == "neworg/new"


def test_id_wins_over_path_and_remotes(conn, make_repo, fake_github):
    fake_github.add("a/one", 1)
    fake_github.add("b/two", 2)
    one = identify_reference(str(make_repo("one", "https://github.com/a/one")))
    two = identify_reference(str(make_repo("two", "https://github.com/b/two")))
    set_profile(conn, one, "work")
    set_profile(conn, two, "personal")
    # Identity claiming id 1 but carrying repo two's path and remote still resolves by id.
    confused = type(one)(path=two.path, remotes=two.remotes, github_id=1)
    assert find_repository(conn, confused) == find_repository(conn, one)


def test_reused_name_with_a_different_id_does_not_inherit_the_profile(conn, make_repo, fake_github):
    fake_github.add("owner/name", 1)
    set_profile(conn, identify_reference("owner/name"), "work")
    # The repository is renamed away; someone else creates a new owner/name (different ID).
    fake_github.rename("owner/name", "owner/moved")
    fake_github.add("owner/name", 2)
    newcomer = identify_reference("owner/name")
    assert newcomer.github_id == 2
    assert get_association(conn, newcomer) is None


def test_reused_path_with_a_different_id_does_not_inherit_the_profile(conn, make_repo, fake_github):
    fake_github.add("o/one", 1)
    fake_github.add("o/other", 2)
    repo = make_repo("one", "https://github.com/o/one")
    set_profile(conn, identify_reference(str(repo)), "work")
    subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin", "https://github.com/o/other"], check=True)
    assert get_association(conn, identify_reference(str(repo))) is None


def test_offline_falls_back_to_path_and_remotes(conn, make_repo, fake_github):
    fake_github.add("o/one", 9)
    repo = make_repo("one", "https://github.com/o/one")
    set_profile(conn, identify_reference(str(repo)), "work")
    fake_github.offline = True
    offline = identify_reference(str(repo))
    assert offline.github_id is None
    assert get_association(conn, offline).profile == "work"


def test_offline_association_is_not_given_a_newcomers_id_when_online(conn, make_repo, fake_github):
    """A row stored without an ID cannot be proven to be the repository GitHub now reports."""
    fake_github.offline = True
    repo = make_repo("one", "https://github.com/o/one")
    set_profile(conn, identify_reference(str(repo)), "work")
    fake_github.offline = False
    fake_github.add("o/one", 9)
    online = identify_reference(str(repo))
    assert get_association(conn, online) is None
    assert conn.execute("SELECT github_id FROM repositories").fetchone()[0] is None
    with pytest.raises(AssociationError) as exc:
        ensure_profile(conn, online, PROFILES, prompter=None)
    assert exc.value.code == "unknown_repository_profile"


def test_ambiguous_match_is_an_error_not_a_guess(conn, make_repo, fake_github):
    # An association stored offline (no ID) and one stored online for the same remote.
    fake_github.add("o/one", 9)
    fake_github.offline = True
    offline_row = identify_reference("o/one")
    set_profile(conn, offline_row, "work")
    fake_github.offline = False
    set_profile(conn, identify_reference("o/one"), "personal")
    assert conn.execute("SELECT count(*) FROM repositories").fetchone()[0] == 2
    with pytest.raises(AssociationError) as exc:
        get_association(conn, offline_row)  # offline again: both rows have this remote
    assert exc.value.code == "ambiguous_repository"
    assert get_association(conn, identify_reference("o/one")).profile == "personal"  # online resolves it


def test_same_remote_in_two_clones_is_one_repository(conn, make_repo):
    a = identify_reference(str(make_repo("a", "git@github.com:o/x.git")))
    b = identify_reference(str(make_repo("b", "https://github.com/o/x")))
    set_profile(conn, a, "work")
    assert get_association(conn, b).profile == "work"


def test_owner_is_never_used_to_infer_a_profile(conn, make_repo):
    set_profile(conn, identify_reference(str(make_repo("a", "https://github.com/acme/a"))), "work")
    sibling = identify_reference(str(make_repo("b", "https://github.com/acme/b")))
    assert get_association(conn, sibling) is None


# --- one primary remote identifies a checkout --------------------------------------------


def _fork(make_repo):
    repo = make_repo("fork", "git@github.com:me/widget.git")
    subprocess.run(["git", "-C", str(repo), "remote", "add", "upstream", "https://github.com/acme/widget"], check=True)
    return repo


@pytest.mark.parametrize("offline", [True, False])
def test_fork_does_not_inherit_the_upstream_profile(conn, make_repo, fake_github, offline):
    fake_github.add("acme/widget", 1)
    fake_github.add("me/widget", 2)
    set_profile(conn, identify_reference("acme/widget"), "work")
    fake_github.offline = offline
    fork = identify_reference(str(_fork(make_repo)))
    assert fork.remotes == ("github.com/me/widget",)
    assert fork.other_remotes == ("github.com/acme/widget",)
    assert fork.full_name == "me/widget"
    assert get_association(conn, fork) is None
    with pytest.raises(AssociationError) as exc:
        ensure_profile(conn, fork, PROFILES, prompter=None)
    assert exc.value.code == "unknown_repository_profile"
    # Nothing leaked onto the work repository's row.
    assert get_association(conn, identify_reference("acme/widget")).profile == "work"
    assert get_association(conn, identify_reference("me/widget")) is None
    assert conn.execute("SELECT count(*) FROM repository_remotes WHERE url = 'github.com/me/widget'").fetchone()[0] == 0


def test_gh_is_asked_about_the_primary_remote_only(make_repo, fake_github):
    fake_github.add("acme/widget", 1)
    fake_github.add("me/widget", 2)
    identity = identify_reference(str(_fork(make_repo)))
    assert identity.github_id == 2
    assert [c[2] for c in fake_github.calls] == ["repos/me/widget"]


def test_primary_remote_falls_back_to_the_branch_upstream_remote(make_repo):
    repo = make_repo("fork")
    subprocess.run(["git", "-C", str(repo), "remote", "add", "mine", "https://github.com/me/widget"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "other", "https://github.com/acme/widget"], check=True)
    assert identify_reference(str(repo), fetch_github=False).remotes == ()  # no origin, no upstream: path only
    branch = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "config", f"branch.{branch}.remote", "mine"], check=True)
    identity = identify_reference(str(repo), fetch_github=False)
    assert identity.remotes == ("github.com/me/widget",)
    assert identity.other_remotes == ("github.com/acme/widget",)


def test_origin_wins_over_the_branch_upstream(make_repo):
    repo = _fork(make_repo)
    branch = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "config", f"branch.{branch}.remote", "upstream"], check=True)
    assert identify_reference(str(repo), fetch_github=False).remotes == ("github.com/me/widget",)


def test_path_only_checkouts_are_matched_by_path(conn, make_repo):
    repo = make_repo("bare")
    set_profile(conn, identify_reference(str(repo)), "work")
    assert get_association(conn, identify_reference(str(repo))).profile == "work"


def test_changed_origin_without_an_id_is_not_matched_by_path(conn, make_repo, fake_github):
    fake_github.offline = True
    repo = make_repo("one", "https://github.com/o/one")
    set_profile(conn, identify_reference(str(repo)), "work")
    subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin", "https://github.com/someone/else"], check=True)
    assert get_association(conn, identify_reference(str(repo))) is None


# --- path matches need the remote state to agree ------------------------------------------


@pytest.mark.parametrize("offline", [True, False])
def test_local_only_repo_replaced_by_a_clone_at_the_same_path_is_unknown(conn, make_repo, fake_github, offline):
    fake_github.add("o/one", 1)
    fake_github.offline = offline
    repo = make_repo("one")  # local only
    set_profile(conn, identify_reference(str(repo)), "work")
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/o/one"], check=True)
    clone = identify_reference(str(repo))
    assert clone.remotes
    assert get_association(conn, clone) is None
    with pytest.raises(AssociationError) as exc:
        ensure_profile(conn, clone, PROFILES, prompter=None)
    assert exc.value.code == "unknown_repository_profile"


@pytest.mark.parametrize("offline", [True, False])
def test_clone_replaced_by_git_init_at_the_same_path_is_unknown(conn, make_repo, fake_github, offline):
    fake_github.add("o/one", 1)
    fake_github.offline = offline
    repo = make_repo("one", "https://github.com/o/one")
    set_profile(conn, identify_reference(str(repo)), "work")
    subprocess.run(["git", "-C", str(repo), "remote", "remove", "origin"], check=True)  # also "origin removed"
    fresh = identify_reference(str(repo))
    assert fresh.remotes == ()
    assert get_association(conn, fresh) is None
    with pytest.raises(AssociationError) as exc:
        ensure_profile(conn, fresh, PROFILES, prompter=None)
    assert exc.value.code == "unknown_repository_profile"


def test_path_match_still_works_when_both_sides_agree(conn, make_repo, fake_github):
    fake_github.offline = True
    with_remote = make_repo("a", "https://github.com/o/a")
    without = make_repo("b")
    set_profile(conn, identify_reference(str(with_remote)), "work")
    set_profile(conn, identify_reference(str(without)), "personal")
    assert get_association(conn, identify_reference(str(with_remote))).profile == "work"
    assert get_association(conn, identify_reference(str(without))).profile == "personal"


# --- Recording a missing GitHub ID: only `set_profile`, only for the exact path (ticket #17) -----------------


def _stored_without_id(conn, make_repo, name="one"):
    path = make_repo(name, f"https://github.com/o/{name}")
    offline = identify_reference(str(path), fetch_github=False)
    set_profile(conn, offline, "work")
    return path, offline


def _with_id(identity, github_id=7):
    from dataclasses import replace

    return replace(identity, github_id=github_id, node_id=f"R_{github_id}", full_name=identity.full_name)


def test_adopt_id_records_it_on_the_row_of_that_exact_path(conn, make_repo):
    from agent_launcher.associations import _adopt_id

    _, offline = _stored_without_id(conn, make_repo)
    assert _adopt_id(conn, _with_id(offline)) is True
    assert conn.execute("SELECT id, github_id, node_id FROM repositories").fetchall() == [(1, 7, "R_7")]


def test_adopt_id_skips_when_the_id_belongs_to_another_row(conn, make_repo):
    from agent_launcher.associations import _adopt_id

    _, offline = _stored_without_id(conn, make_repo)
    other = identify_reference(str(make_repo("two", "https://github.com/o/two")), fetch_github=False)
    set_profile(conn, _with_id(other, 7), "work")
    assert _adopt_id(conn, _with_id(offline, 7)) is False
    assert conn.execute("SELECT github_id FROM repositories ORDER BY id").fetchall() == [(None,), (7,)]


def test_adopt_id_skips_when_the_remote_disagrees(conn, make_repo):
    import subprocess
    from pathlib import Path

    from agent_launcher.associations import _adopt_id

    path, offline = _stored_without_id(conn, make_repo)
    subprocess.run(["git", "-C", str(path), "remote", "set-url", "origin", "https://github.com/o/moved-elsewhere"], check=True)
    changed = _with_id(identify_reference(str(path), fetch_github=False))
    assert changed.remotes != offline.remotes
    assert _adopt_id(conn, changed) is False
    assert conn.execute("SELECT github_id FROM repositories").fetchall() == [(None,)]


def test_adopt_id_skips_when_several_id_less_rows_have_the_path(conn, make_repo):
    from agent_launcher.associations import _adopt_id

    _, offline = _stored_without_id(conn, make_repo)
    conn.execute("INSERT INTO repositories (github_id, node_id, full_name, created_at, updated_at) VALUES (NULL, NULL, 'x/y', 'n', 'n')")
    conn.execute("INSERT INTO repository_paths (repository_id, path) VALUES (2, ?)", (offline.path,))
    conn.commit()
    assert _adopt_id(conn, _with_id(offline)) is False
    assert conn.execute("SELECT github_id FROM repositories").fetchall() == [(None,), (None,)]


def test_adopt_id_needs_an_id_and_a_path(conn, make_repo):
    from dataclasses import replace

    from agent_launcher.associations import _adopt_id

    _, offline = _stored_without_id(conn, make_repo)
    assert _adopt_id(conn, offline) is False
    assert _adopt_id(conn, replace(_with_id(offline), path=None)) is False
