"""`notes_metadata.content_changed_at` and the change-feed tables (#351).

`get_recent` and `list_notes` could only order by file mtime, and a vault-wide
cleanup that touched seventeen notes buried every real change behind them. The
index already sees which rows' content changed and which it pairs as moves, but
kept none of it. This revision adds the column that remembers the first and
the three tables that remember the rest.

## `content_changed_at`

`timestamptz NOT NULL DEFAULT now()`. Added nullable, backfilled with
`COALESCE(modified_at, indexed_at)` (the best pre-033 approximation: no column
recorded a content change before), then `SET NOT NULL`. From here on only the
index pass's upsert writes it, and only when the stored hash changes (design
D1). One UPDATE over `notes_metadata` (low thousands of rows per tenant).

## `note_events`, `note_event_scopes`, `note_event_gaps`

The durable change log (design D2–D5, D9): events with a per-scope `seq`
(`UNIQUE (user_id, seq) NULLS NOT DISTINCT`), one counter row per scope
(`last_seq`, the retention watermark), and the durable record of a pass append
that failed. **No foreign key on any of them**: an FK to `users` would close the
documented deadlock with permanent user deletion, and an event must outlive its
`notes_metadata` row. Durable for backups (#348): history, counters and
watermarks cannot be rebuilt from the vault.

## Reconciliation, not adoption

Every pre-existing unit is verified before anything is created. A table is
compared with a scratch TEMP twin built from the very DDL this revision would
run — columns, primary key, every CHECK/UNIQUE/FK definition, index column
lists and the marker — so any other shape is refused by name, and an FK on a
look-alike is refused like any other difference. `downgrade()` drops only marked
units. `search_path` is pinned and `lock_timeout` / `statement_timeout` set and
`RESET`, for 021's, 024's and 025's reasons.

Revision ID: 033
Revises: 032
Create Date: 2026-10-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '039'
down_revision: Union[str, None] = '038'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NOTES = "notes_metadata"
NOTES_QUALIFIED = "public.notes_metadata"
COLUMN = "content_changed_at"

# Byte identical to the markers in `src/models/db.py`.
COLUMN_MARKER = "content-change stamp (033_content_changed_at)"
EVENTS_MARKER = "change-feed events (033_content_changed_at)"
SCOPES_MARKER = "change-feed scope counters (033_content_changed_at)"
GAPS_MARKER = "change-feed write gaps (033_content_changed_at)"

# Pinned here rather than imported from the models: a migration must keep
# describing the schema it created after the application moves on.
KINDS = ("created", "modified", "moved", "deleted", "gap", "reset")
SOURCES = ("mcp", "index")
REASONS = ("removed", "not_indexable", "quarantined")
GAP_REASONS = ("pass_overflow", "write_failed")
PRINCIPAL_KINDS = ("api_key", "oauth")


def _in(column: str, values) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


EVENT_CHECKS = {
    "ck_note_events_kind": _in("kind", KINDS),
    "ck_note_events_source": _in("source", SOURCES),
    "ck_note_events_reason": _in("reason", REASONS),
    "ck_note_events_gap_reason": _in("gap_reason", GAP_REASONS),
    "ck_note_events_principal_kind": _in("principal_kind", PRINCIPAL_KINDS),
    "ck_note_events_path": (
        "(" + _in("kind", ("gap", "reset")) + " AND path IS NULL) OR ("
        "kind NOT IN ('gap', 'reset') AND path IS NOT NULL)"
    ),
    "ck_note_events_from_path": (
        "(kind = 'moved' AND from_path IS NOT NULL) OR "
        "(kind <> 'moved' AND from_path IS NULL)"
    ),
    "ck_note_events_principal": (
        "(principal_kind IS NULL) = (principal_ref IS NULL) AND "
        "(source = 'mcp' OR (principal_kind IS NULL AND actor_kind IS NULL "
        "AND actor_label IS NULL AND tool IS NULL))"
    ),
}


def _events_ddl(schema: str) -> list[str]:
    t = f"{schema}note_events"
    checks = ",\n".join(
        f"  CONSTRAINT {name} CHECK ({predicate})"
        for name, predicate in EVENT_CHECKS.items()
    )
    return [
        f"""CREATE {'TEMP ' if schema == 'pg_temp.' else ''}TABLE {t} (
  id bigserial NOT NULL,
  user_id integer,
  seq bigint NOT NULL,
  occurred_at timestamp with time zone NOT NULL DEFAULT now(),
  kind varchar(16) NOT NULL,
  path varchar(1024),
  from_path varchar(1024),
  content_hash varchar(64),
  source varchar(8) NOT NULL,
  tool varchar(100),
  actor_kind varchar(20),
  actor_label varchar(255),
  principal_kind varchar(16),
  principal_ref varchar(64),
  gap_reason varchar(32),
  gap_count integer,
  reason varchar(16),
  CONSTRAINT note_events_pkey PRIMARY KEY (id),
  CONSTRAINT uq_note_events_user_id_seq UNIQUE NULLS NOT DISTINCT (user_id, seq),
{checks}
)""",
        f"CREATE INDEX ix_note_events_user_path_seq ON {t} (user_id, path, seq)",
        f"CREATE INDEX ix_note_events_user_from_path_seq ON {t} (user_id, from_path, seq)",
        f"CREATE INDEX ix_note_events_occurred_at ON {t} (occurred_at)",
    ]


def _scopes_ddl(schema: str) -> list[str]:
    t = f"{schema}note_event_scopes"
    return [
        f"""CREATE {'TEMP ' if schema == 'pg_temp.' else ''}TABLE {t} (
  id serial NOT NULL,
  user_id integer,
  last_seq bigint NOT NULL,
  pruned_through_seq bigint NOT NULL DEFAULT 0,
  pruned_at timestamp with time zone,
  CONSTRAINT note_event_scopes_pkey PRIMARY KEY (id),
  CONSTRAINT uq_note_event_scopes_user_id UNIQUE NULLS NOT DISTINCT (user_id)
)""",
    ]


def _gaps_ddl(schema: str) -> list[str]:
    t = f"{schema}note_event_gaps"
    return [
        f"""CREATE {'TEMP ' if schema == 'pg_temp.' else ''}TABLE {t} (
  id bigserial NOT NULL,
  user_id integer,
  gap_count integer NOT NULL,
  recorded_at timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT note_event_gaps_pkey PRIMARY KEY (id)
)""",
        f"CREATE INDEX ix_note_event_gaps_user_id ON {t} (user_id)",
    ]


# `(name, ddl builder, marker)` in creation order.
TABLES = (
    ("note_events", _events_ddl, EVENTS_MARKER),
    ("note_event_scopes", _scopes_ddl, SCOPES_MARKER),
    ("note_event_gaps", _gaps_ddl, GAPS_MARKER),
)


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# --------------------------------------------------------------------------
# catalogue reads
# --------------------------------------------------------------------------


def _oid(bind, name: str):
    return bind.execute(
        sa.text("SELECT CAST(to_regclass(:name) AS oid)"), {"name": name}
    ).scalar()


def _comment(bind, qualified: str):
    return bind.execute(
        sa.text("SELECT obj_description(CAST(:q AS regclass), 'pg_class')"),
        {"q": qualified},
    ).scalar()


def _shape(bind, qualified: str) -> dict:
    """Everything 033 compares about a table, from the catalogue."""
    columns = [
        tuple(r)
        for r in bind.execute(
            sa.text(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
                "       CASE WHEN pg_get_expr(d.adbin, d.adrelid) LIKE 'nextval(%%' "
                "            THEN 'nextval' ELSE pg_get_expr(d.adbin, d.adrelid) END "
                "FROM pg_attribute a "
                "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "WHERE a.attrelid = CAST(:q AS regclass) AND a.attnum > 0 "
                "  AND NOT a.attisdropped ORDER BY a.attnum"
            ),
            {"q": qualified},
        ).fetchall()
    ]
    constraints = sorted(
        (r[0], r[1], r[2], r[3])
        for r in bind.execute(
            sa.text(
                "SELECT c.conname, CAST(c.contype AS text), "
                "       pg_get_constraintdef(c.oid), c.convalidated "
                "FROM pg_constraint c WHERE c.conrelid = CAST(:q AS regclass)"
            ),
            {"q": qualified},
        ).fetchall()
    )
    indexes = sorted(
        (r[0], tuple(r[1] or ()), r[2], r[3], r[4])
        for r in bind.execute(
            sa.text(
                "SELECT ic.relname, "
                "       (SELECT array_agg(a.attname ORDER BY k.ord) "
                "          FROM unnest(string_to_array(CAST(i.indkey AS text), ' ')) "
                "               WITH ORDINALITY AS k(attnum, ord) "
                "          JOIN pg_attribute a ON a.attrelid = i.indrelid "
                "                             AND a.attnum = CAST(k.attnum AS smallint)), "
                "       i.indisunique, (i.indisvalid AND i.indisready), "
                "       (i.indpred IS NOT NULL OR i.indexprs IS NOT NULL) "
                "FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
                "WHERE i.indrelid = CAST(:q AS regclass)"
            ),
            {"q": qualified},
        ).fetchall()
    )
    return {"columns": columns, "constraints": constraints, "indexes": indexes}


def _expected_shape(bind, name: str, ddl) -> dict:
    """The shape 033 would create, measured off a TEMP twin of the same DDL."""
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{name}"))
    for statement in ddl("pg_temp."):
        bind.execute(sa.text(statement))
    shape = _shape(bind, f"pg_temp.{name}")
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{name}"))
    return shape


def _table_problems(bind, name: str, ddl, marker: str) -> list[str]:
    qualified = f"public.{name}"
    problems = []
    if _comment(bind, qualified) != marker:
        problems.append(f"{name} already exists but it does not carry 033's comment marker")
        return problems
    live = _shape(bind, qualified)
    expected = _expected_shape(bind, name, ddl)
    for part in ("columns", "constraints", "indexes"):
        if live[part] != expected[part]:
            extra = sorted(set(live[part]) - set(expected[part]))
            missing = sorted(set(expected[part]) - set(live[part]))
            problems.append(
                f"{name} already exists but its {part} differ from 033's "
                f"(unexpected: {extra}; missing: {missing})"
            )
    for conname, contype, definition, _ in live["constraints"]:
        if contype == "f":
            problems.append(
                f"{name} carries a FOREIGN KEY {conname!r} ({definition}); 033's "
                "feed tables have none, by design"
            )
    return problems


def _column_state(bind):
    return bind.execute(
        sa.text(
            "SELECT format_type(a.atttypid, a.atttypmod), a.attnotnull, "
            "       pg_get_expr(d.adbin, d.adrelid), col_description(a.attrelid, a.attnum) "
            "FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = CAST(:q AS regclass) AND a.attname = :c "
            "  AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"q": NOTES_QUALIFIED, "c": COLUMN},
    ).first()


def _column_problems(bind) -> list[str]:
    state = _column_state(bind)
    if state is None:
        return []
    coltype, notnull, default, comment = state
    problems = []
    if coltype != "timestamp with time zone":
        problems.append(f"{NOTES}.{COLUMN} is {coltype}, not timestamp with time zone")
    if not notnull:
        problems.append(f"{NOTES}.{COLUMN} is nullable; 033 makes it NOT NULL")
    if default != "now()":
        problems.append(f"{NOTES}.{COLUMN} has the server default {default!r}, not 'now()'")
    if comment != COLUMN_MARKER:
        problems.append(f"{NOTES}.{COLUMN} does not carry 033's comment marker")
    return problems


def _assert_is_the_qualified_table(bind, table: str) -> None:
    resolved = _oid(bind, f"public.{table}")
    unqualified = _oid(bind, table)
    if resolved is None or resolved != unqualified:
        raise RuntimeError(
            f"033's {table} is not the table public.{table} resolves to "
            f"(search_path-relative oid {unqualified!r}, qualified oid "
            f"{resolved!r}). Set the migration role's search_path so `public` "
            "comes first, then re-run."
        )


# --------------------------------------------------------------------------
# upgrade / downgrade
# --------------------------------------------------------------------------


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("SET LOCAL search_path TO public")
    _assert_is_the_qualified_table(bind, NOTES)

    refusals = list(_column_problems(bind))
    for name, ddl, marker in TABLES:
        if _oid(bind, f"public.{name}") is not None:
            refusals.extend(_table_problems(bind, name, ddl, marker))
    if refusals:
        raise RuntimeError(
            ". ".join(refusals)
            + ". 033 will not adopt a unit of unknown provenance: the change feed "
            "tells an agent what changed since its cursor, and a shape this "
            "migration did not create can lose or invent an event. Resolve by "
            "hand — drop it and let 033 create it — then re-run. Nothing has "
            "been changed."
        )

    if _column_state(bind) is None:
        op.execute(
            f"ALTER TABLE {NOTES} ADD COLUMN {COLUMN} timestamp with time zone"
        )
        op.execute(
            f"UPDATE {NOTES} SET {COLUMN} = COALESCE(modified_at, indexed_at, now())"
        )
        op.execute(f"ALTER TABLE {NOTES} ALTER COLUMN {COLUMN} SET NOT NULL")
        op.execute(f"ALTER TABLE {NOTES} ALTER COLUMN {COLUMN} SET DEFAULT now()")
        op.execute(f"COMMENT ON COLUMN {NOTES}.{COLUMN} IS {_quote(COLUMN_MARKER)}")

    for name, ddl, marker in TABLES:
        if _oid(bind, f"public.{name}") is None:
            for statement in ddl(""):
                op.execute(statement)
            op.execute(f"COMMENT ON TABLE {name} IS {_quote(marker)}")
        _assert_is_the_qualified_table(bind, name)

    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def downgrade() -> None:
    """Drop each unit only if it carries 033's marker, printing what is left.

    Feed history is lost; nothing else is. The previous build neither reads
    nor writes any of these objects.
    """
    bind = op.get_bind()
    op.execute("SET LOCAL search_path TO public")
    for name, _ddl, marker in reversed(TABLES):
        if _oid(bind, f"public.{name}") is None:
            continue
        _assert_is_the_qualified_table(bind, name)
        if _comment(bind, f"public.{name}") != marker:
            print(
                f"033 downgrade: leaving {name} in place — it does not carry "
                f"033's marker ({marker!r}), so 033 did not create it. Remove it "
                "by hand if you mean to.",
                flush=True,
            )
            continue
        op.execute(f"DROP TABLE {name}")
    state = _column_state(bind)
    if state is not None:
        if state[3] != COLUMN_MARKER:
            print(
                f"033 downgrade: leaving {NOTES}.{COLUMN} in place. It does not "
                f"carry 033's marker ({COLUMN_MARKER!r}), so 033 did not create "
                "it and will not drop it. Remove it by hand if you mean to.",
                flush=True,
            )
        else:
            op.execute(f"ALTER TABLE {NOTES} DROP COLUMN {COLUMN}")
    op.execute("RESET search_path")
