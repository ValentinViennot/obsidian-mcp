"""The change feed against a real PostgreSQL (#351).

What needs the database: the per-scope sequence assigned under the advisory
lock (cursor exactness under concurrent writers), suppression read under the
lock, the pass's stamp and events in its own transaction (poison restart,
baseline, overflow, a lock timeout leaving a durable gap), the write tools'
attributed events, tenancy, the root binding, permanent user deletion without
a lock cycle, retention and its watermark, and the keyset paging of
`get_recent` / `list_notes` over a tie group larger than a page.

Most scenarios run in single-user mode against a real vault directory and the
real index pass; the multi-user ones (tenancy, reassignment, deletion) drive
the append helper and `get_changes` directly with explicit user ids.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set (see `_harness`).
"""
import asyncio
import datetime
import hashlib
import os
import re

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
import src.mcp_server.tools as tools
from src.auth.session import current_actor, current_principal, current_user_id
from src.config import settings
from src.mcp_server.auth import current_permission
from src.services import change_feed, indexer, security_events
from src.services.change_feed import Attribution, Event
from src.services.note_hash import CanonicalNoteHasher, canonical_note_hash
import _harness
import _poison

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8
UTC = datetime.timezone.utc
FEED_TABLES = ("note_events", "note_event_scopes", "note_event_gaps")


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("change_feed_351", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await _poison.install(maker)
    yield maker
    await _poison.uninstall(maker)
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def vault(sessionmaker, monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    for target in (settings, tools.settings, indexer.settings, change_feed.settings):
        monkeypatch.setattr(target, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)
    tokens = [
        (current_permission, current_permission.set("readwrite")),
        (current_actor, current_actor.set(("api_key", "laptop", "omcp_lap"))),
        (current_principal, current_principal.set(("api_key", 12))),
    ]
    indexer._last_full_hash.clear()
    indexer.clear_quarantine()
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM poison_rules"))
        for table in FEED_TABLES + ("note_tasks", "note_links", "note_embeddings",
                                    "notes_metadata", "users"):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()
    try:
        yield root
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
        indexer._last_full_hash.clear()
        indexer.clear_quarantine()


async def index_once():
    await indexer.index_vault(user_id=None)


async def feed(sessionmaker, uid=None):
    where = "user_id IS NULL" if uid is None else "user_id = :u"
    async with sessionmaker() as session:
        return (await session.execute(
            text(f"SELECT * FROM note_events WHERE {where} ORDER BY seq"), {"u": uid}
        )).fetchall()


async def note_events(sessionmaker, uid=None):
    return [r for r in await feed(sessionmaker, uid) if r.kind not in ("gap", "reset")]


async def stamp(sessionmaker, path):
    async with sessionmaker() as session:
        return (await session.execute(
            text("SELECT content_changed_at FROM notes_metadata WHERE file_path = :p"),
            {"p": path},
        )).scalar_one_or_none()


async def changes(uid=None, principal=("api_key", 12), **kw):
    t1 = current_user_id.set(uid)
    t2 = current_principal.set(principal)
    try:
        return await tools.get_changes_impl.__wrapped__(**kw)
    finally:
        current_principal.reset(t2)
        current_user_id.reset(t1)


def next_since(out):
    return out.strip().splitlines()[-1].removeprefix("next_since: ")


def event_lines(out):
    return [ln for ln in out.splitlines() if ln.startswith("- ev:")]


async def baseline(root, sessionmaker, name="seed.md"):
    """A first pass over an empty index is one `reset`, no creations."""
    (root / name).write_text("# seed\n")
    await index_once()
    rows = await feed(sessionmaker)
    assert [r.kind for r in rows] == ["reset"]


# ── 6.5 stamping and pass events ─────────────────────────────────────────────


async def test_baseline_is_one_reset_and_an_empty_vault_writes_nothing(vault, sessionmaker):
    await index_once()
    assert await feed(sessionmaker) == []
    for i in range(5):
        (vault / f"n{i}.md").write_text(f"note {i}\n")
    await index_once()
    rows = await feed(sessionmaker)
    assert [r.kind for r in rows] == ["reset"] and rows[0].seq == 1
    await index_once()
    assert len(await feed(sessionmaker)) == 1


async def test_the_stamp_moves_only_on_a_hash_change(vault, sessionmaker):
    await baseline(vault, sessionmaker)
    note = vault / "a.md"
    note.write_text("first\n")
    old = datetime.datetime(2020, 1, 1, tzinfo=UTC).timestamp()
    os.utime(note, (old, old))
    await index_once()
    first = await stamp(sessionmaker, "a.md")
    # A first-seen row takes the file's mtime.
    assert first == datetime.datetime(2020, 1, 1, tzinfo=UTC)
    assert [(r.kind, r.path, r.source) for r in await note_events(sessionmaker)] == [
        ("created", "a.md", "index")]

    os.utime(note, None)  # a touch
    await index_once()
    await indexer.index_vault(user_id=None, full_hash=True)
    assert await stamp(sessionmaker, "a.md") == first
    async with sessionmaker() as session:  # a stale extraction marker
        await session.execute(text("UPDATE notes_metadata SET extraction_version = 3"))
        await session.commit()
    await index_once()
    assert await stamp(sessionmaker, "a.md") == first
    assert len(await note_events(sessionmaker)) == 1

    note.write_text("second\n")
    await index_once()
    changed = await stamp(sessionmaker, "a.md")
    assert changed > first
    events = await note_events(sessionmaker)
    assert [(e.kind, e.content_hash) for e in events][-1] == (
        "modified", canonical_note_hash(b"second\n"))

    # An edit that keeps every stat field, found only by the full-hash pass.
    st = note.stat()
    with open(note, "r+b") as fh:
        fh.write(b"SECOND\n")
    os.utime(note, ns=(st.st_atime_ns, st.st_mtime_ns))
    await indexer.index_vault(user_id=None, full_hash=True)
    assert await stamp(sessionmaker, "a.md") > changed

    before = await stamp(sessionmaker, "a.md")
    note.rename(vault / "b.md")  # an external, id-preserving move
    await index_once()
    assert await stamp(sessionmaker, "b.md") == before
    last = (await note_events(sessionmaker))[-1]
    assert (last.kind, last.from_path, last.path) == ("moved", "a.md", "b.md")


async def test_deletions_record_their_reason_and_unindexable_paths_once(vault, sessionmaker):
    await baseline(vault, sessionmaker)
    (vault / "gone.md").write_text("bye\n")
    (vault / "bad.md").write_text("fine for now\n")
    await index_once()
    (vault / "gone.md").unlink()
    (vault / "bad.md").write_bytes(b"\xff\xfe not utf-8")
    (vault / "new-bad.md").write_bytes(b"\xff new and not utf-8")
    await index_once()
    await index_once()
    events = [(e.kind, e.path, e.reason) for e in await note_events(sessionmaker)][2:]
    assert sorted(events) == sorted([
        ("deleted", "bad.md", "not_indexable"),
        ("deleted", "gone.md", "removed"),
        ("created", "new-bad.md", "not_indexable"),
    ]), events
    out = await changes(since="ev:0")
    assert '"bad.md" reason=not_indexable by human/sync' in out


async def test_quarantine_and_a_poison_restart_write_events_once(vault, sessionmaker):
    await baseline(vault, sessionmaker)
    (vault / "edited.md").write_text("v1\n")
    await index_once()
    (vault / "edited.md").write_text("v2\n")
    (vault / "fresh-poison.md").write_text("new\n")
    (vault / "ok.md").write_text("ok\n")
    await _poison.poison(sessionmaker, "upsert", "edited.md", "22001")
    await _poison.poison(sessionmaker, "upsert", "fresh-poison.md", "22001")
    result = await indexer.index_vault(user_id=None)
    assert set(result.quarantined) == {"edited.md", "fresh-poison.md"}
    events = [(e.kind, e.path, e.reason) for e in await note_events(sessionmaker)][1:]
    # The re-run's events only: the quarantined edit leaves the index, the
    # quarantined new note has no row and so no event, the healthy note once.
    assert sorted(events) == [("created", "ok.md", None),
                              ("deleted", "edited.md", "quarantined")], events
    await _poison.cure(sessionmaker)


async def test_overflow_is_one_gap_with_the_remainder(vault, sessionmaker, monkeypatch):
    await baseline(vault, sessionmaker)
    monkeypatch.setattr(settings, "change_feed_max_events_per_pass", 3, raising=False)
    for i in range(5):
        (vault / f"bulk{i}.md").write_text(f"{i}\n")
    await index_once()
    rows = (await feed(sessionmaker))[1:]
    assert [r.kind for r in rows] == ["created"] * 3 + ["gap"]
    assert (rows[-1].gap_reason, rows[-1].gap_count) == ("pass_overflow", 2)
    out = await changes(since="ev:0", kinds=["deleted"])
    assert "gap pass_overflow count=2" in out and "reset" in out


# ── suppression ───────────────────────────────────────────────────────────────


async def test_an_agent_edit_is_recorded_once_and_a_human_edit_after_it_too(vault, sessionmaker):
    await baseline(vault, sessionmaker)
    (vault / "a.md").write_text("one\n")
    await index_once()
    out = await tools.edit_note_impl("a.md", "two\n")
    assert "MCP-REFUSAL" not in out, out
    await index_once()
    mine = (await note_events(sessionmaker))[1:]
    assert [(e.kind, e.source, e.tool, e.actor_label, e.principal_ref) for e in mine] == [
        ("modified", "mcp", "edit_note", "laptop", "12")]
    assert mine[0].content_hash == canonical_note_hash(b"two\n")

    await tools.edit_note_impl("a.md", "three\n")
    (vault / "a.md").write_text("four, by a person\n")
    await index_once()
    tail = (await note_events(sessionmaker))[2:]
    assert [(e.kind, e.source) for e in tail] == [("modified", "mcp"), ("modified", "index")]
    out = await changes(since="ev:0")
    assert 'by api_key "laptop" via edit_note (you)' in out
    assert event_lines(out)[-1].endswith("by human/sync")


async def test_crlf_write_file_and_a_nul_upload_are_suppressed(vault, sessionmaker):
    await baseline(vault, sessionmaker)
    import base64

    crlf = b"line one\r\nline two\r\n"
    out = await tools.write_file_impl("crlf.md", base64.b64encode(crlf).decode())
    assert "MCP-REFUSAL" not in out, out
    # The transfer route's hash comes from the streaming hasher; publish the
    # bytes and record exactly what the route records after a redemption.
    nul = b"has a \x00 nul\r\n"
    hasher = CanonicalNoteHasher()
    for i in range(0, len(nul), 3):
        hasher.update(nul[i:i + 3])
    (vault / "nul.md").write_bytes(nul)
    await change_feed.record_tool_events(
        [Event("created", path="nul.md", content_hash=hasher.hexdigest())],
        root=vault, session_factory=sessionmaker, user_id=None,
        attribution=Attribution.of(tool=change_feed.TRANSFER_UPLOAD_TOOL,
                                   actor={"actor_kind": "oauth", "actor_label": "Phone"},
                                   principal=("oauth", "grant-1")),
    )
    await index_once()
    events = await note_events(sessionmaker)
    assert [(e.path, e.source) for e in events] == [("crlf.md", "mcp"), ("nul.md", "mcp")]
    async with sessionmaker() as session:
        stored = dict((await session.execute(text(
            "SELECT file_path, content_hash FROM notes_metadata"))).fetchall())
    assert events[0].content_hash == stored["crlf.md"]
    assert events[1].content_hash == stored["nul.md"]


# ── move_note ─────────────────────────────────────────────────────────────────


async def test_move_note_records_one_attributed_move_and_the_pass_none(vault, sessionmaker):
    await baseline(vault, sessionmaker)
    (vault / "A.md").write_text("moving\n")
    await index_once()
    out = await tools.move_note_impl("A.md", "B.md")
    assert "Moved" in out and "warning" not in out, out
    await index_once()
    events = (await note_events(sessionmaker))[1:]
    assert [(e.kind, e.from_path, e.path, e.source, e.tool) for e in events] == [
        ("moved", "A.md", "B.md", "mcp", "move_note")]


async def test_a_failed_move_event_rolls_back_and_the_pass_records_the_move(
    vault, sessionmaker, monkeypatch,
):
    await baseline(vault, sessionmaker)
    # A self-link: with rewrite_links=True the moved note itself is a rewrite
    # source, and rewriting it would change its hash and break the pairing.
    body = "moving, see [[A]]\n"
    (vault / "A.md").write_text(body)
    await index_once()
    real = change_feed.append_events

    async def failing(session, uid, events, **kw):
        if not kw.get("bounded", True):
            raise RuntimeError("forced")
        return await real(session, uid, events, **kw)

    monkeypatch.setattr(change_feed, "append_events", failing)
    out = await tools.move_note_impl("A.md", "B.md", rewrite_links=True)
    assert "DB update failed" in out, out
    assert "link rewrites were stopped" in out, out
    assert (vault / "B.md").read_text() == body, "no rewrite after a failed metadata update"
    monkeypatch.setattr(change_feed, "append_events", real)
    async with sessionmaker() as session:
        assert (await session.execute(text(
            "SELECT file_path FROM notes_metadata WHERE file_path IN ('A.md','B.md')"
        ))).scalar_one() == "A.md"
    await index_once()
    events = (await note_events(sessionmaker))[1:]
    assert [(e.kind, e.from_path, e.path, e.source) for e in events] == [
        ("moved", "A.md", "B.md", "index")]


async def test_the_move_cycle_is_three_moves(vault, sessionmaker):
    await baseline(vault, sessionmaker)
    (vault / "A.md").write_text("cycle\n")
    await index_once()
    await tools.move_note_impl("A.md", "B.md")
    (vault / "B.md").rename(vault / "A.md")
    await index_once()
    (vault / "A.md").rename(vault / "B.md")
    await index_once()
    events = (await note_events(sessionmaker))[1:]
    assert [(e.kind, e.from_path, e.path, e.source) for e in events] == [
        ("moved", "A.md", "B.md", "mcp"),
        ("moved", "B.md", "A.md", "index"),
        ("moved", "A.md", "B.md", "index"),
    ]


# ── 6.9 the write tools ───────────────────────────────────────────────────────


async def test_each_write_tool_records_its_kind(vault, sessionmaker):
    import base64

    await baseline(vault, sessionmaker)
    assert "Created" in await tools.create_note_impl("w.md", "hello\n")
    await tools.edit_note_impl("w.md", "hello again\n")
    await tools.set_frontmatter_impl("w.md", updates={"status": "done"})
    await tools.write_file_impl("w.md", base64.b64encode(b"raw\n").decode(), overwrite=True)
    await tools.write_file_impl("x.bin", base64.b64encode(b"bin").decode())  # not a note
    await tools.edit_note_impl("w.md", "dry\n", dry_run=True)  # dry run
    refused = await tools.edit_note_impl("w.md", "stale\n", expected_hash="0" * 64)
    assert "MCP-REFUSAL" in refused
    await tools.delete_note_impl("w.md")
    events = await note_events(sessionmaker)
    assert [(e.kind, e.tool) for e in events] == [
        ("created", "create_note"), ("modified", "edit_note"),
        ("modified", "set_frontmatter"), ("modified", "write_file"),
        ("deleted", "delete_note"),
    ]
    assert all(e.actor_kind == "api_key" and e.principal_kind == "api_key" for e in events)
    assert events[-1].reason == "removed"
    assert events[3].content_hash == canonical_note_hash(b"raw\n")


async def test_a_failing_event_insert_changes_nothing_the_caller_sees(
    vault, sessionmaker, monkeypatch,
):
    await baseline(vault, sessionmaker)

    async def broken(*_a, **_k):
        raise RuntimeError("no feed today")

    logged = []

    async def record_usage(tool, params, duration_ms, response_size):
        logged.append((tool, dict(params), response_size))
        return True

    monkeypatch.setattr(tools, "_log_usage", record_usage)
    expected = await tools.create_note_impl("ok.md", "body\n")
    real = change_feed.append_events
    monkeypatch.setattr(change_feed, "append_events", broken)
    got = await tools.create_note_impl("ok2.md", "body\n")
    monkeypatch.setattr(change_feed, "append_events", real)
    assert got == expected.replace("ok.md", "ok2.md")
    # The usage rows are the same success: no error marker, same shape.
    assert [t for t, _, _ in logged] == ["create_note", "create_note"]
    first, second = (p for _, p, _ in logged)
    for params in (first, second):
        assert "error" not in params and "outcome" not in params, params
    first.pop("path"), second.pop("path")
    first.pop("concurrency", None), second.pop("concurrency", None)
    assert first == second
    assert (logged[0][2], logged[1][2]) == (len(expected), len(got))
    await index_once()
    events = await note_events(sessionmaker)
    assert [(e.path, e.source) for e in events] == [("ok.md", "mcp"), ("ok2.md", "index")]


# ── 6.6 cursor safety ─────────────────────────────────────────────────────────


async def test_a_late_commit_never_falls_behind_a_returned_cursor(vault, sessionmaker):
    await baseline(vault, sessionmaker)
    first = sessionmaker()
    second = sessionmaker()
    release = asyncio.Event()
    second_inserted = asyncio.Event()
    task = None
    try:
        await change_feed.append_events(first, None, [Event("created", path="one.md")],
                                        source="index", bounded=False)

        async def later():
            await change_feed.append_events(second, None, [Event("created", path="two.md")],
                                            source="index", bounded=False)
            second_inserted.set()
            await release.wait()
            await second.commit()

        task = asyncio.create_task(later())
        await asyncio.sleep(0.2)
        assert not second_inserted.is_set(), "the second append must wait for the lock"
        await first.commit()
        await asyncio.wait_for(second_inserted.wait(), 5)
        out = await changes(since="ev:1")
        cursor = next_since(out)
        assert [ln.split()[4] for ln in event_lines(out)] == ['"one.md"']
        assert cursor == "ev:2"
        release.set()
        await task
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
        await first.close()
        await second.close()
    out = await changes(since=cursor)
    assert [ln.split()[4] for ln in event_lines(out)] == ['"two.md"']
    assert int(event_lines(out)[0].split()[1][3:]) == 3


async def test_paging_returns_every_event_once(vault, sessionmaker):
    async with sessionmaker() as session:
        await change_feed.append_events(
            session, None, [Event("created", path=f"p{i:03d}.md") for i in range(120)],
            source="index", bounded=False)
        await session.commit()
    seen, cursor = [], "ev:0"
    for _ in range(3):
        out = await changes(since=cursor, limit=50)
        seen += [int(ln.split()[1][3:]) for ln in event_lines(out)]
        cursor = next_since(out)
    assert seen == list(range(1, 121))
    assert "more: false" in (await changes(since=cursor)).splitlines()[0]
    assert next_since(await changes(since=cursor)) == "ev:120"


async def test_a_cursor_above_last_seq_is_refused(vault, sessionmaker):
    await baseline(vault, sessionmaker)
    out = await changes(since="ev:900")
    assert out.rstrip().splitlines()[-1].startswith('MCP-REFUSAL {"code":"invalid_argument"')


async def test_a_lock_timeout_leaves_a_durable_gap(vault, sessionmaker, monkeypatch):
    await baseline(vault, sessionmaker)
    monkeypatch.setattr(settings, "change_feed_lock_timeout_ms", 100, raising=False)
    from src.services import indexer_health

    indexer_health.reset()
    before = indexer_health.snapshot()
    holder = sessionmaker()
    await change_feed.lock_scope(holder, None)
    (vault / "late.md").write_text("late\n")
    result = await indexer.index_vault(user_id=None)  # commits despite the lock
    assert result[1] == 1
    indexer.record_index_outcome(None, True, result)
    after = indexer_health.snapshot()
    assert after["status"] == before["status"] == "ok"
    assert after.get("failing_scopes", 0) == before.get("failing_scopes", 0) == 0
    await holder.rollback()
    await holder.close()
    async with sessionmaker() as session:
        assert (await session.execute(text(
            "SELECT gap_count FROM note_event_gaps"))).scalars().all() == [1]
    (vault / "later.md").write_text("later\n")
    await index_once()
    kinds = [(r.kind, r.gap_reason, r.gap_count, r.path) for r in (await feed(sessionmaker))[1:]]
    assert kinds == [("gap", "write_failed", 1, None), ("created", None, None, "later.md")]


# ── 6.8 tenancy and `by` ──────────────────────────────────────────────────────


async def make_users(sessionmaker, *uids, root="/vaults/x"):
    async with sessionmaker() as session:
        for uid in uids:
            await session.execute(text(
                "INSERT INTO users (id, username, password_hash, vault_path) "
                "VALUES (:i, :u, 'x', :v)"), {"i": uid, "u": f"user{uid}", "v": f"{root}{uid}"})
        await session.commit()


async def append(sessionmaker, uid, events, attribution=None, source="mcp"):
    async with sessionmaker() as session:
        n = await change_feed.append_events(session, uid, events, source=source,
                                            attribution=attribution)
        await session.commit()
        return n


KEY_A = Attribution(tool="edit_note", actor_kind="api_key", actor_label="key A",
                    principal_kind="api_key", principal_ref="31")
GRANT = Attribution(tool="edit_note", actor_kind="oauth", actor_label="Client",
                    principal_kind="oauth", principal_ref="grant-xyz")


async def test_tenants_see_gapless_private_feeds(vault, sessionmaker):
    await make_users(sessionmaker, 7, 9)
    for i in range(10):
        await append(sessionmaker, 7, [Event("modified", path=f"seven{i}.md")], KEY_A)
        await append(sessionmaker, 9, [Event("modified", path=f"nine{i}.md")], GRANT)
    seven = await changes(7, since="ev:0")
    assert [int(ln.split()[1][3:]) for ln in event_lines(seven)] == list(range(1, 11))
    assert "nine" not in seven and "Client" not in seven
    ownerless = await changes(None, since="1970-01-01")
    assert "seven" not in ownerless and "nine" not in ownerless


async def test_by_me_is_the_exact_principal(vault, sessionmaker):
    await make_users(sessionmaker, 7)
    await append(sessionmaker, 7, [Event("modified", path="a.md")], KEY_A)
    await append(sessionmaker, 7, [Event("modified", path="b.md")], GRANT)
    await append(sessionmaker, 7, [Event("modified", path="c.md")], source="index")
    me_key = await changes(7, principal=("api_key", 31), since="ev:0", by="me")
    assert [ln.split()[4] for ln in event_lines(me_key)] == ['"a.md"']
    # A refreshed OAuth token is the same grant, so still "me".
    me_grant = await changes(7, principal=("oauth", "grant-xyz"), since="ev:0", by="me")
    assert [ln.split()[4] for ln in event_lines(me_grant)] == ['"b.md"']
    other_key = await changes(7, principal=("api_key", 32), since="ev:0", by="me")
    assert event_lines(other_key) == [] and next_since(other_key) == "ev:3"
    agents = await changes(7, principal=("api_key", 32), since="ev:0", by="agents")
    assert len(event_lines(agents)) == 2 and 'by api_key "key A"' in agents
    human = await changes(7, since="ev:0", by="human")
    assert [ln.split()[4] for ln in event_lines(human)] == ['"c.md"']
    refused = await changes(7, principal=None, since="ev:0", by="me")
    assert "invalid_argument" in refused


async def test_a_move_out_of_the_folder_is_in_the_folders_feed(vault, sessionmaker):
    await make_users(sessionmaker, 7)
    await append(sessionmaker, 7, [Event("moved", path="Archive/A.md",
                                         from_path="Projects/A.md")], KEY_A)
    await append(sessionmaker, 7, [Event("modified", path="Other/x.md")], KEY_A)
    out = await changes(7, since="ev:0", folder="Projects/")
    assert len(event_lines(out)) == 1 and '"Projects/A.md" -> "Archive/A.md"' in out


# ── reassignment, discard and deletion ────────────────────────────────────────


async def test_a_delayed_append_after_reassignment_writes_nothing(vault, sessionmaker, monkeypatch):
    await make_users(sessionmaker, 7)
    emitted = []
    monkeypatch.setattr(security_events, "emit",
                        lambda event, **kw: emitted.append((event, kw)))
    async with sessionmaker() as session:
        await session.execute(text("UPDATE users SET vault_path = '/vaults/B' WHERE id = 7"))
        await session.commit()
    async with sessionmaker() as session:
        await change_feed.discard_scope(session, 7)
        await session.commit()
    await change_feed.record_tool_events(
        [Event("created", path="Secret.md")], root="/vaults/x7",
        session_factory=sessionmaker, user_id=7, attribution=KEY_A)
    rows = await feed(sessionmaker, 7)
    assert [r.kind for r in rows] == ["reset"]
    assert [e for e, _ in emitted] == ["change_feed_root_changed"]
    assert emitted[0][1]["user_id"] == 7 and "Secret" not in repr(emitted)


async def test_an_append_before_the_discard_is_purged_and_truncates_old_cursors(
    vault, sessionmaker,
):
    await make_users(sessionmaker, 7)
    await change_feed.record_tool_events(
        [Event("created", path="Secret.md")], root="/vaults/x7",
        session_factory=sessionmaker, user_id=7, attribution=KEY_A)
    assert [r.path for r in await feed(sessionmaker, 7)] == ["Secret.md"]
    async with sessionmaker() as session:
        await change_feed.discard_scope(session, 7)
        await session.commit()
    rows = await feed(sessionmaker, 7)
    assert [(r.kind, r.seq) for r in rows] == [("reset", 2)]
    out = await changes(7, since="ev:0")
    assert "history_truncated: true" in out.splitlines()[0] and "Secret" not in out


async def test_permanent_deletion_and_a_pass_append_do_not_deadlock(vault, sessionmaker):
    await make_users(sessionmaker, 7)
    async with sessionmaker() as session:
        await session.execute(text(
            "INSERT INTO notes_metadata (user_id, file_path, title, content_hash) "
            "VALUES (7, 'a.md', 'a', 'h')"))
        await session.commit()
    passer = sessionmaker()
    # The pass: row locks first, then its final append (the feed lock last).
    await passer.execute(text("SELECT id FROM notes_metadata WHERE user_id = 7 FOR UPDATE"))
    await change_feed.append_events(passer, 7, [Event("modified", path="a.md")],
                                    source="index", suppress=True)

    async def delete_user():
        async with sessionmaker() as session:
            await session.execute(text("DELETE FROM users WHERE id = 7"))
            await change_feed.purge_scope(session, 7)
            await session.commit()

    deleter = asyncio.create_task(delete_user())
    await asyncio.sleep(0.3)
    assert not deleter.done(), "the deletion waits on the pass's row locks"
    await passer.commit()
    await passer.close()
    await asyncio.wait_for(deleter, 10)
    async with sessionmaker() as session:
        for table in FEED_TABLES:
            assert (await session.execute(text(
                f"SELECT count(*) FROM {table} WHERE user_id = 7"))).scalar() == 0
    # And an append after the deletion committed writes nothing.
    assert await append(sessionmaker, 7, [Event("modified", path="a.md")], KEY_A) == 0
    assert await feed(sessionmaker, 7) == []


# ── 6.10 retention ────────────────────────────────────────────────────────────


async def test_retention_by_age_and_cap_keeps_the_newest_and_sets_the_watermark(
    vault, sessionmaker, monkeypatch,
):
    await make_users(sessionmaker, 7, 9)
    await append(sessionmaker, 7, [Event("modified", path=f"o{i}.md") for i in range(5)], KEY_A)
    async with sessionmaker() as session:
        await session.execute(text(
            "UPDATE note_events SET occurred_at = now() - interval '200 days' WHERE user_id = 7"))
        await session.commit()
    await append(sessionmaker, 9, [Event("modified", path="quiet.md")], KEY_A)
    async with sessionmaker() as session:
        await session.execute(text(
            "UPDATE note_events SET occurred_at = now() - interval '400 days' WHERE user_id = 9"))
        await session.commit()
    monkeypatch.setattr(settings, "change_feed_retention_days", 90, raising=False)
    removed = await change_feed.prune(session_factory=sessionmaker)
    assert removed == 4
    assert [r.seq for r in await feed(sessionmaker, 7)] == [5]
    assert [r.path for r in await feed(sessionmaker, 9)] == ["quiet.md"]  # newest kept
    async with sessionmaker() as session:
        scope = (await session.execute(text(
            "SELECT pruned_through_seq, pruned_at FROM note_event_scopes WHERE user_id = 7"
        ))).first()
    assert scope.pruned_through_seq == 4 and scope.pruned_at is not None
    assert "history_truncated: true" in (await changes(7, since="ev:2")).splitlines()[0]
    assert "history_truncated: true" in (await changes(7, since="1970-01-01")).splitlines()[0]
    assert "history_truncated" not in (await changes(7, since="ev:4")).splitlines()[0]

    # The cap applies with the age limit off.
    monkeypatch.setattr(settings, "change_feed_retention_days", None, raising=False)
    monkeypatch.setattr(settings, "change_feed_max_events_per_scope", 3, raising=False)
    await append(sessionmaker, 7, [Event("modified", path=f"n{i}.md") for i in range(6)], KEY_A)
    await change_feed.prune(session_factory=sessionmaker)
    assert [r.seq for r in await feed(sessionmaker, 7)] == [9, 10, 11]


async def test_the_backstop_removes_orphaned_scopes(vault, sessionmaker):
    async with sessionmaker() as session:
        await change_feed.append_events(session, 4242, [Event("modified", path="x.md")],
                                        source="index", bounded=False)
        await session.commit()
    # User 4242 never existed: the append wrote nothing at all.
    assert await feed(sessionmaker, 4242) == []
    async with sessionmaker() as session:
        await session.execute(text(
            "INSERT INTO note_events (user_id, seq, kind, path, source) "
            "VALUES (4243, 1, 'modified', 'x.md', 'index')"))
        await session.execute(text(
            "INSERT INTO note_event_scopes (user_id, last_seq) VALUES (4243, 1)"))
        await session.execute(text(
            "INSERT INTO note_event_gaps (user_id, gap_count) VALUES (4243, 2)"))
        await session.commit()
    await change_feed.prune(session_factory=sessionmaker)
    async with sessionmaker() as session:
        for table in FEED_TABLES:
            assert (await session.execute(text(
                f"SELECT count(*) FROM {table} WHERE user_id = 4243"))).scalar() == 0


# ── 6.5 the two listings ──────────────────────────────────────────────────────


async def seed_rows(sessionmaker, n, *, uid=None, when=None, prefix="t"):
    when = when or datetime.datetime(2026, 10, 25, 12, 0, 0, 123456, tzinfo=UTC)
    async with sessionmaker() as session:
        for i in range(n):
            await session.execute(text(
                "INSERT INTO notes_metadata (user_id, file_path, title, content_hash, "
                " modified_at, content_changed_at, file_size) "
                "VALUES (:u, :p, :t, 'h', :m, :c, 10)"),
                {"u": uid, "p": f"{prefix}{i:04d}.md", "t": f"T{i}",
                 "m": when + datetime.timedelta(seconds=i), "c": when})
        await session.commit()


def cursor_of(out):
    found = re.findall(r"^more: cursor=(\S+)$", out, re.M)
    return found[0] if found else None


def paths_of(out):
    return re.findall(r"^- `([^`]+)`", out, re.M)


async def test_keyset_paging_reaches_a_tie_group_larger_than_a_page(vault, sessionmaker):
    await seed_rows(sessionmaker, 501)
    seen, cursor = [], None
    for _ in range(3):
        kw = dict(order_by="content_changed", limit=500)
        if cursor:
            kw["cursor"] = cursor
        out = await tools.list_notes_impl.__wrapped__(**kw)
        seen += paths_of(out)
        cursor = cursor_of(out)
        if not cursor:
            break
    assert seen == [f"t{i:04d}.md" for i in range(501)]
    # get_recent pages the same way and labels the column it shows.
    first = await tools.get_recent_impl.__wrapped__(order_by="content_changed", limit=2)
    assert "(changed 2026-10-25 12:00 UTC)" in first and cursor_of(first)


async def test_windows_over_content_changes(vault, sessionmaker):
    base = datetime.datetime(2026, 10, 20, tzinfo=UTC)
    for day in (19, 20, 25, 26):
        await seed_rows(sessionmaker, 1, prefix=f"Projects/d{day}-",
                        when=base.replace(day=day, hour=23, minute=59, second=59))
    out = await tools.list_notes_impl.__wrapped__(
        folder="Projects/", order_by="content_changed", since="2026-10-20", until="2026-10-25")
    assert sorted(paths_of(out)) == ["Projects/d20-0000.md", "Projects/d25-0000.md"]


async def test_a_call_without_the_new_parameters_is_byte_identical(vault, sessionmaker):
    await seed_rows(sessionmaker, 21, prefix="Projects/x")
    out = await tools.get_recent_impl.__wrapped__(limit=20, folder="Projects/")
    async with sessionmaker() as session:
        rows = (await session.execute(text(
            "SELECT file_path, title, tags, modified_at FROM notes_metadata "
            "WHERE file_path LIKE 'Projects/%' AND user_id IS NULL "
            "ORDER BY modified_at DESC, file_path ASC LIMIT 20"))).fetchall()
    expected = [f"Last {len(rows)} modified notes:\n"]
    for n in rows:
        mod = n.modified_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        expected.append(f"- `{n.file_path}` — {n.title} (modified {mod})")
    assert out == "\n".join(expected)
    assert "more:" not in out


async def test_a_cursor_from_another_owner_is_refused(vault, sessionmaker):
    await make_users(sessionmaker, 7, 9)
    await seed_rows(sessionmaker, 3, uid=9, prefix="nine")
    await seed_rows(sessionmaker, 3, uid=7, prefix="seven")
    t = current_user_id.set(9)
    try:
        theirs = cursor_of(await tools.list_notes_impl.__wrapped__(
            order_by="content_changed", limit=1))
    finally:
        current_user_id.reset(t)
    t = current_user_id.set(7)
    try:
        out = await tools.list_notes_impl.__wrapped__(
            order_by="content_changed", limit=1, cursor=theirs)
    finally:
        current_user_id.reset(t)
    assert out.rstrip().splitlines()[-1].startswith('MCP-REFUSAL {"code":"invalid_argument"')
    assert "nine" not in out


async def test_hash_parity_with_the_stored_row(vault, sessionmaker):
    """The pass's stored hash is `canonical_note_hash` of the file's bytes."""
    await baseline(vault, sessionmaker)
    data = b"\xef\xbb\xbfbom\r\nmixed\rend\x00"
    (vault / "parity.md").write_bytes(data)
    await index_once()
    async with sessionmaker() as session:
        stored = (await session.execute(text(
            "SELECT content_hash FROM notes_metadata WHERE file_path = 'parity.md'"
        ))).scalar_one()
    assert stored == canonical_note_hash(data) != hashlib.sha256(data).hexdigest()


running_as_root = hasattr(os, "geteuid") and os.geteuid() == 0


async def test_move_note_keeps_the_stamp_and_a_requarantined_note_reenters_at_mtime(
    vault, sessionmaker,
):
    await baseline(vault, sessionmaker)
    (vault / "keep.md").write_text("content\n")
    await index_once()
    before = await stamp(sessionmaker, "keep.md")
    assert "Moved" in await tools.move_note_impl("keep.md", "kept.md")
    await index_once()
    assert await stamp(sessionmaker, "kept.md") == before

    (vault / "poisoned.md").write_text("v1\n")
    await index_once()
    (vault / "poisoned.md").write_text("v2\n")
    await _poison.poison(sessionmaker, "upsert", "poisoned.md", "22001")
    await index_once()
    assert await stamp(sessionmaker, "poisoned.md") is None  # quarantined: no row
    await _poison.cure(sessionmaker)
    (vault / "poisoned.md").write_text("v3, healthy\n")
    old = datetime.datetime(2021, 6, 1, tzinfo=UTC).timestamp()
    os.utime(vault / "poisoned.md", (old, old))
    await index_once()
    # It re-enters as a new row, stamped with the file's mtime.
    assert await stamp(sessionmaker, "poisoned.md") == datetime.datetime(2021, 6, 1, tzinfo=UTC)


@pytest.mark.skipif(running_as_root, reason="root ignores directory modes")
async def test_rows_under_an_unlistable_directory_emit_nothing(vault, sessionmaker):
    await baseline(vault, sessionmaker)
    sub = vault / "locked"
    sub.mkdir()
    (sub / "inner.md").write_text("inside\n")
    await index_once()
    count = len(await feed(sessionmaker))
    os.chmod(sub, 0)
    try:
        await index_once()
    finally:
        os.chmod(sub, 0o755)
    assert len(await feed(sessionmaker)) == count
    assert await stamp(sessionmaker, "locked/inner.md") is not None


async def test_an_invalid_utf8_tool_write_whose_event_failed_is_rediscovered(
    vault, sessionmaker, monkeypatch,
):
    import base64

    await baseline(vault, sessionmaker)
    (vault / "A.md").write_text("valid\n")
    await index_once()
    real = change_feed.append_events

    async def broken(*_a, **_k):
        raise RuntimeError("feed down")

    monkeypatch.setattr(change_feed, "append_events", broken)
    for name, overwrite in (("A.md", True), ("B.md", False)):
        out = await tools.write_file_impl(
            name, base64.b64encode(b"\xff\xfe not utf-8").decode(), overwrite=overwrite)
        assert "MCP-REFUSAL" not in out, out
    monkeypatch.setattr(change_feed, "append_events", real)
    # A restart: nothing about the lost events is held in process memory.
    indexer._last_full_hash.clear()
    await index_once()
    await index_once()
    events = [(e.kind, e.path, e.reason, e.source) for e in await note_events(sessionmaker)]
    assert events[1:] == [
        ("deleted", "A.md", "not_indexable", "index"),
        ("created", "B.md", "not_indexable", "index"),
    ], events


# ── fix round (Codex implementation review r1, verifier) ──────────────────────


async def test_age_retention_prunes_only_the_expired_prefix(vault, sessionmaker, monkeypatch):
    """`occurred_at` is not monotone in seq: an unexpired seq 1 below an
    expired seq 2 must survive, and so must everything after it."""
    await make_users(sessionmaker, 7)
    await append(sessionmaker, 7, [Event("modified", path=f"p{i}.md") for i in range(3)], KEY_A)
    async with sessionmaker() as session:
        await session.execute(text(
            "UPDATE note_events SET occurred_at = now() - interval '200 days' "
            "WHERE user_id = 7 AND seq = 2"))
        await session.commit()
    monkeypatch.setattr(settings, "change_feed_retention_days", 90, raising=False)
    assert await change_feed.prune(session_factory=sessionmaker) == 0
    assert [r.seq for r in await feed(sessionmaker, 7)] == [1, 2, 3]
    async with sessionmaker() as session:
        assert (await session.execute(text(
            "SELECT pruned_through_seq FROM note_event_scopes WHERE user_id = 7"
        ))).scalar() == 0


async def test_a_discard_drops_the_previous_roots_gap_rows(vault, sessionmaker):
    await make_users(sessionmaker, 7)
    await append(sessionmaker, 7, [Event("modified", path="old.md")], KEY_A)
    async with sessionmaker() as session:
        await change_feed.record_gap(session, 7, 4)  # a failed pass under root A
        await session.commit()
    async with sessionmaker() as session:
        await session.execute(text("UPDATE users SET vault_path = '/vaults/B' WHERE id = 7"))
        await change_feed.discard_scope(session, 7)
        await session.commit()
    await change_feed.record_tool_events(
        [Event("created", path="new.md")], root="/vaults/B",
        session_factory=sessionmaker, user_id=7, attribution=KEY_A)
    rows = await feed(sessionmaker, 7)
    assert [(r.kind, r.path) for r in rows] == [("reset", None), ("created", "new.md")]
    async with sessionmaker() as session:
        assert (await session.execute(text(
            "SELECT count(*) FROM note_event_gaps WHERE user_id = 7"))).scalar() == 0


async def test_an_append_after_a_committed_deletion_writes_nothing(vault, sessionmaker):
    """The second order of the deletion/pass interleave: the deletion commits
    first, then a pass-shaped append for the same user takes the lock."""
    await make_users(sessionmaker, 7)
    await append(sessionmaker, 7, [Event("modified", path="a.md")], KEY_A)
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM users WHERE id = 7"))
        await change_feed.purge_scope(session, 7)
        await session.commit()
    async with sessionmaker() as session:
        n = await change_feed.append_events(
            session, 7, [Event("modified", path="a.md")], source="index", suppress=True)
        await session.commit()
    assert n == 0
    async with sessionmaker() as session:
        for table in FEED_TABLES:
            assert (await session.execute(text(
                f"SELECT count(*) FROM {table} WHERE user_id = 7"))).scalar() == 0


async def test_a_re_derive_keeps_the_stamp_and_writes_no_events(vault, sessionmaker, monkeypatch):
    from src.services import vault as vault_service

    root = vault / "tenant7"
    root.mkdir()
    (root / "r.md").write_text("re-derive me\n")
    async with sessionmaker() as session:
        await session.execute(text(
            "INSERT INTO users (id, username, password_hash, vault_path, is_active) "
            "VALUES (7, 'user7', 'x', :v, true)"), {"v": str(root)})
        await session.commit()
        await vault_service.warm_user_vault_cache(session, user_id=7)
    try:
        await indexer.index_vault(user_id=7)
        async with sessionmaker() as session:
            first = (await session.execute(text(
                "SELECT content_changed_at FROM notes_metadata WHERE user_id = 7"))).scalar_one()
            # Forget the recorded provenance: the next pass re-derives the
            # scope, re-upserting every row with an unchanged hash.
            await session.execute(text(
                "UPDATE users SET indexed_vault_assignment = NULL, indexed_vault_realpath = NULL "
                "WHERE id = 7"))
            await session.commit()
        count = len(await feed(sessionmaker, 7))
        result = await indexer.index_vault(user_id=7)
        assert result.rederive is not None, "the pass must have been a re-derive"
        async with sessionmaker() as session:
            assert (await session.execute(text(
                "SELECT content_changed_at FROM notes_metadata WHERE user_id = 7"
            ))).scalar_one() == first
        assert len(await feed(sessionmaker, 7)) == count
    finally:
        vault_service.clear_user_vault_cache(user_id=7)


async def test_a_multi_user_tool_event_is_bound_to_its_callers_root(
    vault, sessionmaker, monkeypatch,
):
    """End to end in multi-user mode: two users, a real `create_note`, the
    event lands in the caller's scope through `_vault_root` and the binding."""
    from src.auth.session import current_vault_root
    from src.services import vault as vault_service, vault_overlap

    roots = {}
    async with sessionmaker() as session:
        for uid in (7, 9):
            roots[uid] = vault / f"mu{uid}"
            roots[uid].mkdir()
            await session.execute(text(
                "INSERT INTO users (id, username, password_hash, vault_path, is_active) "
                "VALUES (:i, :u, 'x', :v, true)"),
                {"i": uid, "u": f"user{uid}", "v": str(roots[uid])})
        await session.commit()
        for uid in (7, 9):
            await vault_service.warm_user_vault_cache(session, user_id=uid)
    monkeypatch.setattr(settings, "multi_user_mode", True, raising=False)
    vault_overlap.publish_synthetic_snapshot()
    tokens = [(current_user_id, current_user_id.set(7)),
              (current_vault_root, current_vault_root.set((7, roots[7])))]
    try:
        out = await tools.create_note_impl("mine.md", "hello\n")
        assert "Created" in out, out
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
        vault_service.clear_user_vault_cache()
    seven = await feed(sessionmaker, 7)
    assert [(r.kind, r.path, r.source, r.principal_ref) for r in seven] == [
        ("created", "mine.md", "mcp", "12")]
    assert await feed(sessionmaker, 9) == [] and await feed(sessionmaker, None) == []
