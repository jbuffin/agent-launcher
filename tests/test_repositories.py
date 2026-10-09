import pytest

from agent_launcher import repositories
from agent_launcher.repositories import RepositoryError, identify_reference, normalize_remote


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:Owner/Repo.git",
        "git@GitHub.com:owner/repo",
        "https://github.com/owner/repo",
        "https://github.com/owner/repo.git",
        "https://github.com/owner/repo/",
        "https://user:token@github.com/Owner/Repo.git",
        "ssh://git@github.com/owner/repo.git",
        "ssh://git@github.com:22/owner/repo",
    ],
)
def test_github_remote_forms_normalise_to_one_string(url):
    assert normalize_remote(url) == "github.com/owner/repo"


def test_credentials_are_not_kept():
    assert "token" not in normalize_remote("https://user:token@example.com/a/b.git")


def test_non_github_hosts_keep_path_case():
    assert normalize_remote("git@git.example.com:Team/Repo.git") == "git.example.com/Team/Repo"


def test_local_path_remote_is_kept_as_is():
    assert normalize_remote("/srv/git/repo.git/") == "/srv/git/repo.git"


def test_path_identity_is_canonical_and_lists_remotes(make_repo, tmp_path, fake_github):
    repo = make_repo("one", "git@github.com:Owner/One.git")
    link = tmp_path / "link"
    link.symlink_to(repo)
    fake_github.add("owner/one", 111)
    identity = identify_reference(str(link / "."))
    assert identity.path == str(repo.resolve())
    assert identity.remotes == ("github.com/owner/one",)
    assert (identity.github_id, identity.node_id, identity.full_name) == (111, "R_node111", "owner/one")


def test_subdirectory_resolves_to_the_checkout_top_level(make_repo):
    repo = make_repo("one")
    (repo / "src").mkdir()
    assert identify_reference(str(repo / "src")).path == str(repo.resolve())


def test_not_a_git_repository(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(RepositoryError) as exc:
        identify_reference(str(plain))
    assert exc.value.code == "not_a_repository"


def test_owner_name_and_url_have_no_path(fake_github):
    fake_github.add("owner/one", 5)
    for ref in ("owner/one", "https://github.com/Owner/One", "git@github.com:owner/one.git"):
        identity = identify_reference(ref)
        assert identity.path is None and identity.github_id == 5
        assert identity.remotes == ("github.com/owner/one",)


def test_offline_identification_still_succeeds(make_repo, fake_github):
    fake_github.offline = True
    repo = make_repo("one", "https://github.com/owner/one")
    identity = identify_reference(str(repo))
    assert identity.github_id is None and identity.full_name == "owner/one"
    assert identity.path == str(repo.resolve())


def test_unknown_to_github_and_failures_are_soft(fake_github, make_repo):
    assert identify_reference("owner/missing").github_id is None  # 404

    def garbage(argv, timeout):
        return repositories.CommandResult(0, "not json", "")

    assert repositories.fetch_github_identity("o/n", garbage) is None
    assert repositories.fetch_github_identity("o/n", lambda a, t: repositories.CommandResult(0, '{"id": "7"}', "")) is None


def test_fetch_false_never_calls_gh(fake_github):
    identify_reference("owner/one", fetch_github=False)
    assert fake_github.calls == []


def test_non_github_remote_does_not_call_gh(make_repo, fake_github):
    repo = make_repo("one", "git@git.example.com:team/one.git")
    identity = identify_reference(str(repo))
    assert identity.github_id is None and fake_github.calls == []


@pytest.mark.parametrize("bad", ["", "   ", "not a repo", "a/b/c", "owner/.."])
def test_unrecognised_references(bad):
    with pytest.raises(RepositoryError) as exc:
        identify_reference(bad)
    assert exc.value.code == "invalid_repository"


def test_gh_asked_with_argv_list_and_jq(fake_github):
    identify_reference("owner/one")
    argv = fake_github.calls[0]
    assert argv[:3] == ["gh", "api", "repos/owner/one"] and "--jq" in argv


def test_dot_segments_never_reach_gh(fake_github):
    with pytest.raises(RepositoryError):
        identify_reference("owner/..")
    assert repositories.github_name("github.com/../..") is None
    assert fake_github.calls == []


def test_full_name_is_known_offline_from_the_primary_remote(make_repo):
    repo = make_repo("one", "git@github.com:Owner/One.git")
    assert identify_reference(str(repo), fetch_github=False).full_name == "owner/one"
