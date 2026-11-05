"""The change feed (#351): `note_events`, its one writer, retention, parsing and
rendering.

Read `docs/architecture/indexing-and-embeddings.md` ("Change feed (#351)")
and the change's `design.md` before changing anything here. The load-bearing
rules, in one place:

- **One insert site.** `append_events` is the only code that inserts into
  `note_events` (a guard test pins it). It takes the scope's two-key advisory
  lock `(NOTE_EVENTS_LOCK_NS, user_id or 0)`, held to commit, and assigns
  `seq = last_seq + 1, …` under it, so within a scope seq order is commit
  order: no event a reader's snapshot cannot see can have a seq at or below
  one it can. That is what makes an `ev:<seq>` cursor exact. Seqs are
  per-scope, so no number reveals another tenant's volume.
- **The feed lock is the last lock** a pass or tool transaction takes; after
  it come only rows nobody else can hold (the scope row, serialised by the
  lock itself; new event rows; this scope's gap rows). No foreign key, so no
  wait on `users`. The one deliberate exception to "advisory before any row
  lock" (`embeddings.py`).
- **Tool events are bound to the root the tool ran under** (`root_binding`),
  re-checked under the lock against the user's current assignment: a delayed
  append after a reassignment writes nothing and logs
  `change_feed_root_changed`.
- **Nothing here may fail a pass or a file-only tool.** A bounded append runs
  in a savepoint with `lock_timeout`; on any error it rolls the savepoint back
  and returns a negative count. The pass then records a durable gap row; a
  tool just logs (the next pass rediscovers its change unattributed).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from sqlalchemy import bindparam, insert, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.types import String

from src.config import settings
from src.models.db import NoteEvent
from src.oauth.grants import NOTE_EVENTS_LOCK_NS
from src.services import security_events

logger = logging.getLogger(__name__)

NOTE_KINDS = ("created", "modified", "moved", "deleted")
CONTENT_KINDS = ("created", "modified")
META_KINDS = ("gap", "reset")
SOURCE_MCP = "mcp"
SOURCE_INDEX = "index"
REASON_REMOVED = "removed"
REASON_NOT_INDEXABLE = "not_indexable"
REASON_QUARANTINED = "quarantined"
GAP_PASS_OVERFLOW = "pass_overflow"
GAP_WRITE_FAILED = "write_failed"
#: The `tool` a transfer redemption records (`PUT /transfer/upload`).
TRANSFER_UPLOAD_TOOL = "transfer_upload"
#: Rows per retention DELETE, so one prune never holds a scope's lock long.
PRUNE_BATCH = 5_000
#: Paths per suppression lookup statement.
LOOKUP_CHUNK = 1_000
#: The widest path a feed row can hold (`note_events.path`).
MAX_EVENT_PATH_CHARS = 1024
BY_VALUES = ("any", "me", "agents", "human")
CURSOR_RE = re.compile(r"ev:([0-9]{1,18})")


# ══════════════════════════════════════════════════════════════════════════
# Values
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Event:
    """One event before it has a seq. `path` is the destination for a move."""

    kind: str
    path: str | None = None
    from_path: str | None = None
    content_hash: str | None = None
    reason: str | None = None
    gap_reason: str | None = None
    gap_count: int | None = None


@dataclass(frozen=True)
class Attribution:
    """Who an `mcp` event is attributed to, written at call time (D3)."""

    tool: str | None = None
    actor_kind: str | None = None
    actor_label: str | None = None
    principal_kind: str | None = None
    principal_ref: str | None = None

    @classmethod
    def of(cls, *, tool, actor: dict | None, principal) -> "Attribution":
        actor = actor or {}
        kind = ref = None
        if principal is not None:
            kind, ref = str(principal[0]), str(principal[1])[:64]
        return cls(
            tool=(tool or None) and str(tool)[:100],
            actor_kind=actor.get("actor_kind"),
            actor_label=actor.get("actor_label"),
            principal_kind=kind,
            principal_ref=ref,
        )


def scope_key(user_id: int | None) -> int:
    """The second advisory-lock key: the user id, or 0 for single-user mode."""
    return 0 if user_id is None else int(user_id)


def root_binding(root: str | Path | None) -> str | None:
    """A digest of a canonical vault root (D4) — never the path itself."""
    if root is None:
        return None
    from src.services.transfer import canonical_vault_root

    return hashlib.sha256(
        ("root:" + canonical_vault_root(root)).encode("utf-8", "surrogateescape")
    ).hexdigest()[:32]


def is_note_path(rel: str | None) -> bool:
    """A path the index would index as a note: `.md`, no dot component, and
    storable in a feed row. Non-markdown files get no events (Non-Goals)."""
    if not rel or not rel.endswith(".md") or len(rel) > MAX_EVENT_PATH_CHARS:
        return False
    try:
        rel.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not any(part.startswith(".") for part in rel.split("/") if part)


def _owner_sql(user_id: int | None, column: str = "user_id") -> str:
    return f"{column} IS NULL" if user_id is None else f"{column} = :uid"


def _owner_params(user_id: int | None) -> dict:
    return {} if user_id is None else {"uid": user_id}


async def lock_scope(session, user_id: int | None) -> None:
    """Take the scope's feed lock, held to commit. Re-entrant per transaction."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :key)"),
        {"ns": NOTE_EVENTS_LOCK_NS, "key": scope_key(user_id)},
    )


# ══════════════════════════════════════════════════════════════════════════
# Suppression (D3)
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Latest:
    seq: int
    kind: str
    path: str | None
    from_path: str | None
    content_hash: str | None
    source: str
    reason: str | None


async def latest_events_at(session, user_id: int | None, paths) -> dict[str, Latest]:
    """The highest-seq event *at* each path — whose `path` or `from_path` is
    it — served by the two `(user_id, path|from_path, seq)` indexes."""
    wanted = sorted({p for p in paths if p})
    out: dict[str, Latest] = {}
    owner = _owner_sql(user_id)
    stmt = text(
        "SELECT DISTINCT ON (at) at, seq, kind, path, from_path, content_hash, "
        "       source, reason FROM ("
        f"  SELECT path AS at, * FROM note_events WHERE {owner} AND path = ANY(:paths)"
        "  UNION ALL "
        f"  SELECT from_path AS at, * FROM note_events WHERE {owner} "
        "     AND from_path = ANY(:paths)"
        ") e ORDER BY at, seq DESC"
    ).bindparams(bindparam("paths", type_=ARRAY(String)))
    for start in range(0, len(wanted), LOOKUP_CHUNK):
        chunk = wanted[start:start + LOOKUP_CHUNK]
        rows = (await session.execute(
            stmt, {"paths": chunk, **_owner_params(user_id)}
        )).fetchall()
        for r in rows:
            out[r.at] = Latest(r.seq, r.kind, r.path, r.from_path, r.content_hash,
                               r.source, r.reason)
    return out


def suppressed(candidate: Event, latest: dict[str, Latest]) -> bool:
    """Whether the pass's `candidate` repeats what the feed already says (D3)."""
    at = latest.get(candidate.path)
    if (
        candidate.kind == "created"
        and candidate.reason == REASON_NOT_INDEXABLE
        and at is not None
        and at.reason == REASON_NOT_INDEXABLE
    ):
        # A present-but-not-indexable path is reported once, by anyone (L7).
        return True
    if at is None or at.source != SOURCE_MCP:
        return False
    if candidate.kind in CONTENT_KINDS:
        if at.kind not in CONTENT_KINDS:
            return False
        if candidate.content_hash is not None:
            return at.content_hash == candidate.content_hash
        return (
            at.content_hash is None
            and candidate.reason == REASON_NOT_INDEXABLE
            and at.reason == REASON_NOT_INDEXABLE
        )
    if candidate.kind == "deleted":
        return at.kind == "deleted" and at.reason == candidate.reason
    if candidate.kind == "moved":
        source_side = latest.get(candidate.from_path)
        return (
            at.kind == "moved"
            and at.from_path == candidate.from_path
            and at.path == candidate.path
            # The same MCP move must be the latest event at BOTH endpoints;
            # any later event at either defeats suppression (the A→B, B→A,
            # A→B cycle, Codex spec review round 2).
            and source_side is not None
            and source_side.seq == at.seq
        )
    return False


# ══════════════════════════════════════════════════════════════════════════
# The one writer (D4)
# ══════════════════════════════════════════════════════════════════════════


class _RootChanged(Exception):
    pass


async def _append_locked(
    session,
    user_id: int | None,
    events: list[Event],
    *,
    source: str,
    attribution: Attribution | None,
    binding: str | None,
    suppress: bool,
    max_items: int | None,
) -> int:
    await lock_scope(session, user_id)

    # 1. The scope must still exist, and a tool event's root must still be
    #    the caller's. A plain read: no row lock on `users`, ever.
    if user_id is not None:
        row = (await session.execute(
            text("SELECT vault_path FROM users WHERE id = :uid"), {"uid": user_id}
        )).first()
        if row is None:
            return 0
        if binding is not None and root_binding(row.vault_path) != binding:
            raise _RootChanged()

    # 2. The scope's counter row.
    owner = _owner_sql(user_id)
    params = _owner_params(user_id)
    await session.execute(
        text(
            "INSERT INTO note_event_scopes (user_id, last_seq) VALUES (:u, 0) "
            "ON CONFLICT (user_id) DO NOTHING"
        ),
        {"u": user_id},
    )
    last_seq = (await session.execute(
        text(f"SELECT last_seq FROM note_event_scopes WHERE {owner}"), params
    )).scalar_one()

    # 3. Durable gaps a failed pass append left, as one line ahead of ours.
    gap_rows = (await session.execute(
        text(f"DELETE FROM note_event_gaps WHERE {owner} RETURNING gap_count"), params
    )).scalars().all()
    lead: list[Event] = []
    if gap_rows:
        lead.append(Event("gap", gap_reason=GAP_WRITE_FAILED,
                          gap_count=sum(int(n) for n in gap_rows)))

    # 4. Suppression, read under the lock so every committed tool event is
    #    visible to it.
    if suppress and events:
        paths = {e.path for e in events if e.path} | {
            e.from_path for e in events if e.from_path
        }
        latest = await latest_events_at(session, user_id, paths) if paths else {}
        if any(e.kind == "reset" for e in events):
            # A baseline right after a discard's own `reset` says nothing new.
            newest = (await session.execute(
                text(f"SELECT kind FROM note_events WHERE {owner} "
                     "ORDER BY seq DESC LIMIT 1"), params
            )).scalar()
            if newest == "reset" and not lead:
                events = [e for e in events if e.kind != "reset"]
        events = [e for e in events if e.kind == "reset" or not suppressed(e, latest)]
    if max_items is not None and len(events) > max_items:
        overflow = len(events) - max_items
        events = events[:max_items] + [
            Event("gap", gap_reason=GAP_PASS_OVERFLOW, gap_count=overflow)
        ]
    rows_in = lead + events
    if not rows_in:
        return 0

    # 5. One multi-row INSERT with consecutive seqs, then the new `last_seq`.
    attr = attribution if source == SOURCE_MCP else None
    rows = []
    for i, e in enumerate(rows_in, start=1):
        meta = e.kind in META_KINDS
        rows.append({
            "user_id": user_id,
            "seq": last_seq + i,
            "kind": e.kind,
            "path": None if meta else e.path,
            "from_path": e.from_path if e.kind == "moved" else None,
            "content_hash": e.content_hash,
            # A gap line is the server's own statement about completeness,
            # never attributed to a caller.
            "source": SOURCE_INDEX if meta else source,
            "tool": attr.tool if attr and not meta else None,
            "actor_kind": attr.actor_kind if attr and not meta else None,
            "actor_label": attr.actor_label if attr and not meta else None,
            "principal_kind": attr.principal_kind if attr and not meta else None,
            "principal_ref": attr.principal_ref if attr and not meta else None,
            "gap_reason": e.gap_reason,
            "gap_count": e.gap_count,
            "reason": e.reason,
        })
    await session.execute(insert(NoteEvent), rows)
    await session.execute(
        text(f"UPDATE note_event_scopes SET last_seq = :s WHERE {owner}"),
        {"s": last_seq + len(rows), **params},
    )
    return len(rows)


async def append_events(
    session,
    user_id: int | None,
    events: list[Event],
    *,
    source: str,
    attribution: Attribution | None = None,
    binding: str | None = None,
    suppress: bool = False,
    max_items: int | None = None,
    bounded: bool = True,
) -> int:
    """Append `events` to the scope's feed in `session`'s transaction.

    Returns the number of rows written (possibly 0: nothing left after
    suppression, the user is gone, or the root changed). With `bounded=True`
    the work runs in a savepoint under `lock_timeout =
    CHANGE_FEED_LOCK_TIMEOUT_MS`; any failure rolls the savepoint back, logs
    one WARNING (scope and count, never a path) and returns `-len(events)`.
    With `bounded=False` (only `move_note`'s metadata transaction and the
    provenance discard) exceptions propagate, so the caller's transaction
    rolls back as a whole.
    """
    kw = dict(source=source, attribution=attribution, binding=binding,
              suppress=suppress, max_items=max_items)
    try:
        if not bounded:
            return await _append_locked(session, user_id, list(events), **kw)
        async with session.begin_nested():
            await session.execute(text(
                f"SET LOCAL lock_timeout = '{int(settings.change_feed_lock_timeout_ms)}ms'"
            ))
            return await _append_locked(session, user_id, list(events), **kw)
    except _RootChanged:
        security_events.emit(
            "change_feed_root_changed",
            subject=security_events.subject_for(user_id=user_id),
            user_id=user_id,
            tool=attribution.tool if attribution else None,
        )
        return 0
    except Exception as exc:
        if not bounded:
            raise
        logger.warning(
            "Change-feed append failed for scope %s (%d event(s) not recorded): %s",
            "single-user" if user_id is None else f"user_id={user_id}",
            len(events),
            type(exc).__name__,
        )
        return -len(events)


async def record_gap(session, user_id: int | None, count: int) -> None:
    """The durable record of a failed pass append (D5). A plain INSERT of a
    new row: it waits on nothing, and it is not an event row."""
    await session.execute(
        text("INSERT INTO note_event_gaps (user_id, gap_count) VALUES (:u, :n)"),
        {"u": user_id, "n": int(count)},
    )


async def record_tool_events(
    events: list[Event],
    *,
    root,
    session_factory,
    user_id: int | None = ...,  # type: ignore[assignment]
    attribution: Attribution | None = None,
) -> None:
    """Record a file-only tool's events after its confirmed publication (D3).

    Its own session and transaction. `user_id`, the actor, the principal and
    the tool default to the request's context (`current_user_id`,
    `actor_columns()`, `current_principal`, `_current_tool_name`); the transfer
    route passes them explicitly. **Swallows everything**, the session open
    included, and writes no durable marker: the next pass observes the change
    and records it unattributed (L6).
    """
    try:
        events = [e for e in events if is_note_path(e.path)
                  and (e.from_path is None or is_note_path(e.from_path))]
        if not events:
            return
        if user_id is ...:
            from src.auth.session import current_user_id

            user_id = current_user_id.get()
        if attribution is None:
            attribution = current_attribution()
        binding = root_binding(root) if user_id is not None else None
        async with session_factory() as session:
            written = await append_events(
                session, user_id, events, source=SOURCE_MCP,
                attribution=attribution, binding=binding,
            )
            if written >= 0:
                await session.commit()
            else:
                await session.rollback()
    except Exception as exc:  # noqa: BLE001 - never fails the tool
        logger.warning(
            "Change-feed event for a tool publication not recorded (%s); the "
            "next index pass records the change unattributed.",
            type(exc).__name__,
        )


def current_attribution(tool: str | None = None) -> Attribution:
    """The request's attribution, from the same ContextVars `usage_logs` uses."""
    from src.auth.session import actor_columns, current_principal

    if tool is None:
        try:
            from src.mcp_server.tools import _current_tool_name

            tool = _current_tool_name.get()
        except Exception:  # noqa: BLE001
            tool = None
    return Attribution.of(tool=tool, actor=actor_columns(),
                          principal=current_principal.get())


# ══════════════════════════════════════════════════════════════════════════
# Purges and retention (D4, D5, D9)
# ══════════════════════════════════════════════════════════════════════════


async def purge_scope(session, user_id: int) -> None:
    """Permanent user deletion: drop every feed row of the scope, under the
    feed lock taken **last** (after the `users` DELETE and its cascade). Does
    not commit. Flushes the caller's pending ORM delete first, so the order is
    always users row, cascade, feed lock."""
    await session.flush()
    await lock_scope(session, user_id)
    params = {"uid": user_id}
    for table in ("note_events", "note_event_gaps", "note_event_scopes"):
        await session.execute(text(f"DELETE FROM {table} WHERE user_id = :uid"), params)


async def discard_scope(session, user_id: int | None) -> None:
    """A provenance discard (D5): the previous root's events and durable gap
    rows go with its rows, the watermark rises to `last_seq` so an old cursor reads
    `history_truncated`, and one `reset` line starts the new root's history.
    In the discard's own transaction; failures propagate (all or nothing)."""
    await lock_scope(session, user_id)
    owner = _owner_sql(user_id)
    params = _owner_params(user_id)
    newest = (await session.execute(
        text(f"DELETE FROM note_events WHERE {owner} RETURNING occurred_at"), params
    )).scalars().all()
    await session.execute(
        text(
            "INSERT INTO note_event_scopes (user_id, last_seq) VALUES (:u, 0) "
            "ON CONFLICT (user_id) DO NOTHING"
        ),
        {"u": user_id},
    )
    await session.execute(
        text(
            "UPDATE note_event_scopes SET pruned_through_seq = last_seq, "
            "pruned_at = GREATEST(pruned_at, CAST(:at AS timestamptz)) "
            f"WHERE {owner}"
        ),
        {"at": max(newest) if newest else None, **params},
    )
    # A durable `write_failed` gap recorded under the previous root describes
    # that root's events; consumed by the new root's next append it would
    # claim the new feed is incomplete (Codex implementation review r1).
    await session.execute(text(f"DELETE FROM note_event_gaps WHERE {owner}"), params)
    await append_events(session, user_id, [Event("reset")], source=SOURCE_INDEX,
                        bounded=False)


async def prune(now: _dt.datetime | None = None, *, session_factory=None) -> int:
    """Retention (D9), from the periodic tick's `finally`, never in a pass.

    Per scope, in batches of `PRUNE_BATCH`, each batch in its own transaction
    under the scope's feed lock: delete events older than
    `CHANGE_FEED_RETENTION_DAYS` (unless null) and beyond the newest
    `CHANGE_FEED_MAX_EVENTS_PER_SCOPE`, **never the scope's newest event**,
    and raise `pruned_through_seq` / `pruned_at` in the same transaction.
    Then the backstop: feed rows of user ids with no `users` row. Each
    scope's failure is logged and retried next tick. Returns rows removed.
    """
    if session_factory is None:
        from src import database

        session_factory = database.async_session
    now = now or _dt.datetime.now(_dt.timezone.utc)
    days = settings.change_feed_retention_days
    cutoff = now - _dt.timedelta(days=days) if days is not None else None
    cap = int(settings.change_feed_max_events_per_scope)
    removed = 0

    async with session_factory() as session:
        scopes = (await session.execute(text(
            "SELECT user_id FROM note_event_scopes"
        ))).scalars().all()
        # Every event insert upserts its scope row in the same transaction,
        # and every purge removes all three tables together, so the scope and
        # gap tables name every scope that has rows — no full scan of events.
        orphans = (await session.execute(text(
            "SELECT DISTINCT user_id FROM ("
            "  SELECT user_id FROM note_event_scopes UNION "
            "  SELECT user_id FROM note_event_gaps) s "
            "WHERE user_id IS NOT NULL AND NOT EXISTS "
            "  (SELECT 1 FROM users u WHERE u.id = s.user_id)"
        ))).scalars().all()

    for uid in orphans:
        try:
            async with session_factory() as session:
                await purge_scope(session, uid)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - housekeeping
            logger.error("Change-feed orphan purge failed for user_id=%s: %s",
                         uid, type(exc).__name__)
    orphan_set = set(orphans)

    for uid in scopes:
        if uid in orphan_set:
            continue
        try:
            removed += await _prune_scope(session_factory, uid, cutoff, cap)
        except Exception as exc:  # noqa: BLE001 - housekeeping
            logger.error(
                "Change-feed retention failed for scope %s: %s",
                "single-user" if uid is None else f"user_id={uid}",
                type(exc).__name__,
            )
    if removed:
        logger.info("Change-feed retention removed %d event(s)", removed)
    return removed


async def _prune_scope(session_factory, uid, cutoff, cap) -> int:
    owner = _owner_sql(uid)
    params = _owner_params(uid)
    removed = 0
    while True:
        async with session_factory() as session:
            await lock_scope(session, uid)
            newest = (await session.execute(
                text(f"SELECT max(seq) FROM note_events WHERE {owner}"), params
            )).scalar()
            if newest is None:
                await session.rollback()
                return removed
            threshold = None
            beyond = (await session.execute(
                text(f"SELECT seq FROM note_events WHERE {owner} "
                     "ORDER BY seq DESC OFFSET :cap LIMIT 1"),
                {"cap": cap, **params},
            )).scalar()
            if beyond is not None:
                threshold = beyond
            if cutoff is not None:
                # The expired *prefix*: everything before the oldest event that
                # has not expired. `occurred_at` is a transaction's start, not
                # monotone in seq, so `max(seq) WHERE occurred_at < cutoff`
                # could delete an unexpired event committed with a lower seq
                # (Codex implementation review r1). A non-prefix row is never
                # deleted, so `pruned_through_seq` stays a true watermark.
                oldest_live = (await session.execute(
                    text(f"SELECT min(seq) FROM note_events WHERE {owner} "
                         "AND occurred_at >= :cutoff"),
                    {"cutoff": cutoff, **params},
                )).scalar()
                aged = newest if oldest_live is None else oldest_live - 1
                threshold = aged if threshold is None else max(threshold, aged)
            if threshold is None:
                await session.rollback()
                return removed
            # Never the scope's newest event: a quiet scope keeps its last line.
            threshold = min(threshold, newest - 1)
            gone = (await session.execute(
                text(
                    "DELETE FROM note_events WHERE id IN ("
                    f"  SELECT id FROM note_events WHERE {owner} AND seq <= :t "
                    "  ORDER BY seq LIMIT :n) RETURNING seq, occurred_at"
                ),
                {"t": threshold, "n": PRUNE_BATCH, **params},
            )).fetchall()
            if not gone:
                await session.rollback()
                return removed
            await session.execute(
                text(
                    "UPDATE note_event_scopes SET "
                    "pruned_through_seq = GREATEST(pruned_through_seq, :s), "
                    "pruned_at = GREATEST(pruned_at, CAST(:at AS timestamptz)) "
                    f"WHERE {owner}"
                ),
                {"s": max(r.seq for r in gone),
                 "at": max(r.occurred_at for r in gone), **params},
            )
            await session.commit()
            removed += len(gone)
            if len(gone) < PRUNE_BATCH:
                return removed


# ══════════════════════════════════════════════════════════════════════════
# Parsing (note-filters, change-feed)
# ══════════════════════════════════════════════════════════════════════════


class BoundError(ValueError):
    """An invalid `since` / `until` / cursor; the message is caller-facing."""


_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_OFFSET_RE = re.compile(r"(Z|z|[+-][0-9]{2}:[0-9]{2})$")
MAX_BOUND_CHARS = 64


def parse_utc_bound(value: str, role: str, *, tool: str = "get_recent") -> _dt.datetime:
    """`YYYY-MM-DD` (the whole UTC day) or an ISO 8601 datetime with an
    explicit `Z` / `±HH:MM` offset, normalised to UTC. `role` is `since`
    (inclusive; a date means its 00:00Z) or `until` (exclusive; a date means
    the next day's 00:00Z, so the named day is included)."""
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_BOUND_CHARS:
        raise BoundError(f"{tool}: `{role}` must be a UTC date (YYYY-MM-DD) or an "
                         "ISO 8601 datetime with an offset (Z or ±HH:MM).")
    raw = value.strip()
    if CURSOR_RE.fullmatch(raw):
        raise BoundError(f"{tool}: `{role}` got a change-feed cursor ({raw}); "
                         "cursors are for `get_changes`. Pass a date or datetime.")
    if _DATE_RE.fullmatch(raw):
        try:
            day = _dt.date.fromisoformat(raw)
        except ValueError:
            raise BoundError(f"{tool}: `{role}` is not a valid date: {raw!r}.") from None
        start = _dt.datetime(day.year, day.month, day.day, tzinfo=_dt.timezone.utc)
        if role == "until":
            try:
                return start + _dt.timedelta(days=1)
            except OverflowError:
                raise BoundError(f"{tool}: `until` is out of range.") from None
        return start
    if "T" not in raw and " " not in raw:
        raise BoundError(f"{tool}: `{role}` is not a date or datetime: {raw!r}.")
    if not _OFFSET_RE.search(raw):
        raise BoundError(
            f"{tool}: `{role}` has no UTC offset ({raw!r}). A datetime needs an "
            "explicit offset (Z or ±HH:MM); a bare date (YYYY-MM-DD) means the UTC day."
        )
    try:
        parsed = _dt.datetime.fromisoformat(raw[:-1] + "+00:00" if raw[-1] in "Zz" else raw)
    except ValueError:
        raise BoundError(f"{tool}: `{role}` is not an ISO 8601 datetime: {raw!r}.") from None
    if parsed.tzinfo is None:  # pragma: no cover - the regex guarantees one
        raise BoundError(f"{tool}: `{role}` has no UTC offset.")
    return parsed.astimezone(_dt.timezone.utc)


def parse_window(since: str | None, until: str | None, *, tool: str):
    """Both bounds, refusing `since >= until`."""
    lo = parse_utc_bound(since, "since", tool=tool) if since is not None else None
    hi = parse_utc_bound(until, "until", tool=tool) if until is not None else None
    if lo is not None and hi is not None and lo >= hi:
        raise BoundError(f"{tool}: `since` must be earlier than `until`.")
    return lo, hi


def parse_since(value: str) -> int | _dt.datetime:
    """`get_changes`' `since`: an `ev:<seq>` cursor (int) or a UTC bound."""
    if isinstance(value, str):
        m = CURSOR_RE.fullmatch(value.strip())
        if m:
            return int(m.group(1))
        if value.strip().startswith("ev:"):
            raise BoundError("get_changes: `since` is not a valid cursor; a cursor "
                             "is `ev:<number>` exactly as `next_since` returned it.")
    return parse_utc_bound(value, "since", tool="get_changes")


# ══════════════════════════════════════════════════════════════════════════
# Rendering (D7)
# ══════════════════════════════════════════════════════════════════════════

_EXTRA_ESCAPES = {
    **{chr(c): f"\\u{c:04x}" for c in range(0x80, 0xA0)},
    " ": "\\u2028",
    " ": "\\u2029",
    "\x7f": "\\u007f",
}
_EXTRA_RE = re.compile("[" + "".join(re.escape(k) for k in _EXTRA_ESCAPES) + "]")


def quote(value: str | None) -> str:
    """A note- or user-controlled string as one JSON string literal: C0 and
    C1 controls, U+2028 and U+2029 escaped, so a filename holding a newline
    stays on its line and stays recoverable (unlike `_one_line`)."""
    encoded = json.dumps(value if value is not None else "", ensure_ascii=False)
    return _EXTRA_RE.sub(lambda m: _EXTRA_ESCAPES[m.group(0)], encoded)


def format_time(value: _dt.datetime | None) -> str:
    if value is None:
        return "unknown"
    return value.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


RESYNC_HINT = 'use get_recent(order_by="content_changed", since=…) to resync'


def render_event_line(row, caller_principal) -> str:
    """One event, one line. Renders `ev:<seq>`, never the surrogate id, and of
    the attribution only kind, label, tool and `(you)`."""
    head = f"- ev:{row.seq} {format_time(row.occurred_at)} {row.kind}"
    if row.kind == "gap":
        return (f"{head} {row.gap_reason or 'unknown'} count={row.gap_count or 0}"
                f" — {RESYNC_HINT}")
    if row.kind == "reset":
        return (f"{head} — the index was rebuilt from empty; earlier events "
                f"do not describe this vault — {RESYNC_HINT}")
    if row.kind == "moved":
        body = f"{quote(row.from_path)} -> {quote(row.path)}"
    else:
        body = quote(row.path)
    if row.reason:
        body += f" reason={row.reason}"
    if row.source == SOURCE_MCP:
        who = (f"{row.actor_kind} {quote(row.actor_label)}" if row.actor_kind
               else "agent")
        actor = f"by {who} via {row.tool or 'unknown'}"
        if (
            caller_principal is not None
            and row.principal_kind is not None
            and row.principal_kind == str(caller_principal[0])
            and row.principal_ref == str(caller_principal[1])[:64]
        ):
            actor += " (you)"
    else:
        actor = "by human/sync"
    return f"{head} {body} {actor}"


@dataclass
class ChangesPage:
    since_label: str
    lines: list[str] = field(default_factory=list)
    more: bool = False
    next_since: int = 0
    truncated_history: bool = False


def render_changes(page: ChangesPage) -> str:
    n = len(page.lines)
    header = (f"Changes since {page.since_label} — {n} event{'s' if n != 1 else ''}"
              f" — more: {'true' if page.more else 'false'}"
              f" — next_since: ev:{page.next_since}")
    if page.truncated_history:
        header += (" — history_truncated: true (events older than the retained "
                   f"history were removed; {RESYNC_HINT})")
    body = [header]
    if not page.lines:
        body.append("No events.")
    body.extend(page.lines)
    body.append(f"next_since: ev:{page.next_since}")
    return "\n".join(body)


def fit_lines(page: ChangesPage, rows, caller_principal, budget: int, limit: int):
    """Render `rows` (seq order) into `page` while the response stays under
    `budget`; returns the seq of the last rendered row, or None. A row that
    does not fit ends the page with `more: true` and `next_since` stays the
    last *rendered* seq, so nothing is skipped between pages."""
    if not rows:
        return None
    # The frame (header, footer) measured at its widest: `more: true`, the
    # largest seq this page could name, and the empty-page line dropped.
    widest = replace(page, lines=["x"], more=True,
                     next_since=max(page.next_since, max(r.seq for r in rows)))
    used = len(render_changes(widest)) - 1
    last = None
    for i, row in enumerate(rows):
        if i >= limit:
            page.more = True
            break
        line = render_event_line(row, caller_principal)
        cost = len(line) + (1 if page.lines else 0)
        if used + cost > budget and page.lines:
            page.more = True
            break
        page.lines.append(line)
        used += cost
        last = row.seq
    return last
