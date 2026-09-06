"""Storing a walked history: what becomes a row, what becomes a stamp, and
what becomes nothing (migration 025).

The walker's job is to say *what the versions are*; this module covers the
half that can get the database wrong. Three properties matter more than the
rest and each has its own case below:

- the current version is **stamped, never inserted**, because its vectors
  already exist and a second copy would double every search hit for that note;
- a version an earlier run already stored is skipped by matching its exact
  interval, which is the whole of the resumability story;
- `--dry-run` makes no provider call and writes nothing.

Embeddings come from `tests/fakes.FakeProvider`, so nothing here talks to a
provider. The end-to-end version against real pgvector rows lives in
`tests/integration/test_temporal_embeddings_pg.py`.
"""
import datetime
import os
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from src.services import history_indexer  # noqa: E402
from src.services.history_indexer import (  # noqa: E402
    BackfillStats,
    VersionRef,
    backfill_note,
    plan_note_backfill,
    stamp_current_version,
    store_version,
)
from tests.fakes import FakeProvider  # noqa: E402

UTC = datetime.timezone.utc


def at(month: int) -> datetime.datetime:
    return datetime.datetime(2024, month, 1, tzinfo=UTC)


class _Note:
    def __init__(self, note_id=1, path="alpha.md"):
        self.id = note_id
        self.file_path = path
        self.content_hash = "hash-1"
        self.embedded_content_hash = "hash-1"


class _Result:
    """Enough of a SQLAlchemy result for both shapes this module executes: an
    UPDATE (read for `rowcount`) and a SELECT (read for `all()`)."""

    def __init__(self, rowcount, rows=()):
        self.rowcount = rowcount
        self._rows = list(rows)

    def all(self):
        return self._rows


class _Session:
    """Records what was added, updated, committed and rolled back."""

    def __init__(self, update_rowcount=3, rows=()):
        self.added: list = []
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self._update_rowcount = update_rowcount
        self._rows = rows

    async def execute(self, stmt, *_a, **_k):
        self.statements.append(" ".join(str(stmt).split()))
        return _Result(self._update_rowcount, self._rows)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.fixture(autouse=True)
def fake_provider(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(history_indexer, "get_embeddings_batch", provider.embed_batch)
    monkeypatch.setattr(history_indexer.settings, "embedding_dimensions", 8)
    monkeypatch.setattr(history_indexer.settings, "chunk_size", 16)
    monkeypatch.setattr(history_indexer.settings, "chunk_overlap", 0)
    return provider


def _versions():
    return [
        VersionRef("alpha.md", "blob-1", at(1), at(2)),
        VersionRef("alpha.md", "blob-2", at(2), at(4)),
        VersionRef("alpha.md", "blob-3", at(4), None),
    ]


# ── planning ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_current_version_is_planned_as_a_stamp_not_an_insert(monkeypatch):
    """Its vectors already exist — the ordinary embed pass wrote them from the
    working tree, and they are the rows every search reads. Inserting a second
    copy from git would double every current vector for that note."""
    async def _none(_session, _note_id):
        return set()

    monkeypatch.setattr(history_indexer, "_existing_intervals", _none)
    stats = BackfillStats()

    plan = await plan_note_backfill(_Session(), _Note(), _versions(), stats)

    assert [v.blob for v in plan.historical] == ["blob-1", "blob-2"]
    assert plan.current is not None and plan.current.blob == "blob-3"


@pytest.mark.asyncio
async def test_an_already_stored_version_is_skipped_and_counted(monkeypatch):
    """Resumability, derived from the data rather than from a cursor: an
    interrupted run repeats only the note it was in the middle of, and a
    completed run is a no-op."""
    async def _one(_session, _note_id):
        return {(at(1), at(2))}

    monkeypatch.setattr(history_indexer, "_existing_intervals", _one)
    stats = BackfillStats()

    plan = await plan_note_backfill(_Session(), _Note(), _versions(), stats)

    assert [v.blob for v in plan.historical] == ["blob-2"]
    assert stats.versions_already_stored == 1


@pytest.mark.asyncio
async def test_the_existing_interval_query_only_looks_at_historical_rows():
    """A current row has `valid_from` set by an earlier stamp and `valid_to`
    NULL; matching against it would make the *current* version look already
    stored and skip the stamp for ever."""
    session = _Session(rows=[(at(1), at(2)), (None, at(3))])
    found = await history_indexer._existing_intervals(session, 1)

    assert len(session.statements) == 1
    assert "note_embeddings.valid_to IS NOT NULL" in session.statements[0]
    # A row with a NULL start cannot be matched against a walked version — it
    # names no interval — so it must not silently become one.
    assert found == {(at(1), at(2))}


# ── writing ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_stored_version_carries_its_whole_interval():
    session = _Session()
    stats = BackfillStats()
    version = VersionRef("alpha.md", "blob-1", at(1), at(2))

    chunks = await store_version(
        session, 7, version, "some superseded prose about penguins", stats
    )

    assert chunks == len(session.added) > 0
    for index, row in enumerate(session.added):
        assert row.note_id == 7
        assert row.chunk_index == index
        assert row.valid_from == at(1)
        assert row.valid_to == at(2), (
            "a historical row must carry a closed interval — a NULL valid_to "
            "would make deleted text current"
        )
    assert stats.versions_embedded == 1
    assert stats.chunks_embedded == chunks


@pytest.mark.asyncio
async def test_a_short_provider_response_refuses_to_store_a_partial_version(
    monkeypatch,
):
    """The exactness `embed_note` already demands. Partial coverage of a
    version stores a fragment of a past that reads as the whole of it, and
    nothing downstream could tell."""
    async def _short(chunks):
        return [[0.0] * 8] * (len(chunks) - 1)

    monkeypatch.setattr(history_indexer, "get_embeddings_batch", _short)
    session = _Session()
    stats = BackfillStats()

    with pytest.raises(RuntimeError, match="vectors for"):
        await store_version(
            session,
            7,
            VersionRef("alpha.md", "b", at(1), at(2)),
            "a much longer body so that it is split into several chunks " * 4,
            stats,
        )

    assert session.added == []
    assert stats.versions_embedded == 0


@pytest.mark.asyncio
async def test_an_empty_version_stores_nothing():
    session = _Session()
    stats = BackfillStats()

    assert await store_version(
        session, 7, VersionRef("alpha.md", "b", at(1), at(2)), "   \n", stats
    ) == 0
    assert session.added == []


@pytest.mark.asyncio
async def test_the_stamp_can_only_touch_current_rows():
    """Conditional on `valid_to IS NULL`, so it can never overwrite a
    historical row's interval — which is a fact, not a placeholder."""
    session = _Session(update_rowcount=4)
    stats = BackfillStats()

    await stamp_current_version(
        session, 7, VersionRef("alpha.md", "b", at(4), None), stats
    )

    assert len(session.statements) == 1
    statement = session.statements[0]
    assert statement.startswith("UPDATE note_embeddings")
    assert "note_embeddings.valid_to IS NULL" in statement
    assert stats.current_versions_stamped == 1


@pytest.mark.asyncio
async def test_a_note_with_no_current_rows_yet_is_a_no_op_not_an_error():
    """The embed pass has not reached it. It will, and the next backfill will
    stamp it — an error here would fail a run for a transient ordering."""
    session = _Session(update_rowcount=0)
    stats = BackfillStats()

    await stamp_current_version(
        session, 7, VersionRef("alpha.md", "b", at(4), None), stats
    )

    assert stats.current_versions_stamped == 0


# ── the per-note transaction ───────────────────────────────────────────────


@pytest.fixture
def stub_reads(monkeypatch):
    async def _none(_session, _note_id):
        return set()

    async def _content(_repo, blob):
        return f"body of {blob} with enough words to chunk more than once"

    monkeypatch.setattr(history_indexer, "_existing_intervals", _none)
    monkeypatch.setattr(history_indexer, "read_version_content", _content)


@pytest.mark.asyncio
async def test_a_note_is_committed_once(stub_reads):
    """One transaction per note is the unit an interrupt may split, which is
    what makes the interval-matching skip exact on a resumed run."""
    session = _Session()
    stats = BackfillStats()

    await backfill_note(session, "/repo", _Note(), _versions(), stats)

    assert session.commits == 1
    assert session.rollbacks == 0
    assert stats.versions_embedded == 2, "two historical versions, not three"
    assert stats.current_versions_stamped == 1


@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing_and_calls_no_provider(monkeypatch, stub_reads):
    async def _explode(_chunks):
        raise AssertionError("a dry run must not reach the provider")

    monkeypatch.setattr(history_indexer, "get_embeddings_batch", _explode)
    session = _Session()
    stats = BackfillStats()

    await backfill_note(session, "/repo", _Note(), _versions(), stats, dry_run=True)

    assert session.added == []
    assert session.commits == 0
    assert session.rollbacks == 1
    # It still reports what it *would* do, which is the whole point of the mode.
    assert stats.versions_embedded == 2
    assert stats.chunks_embedded > 0
    assert stats.current_versions_stamped == 1


@pytest.mark.asyncio
async def test_a_non_utf8_version_is_stepped_over_and_counted(monkeypatch, stub_reads):
    """A binary or mis-encoded blob is a fact about one version, not a reason
    to abandon a note's history."""
    async def _undecodable(_repo, blob):
        return None if blob == "blob-1" else "readable body text"

    monkeypatch.setattr(history_indexer, "read_version_content", _undecodable)
    session = _Session()
    stats = BackfillStats()

    await backfill_note(session, "/repo", _Note(), _versions(), stats)

    assert stats.versions_unreadable == 1
    assert stats.versions_embedded == 1
    assert session.commits == 1


def test_the_stats_line_names_every_counter():
    """A run that does almost nothing has to be able to say *which* kind of
    nothing it did, so no counter may be missing from the operator's line."""
    rendered = BackfillStats().render()
    for field in BackfillStats.__dataclass_fields__:
        token = {
            "notes_walked": "notes=",
            "versions_walked": "versions=",
            "versions_dropped": "dropped=",
            "versions_already_stored": "already_stored=",
            "versions_embedded": "embedded=",
            "chunks_embedded": "chunks=",
            "current_versions_stamped": "stamped_current=",
            "notes_without_metadata": "no_metadata=",
            "versions_unreadable": "unreadable=",
            "paths_excluded": "excluded=",
        }[field]
        assert token in rendered, field
