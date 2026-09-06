"""Shared SQL filter helpers for NoteMetadata and NoteEmbedding queries.

This is the single supported way to apply `folder`, `tags`, and `frontmatter`
filters to a `select` over `NoteMetadata`, and the single supported way to
spell "the vectors that describe the note as it stands" over `NoteEmbedding`.
Inlining the equivalents in callers risks divergence (escape rules,
containment semantics, and — for the temporal predicate — a search that
silently starts returning text the note no longer contains).
"""

import datetime

from sqlalchemy import ColumnElement, Select, and_, or_

from src.models.db import NoteEmbedding, NoteMetadata


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def apply_note_filters(
    stmt: Select,
    *,
    folder: str | None = None,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
    user_id: int | None = None,
) -> Select:
    """Append optional `folder`, `tags`, `frontmatter`, `user_id` predicates
    to a select over NoteMetadata.

    - `folder`: prefix match on `file_path`. LIKE wildcards (`%`, `_`, `\\`) are escaped.
    - `tags`: ARRAY containment (`notes_metadata.tags @> ARRAY[...]`). AND semantics.
    - `frontmatter`: JSONB containment (`notes_metadata.frontmatter @> :json`). Strict types.
    - `user_id`: **always** scoped, by a total mapping — `None` appends
      `notes_metadata.user_id IS NULL` and an `int` appends
      `notes_metadata.user_id = :uid`. `None` is a scoping value, not the
      absence of one.

    `folder`, `tags` and `frontmatter` are optional: a None or empty argument
    means "no filter" and the predicate is not appended. `user_id` is not one
    of those — see below.
    """
    # The owner predicate is total, and that is the whole of #127's read-path
    # fix. `None` used to append nothing, while every write path maps `None` to
    # `user_id IS NULL`; on a database that holds rows owned by named users —
    # which `MULTI_USER_MODE` being off does not prevent, because the flag can
    # be turned off after users exist — an ownerless credential read *every*
    # tenant's paths, titles, tags, frontmatter and chunk excerpts. The NULL
    # slice is exactly what such a credential owns, so that is what it reads.
    # A single-user deployment is unaffected: every row there is NULL-owned.
    #
    # A consequence the vector paths depend on: there is now no such thing as
    # an unfiltered query through this helper, so a zero-row approximate scan
    # is always ambiguous and always re-runs exact (see `semantic_search`).
    if folder:
        escaped = _escape_like(folder)
        stmt = stmt.where(NoteMetadata.file_path.like(f"{escaped}%", escape="\\"))
    if tags:
        stmt = stmt.where(NoteMetadata.tags.contains(tags))
    if frontmatter:
        stmt = stmt.where(NoteMetadata.frontmatter.contains(frontmatter))
    if user_id is None:
        stmt = stmt.where(NoteMetadata.user_id.is_(None))
    else:
        stmt = stmt.where(NoteMetadata.user_id == user_id)
    return stmt


# ── The temporal predicate (migration 025) ─────────────────────────────────
#
# `note_embeddings` holds more than one generation of vectors once
# `scripts/backfill_history.py` has run: a row carries the interval over which
# its `chunk_text` was the note's text. `valid_to IS NULL` means "still is",
# and it is the **only** thing separating a vector over deleted content from a
# vector over live content.
#
# That makes this the most load-bearing predicate on the read path. A search
# that forgets it does not fail; it returns paragraphs the author deleted,
# ranked and quoted exactly like current ones, to an agent that acts on them
# without a human ever seeing the query. So it is written once, here, and every
# current-state reader calls it — there is no correct hand-rolled spelling to
# prefer.


def current_embedding_predicate() -> ColumnElement[bool]:
    """`note_embeddings.valid_to IS NULL` — the vectors that describe the note
    as it stands.

    A bare predicate rather than only a statement helper because `DELETE`
    needs it too: `embed_note` replaces a note's *current* vectors and must
    leave its history alone, and a `delete()` is not a `Select`.
    """
    return NoteEmbedding.valid_to.is_(None)


def embedding_valid_at_predicate(when: datetime.datetime) -> ColumnElement[bool]:
    """The rows whose validity interval contains `when`.

    Half-open, `[valid_from, valid_to)`, so consecutive versions of a note
    tile the timeline with no gap and no overlap: version *n* ends at exactly
    the instant version *n+1* begins, and that instant belongs to *n+1*.

    **A NULL bound is unbounded, not unknown-and-excluded.** `valid_from IS
    NULL` reads as "since before anything this row can speak to" — which is
    the honest reading for every row the ordinary embed pass wrote, since it
    knows what a note says but not since when. Excluding those instead would
    make a point-in-time query answer *nothing* for the overwhelmingly common
    note that has never been through a history backfill, which is a worse
    answer than a slightly over-inclusive one: the text it returns does at
    least exist in the vault today.
    """
    return and_(
        or_(NoteEmbedding.valid_from.is_(None), NoteEmbedding.valid_from <= when),
        or_(NoteEmbedding.valid_to.is_(None), NoteEmbedding.valid_to > when),
    )


def apply_embedding_validity(
    stmt: Select, *, as_of: datetime.datetime | None = None
) -> Select:
    """Scope a select over `NoteEmbedding` in time.

    `as_of=None` — the default, and what every ordinary search passes — is
    **current only**, not "unfiltered". That asymmetry is deliberate and is the
    same one `apply_note_filters` makes for `user_id`: the absence of an
    argument is a scoping decision, never the absence of one, because the
    failure mode of "no predicate" here is deleted text served as current.
    """
    if as_of is None:
        return stmt.where(current_embedding_predicate())
    return stmt.where(embedding_valid_at_predicate(as_of))
