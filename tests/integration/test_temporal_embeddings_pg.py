"""Temporal embeddings end to end, against a real PostgreSQL (migration 025).

The statement-level assertions live in
`tests/test_temporal_current_state_queries.py` and run everywhere. This module
is the half a fake session cannot produce: real rows in a real
`note_embeddings`, real NULL semantics in the temporal predicate, a real
`ORDER BY` over pgvector distance, and a real backfill writing through the
production code path with a `FakeProvider` standing in for the embedding
service.

What it is here to catch is one regression above all others: **an ordinary
search returning text the author deleted.** A vector over a deleted paragraph
is indistinguishable from a live one except by `valid_to`, it ranks and quotes
identically, and an agent acts on it without a human ever seeing the query. If
this module fails, do not run `make backfill-history` against a vault you care
about.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import datetime
import os
import subprocess

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
from src.config import settings
from src.models.db import NoteEmbedding, NoteMetadata
from src.services import embeddings as embeddings_service
from src.services import history_indexer
from src.services.embeddings import semantic_search
from src.services.history_indexer import backfill_vault_history
from tests.fakes import FakeProvider

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

# The ORM's vector column width is bound at import from `settings`, so the
# throwaway database must be migrated at the same width or every insert is
# rejected by the `::vector(n)` cast. `tests/integration/test_pgvector_search.py`
# takes the same line.
DIM = settings.embedding_dimensions
UTC = datetime.timezone.utc

#: Two orthogonal directions, so "which version was retrieved" is decided by
#: the predicate rather than by luck in a near-tie.
PENGUIN = [1.0] + [0.0] * (DIM - 1)
GLACIER = [0.0, 1.0] + [0.0] * (DIM - 2)


def at(month: int) -> datetime.datetime:
    return datetime.datetime(2024, month, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("temporal_embeddings", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def corpus(sessionmaker, monkeypatch):
    """One note with a past.

    `alpha.md` said "penguins" from January to March and has said "glaciers"
    ever since. The historical row is the one an ordinary search must never
    return, and the one a point-in-time query for February must.
    """
    monkeypatch.setattr(settings, "embedding_dimensions", DIM, raising=False)

    async def _embed_query(_text):
        return list(PENGUIN)

    monkeypatch.setattr(embeddings_service, "get_embedding", _embed_query)

    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.commit()

    async with sessionmaker() as session:
        note = NoteMetadata(
            user_id=None,
            file_path="alpha.md",
            title="alpha",
            tags=[],
            frontmatter={},
            content_hash="h",
            embedded_content_hash="h",
        )
        session.add(note)
        await session.flush()
        # The current slice, exactly as the ordinary embed pass writes it:
        # NULL/NULL.
        session.add(
            NoteEmbedding(
                note_id=note.id,
                chunk_index=0,
                chunk_text="alpha now talks about glaciers",
                embedding=list(GLACIER),
            )
        )
        # The past: a closed interval over text the note no longer contains.
        session.add(
            NoteEmbedding(
                note_id=note.id,
                chunk_index=0,
                chunk_text="alpha once talked about penguins",
                embedding=list(PENGUIN),
                valid_from=at(1),
                valid_to=at(3),
            )
        )
        await session.commit()
        yield note.id


async def test_an_ordinary_search_never_returns_deleted_text(corpus, sessionmaker):
    """The single most important property of this feature.

    The query vector is the historical row's *own* vector, so the deleted
    paragraph is the nearest thing in the database by a wide margin. It must
    still not be returned, and the note must still be found through its current
    chunk.
    """
    async with sessionmaker() as session:
        results = await semantic_search(session, "penguins", user_id=None)

    assert [r["path"] for r in results] == ["alpha.md"]
    assert results[0]["chunk"] == "alpha now talks about glaciers"
    assert "penguins" not in results[0]["chunk"]


async def test_a_point_in_time_search_returns_the_version_that_was_live(
    corpus, sessionmaker
):
    async with sessionmaker() as session:
        results = await semantic_search(
            session, "penguins", user_id=None, as_of=at(2)
        )

    assert [r["chunk"] for r in results] == ["alpha once talked about penguins"]


async def test_a_point_in_time_search_after_the_edit_returns_the_present(
    corpus, sessionmaker
):
    """Half-open intervals: the instant a version ends belongs to its
    successor, so a query at exactly `valid_to` must not return the version
    that ended there."""
    async with sessionmaker() as session:
        at_boundary = await semantic_search(
            session, "penguins", user_id=None, as_of=at(3)
        )
        later = await semantic_search(session, "penguins", user_id=None, as_of=at(6))

    assert [r["chunk"] for r in at_boundary] == ["alpha now talks about glaciers"]
    assert [r["chunk"] for r in later] == ["alpha now talks about glaciers"]


async def test_a_point_in_time_search_before_the_note_existed_finds_nothing(
    corpus, sessionmaker
):
    """The current row has a NULL `valid_from` here — it has never been through
    a backfill — so it reads as unbounded in the past and answers. That is the
    documented, deliberately over-inclusive reading; what must *not* happen is
    the historical row answering for an instant before its interval began."""
    async with sessionmaker() as session:
        results = await semantic_search(
            session, "penguins", user_id=None, as_of=datetime.datetime(
                2023, 1, 1, tzinfo=UTC
            )
        )

    assert [r["chunk"] for r in results] == ["alpha now talks about glaciers"]


async def test_find_related_ignores_history(corpus, sessionmaker, monkeypatch):
    """`find_related` averages the source note's chunk vectors. Averaging its
    history in would build a query vector describing what the note used to say
    as much as what it says."""
    import src.mcp_server.tools as tools

    async with sessionmaker() as session:
        chunks = (
            await session.execute(
                tools.apply_embedding_validity(
                    select(NoteEmbedding.embedding).where(
                        NoteEmbedding.note_id == corpus
                    )
                )
            )
        ).scalars().all()

    assert [list(c) for c in chunks] == [GLACIER]


# ── the backfill, through the production path ──────────────────────────────


class _Repo:
    def __init__(self, path):
        self.path = path
        os.makedirs(path, exist_ok=True)
        self._run("init", "-q", "-b", "main", ".")
        self._run("config", "user.email", "fixture@example.invalid")
        self._run("config", "user.name", "fixture")

    def _run(self, *args, env=None):
        subprocess.run(
            [history_indexer.GIT, "-C", self.path, *args],
            capture_output=True,
            check=True,
            env=env,
        )

    def commit(self, rel, content, when):
        with open(os.path.join(self.path, rel), "w", encoding="utf-8") as handle:
            handle.write(content)
        stamp = when.isoformat()
        env = dict(os.environ)
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
        self._run("add", "-A")
        self._run("commit", "-q", "-m", rel, env=env)


@pytest_asyncio.fixture(loop_scope="module")
async def git_vault(sessionmaker, monkeypatch, tmp_path_factory):
    """A three-version note, backfilled through `backfill_vault_history`."""
    monkeypatch.setattr(settings, "embedding_dimensions", DIM, raising=False)
    monkeypatch.setattr(settings, "chunk_size", 512, raising=False)
    monkeypatch.setattr(settings, "chunk_overlap", 0, raising=False)
    provider = FakeProvider()
    monkeypatch.setattr(
        history_indexer, "get_embeddings_batch", provider.embed_batch
    )

    root = str(tmp_path_factory.mktemp("vault"))
    repo = _Repo(root)
    repo.commit("beta.md", "the first thing beta said\n", at(1))
    repo.commit("beta.md", "the second thing beta said\n", at(2))
    repo.commit("beta.md", "what beta says now\n", at(4))

    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(text("DELETE FROM notes_metadata"))
        note = NoteMetadata(
            user_id=None,
            file_path="beta.md",
            title="beta",
            tags=[],
            frontmatter={},
            content_hash="h",
            embedded_content_hash="h",
        )
        session.add(note)
        await session.flush()
        session.add(
            NoteEmbedding(
                note_id=note.id,
                chunk_index=0,
                chunk_text="what beta says now",
                embedding=list(GLACIER),
            )
        )
        await session.commit()
        note_id = note.id

    yield root, note_id


async def _rows(sessionmaker, note_id):
    async with sessionmaker() as session:
        result = await session.execute(
            select(
                NoteEmbedding.chunk_text,
                NoteEmbedding.valid_from,
                NoteEmbedding.valid_to,
            )
            .where(NoteEmbedding.note_id == note_id)
            .order_by(NoteEmbedding.valid_to.nulls_last(), NoteEmbedding.chunk_index)
        )
        # Chunk text is compared stripped: a version's blob ends with the
        # trailing newline the file had, and the point of these assertions is
        # which *version* landed where, not byte-level chunker behaviour.
        return [(r[0].strip(), r[1], r[2]) for r in result.all()]


async def test_a_dry_run_writes_nothing(git_vault, sessionmaker):
    root, note_id = git_vault
    before = await _rows(sessionmaker, note_id)

    stats = await backfill_vault_history(sessionmaker, root, dry_run=True)

    assert stats.versions_embedded == 2, "two superseded versions"
    assert stats.chunks_embedded > 0
    assert await _rows(sessionmaker, note_id) == before


async def test_the_backfill_chains_intervals_and_leaves_the_present_alone(
    git_vault, sessionmaker
):
    root, note_id = git_vault

    stats = await backfill_vault_history(sessionmaker, root)

    assert stats.versions_embedded == 2
    assert stats.current_versions_stamped == 1

    rows = await _rows(sessionmaker, note_id)
    assert [r[0] for r in rows] == [
        "the first thing beta said",
        "the second thing beta said",
        "what beta says now",
    ]
    # The chain: each interval ends where the next begins, and only the current
    # row's end is NULL.
    assert [(r[1], r[2]) for r in rows] == [
        (at(1), at(2)),
        (at(2), at(4)),
        (at(4), None),
    ]
    # The current row is the *same* row the embed pass wrote — its vector was
    # not replaced, only its start recorded.
    async with sessionmaker() as session:
        current = (
            await session.execute(
                select(NoteEmbedding.embedding).where(
                    NoteEmbedding.note_id == note_id,
                    NoteEmbedding.valid_to.is_(None),
                )
            )
        ).scalars().all()
    assert [list(v) for v in current] == [GLACIER]


async def test_re_running_the_backfill_is_a_no_op(git_vault, sessionmaker):
    """Resumability without a cursor: an already-stored version is skipped by
    matching its exact interval, so a completed run repeats nothing and an
    interrupted one repeats only the note it was inside."""
    root, note_id = git_vault
    await backfill_vault_history(sessionmaker, root)
    before = await _rows(sessionmaker, note_id)

    stats = await backfill_vault_history(sessionmaker, root)

    assert stats.versions_embedded == 0
    assert stats.versions_already_stored == 2
    assert await _rows(sessionmaker, note_id) == before


async def test_the_backfill_does_not_change_the_number_of_current_rows(
    git_vault, sessionmaker
):
    """The run's own invariant, and the one the runner script warns about: this
    tool may only ever add rows with a closed interval and stamp existing
    current ones."""
    root, _note_id = git_vault

    async def current_count():
        async with sessionmaker() as session:
            return (
                await session.execute(
                    select(func.count(NoteEmbedding.id)).where(
                        NoteEmbedding.valid_to.is_(None)
                    )
                )
            ).scalar()

    before = await current_count()
    await backfill_vault_history(sessionmaker, root)
    assert await current_count() == before


async def test_search_after_a_backfill_still_only_sees_the_present(
    git_vault, sessionmaker, monkeypatch
):
    """The whole point, stated end to end: a backfilled database answers an
    ordinary query exactly as it did before the backfill."""
    root, _note_id = git_vault
    await backfill_vault_history(sessionmaker, root)

    async def _embed_query(_text):
        return list(GLACIER)

    monkeypatch.setattr(embeddings_service, "get_embedding", _embed_query)

    async with sessionmaker() as session:
        results = await semantic_search(session, "beta", user_id=None)

    assert [r["chunk"] for r in results] == ["what beta says now"]


async def test_an_ordinary_re_embed_does_not_erase_the_history(
    git_vault, sessionmaker, monkeypatch
):
    """An edit replaces the note's present, not its past. An unscoped DELETE in
    `embed_note` would silently drop every backfilled version of the note it
    touched, and nothing would report it."""
    root, note_id = git_vault
    await backfill_vault_history(sessionmaker, root)

    async def _batch(chunks):
        return [list(GLACIER) for _ in chunks]

    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", _batch)

    async with sessionmaker() as session:
        note = await session.get(NoteMetadata, note_id)
        await embeddings_service.embed_note(session, note, "beta says something new")
        await session.commit()

    rows = await _rows(sessionmaker, note_id)
    historical = [r for r in rows if r[2] is not None]
    assert [r[0] for r in historical] == [
        "the first thing beta said",
        "the second thing beta said",
    ]
    assert [(r[1], r[2]) for r in historical] == [(at(1), at(2)), (at(2), at(4))]
    assert [r[0] for r in rows if r[2] is None] == ["beta says something new"]
