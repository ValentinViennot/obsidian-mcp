"""Historical vectors must never reach an ordinary search (migration 025).

This is the single most important correctness property of temporal embeddings,
and it is a property about *statements*, not about rows: once
`scripts/backfill_history.py` has run, `note_embeddings` holds vectors over
text the author deleted, and the only thing standing between those and a
`semantic_search` result is `valid_to IS NULL` on the statement. A reader that
forgets it does not fail — it quotes a deleted paragraph back to an agent as
though the note still said it, which is the failure this server ranks above
every expensive one.

So every current-state reader is asserted here at the statement level, offline.
The end-to-end version (real rows, real pgvector, a real point-in-time query)
lives in `tests/integration/test_temporal_embeddings_pg.py`, which needs a
database; this module needs nothing and therefore runs on every push.
"""
import datetime
import os
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from sqlalchemy import delete, select  # noqa: E402

from src.models.db import NoteEmbedding  # noqa: E402
from src.services import embeddings  # noqa: E402
from src.services.filters import (  # noqa: E402
    apply_embedding_validity,
    current_embedding_predicate,
    embedding_valid_at_predicate,
)

CURRENT_ONLY = "note_embeddings.valid_to IS NULL"


def _sql(clause) -> str:
    return " ".join(str(clause).split())


# ── The predicate itself ───────────────────────────────────────────────────


def test_current_is_spelled_valid_to_is_null():
    assert _sql(current_embedding_predicate()) == CURRENT_ONLY


def test_the_default_is_current_only_not_unfiltered():
    """`as_of=None` is a scoping decision, exactly as `user_id=None` is in
    `apply_note_filters`. A helper that returned the statement untouched would
    make "I forgot the argument" and "I want everything" the same call."""
    stmt = apply_embedding_validity(select(NoteEmbedding))
    assert CURRENT_ONLY in _sql(stmt)


def test_a_point_in_time_query_is_half_open_and_treats_null_as_unbounded():
    """`[valid_from, valid_to)` — consecutive versions tile the timeline with
    no gap and no overlap — and a NULL bound is unbounded rather than
    unknown-and-excluded, so a note that has never been backfilled still
    answers."""
    when = datetime.datetime(2024, 6, 1, tzinfo=datetime.timezone.utc)
    rendered = _sql(embedding_valid_at_predicate(when))

    assert "note_embeddings.valid_from IS NULL" in rendered
    assert "note_embeddings.valid_from <=" in rendered
    assert "note_embeddings.valid_to IS NULL" in rendered
    assert "note_embeddings.valid_to >" in rendered
    # Half-open at the upper bound: `>=` would return the version that ended at
    # exactly `when` *and* the one that started there.
    assert "valid_to >=" not in rendered


# ── The readers ────────────────────────────────────────────────────────────


class _Result:
    def fetchall(self):
        return []

    def scalars(self):
        return self

    def all(self):
        return []


class _RecordingSession:
    def __init__(self):
        self.statements: list[str] = []

    async def execute(self, clause, *_a, **_k):
        self.statements.append(_sql(clause))
        return _Result()


@pytest.mark.asyncio
async def test_semantic_search_scopes_to_current_by_default(monkeypatch):
    async def _embed(_q):
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(embeddings, "get_embedding", _embed)
    session = _RecordingSession()

    await embeddings.semantic_search(session, "needle", user_id=None)

    selects = [s for s in session.statements if s.startswith("SELECT")]
    assert selects, session.statements
    for stmt in selects:
        assert CURRENT_ONLY in stmt, stmt


@pytest.mark.asyncio
async def test_semantic_search_as_of_asks_for_the_version_that_was_live(monkeypatch):
    """The escape hatch exists, and it *replaces* the current-only predicate
    rather than adding to it — otherwise a point-in-time query could only ever
    return rows that are still current, which is no point-in-time query at
    all."""
    async def _embed(_q):
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(embeddings, "get_embedding", _embed)
    session = _RecordingSession()
    when = datetime.datetime(2024, 6, 1, tzinfo=datetime.timezone.utc)

    await embeddings.semantic_search(session, "needle", user_id=None, as_of=when)

    selects = [s for s in session.statements if s.startswith("SELECT")]
    assert selects, session.statements
    for stmt in selects:
        assert "note_embeddings.valid_from" in stmt, stmt
        assert "note_embeddings.valid_to >" in stmt, stmt


def test_find_related_scopes_to_current():
    from src.mcp_server.tools import find_related_stmt

    stmt = find_related_stmt(7, [0.1, 0.2, 0.3], None, 10)
    assert CURRENT_ONLY in _sql(stmt)


# ── The writer ─────────────────────────────────────────────────────────────


class _Note:
    id = 1
    file_path = "A.md"
    content_hash = "hash-1"
    embedded_content_hash = "old"


class _EmbedSession(_RecordingSession):
    def __init__(self):
        super().__init__()
        self.added: list = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_embed_note_replaces_only_the_current_vectors(monkeypatch):
    """An edit does not falsify the note's history — it is precisely what
    creates more of it. An unscoped DELETE here would make every ordinary edit
    silently erase that note's backfilled history, with nothing to report it.
    """
    monkeypatch.setattr(embeddings.settings, "chunk_size", 1)
    monkeypatch.setattr(embeddings.settings, "chunk_overlap", 0)

    async def _batch(chunks):
        return [[0.0, 1.0]] * len(chunks)

    monkeypatch.setattr(embeddings, "get_embeddings_batch", _batch)

    async def _generation_ok(_session, _note):
        return True

    monkeypatch.setattr(embeddings, "_generation_matches", _generation_ok)

    session = _EmbedSession()
    await embeddings.embed_note(session, _Note(), "alpha beta gamma")

    deletes = [s for s in session.statements if s.startswith("DELETE")]
    assert deletes, session.statements
    for stmt in deletes:
        assert CURRENT_ONLY in stmt, stmt


@pytest.mark.asyncio
async def test_the_empty_note_path_also_spares_history(monkeypatch):
    """The other DELETE in `embed_note`. A note emptied to nothing still had a
    past, and the empty-note branch is the one an accidental unscoped delete
    would hide in longest — it runs without a provider call, so no test that
    stubs the provider would ever notice."""
    monkeypatch.setattr(embeddings.settings, "chunk_size", 512)
    monkeypatch.setattr(embeddings.settings, "chunk_overlap", 0)

    session = _EmbedSession()
    result = await embeddings.embed_note(session, _Note(), "   ")

    assert result.outcome is embeddings.NoteEmbedOutcome.CERTIFIED_EMPTY
    deletes = [s for s in session.statements if s.startswith("DELETE")]
    assert deletes, session.statements
    for stmt in deletes:
        assert CURRENT_ONLY in stmt, stmt


def test_the_predicate_composes_onto_a_delete():
    """`current_embedding_predicate` is a bare predicate rather than only a
    statement helper precisely so `embed_note`'s DELETE can use it; a helper
    that only took a `Select` would have forced a hand-rolled copy there."""
    stmt = delete(NoteEmbedding).where(
        NoteEmbedding.note_id == 1, current_embedding_predicate()
    )
    assert CURRENT_ONLY in _sql(stmt)
