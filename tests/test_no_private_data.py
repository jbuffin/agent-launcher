"""Tracked files hold no real home paths and none of the developer's private terms (AGENTS.md, "no private data").

Home paths must use a placeholder user. Private terms (employer, private repositories) are read from an untracked
`.private-terms` file at the repository root, one per line, so the list itself is never published.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER_USERS = {"me", "alice", "bob", "eve", "u", "x", "config.json"}
HOME_PATH = re.compile(r"/(?:Users|home)/([A-Za-z0-9_.-]+)")


def tracked_texts() -> list[tuple[str, str]]:
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, timeout=30)
    if listed.returncode != 0:
        pytest.skip("not a git checkout")
    texts = []
    for name in filter(None, listed.stdout.split("\0")):
        try:
            texts.append((name, (ROOT / name).read_text(encoding="utf-8")))
        except (UnicodeDecodeError, FileNotFoundError):
            continue
    return texts


def test_home_paths_use_placeholder_users():
    found = {
        f"{name}: /…/{user}"
        for name, text in tracked_texts()
        for user in HOME_PATH.findall(text)
        if user not in PLACEHOLDER_USERS
    }
    assert not found, "real-looking home paths; use a placeholder user:\n" + "\n".join(sorted(found))


def test_no_private_terms():
    terms_file = ROOT / ".private-terms"
    if not terms_file.exists():
        pytest.skip("no .private-terms file")
    terms = [t.strip().lower() for t in terms_file.read_text().splitlines() if t.strip() and not t.startswith("#")]
    found = {name for name, text in tracked_texts() for term in terms if term in text.lower()}
    assert not found, "files contain a term from .private-terms:\n" + "\n".join(sorted(found))
