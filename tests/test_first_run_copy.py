"""One-time copy of `~/.agent-launcher` to `~/.commodore-launcher` (issue #13, acceptance 2 and 3).

Driven through the CLI callback with a real command (`version`, `profile list`); `--help` bypasses the callback.
`HOME` is a temp dir (the autouse `fake_default_home`) and `COMMODORE_LAUNCHER_HOME` is unset here, because the
autouse `launcher_home` fixture sets it for every other test.

Until the rename lands every test fails at `import commodore_launcher`, which is the right reason for this red phase.
"""

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

runner = CliRunner()
NO_COMMODORE_SKILLS = True  # these tests list the fake HOME's contents (see conftest.commodore_skills_installed)

SECRET = b"outside-the-old-home\n"


def invoke(*args: str):
    from commodore_launcher.cli import app

    return runner.invoke(app, list(args))


@pytest.fixture(autouse=True)
def home_variable_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COMMODORE_LAUNCHER_HOME", raising=False)


@pytest.fixture
def old_home(fake_default_home: Path) -> Path:
    return fake_default_home / ".agent-launcher"


@pytest.fixture
def new_home(fake_default_home: Path) -> Path:
    return fake_default_home / ".commodore-launcher"


CONFIG = {"version": 2, "profiles": {"work": {}, "personal": {}}}
# What the copy writes for it: the migrations to the current version have run inside the copy (issue #14).
MIGRATED_CONFIG = {"version": 4, "profiles": {"work": {}, "personal": {}}}

def _real_state_db() -> bytes:
    """A schema-10 `state.db` as the earlier release left it: the copy opens it (to archive its tasks), so it must be one."""
    import tempfile

    import old_state

    with tempfile.TemporaryDirectory() as scratch:
        old_state.build(Path(scratch) / "state.db", 10).close()
        return (Path(scratch) / "state.db").read_bytes()


COPIED = {
    "config.json": json.dumps(CONFIG).encode(),
    "workflows.json": b'{"version": 1, "workflows": []}\n',
    "state.db": _real_state_db(),
    "state.db-wal": b"\x37\x7f\x06\x82" + bytes(reversed(range(256))) * 4,
    "state.db-shm": b"\x00\x01shm" * 64,
    "templates/review.md": b"Review {{ url }}\n",
    "logs/launcher.log": b'{"event": "old run"}\n',
}

NOT_COPIED = {
    "worktrees/t-1/README.md": b"a task worktree\n",
    "maintenance/last-run.json": b"{}\n",
    "locks/t-1.lock": b"",
    "backups/config.json.bak": b"{}\n",
    "mock-terminal.json": b"{}\n",
}


def write_tree(root: Path, files: dict[str, bytes]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def snapshot(root: Path) -> dict[str, tuple]:
    """Everything observable about a tree: names, kinds, bytes and mtimes."""
    seen: dict[str, tuple] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            seen[rel] = ("link", os.readlink(path))
        elif path.is_dir():
            seen[rel] = ("dir",)
        else:
            seen[rel] = ("file", path.read_bytes(), path.stat().st_mtime_ns)
    return seen


@pytest.fixture
def full_old_home(old_home: Path) -> Path:
    write_tree(old_home, {**COPIED, **NOT_COPIED})
    return old_home


def copy_lines(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if ".agent-launcher" in line and ".commodore-launcher" in line]


def test_first_command_copies_the_five_items_and_says_so_once_on_stderr(full_old_home, new_home):
    from commodore_launcher import __version__

    result = invoke("version")
    assert result.exit_code == 0
    assert result.stdout.strip() == f"commodore {__version__}"
    for name, content in COPIED.items():
        if name == "logs/launcher.log":
            assert (new_home / name).read_bytes().startswith(content)
        elif name == "config.json":
            assert json.loads((new_home / name).read_text()) == MIGRATED_CONFIG
        elif name.startswith("state.db"):
            continue  # the copy migrates and archives its state database, so its bytes change (see below)
        else:
            assert (new_home / name).read_bytes() == content, name
    assert len(copy_lines(result.stderr)) == 1
    assert len([line for line in result.stderr.splitlines() if line.strip()]) == 1


def test_only_the_five_items_are_copied(full_old_home, new_home):
    assert invoke("version").exit_code == 0
    for name in NOT_COPIED:
        assert not (new_home / name).exists(), name
    for directory in ("worktrees", "maintenance", "locks", "backups"):
        assert not (new_home / directory).exists(), directory


def test_the_old_home_is_byte_for_byte_unchanged_including_its_wal(full_old_home):
    before = snapshot(full_old_home)
    assert "state.db-wal" in before
    assert invoke("version").exit_code == 0
    assert snapshot(full_old_home) == before
    assert invoke("version").exit_code == 0
    assert snapshot(full_old_home) == before


def test_no_temporary_sibling_is_left_in_the_user_home(full_old_home, fake_default_home):
    assert invoke("version").exit_code == 0
    assert sorted(p.name for p in fake_default_home.iterdir()) == [".agent-launcher", ".commodore-launcher"]


def test_second_command_copies_nothing_and_prints_nothing_about_it(full_old_home, new_home):
    assert invoke("version").exit_code == 0
    marker = new_home / "config.json"
    marker.write_text('{"version": 4, "profiles": {"edited": {}}}')
    (full_old_home / "templates" / "added-later.md").write_text("new in the old home\n")
    second = invoke("version")
    assert second.exit_code == 0
    assert copy_lines(second.stderr) == [] and ".agent-launcher" not in second.stderr
    assert json.loads(marker.read_text())["profiles"] == {"edited": {}}
    assert not (new_home / "templates" / "added-later.md").exists()


def test_no_old_home_means_no_copy_and_no_message(fake_default_home, old_home):
    result = invoke("version")
    assert result.exit_code == 0
    assert ".agent-launcher" not in result.stderr
    assert not old_home.exists()


def test_with_the_home_variable_set_no_copy_is_made(full_old_home, fake_default_home, monkeypatch):
    elsewhere = fake_default_home / "elsewhere"
    monkeypatch.setenv("COMMODORE_LAUNCHER_HOME", str(elsewhere))
    before = snapshot(full_old_home)
    result = invoke("version")
    assert result.exit_code == 0
    assert not (fake_default_home / ".commodore-launcher").exists()
    assert not (elsewhere / "config.json").exists() and not (elsewhere / "state.db").exists()
    assert ".agent-launcher" not in result.stderr
    assert snapshot(full_old_home) == before


def test_an_empty_home_variable_counts_as_unset(full_old_home, new_home, monkeypatch):
    monkeypatch.setenv("COMMODORE_LAUNCHER_HOME", "")
    assert invoke("version").exit_code == 0
    assert json.loads((new_home / "config.json").read_text()) == MIGRATED_CONFIG


def test_an_existing_new_home_is_left_alone(full_old_home, new_home):
    new_home.mkdir()
    (new_home / "config.json").write_text('{"version": 3}')
    result = invoke("version")
    assert result.exit_code == 0
    assert copy_lines(result.stderr) == []
    assert sorted(p.name for p in new_home.iterdir() if p.name != "logs") == ["config.json"]
    assert (new_home / "config.json").read_text() == '{"version": 3}'


def test_a_partial_old_home_copies_cleanly(old_home, new_home):
    write_tree(old_home, {"config.json": COPIED["config.json"]})
    result = invoke("profile", "list", "--json")
    assert result.exit_code == 0
    assert [p["name"] for p in json.loads(result.stdout)["profiles"]] == ["personal", "work"]
    assert json.loads((new_home / "config.json").read_text()) == MIGRATED_CONFIG
    assert len(copy_lines(result.stderr)) == 1


def test_symlinks_are_not_followed_out_of_the_old_home(old_home, new_home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(SECRET)
    write_tree(old_home, {"config.json": COPIED["config.json"], "templates/real.md": b"kept\n"})
    (old_home / "templates" / "peek.md").symlink_to(outside / "secret.txt")
    (old_home / "logs").mkdir()
    (old_home / "logs" / "peek-dir").symlink_to(outside, target_is_directory=True)
    (old_home / "workflows.json").symlink_to(outside / "secret.txt")
    before = snapshot(old_home)
    assert invoke("version").exit_code == 0
    assert json.loads((new_home / "config.json").read_text()) == MIGRATED_CONFIG
    assert (new_home / "templates" / "real.md").read_bytes() == b"kept\n"
    for root, _dirs, files in os.walk(new_home, followlinks=False):
        for name in files:
            path = Path(root, name)
            assert path.is_symlink() or path.read_bytes() != SECRET, f"followed a link out of the old home: {path}"
    assert not any(p.is_dir() and not p.is_symlink() for p in [new_home / "logs" / "peek-dir"])
    assert snapshot(old_home) == before
    assert (outside / "secret.txt").read_bytes() == SECRET


def test_the_copy_line_goes_to_stderr_so_json_stdout_stays_one_object(full_old_home):
    result = invoke("profile", "list", "--json")
    assert result.exit_code == 0
    assert [p["name"] for p in json.loads(result.stdout)["profiles"]] == ["personal", "work"]
    assert len(copy_lines(result.stderr)) == 1
    assert ".commodore-launcher" not in result.stdout


def test_the_copy_happens_on_the_first_run_with_debug(full_old_home, new_home):
    """`--debug` sets up logging, which would create the new home; the copy must come first."""
    result = invoke("--debug", "version")
    assert result.exit_code == 0
    assert json.loads((new_home / "config.json").read_text()) == MIGRATED_CONFIG
    assert (new_home / "state.db").is_file()  # migrated and archived in the copy, so not byte-identical
    assert len(copy_lines(result.stderr)) == 1


def test_profiles_and_repository_associations_from_the_old_home_are_in_effect(
    old_home, new_home, make_repo, fake_github, monkeypatch
):
    fake_github.add("o/one", 3)
    repo = make_repo("one", "https://github.com/o/one")
    # Build a real old home through the public CLI, with its home variable pointing at the old directory.
    old_home.mkdir()
    (old_home / "config.json").write_text(json.dumps(MIGRATED_CONFIG))
    monkeypatch.setenv("COMMODORE_LAUNCHER_HOME", str(old_home))
    assert invoke("profile", "set", str(repo), "work").exit_code == 0
    assert (old_home / "state.db").is_file()
    monkeypatch.delenv("COMMODORE_LAUNCHER_HOME")
    before = snapshot(old_home)

    profiles = invoke("profile", "list", "--json")
    assert profiles.exit_code == 0
    assert [p["name"] for p in json.loads(profiles.stdout)["profiles"]] == ["personal", "work"]
    assert len(copy_lines(profiles.stderr)) == 1

    which = invoke("profile", "which", str(repo), "--json")
    assert which.exit_code == 0
    assert json.loads(which.stdout)["profile"] == "work"
    assert json.loads(which.stdout)["newly_associated"] is False
    assert (new_home / "state.db").read_bytes() != b""
    assert snapshot(old_home) == before


# --- failures, a repointed worktree root, symlinked homes --------------------------------------------


def leftovers(home: Path) -> list[str]:
    return sorted(p.name for p in home.iterdir() if p.name not in {".agent-launcher", ".commodore-launcher"})


def test_a_failed_copy_stops_the_command_and_is_retried_after_the_fix(full_old_home, new_home, fake_default_home):
    unreadable = full_old_home / "templates" / "review.md"
    before = snapshot(full_old_home)
    unreadable.chmod(0)
    try:
        result = invoke("profile", "add", "work", "--json")
        assert result.exit_code == 1
        assert result.stdout == ""
        assert "error:" in result.stderr and str(unreadable) in result.stderr
        assert not new_home.exists()
        assert leftovers(fake_default_home) == []
    finally:
        unreadable.chmod(0o644)
    assert snapshot(full_old_home) == before
    fixed = invoke("version")
    assert fixed.exit_code == 0 and len(copy_lines(fixed.stderr)) == 1
    assert (new_home / "templates" / "review.md").read_bytes() == COPIED["templates/review.md"]


def test_an_interruption_mid_copy_leaves_no_staging_directory(full_old_home, new_home, fake_default_home, monkeypatch):
    from commodore_launcher import paths

    real = paths._copy_entry
    calls = []

    def interrupted(source, target):
        calls.append(source)
        if len(calls) == 3:
            raise KeyboardInterrupt
        real(source, target)

    monkeypatch.setattr(paths, "_copy_entry", interrupted)
    with pytest.raises(KeyboardInterrupt):
        paths.copy_old_home(lambda config_file: None)
    assert not new_home.exists() and leftovers(fake_default_home) == []


def write_config(old_home: Path, repositories: dict) -> dict:
    config = {"version": 2, "profiles": {"work": {}}, "repositories": repositories}
    write_tree(old_home, {"config.json": json.dumps(config).encode()})
    return config


@pytest.mark.parametrize("content", [b"{ not json", b"[1, 2]", b'{"repositories": "x"}', b'{"repositories": {}}'])
def test_a_config_that_is_not_what_we_expect_is_copied_byte_for_byte(old_home, new_home, content):
    write_tree(old_home, {"config.json": content})
    assert invoke("version").exit_code == 0
    assert (new_home / "config.json").read_bytes() == content


def test_a_symlinked_old_home_is_resolved_once_and_copied(fake_default_home, new_home, tmp_path):
    dotfiles = tmp_path / "dotfiles" / "agent-launcher-home"
    write_tree(dotfiles, COPIED)
    (fake_default_home / ".agent-launcher").symlink_to(dotfiles, target_is_directory=True)
    before = snapshot(dotfiles)
    result = invoke("version")
    assert result.exit_code == 0 and len(copy_lines(result.stderr)) == 1
    assert json.loads((new_home / "config.json").read_text()) == MIGRATED_CONFIG
    assert (new_home / "logs" / "launcher.log").read_bytes() == COPIED["logs/launcher.log"]
    assert not (new_home).is_symlink()
    assert snapshot(dotfiles) == before


def test_links_inside_a_symlinked_old_home_are_still_not_followed(fake_default_home, new_home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(SECRET)
    dotfiles = tmp_path / "dotfiles"
    write_tree(dotfiles, {"config.json": COPIED["config.json"]})
    (dotfiles / "templates").symlink_to(outside, target_is_directory=True)
    (fake_default_home / ".agent-launcher").symlink_to(dotfiles, target_is_directory=True)
    assert invoke("version").exit_code == 0
    assert (new_home / "config.json").is_file()
    assert not (new_home / "templates").exists()


def test_a_symlinked_templates_directory_and_nested_links_are_not_followed(old_home, new_home, tmp_path):
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "secret.txt").write_bytes(SECRET)
    write_tree(old_home, {"config.json": COPIED["config.json"], "logs/a/b/real.log": b"x\n"})
    (old_home / "logs" / "a" / "b" / "deep-link").symlink_to(outside / "sub", target_is_directory=True)
    (old_home / "logs" / "a" / "file-link").symlink_to(outside / "sub" / "secret.txt")
    (old_home / "templates").symlink_to(outside, target_is_directory=True)
    before = snapshot(old_home)
    assert invoke("version").exit_code == 0
    assert not (new_home / "templates").exists()
    assert (new_home / "logs" / "a" / "b" / "real.log").read_bytes() == b"x\n"
    for root, _dirs, files in os.walk(new_home, followlinks=False):
        for name in files:
            assert Path(root, name).read_bytes() != SECRET
    assert not (new_home / "logs" / "a" / "b" / "deep-link").exists()
    assert snapshot(old_home) == before


def test_a_read_only_old_config_is_copied_and_migrated_in_the_copy(old_home, new_home):
    config = write_config(old_home, {"search_roots": ["~/code"]})
    raw = (old_home / "config.json").read_bytes()
    (old_home / "config.json").chmod(0o444)
    before = snapshot(old_home)
    mode = (old_home / "config.json").stat().st_mode
    try:
        result = invoke("version")
        assert result.exit_code == 0 and len(copy_lines(result.stderr)) == 1
        copied = json.loads((new_home / "config.json").read_text())
        assert copied["version"] == 4 and copied["repositories"] == config["repositories"]
        assert copied["profiles"] == config["profiles"]
        assert (old_home / "config.json").read_bytes() == raw
        assert (old_home / "config.json").stat().st_mode == mode
        assert snapshot(old_home) == before
    finally:
        (old_home / "config.json").chmod(0o644)


def test_an_unreadable_directory_fails_the_copy_the_same_way(full_old_home, new_home, fake_default_home):
    unreadable = full_old_home / "logs"
    before = snapshot(full_old_home)
    unreadable.chmod(0)
    try:
        result = invoke("version")
        assert result.exit_code == 1 and "error:" in result.stderr and str(unreadable) in result.stderr
        assert "COMMODORE_LAUNCHER_HOME" in result.stderr
        assert not new_home.exists() and leftovers(fake_default_home) == []
    finally:
        unreadable.chmod(0o755)
    assert snapshot(full_old_home) == before
    assert invoke("version").exit_code == 0 and (new_home / "logs" / "launcher.log").is_file()
