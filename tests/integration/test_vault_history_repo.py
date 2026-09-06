"""Opt-in acceptance pass for the history tools against a **real** vault repo.

Skipped unless `VAULT_HISTORY_TEST_REPO` names a git working tree, in the same
shape as `PGVECTOR_TEST_ADMIN_URL` guards the pgvector modules in this
directory: CI has no such repository and the vault this was written for is
private, so nothing here may be a committed dependency.

    VAULT_HISTORY_TEST_REPO=/path/to/obsidian-vault \\
        pytest -q tests/integration/test_vault_history_repo.py

**These tests read only.** They run `git log`, `git blame` and `git log -S`
through the tools' own code path and assert *shape*, never content: which note
exists, who wrote it and what it says are properties of somebody's vault, and
an assertion about any of them would be both unportable and a leak. What they
check is that the three tools answer at all on a real reconstructed history,
that the birth commit resolves, that timestamps come back as ISO 8601 with an
offset, and that a whole-vault pickaxe returns within the tools' own bounds.

The note they run against is chosen at runtime: the most recently committed
markdown file the repository tracks. Nothing is hard-coded about the vault's
layout.
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.services import git_history  # noqa: E402

VAULT_HISTORY_TEST_REPO = os.environ.get("VAULT_HISTORY_TEST_REPO")

requires_vault_repo = pytest.mark.skipif(
    not VAULT_HISTORY_TEST_REPO,
    reason="set VAULT_HISTORY_TEST_REPO to a git working tree to run these",
)

pytestmark = [
    requires_vault_repo,
    pytest.mark.skipif(
        shutil.which("git") is None, reason="the history tools need a git executable"
    ),
]

# `2019-03-04T09:12:00+01:00` or `…Z`. The offset is the point: a history
# reconstructed from file timestamps is worthless if the tools re-render it in
# the server's timezone.
ISO_WITH_OFFSET = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)")


@pytest.fixture(autouse=True)
def _no_usage_log(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(tools, "_log_usage", _noop)


@pytest.fixture
def vault(monkeypatch):
    root = Path(VAULT_HISTORY_TEST_REPO).expanduser().resolve()
    if not (root / ".git").exists():
        pytest.skip(f"{root} is not a git working tree")
    monkeypatch.setattr(tools.settings, "vault_path", str(root))
    return root


def _a_tracked_note(root: Path) -> str:
    """The most recently committed markdown file, vault-relative.

    Picked from the repository rather than named, so this module carries no
    knowledge of anybody's vault. Skips rather than fails when there is no
    markdown at all — that is a property of the repository handed in, not a
    defect in the tools.
    """
    done = subprocess.run(
        [
            shutil.which("git"),
            "-C", str(root),
            "log", "-1", "--name-only", "--pretty=format:", "--", "*.md",
        ],
        capture_output=True,
        text=True,
    )
    for line in done.stdout.splitlines():
        candidate = line.strip()
        if candidate.endswith(".md") and (root / candidate).is_file():
            return candidate
    pytest.skip("no committed markdown file found in VAULT_HISTORY_TEST_REPO")


async def test_the_repository_resolves(vault):
    repo = git_history.resolve_repo(vault)

    assert repo.toplevel
    assert repo.prefix == "" or repo.prefix.endswith("/")


async def test_history_answers_with_a_birth_commit_and_offset_timestamps(vault):
    note = _a_tracked_note(vault)

    result = await tools.note_history_impl(note, limit=20)

    assert "No git history" not in result
    assert "**Created**" in result, result[:400]
    created = next(line for line in result.splitlines() if line.startswith("**Created**"))
    assert ISO_WITH_OFFSET.search(created), created
    assert len(result) <= tools.settings.max_read_response_chars


async def test_blame_answers_within_its_caps(vault):
    note = _a_tracked_note(vault)

    result = await tools.note_blame_impl(note, start_line=1, end_line=40)

    assert "not a git repository" not in result
    assert ISO_WITH_OFFSET.search(result), result[:400]
    assert len(result) <= tools.settings.max_read_response_chars


async def test_the_pickaxe_dates_a_line_taken_from_a_real_note(vault):
    """End-to-end on the headline use case, with a needle taken from the vault.

    A distinctive line of an existing note must be datable: the pickaxe finds
    the commit whose diff changed its occurrence count. If the vault's history
    is a single import commit, that commit is the answer — which is exactly
    the (correct, if coarse) attribution such a history can support.
    """
    note = _a_tracked_note(vault)
    text = (Path(VAULT_HISTORY_TEST_REPO).expanduser().resolve() / note).read_text(
        encoding="utf-8", errors="replace"
    )
    needle = next(
        (
            line.strip()
            for line in text.splitlines()
            if 30 <= len(line.strip()) <= 200 and not line.strip().startswith(("#", "-", "*"))
        ),
        None,
    )
    if needle is None:
        pytest.skip("no distinctive line to date in the chosen note")

    result = await tools.find_when_written_impl(needle, limit=5, path=note)

    assert "No commit changed" not in result, needle[:60]
    assert ISO_WITH_OFFSET.search(result), result[:400]


async def test_a_whole_vault_pickaxe_stays_inside_its_bounds(vault):
    """The unscoped search is the expensive one; it must still answer bounded."""
    result = await tools.find_when_written_impl("the", limit=5)

    assert len(result) <= tools.settings.max_read_response_chars
    assert "did not finish within" not in result, (
        "the whole-vault pickaxe timed out on this repository"
    )
