"""#127 / D5 — the Ollama batch has no aggregate deadline.

The old `OllamaProvider.embed_batch` carried a fixed 300 s budget over the
whole batch. A hung provider trips the per-request `wait_for` long before that,
so the only thing the aggregate ever caught was a note with more chunks than
300 s of *healthy* latency covers: it raised, `embed_note` returned 0, the row
was never certified, and the next pass selected it again — a permanent 300 s
burn per tick under `index_pass_lock` that could never complete.

The property survives native batching: `embed_batch` now issues one request per
`OLLAMA_BATCH_LIMIT` chunks, and every deadline belongs to exactly one of those
bounded requests. These cases are written against `_post` — the single HTTP
round trip — rather than `embed_one`, precisely so they keep asserting the
*property* (no budget spans the batch) and not the retired mechanism.

Fully offline: the provider's HTTP call is replaced.
"""

import asyncio
import os
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from src.services import embeddings  # noqa: E402
from src.services.embeddings import NoteEmbedOutcome  # noqa: E402


def test_embed_batch_takes_no_timeout_argument():
    """The retired knob is gone from the signature, not merely defaulted.

    A caller could otherwise re-impose the deadline this change removed, and
    `get_embeddings_batch` — the only production caller — has no way to pass
    one, so a surviving parameter would be a trap with no user.
    """
    import inspect

    params = inspect.signature(embeddings.OllamaProvider.embed_batch).parameters
    assert list(params) == ["self", "texts"], params
    assert list(
        inspect.signature(embeddings.get_embeddings_batch).parameters
    ) == ["texts"]


@pytest.mark.asyncio
async def test_a_batch_past_the_old_aggregate_budget_completes(monkeypatch):
    """Many chunks, each individually healthy, at a simulated latency whose
    sum exceeds the retired 300 s budget.

    400 chunks is 13 requests at `OLLAMA_BATCH_LIMIT`; none of them may inherit
    a deadline from the ones before it.
    """
    requests: list[int] = []
    # 400 chunks at a nominal 30 simulated seconds per request = 390 simulated
    # seconds, well past the retired 300 s. The clock is faked rather than
    # slept through: this must stay a unit test.
    fake_now = {"t": 0.0}
    monkeypatch.setattr(embeddings.time, "monotonic", lambda: fake_now["t"])

    async def _post(payload_input, *, timeout):  # noqa: ARG001
        requests.append(len(payload_input))
        fake_now["t"] += 30.0
        return [[0.1, 0.2, 0.3]] * len(payload_input)

    provider = embeddings.OllamaProvider()
    monkeypatch.setattr(provider, "_post", _post)

    out = await provider.embed_batch([f"chunk {i}" for i in range(400)])

    assert len(out) == 400
    assert sum(requests) == 400
    assert max(requests) <= embeddings.OLLAMA_BATCH_LIMIT
    assert fake_now["t"] > 300.0, "the run must exceed the retired budget"


@pytest.mark.asyncio
async def test_a_hung_request_still_fails_at_the_per_request_timeout(monkeypatch):
    """The per-request bound is the liveness guarantee that replaces the
    aggregate — and it is the *only* one, so it must still fire.

    A one-chunk batch must still ask for exactly 30 s: that is the call this
    provider made before batching existed, and its deadline may not drift.
    """
    async def _hangs(_payload_input, *, timeout):  # noqa: ARG001
        await asyncio.sleep(3600)

    provider = embeddings.OllamaProvider()
    monkeypatch.setattr(provider, "_post", _hangs)

    real_wait_for = asyncio.wait_for
    seen: list[float] = []

    async def _spy(coro, timeout):
        seen.append(timeout)
        # Run the real thing at a timeout short enough for a test, having
        # recorded the one production actually asks for.
        return await real_wait_for(coro, 0.05)

    monkeypatch.setattr(embeddings.asyncio, "wait_for", _spy)

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await provider.embed_batch(["a"])

    assert seen == [30.0], "the one-chunk request timeout must stay at 30 s"


@pytest.mark.asyncio
async def test_partial_coverage_is_not_certified_on_the_certified_path(monkeypatch):
    """`embed_note`'s `len(embeddings) != len(chunks)` refusal, exercised on
    the path `embed_vault` actually uses — with `certified_hash`/
    `certified_path` supplied. Nothing may be stamped and nothing deleted."""
    class _Session:
        def __init__(self):
            self.executed = []
            self.added = []

        async def execute(self, stmt, *_a, **_k):
            self.executed.append(stmt)
            raise AssertionError("no statement may run for a partial batch")

        def add(self, obj):
            self.added.append(obj)

        async def flush(self):
            raise AssertionError("nothing may be flushed for a partial batch")

    class _Note:
        id = 1
        file_path = "A.md"
        content_hash = "hash-1"
        embedded_content_hash = "old"

    monkeypatch.setattr(embeddings.settings, "chunk_size", 1)
    monkeypatch.setattr(embeddings.settings, "chunk_overlap", 0)

    async def _short(chunks):
        return [[0.0, 1.0]] * (len(chunks) - 1)

    monkeypatch.setattr(embeddings, "get_embeddings_batch", _short)

    session, note = _Session(), _Note()
    result = await embeddings.embed_note(
        session, note, "first chunk second chunk third chunk",
        certified_hash="hash-1", certified_path="A.md",
    )

    # Its own outcome, carrying both cardinalities, and **no statement at
    # all** — not the generation lock either, which is taken only on the path
    # that is about to write.
    assert result.outcome is NoteEmbedOutcome.PROVIDER_CARDINALITY_MISMATCH
    assert result.chunks_embedded == 0
    assert result.failure.requested == result.chunks_submitted
    assert result.failure.received == result.chunks_submitted - 1
    assert session.executed == [] and session.added == []
    assert note.embedded_content_hash == "old"
