"""#213 — `modified_at` is corrected from git history, including for notes
that have not changed.

**Why this needs real PostgreSQL and could not be a unit test.** The unit
tests prove `last_commit_times` parses git correctly and `_modified_at` picks
the right source. Neither touches the thing that actually decides whether the
fix reaches a deployed vault: the indexer's upsert carries only *changed*
notes, and the notes whose dates are wrong are exactly the ones that have not
changed since the checkout that mis-stamped them.

Without the reconciliation statement, deploying this change to a vault of
1,222 correct-content notes would have corrected **zero** of them, and each
would have kept its bogus checkout timestamp until somebody happened to edit
it. The tool would still have answered; the answer would still have been
noise; and the commit message would have claimed a fix that did nothing. That
is the assertion this module exists to make, and it needs a real database,
because the claim is about what an UPDATE did to rows the upsert never
mentioned.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set.
"""
import os
import shutil
import subprocess
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
import src.mcp_server.tools as tools
from src.config import settings
from src.models.db import NoteMetadata
from src.services import indexer
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(
        shutil.which("git") is None, reason="reading commit times needs git"
    ),
]

DIM = 8

ANCIENT = "2021-04-01T10:00:00+00:00"
RECENT = "2023-12-25T09:05:00+00:00"


def _epoch(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(timezone.utc)


def _git(root, *args, env=None):
    base = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    base.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    if env:
        base.update(env)
    done = subprocess.run(
        [shutil.which("git"), "-c", "commit.gpgsign=false", *args],
        cwd=str(root),
        env=base,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, f"git {args}: {done.stderr}"
    return done.stdout


def _commit(root, message, when):
    _git(
        root,
        "-c", "user.name=Fixture",
        "-c", "user.email=fixture@example.invalid",
        "commit", "-q", "-m", message,
        env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when},
    )


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("modified_at_213", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def vault(sessionmaker, monkeypatch, tmp_path):
    """A git vault whose *working tree* mtimes are all "now" — the shape a
    server has after cloning — while its history spans years."""
    root = tmp_path / "vault"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")

    (root / "Ancient.md").write_text("# Ancient\n\nwritten long ago\n", encoding="utf-8")
    _git(root, "add", "-A")
    _commit(root, "import", ANCIENT)

    (root / "Recent.md").write_text("# Recent\n\nwritten lately\n", encoding="utf-8")
    _git(root, "add", "-A")
    _commit(root, "add recent", RECENT)

    monkeypatch.setattr(settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(tools.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)

    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM note_links"))
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.commit()

    yield root


async def _modified(sessionmaker, path):
    async with sessionmaker() as session:
        got = (
            await session.execute(
                select(NoteMetadata.modified_at).where(NoteMetadata.file_path == path)
            )
        ).scalar_one_or_none()
    return got.astimezone(timezone.utc) if got else None


async def test_a_pass_with_the_git_vault_off_records_the_checkout_stamp(
    sessionmaker, vault, monkeypatch
):
    """The behaviour being replaced, so the next test's delta is real."""
    monkeypatch.setattr(indexer.settings, "git_vault_enabled", False)
    await indexer.index_vault(user_id=None)

    ancient = await _modified(sessionmaker, "Ancient.md")
    recent = await _modified(sessionmaker, "Recent.md")
    # Both stamped with the working tree's mtime — moments apart, and nothing
    # like the years between the commits that wrote them.
    assert abs((ancient - recent).total_seconds()) < 5
    assert ancient.year >= 2026


async def test_the_next_pass_corrects_notes_that_did_not_change(
    sessionmaker, vault, monkeypatch
):
    """**The assertion this module exists for.**

    Nothing on disk changes between the two passes, so the upsert carries no
    rows at all — `existing[rel_path] == h` short-circuits every note. If the
    fix lived only in `_modified_at`, both rows would still hold the checkout
    stamp and a deploy would correct nothing until each note was edited.
    """
    monkeypatch.setattr(indexer.settings, "git_vault_enabled", False)
    await indexer.index_vault(user_id=None)
    stale = await _modified(sessionmaker, "Ancient.md")
    assert stale.year >= 2026

    monkeypatch.setattr(indexer.settings, "git_vault_enabled", True)
    await indexer.index_vault(user_id=None)

    assert await _modified(sessionmaker, "Ancient.md") == _epoch(ANCIENT)
    assert await _modified(sessionmaker, "Recent.md") == _epoch(RECENT)


async def test_the_reconciliation_is_idempotent(sessionmaker, vault, monkeypatch):
    """Steady state updates zero rows, so it is cheap to run every pass."""
    monkeypatch.setattr(indexer.settings, "git_vault_enabled", True)
    await indexer.index_vault(user_id=None)
    first = await _modified(sessionmaker, "Ancient.md")
    await indexer.index_vault(user_id=None)
    assert await _modified(sessionmaker, "Ancient.md") == first == _epoch(ANCIENT)


async def test_get_recent_orders_by_the_real_dates(sessionmaker, vault, monkeypatch):
    """The tool the bug was reported through, end to end.

    Under checkout mtimes both notes tie to the same instant and the order is
    whatever the planner returns. Under commit times the newer note leads.
    """
    monkeypatch.setattr(indexer.settings, "git_vault_enabled", True)
    await indexer.index_vault(user_id=None)

    out = await tools.get_recent_impl(limit=10)
    assert out.index("Recent.md") < out.index("Ancient.md")
    assert "2021-04-01" in out and "2023-12-25" in out


async def test_a_new_note_is_dated_by_its_commit_not_its_write(
    sessionmaker, vault, monkeypatch
):
    """A note committed with a backdated author date is dated by the commit,
    and one written but never committed keeps its filesystem time — the
    documented fallback, exercised rather than asserted."""
    monkeypatch.setattr(indexer.settings, "git_vault_enabled", True)

    (vault / "Backdated.md").write_text("# Backdated\n", encoding="utf-8")
    _git(vault, "add", "-A")
    _commit(vault, "backdated", ANCIENT)

    (vault / "Uncommitted.md").write_text("# Uncommitted\n", encoding="utf-8")

    await indexer.index_vault(user_id=None)

    assert await _modified(sessionmaker, "Backdated.md") == _epoch(ANCIENT)
    uncommitted = await _modified(sessionmaker, "Uncommitted.md")
    assert uncommitted.year >= 2026, "an uncommitted note falls back to its mtime"
