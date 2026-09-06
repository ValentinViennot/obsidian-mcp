"""Validity interval on each embedding row, so history is queryable.

Today `note_embeddings` holds exactly one generation of vectors: whatever the
last embed pass produced for the note as it stands at HEAD. Content that was
edited away is unreachable by `semantic_search` from the moment the pass
overwrites it — the note is the vault's memory and the index is deliberately
amnesiac about it. Two nullable timestamps turn each row into a row with a
*validity interval*, which is what lets a version-walking backfill store the
vectors for content that no longer exists without disturbing the ones that do.

## The shape, and why both columns are nullable

    valid_from  timestamptz NULL   -- when this content became the note's text
    valid_to    timestamptz NULL   -- when it stopped being it; NULL = still is

`valid_to IS NULL` **is** the definition of "current", and that is the entire
compatibility story. Every row that exists when this runs was written by the
ordinary embed pass, describes the note as it stands at HEAD, and is therefore
current — so `NULL` is not a placeholder for an unknown value, it is the
correct value, and `ADD COLUMN` with no default satisfies it from the catalogue
with no table rewrite on a table holding one row per chunk of the whole vault.

`valid_from` is nullable for a different reason and the difference matters. An
ordinary pass knows *what* the note says, not *since when* — the vault is a
directory of files, and nothing in `notes_metadata` records when the current
text first appeared. `NULL` there means "unbounded in the past, as far as this
row knows", which is exactly what a point-in-time query must assume for it:

    WHERE (valid_from IS NULL OR valid_from <= :t)
      AND (valid_to   IS NULL OR valid_to   >  :t)

Only rows written by the history backfill carry a real `valid_from`, because
only it has a commit date to put there.

NOT NULL with a sentinel default was rejected. `-infinity` for `valid_from`
would be honest; `+infinity` for `valid_to` would not — it would make "current"
a *value* rather than the absence of an end, so the ordinary pass would have to
write it, every existing query filtering on it would need the sentinel spelled
out, and a row the pass forgot to stamp would read as expired rather than as
current. NULL fails safe in the one direction that matters: a row nobody
stamped stays visible to ordinary search.

## Non-destructive, reversible, and re-runnable

Nothing is backfilled and nothing is rewritten: the columns arrive NULL, which
is the truth for every pre-existing row. `downgrade()` drops them, which
discards the recorded intervals and *merges history back into the present* —
so it also drops the historical rows, because a historical row without its
interval is indistinguishable from a current one and would be returned by
ordinary `semantic_search` as though the deleted text were still in the note.
That is the silently-wrong-search-result failure this server ranks above every
expensive one, so the downgrade must not leave it behind. The rows it deletes
are derived and re-derivable by re-running the backfill; see the docstring on
`downgrade()`.

A stamp-back re-run (the schema gate does `alembic stamp 024` then
`upgrade head`) reconciles the existing columns and writes nothing.

## The index

    ix_note_embeddings_validity  btree (valid_to, valid_from)

`valid_to` leads because *every* query filters on it and most filter on nothing
else: the default read path is `valid_to IS NULL`, which a btree serves — NULLs
are indexed — and a point-in-time query adds the `valid_from` bound the second
column covers.

It is deliberately **not** a partial index on `WHERE valid_to IS NULL`. That
would be the better index for the default path in a database that is mostly
history, and the worse one today, where every row is current and the partial
index is a full copy of the table's rows. More to the point, the default vector
path does not reach this index at all: it is served by the HNSW index on
`embedding`, with `valid_to IS NULL` applied as a filter over the candidates
that scan returns. Reshaping the HNSW index is a separate, expensive and
rewrite-carrying decision, and this revision does not pre-empt it.

## The deploy window

The deploy migrates and *then* recreates the container, so the previous code
serves for a few seconds against the new columns. It neither reads nor writes
them and its inserts omit them, so they arrive NULL — which is the correct
value for anything the old code writes. Nothing is mis-recorded, and no
historical row can exist yet.

## Locks

Two `ALTER TABLE ... ADD COLUMN` with no default, then one `CREATE INDEX`.
The ADDs take ACCESS EXCLUSIVE on `note_embeddings` and hold it to COMMIT but
perform no rewrite. The `CREATE INDEX` is **not** CONCURRENTLY: alembic runs
the whole upgrade in one transaction and CONCURRENTLY cannot run inside one.
On the production table (~17k rows) a plain build is well inside the 60 s
`statement_timeout` set below; a deployment whose embedding table is large
enough for that to be false should build the index by hand, CONCURRENTLY,
before migrating — the reconciler below adopts an index it finds already
present as long as it carries this revision's marker.

`lock_timeout` / `statement_timeout` make a blocked migration fail fast
instead of stalling the deploy, and both are `RESET` at the end because
alembic runs every pending revision in one transaction and `SET LOCAL` would
otherwise leak into a later revision (013 through 024 do the same).

Revision ID: 025
Revises: 024
Create Date: 2026-09-06
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "025"
down_revision: Union[str, None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "note_embeddings"
COLUMNS = ("valid_from", "valid_to")
EXPECTED_TYPE = "timestamp with time zone"
INDEX_NAME = "ix_note_embeddings_validity"

# 013's device, and 015's through 024's: the migration marks what it created,
# so `downgrade()` can tell its own work from somebody else's and drop only the
# former.
#
# Declared on the ORM columns too (`src/models/db.py`,
# `_TEMPORAL_VALIDITY_COLUMN_MARKER`), so `alembic check` compares it like any
# other column attribute: a marker that drifted from the model, or one silently
# dropped, is a dirty check rather than a migration that quietly stops
# recognising its own work. Keep the two byte identical.
MARKER = "embedding validity interval (025_temporal_embeddings)"


def _quote(value: str) -> str:
    """A single-quoted SQL string literal. `MARKER` is a module constant with
    no quotes in it; the doubling is here so it stays correct if that changes."""
    return "'" + value.replace("'", "''") + "'"


def _column_state(bind, column: str):
    """`(formatted_type, attnotnull, default_expr, comment)`, or None if absent."""
    return bind.execute(
        sa.text(
            "SELECT format_type(a.atttypid, a.atttypmod) AS coltype, "
            "       a.attnotnull, "
            "       pg_get_expr(d.adbin, d.adrelid) AS coldefault, "
            "       col_description(a.attrelid, a.attnum) AS comment "
            "FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = CAST(:table AS regclass) AND a.attname = :column "
            "  AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"table": TABLE, "column": column},
    ).first()


def _index_comment(bind):
    """`(exists, comment)` for `INDEX_NAME` on this table."""
    row = bind.execute(
        sa.text(
            "SELECT obj_description(i.indexrelid) AS comment "
            "FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_class t ON t.oid = i.indrelid "
            "WHERE c.relname = :index AND t.relname = :table"
        ),
        {"index": INDEX_NAME, "table": TABLE},
    ).first()
    return (row is not None, row[0] if row is not None else None)


def _reconcile_column(bind, column: str) -> None:
    """Create the column, or verify a pre-existing one is exactly 025's.

    013's philosophy: reconcile a database that demonstrably has our shape,
    refuse to guess for one that does not. The whole shape is checked, not just
    the name. A `timestamp without time zone` column would silently reinterpret
    every stored instant in the server's local zone, which for a validity
    interval means a point-in-time query answering with the *neighbouring*
    version around a DST boundary. A NOT NULL one could not hold the "current"
    state at all, since `valid_to IS NULL` is what current means here. A server
    default would stamp an interval on rows the ordinary embed pass writes,
    which know no interval, and would make every one of them expire.
    """
    state = _column_state(bind, column)
    if state is None:
        op.add_column(
            TABLE,
            sa.Column(column, sa.DateTime(timezone=True), nullable=True),
        )
        # Stamped in the same transaction as the ADD, so the marker and the
        # column can never disagree about who made it. `COMMENT ON` is utility
        # DDL and takes no bind parameter, so the literal is quoted.
        op.execute(f"COMMENT ON COLUMN {TABLE}.{column} IS {_quote(MARKER)}")
        return

    coltype, notnull, default, comment = state
    problems = []
    if coltype != EXPECTED_TYPE:
        problems.append(f"it is {coltype}, not {EXPECTED_TYPE}")
    if notnull:
        problems.append("it is NOT NULL; 025 creates it nullable")
    if default is not None:
        problems.append(f"it carries a server default ({default!r}); 025 sets none")
    if comment != MARKER:
        problems.append("it does not carry 025's comment marker")
    if problems:
        raise RuntimeError(
            f"{TABLE}.{column} already exists but {'; '.join(problems)}. 025 "
            "will not adopt a column of unknown provenance: the read path "
            "treats `valid_to IS NULL` as 'this vector describes the note's "
            "current text', so a wrong value either hides current content from "
            "search or serves deleted text as though it were still there. "
            "Resolve by hand — drop it and let 025 create it, or make it match "
            "— then re-run. Nothing has been changed."
        )


def _reconcile_index(bind) -> None:
    """Create the validity index, or adopt one this revision already made.

    An index built by hand CONCURRENTLY ahead of a large migration is a
    supported path (see the module docstring), but only if it was stamped with
    025's marker — an index of the same name over different columns would be
    adopted silently and would then serve nothing, while `alembic check` sees
    only that "an index by that name exists".
    """
    exists, comment = _index_comment(bind)
    if not exists:
        op.create_index(INDEX_NAME, TABLE, ["valid_to", "valid_from"])
        op.execute(f"COMMENT ON INDEX {INDEX_NAME} IS {_quote(MARKER)}")
        return
    if comment != MARKER:
        raise RuntimeError(
            f"An index named {INDEX_NAME} already exists on {TABLE} but does "
            f"not carry 025's comment marker ({MARKER!r}), so 025 did not "
            "create it and will not adopt it. Nothing has been changed. Drop "
            "it, or stamp it with that comment if it really is (valid_to, "
            "valid_from), then re-run."
        )


def upgrade() -> None:
    bind = op.get_bind()

    # Fail fast rather than queueing behind a long-lived transaction: the
    # deploy migrates before recreating the container, so a stalled migration
    # is a stalled deploy while the old container is still serving. Per
    # statement and per lock acquisition, not a budget for the transaction.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")

    for column in COLUMNS:
        _reconcile_column(bind, column)
    _reconcile_index(bind)

    # **No backfill**, deliberately — see the module docstring. Every
    # pre-existing row describes the note's current text, and NULL/NULL is what
    # this schema calls that.

    # `SET LOCAL` is scoped to the transaction and alembic runs every pending
    # revision in *one*, so without this the next revision would silently
    # inherit these timeouts and blame its own SQL when it tripped them.
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")


def downgrade() -> None:
    """Delete the historical rows, then drop 025's index and columns.

    013's rule: a downgrade must undo *this* migration, not delete columns
    somebody else put there under these names. The marker is the only evidence
    of authorship, and a column without it aborts the downgrade having changed
    nothing.

    **The row deletion is not optional and its order is not arbitrary.** A
    historical row is a vector over text the note no longer contains; the only
    thing separating it from a current one is `valid_to`. Drop the column first
    and every superseded paragraph in the database becomes, to `semantic_search`
    and `find_related`, part of the note as it stands now — deleted text
    returned to an agent as current, permanently, with nothing left in the
    schema able to detect it. So the rows go while the column that identifies
    them still exists.

    What is lost is derived state: `scripts/backfill_history.py` rebuilds it
    from the vault's git history, which is where the facts actually live. What
    is kept is every row the ordinary embed pass wrote, because those are
    exactly the rows with `valid_to IS NULL`.
    """
    bind = op.get_bind()

    states = {column: _column_state(bind, column) for column in COLUMNS}
    present = {c: s for c, s in states.items() if s is not None}
    for column, state in present.items():
        if state[3] != MARKER:
            raise RuntimeError(
                f"{TABLE}.{column} does not carry 025's comment marker "
                f"({MARKER!r}), so 025 did not create it and will not drop it. "
                "Nothing has been changed. Remove it by hand if you mean to."
            )

    if "valid_to" in present:
        # Before the DROP, and the only statement that touches rows: after it,
        # nothing can tell a historical vector from a current one.
        op.execute(f"DELETE FROM {TABLE} WHERE valid_to IS NOT NULL")

    exists, comment = _index_comment(bind)
    if exists:
        if comment != MARKER:
            raise RuntimeError(
                f"An index named {INDEX_NAME} exists on {TABLE} but does not "
                f"carry 025's comment marker ({MARKER!r}), so 025 did not "
                "create it and will not drop it. Nothing has been changed."
            )
        op.drop_index(INDEX_NAME, table_name=TABLE)

    for column in present:
        op.drop_column(TABLE, column)
