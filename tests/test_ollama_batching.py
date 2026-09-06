"""Native batching on `OllamaProvider.embed_batch`.

`/api/embed` accepts an array in `input` and answers with an array in
`embeddings`. Measured against the production endpoint, one request per chunk
costs 0.092 s/chunk while a batch of 32 costs 0.0169 s/chunk — a 5.4x speedup,
and 32x fewer requests against an instance shared with other services.

The four properties that make that safe are the four this module asserts:

1. results map back to input order, across sub-batch boundaries;
2. a response of the wrong length raises rather than guessing a pairing;
3. a failed sub-batch degrades to one request per chunk;
4. a hung provider still fails in bounded time, and the bound scales with the
   request rather than with the note.

`respx` intercepts httpx where the real request shape matters; the fault
injection patches `_post`, the single round trip, where it does not.
"""
import json

import asyncio

import pytest
import respx
from httpx import ConnectError, Response

from src.config import settings
from src.services import embeddings
from src.services.embeddings import (
    OLLAMA_BATCH_LIMIT,
    OllamaProvider,
    ProviderBatchSizeMismatch,
    _ollama_batch_timeout,
)


@pytest.fixture
def ollama_settings(monkeypatch):
    monkeypatch.setattr(settings, "ollama_url", "http://ollama:11434")
    monkeypatch.setattr(settings, "embedding_model", "bge-m3")
    monkeypatch.setattr(settings, "ollama_keep_alive", "30m")
    return settings


def test_batch_limit_mirrors_the_openai_provider_shape():
    """A module constant, bounded, and small enough that a failed request is
    cheap to re-issue one chunk at a time."""
    assert isinstance(OLLAMA_BATCH_LIMIT, int)
    assert 1 < OLLAMA_BATCH_LIMIT <= 64


def test_the_request_deadline_scales_with_the_request_not_the_note():
    """Affine in the chunk count, equal to the pre-batching 30 s at one chunk,
    and finite because `OLLAMA_BATCH_LIMIT` bounds its argument.

    The upper bound is the whole safety argument: n chunks used to get n x 30 s
    as n separate calls, so this may never exceed that.
    """
    assert _ollama_batch_timeout(1) == 30.0
    assert _ollama_batch_timeout(2) > _ollama_batch_timeout(1)
    for n in range(1, OLLAMA_BATCH_LIMIT + 1):
        assert _ollama_batch_timeout(n) <= 30.0 * n
    assert _ollama_batch_timeout(OLLAMA_BATCH_LIMIT) < 600.0


@pytest.mark.asyncio
async def test_one_request_carries_the_whole_sub_batch(ollama_settings):
    """The point of the change: `input` is an array, not a string, and 8 chunks
    are one round trip rather than eight."""
    provider = OllamaProvider()
    texts = [f"chunk {i}" for i in range(8)]

    with respx.mock(base_url="http://ollama:11434") as mock:
        route = mock.post("/api/embed").mock(
            return_value=Response(
                200, json={"embeddings": [[float(i)] for i in range(8)]}
            )
        )
        out = await provider.embed_batch(texts)

    assert route.call_count == 1
    body = json.loads(route.calls[0].request.read())
    assert body["input"] == texts
    assert body["model"] == "bge-m3"
    assert body["keep_alive"] == "30m"
    assert out == [[float(i)] for i in range(8)]


@pytest.mark.asyncio
async def test_order_is_preserved_across_sub_batch_boundaries(monkeypatch):
    """Every vector must come back paired with the chunk it was built from.

    The provider is made order-revealing — each vector encodes its own input —
    so a reversal, a rotation, or a sub-batch stitched back in the wrong place
    is visible rather than merely plausible. `OLLAMA_BATCH_LIMIT * 2 + 3`
    chunks guarantees at least three requests, the last one short.
    """
    provider = OllamaProvider()
    n = OLLAMA_BATCH_LIMIT * 2 + 3
    texts = [f"chunk-{i}" for i in range(n)]

    async def _post(payload_input, *, timeout):  # noqa: ARG001
        assert isinstance(payload_input, list)
        assert len(payload_input) <= OLLAMA_BATCH_LIMIT
        return [[float(int(t.split("-")[1]))] for t in payload_input]

    monkeypatch.setattr(provider, "_post", _post)

    out = await provider.embed_batch(texts)

    assert out == [[float(i)] for i in range(n)]


@pytest.mark.asyncio
async def test_a_short_response_raises_rather_than_guessing(monkeypatch):
    """`/api/embed` carries no `index` field, so length is the only evidence
    that vector i describes input i. A mismatch must be loud."""
    provider = OllamaProvider()

    async def _post(payload_input, *, timeout):  # noqa: ARG001
        return [[0.1]] * (len(payload_input) - 1)

    monkeypatch.setattr(provider, "_post", _post)

    with pytest.raises(ProviderBatchSizeMismatch) as excinfo:
        await provider.embed_batch([f"c{i}" for i in range(5)])

    assert "4 vectors for 5 inputs" in str(excinfo.value)


@pytest.mark.asyncio
async def test_an_over_long_response_raises_too(monkeypatch):
    """Symmetry matters: extra vectors are as unpairable as missing ones, and
    the caller would otherwise silently keep the first n."""
    provider = OllamaProvider()

    async def _post(payload_input, *, timeout):  # noqa: ARG001
        return [[0.1]] * (len(payload_input) + 1)

    monkeypatch.setattr(provider, "_post", _post)

    with pytest.raises(ProviderBatchSizeMismatch):
        await provider.embed_batch([f"c{i}" for i in range(3)])


@pytest.mark.asyncio
async def test_a_size_mismatch_is_not_papered_over_by_the_fallback(monkeypatch):
    """The degradation path must not swallow a broken provider.

    Re-issuing the work per chunk would turn a contract violation into a slow
    green pass, for ever.
    """
    provider = OllamaProvider()
    per_chunk_calls = {"n": 0}

    async def _post(payload_input, *, timeout):  # noqa: ARG001
        if isinstance(payload_input, str):
            per_chunk_calls["n"] += 1
            return [[0.1]]
        return [[0.1]] * (len(payload_input) - 1)

    monkeypatch.setattr(provider, "_post", _post)

    with pytest.raises(ProviderBatchSizeMismatch):
        await provider.embed_batch([f"c{i}" for i in range(4)])

    assert per_chunk_calls["n"] == 0


@pytest.mark.asyncio
async def test_a_failed_sub_batch_falls_back_to_one_request_per_chunk(monkeypatch):
    """A batch-level fault that per-chunk requests do not reproduce must cost
    nothing but the retry, and the results must still be in order."""
    provider = OllamaProvider()
    seen: list[object] = []

    async def _post(payload_input, *, timeout):  # noqa: ARG001
        seen.append(payload_input)
        if isinstance(payload_input, list):
            raise ConnectError("connection reset")
        return [[float(int(payload_input.split("-")[1]))]]

    monkeypatch.setattr(provider, "_post", _post)

    out = await provider.embed_batch([f"chunk-{i}" for i in range(5)])

    assert out == [[float(i)] for i in range(5)]
    # One failed array request, then five single-string requests, in order.
    assert isinstance(seen[0], list) and len(seen[0]) == 5
    assert seen[1:] == [f"chunk-{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_only_the_failed_sub_batch_degrades(monkeypatch):
    """A poisoned chunk must not drag the other requests down with it: the
    healthy sub-batches stay batched."""
    provider = OllamaProvider()
    array_requests: list[int] = []
    string_requests: list[str] = []
    n = OLLAMA_BATCH_LIMIT * 2

    async def _post(payload_input, *, timeout):  # noqa: ARG001
        if isinstance(payload_input, str):
            string_requests.append(payload_input)
            return [[float(int(payload_input.split("-")[1]))]]
        array_requests.append(len(payload_input))
        if payload_input[0] == "chunk-0":
            raise ConnectError("first sub-batch is cursed")
        return [[float(int(t.split("-")[1]))] for t in payload_input]

    monkeypatch.setattr(provider, "_post", _post)

    out = await provider.embed_batch([f"chunk-{i}" for i in range(n)])

    assert out == [[float(i)] for i in range(n)]
    assert array_requests == [OLLAMA_BATCH_LIMIT, OLLAMA_BATCH_LIMIT]
    # Only the first sub-batch was re-issued chunk by chunk.
    assert string_requests == [f"chunk-{i}" for i in range(OLLAMA_BATCH_LIMIT)]


@pytest.mark.asyncio
async def test_a_single_chunk_sub_batch_does_not_retry_itself(monkeypatch):
    """There is nothing narrower to degrade to, so a second attempt would only
    double the latency of every genuine failure."""
    provider = OllamaProvider()
    calls = {"n": 0}

    async def _post(payload_input, *, timeout):  # noqa: ARG001
        calls["n"] += 1
        raise ConnectError("down")

    monkeypatch.setattr(provider, "_post", _post)

    with pytest.raises(ConnectError):
        await provider.embed_batch(["only one"])

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_a_hung_provider_times_out_in_bounded_time(monkeypatch):
    """Liveness, at the batch size production actually uses.

    The deadline asked for must be the one `_ollama_batch_timeout` computes for
    that request — not a note-wide budget, and not an unbounded wait — and a
    timeout must **not** trigger the per-chunk fallback, which would multiply
    exactly the burn the deadline exists to stop.
    """
    provider = OllamaProvider()
    calls = {"n": 0}

    async def _hangs(payload_input, *, timeout):  # noqa: ARG001
        calls["n"] += 1
        await asyncio.sleep(3600)

    monkeypatch.setattr(provider, "_post", _hangs)

    real_wait_for = asyncio.wait_for
    seen: list[float] = []

    async def _spy(coro, timeout):
        seen.append(timeout)
        return await real_wait_for(coro, 0.05)

    monkeypatch.setattr(embeddings.asyncio, "wait_for", _spy)

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await provider.embed_batch([f"c{i}" for i in range(OLLAMA_BATCH_LIMIT)])

    assert seen == [_ollama_batch_timeout(OLLAMA_BATCH_LIMIT)]
    assert calls["n"] == 1, "a timeout must not fall back to per-chunk requests"


@pytest.mark.asyncio
async def test_an_empty_batch_issues_no_request(monkeypatch):
    provider = OllamaProvider()

    async def _post(payload_input, *, timeout):  # noqa: ARG001
        raise AssertionError("no request may be issued for an empty batch")

    monkeypatch.setattr(provider, "_post", _post)

    assert await provider.embed_batch([]) == []


@pytest.mark.asyncio
async def test_embed_one_still_sends_a_bare_string(ollama_settings):
    """The single-input request shape is unchanged by batching: every
    deployment this has run against accepts a bare string, and
    `semantic_search` embeds its query through this path on every call."""
    provider = OllamaProvider()

    with respx.mock(base_url="http://ollama:11434") as mock:
        route = mock.post("/api/embed").mock(
            return_value=Response(200, json={"embeddings": [[0.5, 0.25]]})
        )
        out = await provider.embed_one("a query")

    assert out == [0.5, 0.25]
    assert json.loads(route.calls[0].request.read())["input"] == "a query"
