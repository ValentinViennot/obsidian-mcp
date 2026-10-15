"""#309 — the pass does not prune what it could not see (offline half).

The walk's structured failed prefixes (D1), the protected-path rule (D2), the
indeterminate-root predicate (D3), the empty-prune permission registry (D5),
the `walk_incomplete` accounting and `/health` (D4), the run-record line, and
the manual single-user reindex's stage isolation. The database-backed
behaviour (rows actually kept, a prune actually refused) is in
`tests/integration/test_issue_309_prune_guards_pg.py`.
"""
import asyncio
import contextlib
import logging
import os

import pytest

from src.services import empty_prune, indexer, indexer_health
from src.services.indexer import (
    IndexIndeterminate,
    IndexPassResult,
    PassStats,
    _check_determinate,
    _walk_protected,
    discover_markdown_files_at,
    format_walk_incomplete,
)

running_as_root = hasattr(os, "geteuid") and os.geteuid() == 0


def _walk(root):
    skips: list[str] = []
    prefixes: list[str] = []
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        rels = [
            f.rel
            for f in discover_markdown_files_at(
                fd, skips=skips, failed_prefixes=prefixes
            )
        ]
    finally:
        os.close(fd)
    return rels, skips, prefixes


# ── D1: failed prefixes ──────────────────────────────────────────────────


@pytest.mark.skipif(running_as_root, reason="root ignores directory modes")
def test_an_unlistable_subdirectory_is_recorded_as_its_prefix(tmp_path):
    (tmp_path / "a.md").write_text("a")
    sub = tmp_path / "sub" / "deep"
    sub.mkdir(parents=True)
    (sub / "x.md").write_text("x")
    (sub / "locked").mkdir()
    (sub / "locked" / "y.md").write_text("y")
    os.chmod(sub / "locked", 0)
    try:
        rels, skips, prefixes = _walk(tmp_path)
    finally:
        os.chmod(sub / "locked", 0o755)
    assert sorted(rels) == ["a.md", "sub/deep/x.md"]
    assert prefixes == ["sub/deep/locked"]
    assert any(s.startswith("sub/deep/locked (directory:") for s in skips)


def test_the_root_listing_failure_is_the_empty_prefix(tmp_path, monkeypatch):
    real = os.scandir

    def failing(target):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "scandir", failing)
    try:
        rels, skips, prefixes = _walk(tmp_path)
    finally:
        monkeypatch.setattr(os, "scandir", real)
    assert rels == [] and prefixes == [""]
    assert skips and skips[0].startswith(". (directory:")


class _Entry:
    """A `DirEntry` stand-in whose `is_dir` can fail."""

    def __init__(self, real, fail: bool):
        self._real = real
        self.name = real.name
        self._fail = fail

    def is_dir(self, *, follow_symlinks=True):
        if self._fail:
            raise OSError(5, "Input/output error")
        return self._real.is_dir(follow_symlinks=follow_symlinks)


def _scandir_failing_type_of(monkeypatch, name: str):
    real = os.scandir

    @contextlib.contextmanager
    def fake(target):
        with real(target) as entries:
            yield [_Entry(e, e.name == name) for e in entries]

    monkeypatch.setattr(os, "scandir", fake)


def test_an_entry_whose_type_lookup_fails_is_a_failed_prefix(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.md").write_text("b")
    _scandir_failing_type_of(monkeypatch, "sub")
    rels, skips, prefixes = _walk(tmp_path)
    assert rels == ["a.md"]
    assert prefixes == ["sub"]


def test_deliberate_non_descents_are_not_failed_prefixes(tmp_path):
    """ELOOP (a directory symlink) keeps its present treatment (spec)."""
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "n.md").write_text("n")
    os.symlink(tmp_path / "real", tmp_path / "link")
    rels, skips, prefixes = _walk(tmp_path)
    assert rels == ["real/n.md"] and prefixes == [] and skips == []


# ── D2: the protected-path rule ──────────────────────────────────────────


def test_a_prefix_protects_itself_and_its_subtree_not_a_shared_name_sibling():
    locked = ["sub", "sub/a.md", "sub/x/b.md", "subway.md", "sub.md", "other/sub/c.md"]
    assert _walk_protected(locked, ["sub"]) == {"sub", "sub/a.md", "sub/x/b.md"}


def test_nested_and_root_prefixes():
    locked = ["a/b/c.md", "a/bc.md", "a/b.md", "z.md"]
    assert _walk_protected(locked, ["a/b"]) == {"a/b/c.md"}
    assert _walk_protected(locked, [""]) == set(), "the root aborts instead"
    assert _walk_protected(locked, []) == set()


# ── D3: the indeterminate predicate ──────────────────────────────────────


def _auth(assignment="/v", ttl=900):
    return empty_prune.Authorisation(
        scope=None, expires_at=empty_prune._clock() + ttl, assignment=assignment
    )


def test_root_failure_is_indeterminate_even_with_a_permission():
    with pytest.raises(IndexIndeterminate) as exc:
        _check_determinate(
            walk_failed_prefixes=[""], seen={"a.md"}, rows=3,
            auth=_auth(), assignment="/v",
        )
    assert "could not be listed" in str(exc.value)


def test_empty_with_rows_is_indeterminate_and_names_no_path():
    with pytest.raises(IndexIndeterminate) as exc:
        _check_determinate(
            walk_failed_prefixes=[], seen=set(), rows=50, auth=None,
            assignment="/srv/secret-vault",
        )
    msg = str(exc.value)
    assert "50 note(s)" in msg and "nothing was deleted" in msg
    assert "Danger zone" in msg and "/srv/secret-vault" not in msg


def test_empty_without_rows_and_populated_roots_are_determinate():
    _check_determinate(
        walk_failed_prefixes=[], seen=set(), rows=0, auth=None, assignment="/v"
    )
    _check_determinate(
        walk_failed_prefixes=["sub"], seen={"a.md"}, rows=9, auth=None,
        assignment="/v",
    )


def test_a_permission_authorises_only_unexpired_and_for_its_assignment(monkeypatch):
    kw = dict(walk_failed_prefixes=[], seen=set(), rows=5)
    _check_determinate(auth=_auth("/v"), assignment="/v", **kw)
    with pytest.raises(IndexIndeterminate):
        _check_determinate(auth=_auth("/other"), assignment="/v", **kw)
    with pytest.raises(IndexIndeterminate):
        _check_determinate(auth=_auth("/v", ttl=-1), assignment="/v", **kw)


# ── D5: the registry ─────────────────────────────────────────────────────


def test_grant_take_is_single_use_and_per_scope(caplog):
    with caplog.at_level(logging.WARNING, logger="src.services.empty_prune"):
        empty_prune.grant(7, "/v7", granted_by="admin")
    assert empty_prune.pending(7) and not empty_prune.pending(8)
    assert any("admin" in r.getMessage() for r in caplog.records)
    with caplog.at_level(logging.WARNING, logger="src.services.empty_prune"):
        auth = empty_prune.take(7)
    assert auth.scope == 7 and auth.assignment == "/v7" and auth.valid_for("/v7")
    assert any("taken" in r.getMessage() for r in caplog.records)
    assert empty_prune.take(7) is None and not empty_prune.pending(7)


def test_a_permission_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(empty_prune, "_clock", lambda: now[0])
    empty_prune.grant(None, "/v")
    now[0] += 899
    assert empty_prune.pending(None)
    now[0] += 2
    assert not empty_prune.pending(None)
    auth = empty_prune.take(None)
    assert auth is not None and not auth.valid_for("/v")


# ── D4: accounting and /health ───────────────────────────────────────────


def test_walk_incomplete_counts_alerts_once_and_resets(monkeypatch, caplog):
    monkeypatch.setattr(
        indexer_health.settings, "indexer_degraded_after_failures", 3, raising=False
    )
    with caplog.at_level(logging.CRITICAL, logger="src.services.indexer_health"):
        for _ in range(4):
            indexer_health.record_walk(5, True)
    assert indexer_health._scopes[5].walk_incomplete == 4
    assert sum(r.levelno == logging.CRITICAL for r in caplog.records) == 1
    snap = indexer_health.snapshot()
    assert snap["status"] == "degraded"
    assert snap["failing_scopes"] == 1 and snap["max_consecutive_failures"] == 4
    indexer_health.record_walk(5, False)
    assert indexer_health._scopes[5].walk_incomplete == 0
    assert indexer_health.snapshot()["status"] == "ok"


def test_failing_scopes_counts_distinct_scopes(monkeypatch):
    monkeypatch.setattr(
        indexer_health.settings, "indexer_degraded_after_failures", 2, raising=False
    )
    for _ in range(2):
        indexer_health.record_walk(1, True)
        indexer_health.record_index(1, False)
        indexer_health.record_rederive(2, False)
    snap = indexer_health.snapshot()
    assert snap["failing_scopes"] == 2


def test_record_index_outcome_feeds_the_walk_counter():
    incomplete = IndexPassResult(3, 1, walk_failed=("sub",), walk_protected=2)
    indexer.record_index_outcome(None, True, incomplete)
    assert indexer_health._scopes[None].walk_incomplete == 1
    indexer.record_index_outcome(None, False)
    assert indexer_health._scopes[None].walk_incomplete == 1, (
        "a pass that raised leaves the walk counter alone"
    )
    indexer.record_index_outcome(None, True, IndexPassResult(3, 0))
    assert indexer_health._scopes[None].walk_incomplete == 0


def test_the_run_record_names_the_directories_and_reads_failed():
    dirs = [f"d{i}" for i in range(7)] + ["new\nline"]
    line = format_walk_incomplete(dirs)
    assert line.startswith("walk incomplete: 8 dir(s) not listed: d0, d1")
    assert line.endswith("d4 (+3 more)") and "\n" not in line
    assert "…" not in line
    # Exactly at the limit: no remainder marker.
    assert format_walk_incomplete([f"d{i}" for i in range(5)]).endswith("d3, d4")
    # The WARNING renders through the same helper: escapes and remainder.
    assert indexer._format_dir_list(["a\rb"] + [f"x{i}" for i in range(5)]) == (
        "a\\rb, x0, x1, x2, x3 (+1 more)"
    )
    stats = PassStats()
    stats.record_index(IndexPassResult(5, 2, walk_failed=("bad\udce9",)))
    assert stats.error_text == "walk incomplete: 1 dir(s) not listed: bad\\udce9"
    assert indexer_health.run_outcome(stats.error_text) == indexer_health.RUN_FAILED
    stats.record_index(IndexPassResult(5, 2))
    assert len(stats.errors) == 1


# ── The manual single-user reindex isolates its stages (D3, codex m1) ────


def test_manual_single_user_reindex_embeds_after_an_indeterminate_index(monkeypatch):
    import src.control_panel.routes as routes

    monkeypatch.setattr(routes.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(indexer, "index_pass_lock", asyncio.Lock())
    recorded: list[PassStats] = []

    @contextlib.asynccontextmanager
    async def fake_run(trigger, user_id=None):
        stats = PassStats()
        recorded.append(stats)
        yield stats

    async def no_overlaps(_where):
        return True

    async def indeterminate(*_a, **_k):
        raise IndexIndeterminate("vault root is empty but the index holds 2 note(s)")

    embedded: list[bool] = []

    async def embed(*_a, **_k):
        embedded.append(True)
        return 0

    monkeypatch.setattr(indexer, "record_indexer_run", fake_run)
    monkeypatch.setattr(indexer, "detect_root_overlaps", no_overlaps)
    monkeypatch.setattr(indexer, "index_vault", indeterminate)
    monkeypatch.setattr(indexer, "embed_vault", embed)

    asyncio.run(routes._reindex_background())

    assert embedded == [True], "the embed stage runs after a failed index stage"
    assert recorded[0].errors[0].startswith("index: IndexIndeterminate")
    assert indexer_health._scopes[None].index_consecutive_failures == 1
    assert indexer_health._scopes[None].embed_consecutive_failures == 0


# ── The scope-targeted pass ──────────────────────────────────────────────


def test_index_scope_now_runs_exactly_one_scope_full_hash(monkeypatch):
    monkeypatch.setattr(indexer.settings, "multi_user_mode", True, raising=False)
    monkeypatch.setattr(indexer, "index_pass_lock", asyncio.Lock())
    calls: list = []

    @contextlib.asynccontextmanager
    async def fake_run(trigger, user_id=None):
        calls.append(("run", trigger, user_id))
        yield PassStats()

    @contextlib.asynccontextmanager
    async def fake_session():
        yield object()

    async def warm(_session, user_id=None):
        calls.append(("warm", user_id))

    async def no_overlaps(_where):
        return True

    async def index(user_id=None, *, full_hash=False):
        calls.append(("index", user_id, full_hash))
        return IndexPassResult(0, 0)

    async def embed(user_id=None):
        calls.append(("embed", user_id))
        return 0

    monkeypatch.setattr(indexer, "record_indexer_run", fake_run)
    monkeypatch.setattr(indexer, "async_session", fake_session)
    monkeypatch.setattr(indexer, "warm_user_vault_cache", warm)
    monkeypatch.setattr(indexer, "detect_root_overlaps", no_overlaps)
    monkeypatch.setattr(indexer, "index_vault", index)
    monkeypatch.setattr(indexer, "embed_vault", embed)

    assert asyncio.run(indexer.index_scope_now(4)) is True
    assert calls == [
        ("warm", 4), ("run", "manual", 4), ("index", 4, True), ("embed", 4),
    ]
    with pytest.raises(ValueError):
        asyncio.run(indexer.index_scope_now(None))
