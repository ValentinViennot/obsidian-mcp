"""Federated panel identity: `users.oidc_subject` (`AUTH_MODE=pocketid`).

Two artifacts, one revision, because they are one fact:

1. `users.oidc_subject` — a nullable `varchar(255)`. NULL is what every
   existing row keeps and means "this account has never signed in through an
   identity provider", so the deploy changes no user's behaviour and
   `AUTH_MODE` still defaults to `local`.
2. `ux_users_oidc_subject` — a **partial unique** index over the non-NULL
   rows, which is the database's half of "one provider identity, one account".

## Why the link is on `sub` and not on the email

`sub` is the identity provider's stable identifier for an account. An email
address is not: providers let one be changed, and a provider that later handed
a former address to a different person would, under an email-keyed link, hand
that person the first person's vault on their next login. The email is used
*once*, to pick which pre-existing local row a first login may adopt, and never
again after `oidc_subject` is written.

## Why the index is partial, and why it is not merely tidy

Every local account has `oidc_subject IS NULL`, and PostgreSQL treats NULLs as
distinct, so a plain `UNIQUE` would in fact also work. The predicate is written
out anyway for two reasons: it states the rule rather than depending on a NULL
semantic the next reader has to recall, and it is the index the callback's
`WHERE oidc_subject = :sub` lookup actually reads — a smaller index over the
handful of federated rows rather than one entry per user.

Uniqueness is not decoration. The callback serialises its check-then-act under
the same advisory lock bootstrap registration takes, so two concurrent first
logins cannot both insert; the constraint is what still holds the day a future
call site forgets that lock. Without it two `users` rows could claim one
provider identity, and which vault a person reached would depend on row order.

## No backfill

There is nothing to backfill *from*. No existing row has ever authenticated
against a provider, NULL is the correct value for all of them, and a first
federated login is what writes the column — by adopting a row whose username
matches the provider's email local part, or by inserting a new one. Inventing a
value here would be inventing an identity claim nobody made.

Because nothing is written, a stamp-back re-run (the schema gate does
`alembic stamp 024` then `upgrade head`) reconciles the existing column and
index and writes nothing.

## Reconciliation, and why neither is a bare ADD/CREATE

The gate re-runs this body against a database that already carries its work, so
bare DDL raises there and `IF NOT EXISTS` is worse: it would adopt *any* column
of that name — a `NOT NULL` one (which no local account could satisfy), one
carrying a server default (an identity every new row silently acquires) — and
*any* index of that name, including a non-unique one, which is the damaging
case because every other check would pass while the "one provider identity, one
account" invariant quietly stopped being true. 013's rule applies: reconcile a
database that demonstrably has our shape, refuse to guess for one that does
not, and **name what disagreed**.

The column carries a `COMMENT` marker mirrored in `src/models/db.py` as
`_OIDC_SUBJECT_COLUMN_MARKER`, so `alembic check` compares it like any other
attribute and `downgrade()` can tell its own work from somebody else's. The
index has no marker available (PostgreSQL takes a comment on one, but nothing
reads it and `alembic check` does not compare it), so its *definition* is the
evidence instead — 020's device for `ix_usage_logs_key_id_created_at`.

## search_path

`op.add_column`, `COMMENT ON`, `op.create_index` and their drops are all
unqualified and resolve through `search_path`, so on a role whose path does not
start with `public` this would add the column to a `users` the application
never reads — and federated login would then insert a row and fail to find it
again on the next request. 021's lesson, 023's and 024's repetition of it: each
of those `RESET`s its own pin, so 025 needs one, and asserts afterwards that
the unqualified name really is the qualified one.

## Locks

`ADD COLUMN` of a nullable column with no default is metadata-only. The
`CREATE INDEX` takes a `SHARE` lock on `users` that blocks writes for its
duration; `users` holds a handful of rows and the deploy migrates before
recreating the container, so the blocked writers are the old container's
`last_login_at` updates. `lock_timeout` / `statement_timeout` make a blocked
migration fail fast instead of stalling the deploy, and all three settings are
`RESET` at the end because alembic runs every pending revision in one
transaction and `SET LOCAL` would otherwise leak into a later revision (013
through 024 do the same).

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


TABLE = "users"

# **Every catalog lookup below resolves this**, not the bare name, for 021's
# reason: an unqualified reference resolves through `search_path`, so a role
# pointing elsewhere would have the application and this migration each
# addressing a different table of that name.
QUALIFIED = "public.users"

COLUMN = "oidc_subject"
INDEX = "ux_users_oidc_subject"

# 013's device, and 015's through 024's: the migration marks what it created,
# so `downgrade()` can tell its own work from somebody else's and drop only the
# former. Must stay byte identical to `_OIDC_SUBJECT_COLUMN_MARKER` in
# `src/models/db.py`, where it is declared as the column comment so
# `alembic check` compares it like any other attribute.
COLUMN_MARKER = "the identity provider's stable subject claim (025_oidc_subject)"

EXPECTED_COLUMN_TYPE = "character varying(255)"

#: `(columns, unique, usable, restricted)` for the index 025 creates.
#: `restricted` is True here — unlike every earlier migration's indexes — and
#: that is the whole point: this one **is** partial, and an index of this name
#: that is not would silently cover the NULL rows it must not.
EXPECTED_INDEX = (["oidc_subject"], True, True, True)


def _quote(value: str) -> str:
    """A single-quoted SQL string literal. `COLUMN_MARKER` is a module constant
    with no quotes in it; the doubling is here so it stays correct if that
    changes."""
    return "'" + value.replace("'", "''") + "'"


# --------------------------------------------------------------------------
# catalogue reads
# --------------------------------------------------------------------------


def _oid(bind, name: str):
    """The OID `name` resolves to, or None. `to_regclass` never raises."""
    return bind.execute(
        sa.text("SELECT CAST(to_regclass(:name) AS oid)"), {"name": name}
    ).scalar()


def _column_state(bind):
    """`(format_type, attnotnull, default_expr, comment)` for the column, or None."""
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
        {"table": QUALIFIED, "column": COLUMN},
    ).first()


def _index_definitions(bind) -> dict:
    """`{name: (columns, unique, usable, restricted)}` — 019's/020's/024's reader.

    Names are not definitions, and here the distinction has teeth in both
    directions. An `ux_users_oidc_subject` recreated **non-unique** keeps the
    name every existence check looks for while two accounts may claim one
    provider identity; recreated **non-partial** it covers the NULL rows too,
    which under PostgreSQL's distinct-NULL rule changes nothing today and would
    change everything the day the column acquired a non-NULL default.

    `indkey` is an `int2vector`, which has no direct array cast; going through
    its text rendering is the portable idiom. An expression index has `attnum`
    0 there and joins to nothing, which is why `restricted` is read separately
    rather than inferred from a short column list — and it is read as one flag
    covering "partial or over an expression" because either one makes the index
    something other than the plain column index this migration reasons about.
    """
    rows = bind.execute(
        sa.text(
            "SELECT ic.relname AS name, "
            "       (SELECT array_agg(a.attname ORDER BY k.ord) "
            "          FROM unnest(string_to_array(CAST(i.indkey AS text), ' ')) "
            "               WITH ORDINALITY AS k(attnum, ord) "
            "          JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "                             AND a.attnum = CAST(k.attnum AS smallint)"
            "       ) AS columns, "
            "       i.indisunique, "
            "       (i.indisvalid AND i.indisready) AS usable, "
            "       (i.indpred IS NOT NULL OR i.indexprs IS NOT NULL) AS restricted "
            "FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
            "WHERE i.indrelid = CAST(:table AS regclass)"
        ),
        {"table": QUALIFIED},
    ).fetchall()
    return {
        row.name: (
            list(row.columns or []),
            row.indisunique,
            row.usable,
            row.restricted,
        )
        for row in rows
    }


def _index_predicate(bind) -> str | None:
    """The rendered `WHERE` of the index, or None when it has none.

    Read as well as the `restricted` flag above, because "it is partial" is not
    "it is partial *on the right predicate*": an index restricted to, say,
    `WHERE oidc_subject <> ''` is partial, unique and on the right column while
    admitting two rows that both hold `''`. Compared against the server's own
    rendering of the identical declaration (013's scratch-`TEMP`-table device),
    so the check is not pinned to one PostgreSQL major's normalisation.
    """
    return bind.execute(
        sa.text(
            "SELECT pg_get_expr(i.indpred, i.indrelid) "
            "FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
            "WHERE i.indrelid = CAST(:table AS regclass) AND ic.relname = :name"
        ),
        {"table": QUALIFIED, "name": INDEX},
    ).scalar()


def _canonical_predicate(bind) -> str:
    """What this server renders `oidc_subject IS NOT NULL` as, measured not guessed."""
    scratch = "_omcp_025_predicate_probe"
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    bind.execute(
        sa.text(f"CREATE TEMP TABLE {scratch} ({COLUMN} varchar(255))")
    )
    bind.execute(
        sa.text(
            f"CREATE UNIQUE INDEX {scratch}_ix ON pg_temp.{scratch} ({COLUMN}) "
            f"WHERE {COLUMN} IS NOT NULL"
        )
    )
    rendered = bind.execute(
        sa.text(
            "SELECT pg_get_expr(i.indpred, i.indrelid) "
            "FROM pg_index i "
            "WHERE i.indrelid = CAST(:scratch AS regclass)"
        ),
        {"scratch": f"pg_temp.{scratch}"},
    ).scalar()
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    return rendered


def _duplicate_subjects(bind) -> int:
    """How many `oidc_subject` values more than one row already claims.

    Read **before** the unique index is created, so the refusal names the
    invariant rather than surfacing as a raw `duplicate key value` from
    `CREATE UNIQUE INDEX` on a table an operator has been editing by hand. It
    can only be non-zero on a database where the column was added outside this
    migration.
    """
    return bind.execute(
        sa.text(
            f"SELECT count(*) FROM (SELECT {COLUMN} FROM {QUALIFIED} "
            f"WHERE {COLUMN} IS NOT NULL GROUP BY {COLUMN} HAVING count(*) > 1) d"
        )
    ).scalar() or 0


# --------------------------------------------------------------------------
# create / verify
# --------------------------------------------------------------------------


def _pin_search_path() -> None:
    """Pin `search_path` to `public` for the rest of this transaction.

    021's device, and 023's and 024's repetition of it. Every `op.*` call below
    is unqualified and resolves through `search_path`; giving each one
    `schema="public"` would make the objects schema-qualified in alembic's eyes
    while the ORM model declares no schema, so autogenerate would see the two
    disagree and `alembic check` would never be clean again. Pinning the path
    instead makes the unqualified names resolve to `public` while leaving both
    sides schema-less.

    `SET LOCAL` is transaction-scoped, which is how alembic runs, and is why
    `upgrade()` and `downgrade()` `RESET` it at the end rather than leaving it
    for a later revision to inherit — **024 `RESET`s its own, which is
    precisely why 025 cannot rely on it still being in force.**
    """
    op.execute("SET LOCAL search_path TO public")


def _assert_is_the_qualified_table(bind) -> None:
    """What the unqualified name resolves to is `public.users`.

    Belt and braces behind the pin, in 021's, 023's and 024's shape: if the pin
    ever fails to take effect this fails closed, rather than adding the column
    to a `users` in a schema the application never reads — a state whose
    symptom is that a federated login inserts a row and cannot find it again.
    """
    qualified = _oid(bind, QUALIFIED)
    unqualified = _oid(bind, TABLE)
    if qualified is None or qualified != unqualified:
        raise RuntimeError(
            f"025's {TABLE} is not the table {QUALIFIED} resolves to "
            f"(search_path-relative oid {unqualified!r}, qualified oid "
            f"{qualified!r}). The login callback reads the unqualified name, "
            "so a table elsewhere on the path would leave every federated "
            "identity written somewhere nothing looks. Set the migration "
            "role's search_path so `public` comes first, then re-run."
        )


def _refuse(what: str, problems: list, consequence: str) -> None:
    raise RuntimeError(
        f"{what} already exists but {'; '.join(problems)}. 025 will not adopt "
        f"an object of unknown provenance: {consequence} Resolve by hand — "
        "drop it and let 025 create it, or make it match — then re-run. "
        "Nothing has been changed."
    )


def _reconcile_column(bind) -> None:
    state = _column_state(bind)
    if state is None:
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=255), nullable=True))
        # Stamped in the same transaction as the ADD, so the marker and the
        # column can never disagree about who made it. `COMMENT ON` is utility
        # DDL and takes no bind parameter, so the literal is quoted.
        op.execute(f"COMMENT ON COLUMN {TABLE}.{COLUMN} IS {_quote(COLUMN_MARKER)}")
        return

    coltype, notnull, default, comment = state
    problems = []
    if coltype != EXPECTED_COLUMN_TYPE:
        problems.append(f"it is {coltype}, not {EXPECTED_COLUMN_TYPE}")
    if notnull:
        problems.append(
            "it is NOT NULL; 025 creates it nullable, and NULL is the value "
            "that means 'this account has never signed in through a provider' "
            "— which is every local account, so a NOT NULL column cannot "
            "describe the table it is on"
        )
    if default is not None:
        problems.append(
            f"it has a server default of {default!r}; 025 creates none, and a "
            "default here is a provider identity every new row silently "
            "acquires — under a unique index, the second such row fails to "
            "insert at all"
        )
    if comment != COLUMN_MARKER:
        problems.append("it does not carry 025's comment marker")
    if problems:
        _refuse(
            f"{TABLE}.{COLUMN}",
            problems,
            "the login callback resolves a person to an account by this "
            "column, so a wrong shape here is either a login that resolves "
            "nobody or one that resolves the wrong account.",
        )


def _reconcile_index(bind) -> None:
    live = _index_definitions(bind).get(INDEX)
    if live is None:
        duplicates = _duplicate_subjects(bind)
        if duplicates:
            raise RuntimeError(
                f"{TABLE}.{COLUMN} already holds {duplicates} value(s) claimed "
                "by more than one row, so the unique index 025 creates cannot "
                "be built. That state is only reachable if the column was "
                "populated outside this migration. Resolve the duplicates by "
                "hand — each provider identity must name exactly one account, "
                "or which vault a person reaches depends on row order — then "
                "re-run. Nothing has been changed."
            )
        op.create_index(
            INDEX,
            TABLE,
            [COLUMN],
            unique=True,
            postgresql_where=sa.text(f"{COLUMN} IS NOT NULL"),
        )
        return

    problems = []
    if live != EXPECTED_INDEX:
        columns, unique, usable, restricted = live
        problems.append(
            f"it is on {columns} (unique={unique}, usable={usable}, "
            f"partial-or-expression={restricted}), not {EXPECTED_INDEX[0]} as "
            "a valid, unique, partial index"
        )
    else:
        # Only worth asking once the flags agree: a non-partial index has no
        # predicate to compare, and its own problem is already reported above.
        canonical = _canonical_predicate(bind)
        predicate = _index_predicate(bind)
        if predicate != canonical:
            problems.append(
                f"its predicate is {predicate!r}, not {canonical!r} — a "
                "different restriction is a different set of rows the "
                "uniqueness covers"
            )
    if problems:
        _refuse(
            f"index {INDEX} on {TABLE}",
            problems,
            "it is the database's half of 'one provider identity, one "
            "account'; a same-named non-unique index satisfies every "
            "existence check and enforces nothing.",
        )


# --------------------------------------------------------------------------
# upgrade / downgrade
# --------------------------------------------------------------------------


def upgrade() -> None:
    bind = op.get_bind()

    # Fail fast rather than queueing behind a long-lived transaction: the
    # deploy migrates before recreating the container, so a stalled migration
    # is a stalled deploy while the old container is still serving. Per
    # statement and per lock acquisition, not a budget for the transaction.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    _pin_search_path()
    _assert_is_the_qualified_table(bind)

    _reconcile_column(bind)
    _reconcile_index(bind)

    # **No backfill and no data write on any path**, deliberately — see the
    # module docstring. No existing row has ever authenticated against a
    # provider, so NULL is the correct value for all of them, and inventing one
    # would be inventing an identity claim nobody made. It is also what makes
    # the gate's stamp-back re-run non-destructive.

    # `SET LOCAL` is scoped to the transaction and alembic runs every pending
    # revision in *one*, so without this the next revision would silently
    # inherit these settings and blame its own SQL when it tripped them. The
    # same applies to the `search_path` pin — 024 `RESET`s its own, which is
    # exactly why this revision needed a pin of its own.
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def downgrade() -> None:
    """Undo *this* migration, and nothing that merely shares its names.

    013's rule. The column is dropped only if it carries 025's marker, and the
    index only if it is exactly the index 025 creates; anything else raises
    rather than being removed, because the marker and the definition are the
    only evidence of authorship.

    Dropping loses every federated link, so the next login through the provider
    re-adopts the matching local row by username or creates a fresh one. That
    is a downgrade to "local login only", which is the pre-025 behaviour —
    correct only if `AUTH_MODE` goes back to `local` in the same step, since
    the previous image does not know the setting at all.
    """
    bind = op.get_bind()
    # `op.drop_index` / `op.drop_column` resolve through `search_path` too, so
    # the same pin decides *which* objects a downgrade would remove, and the
    # identity assertion stays as the belt-and-braces check that it took effect
    # before anything is dropped.
    _pin_search_path()
    _assert_is_the_qualified_table(bind)

    live = _index_definitions(bind).get(INDEX)
    if live is not None:
        # No marker is available for an index (PostgreSQL takes a comment on
        # one, but `alembic check` does not compare it and nothing else reads
        # it). The definition is the evidence instead — 020's device.
        if live != EXPECTED_INDEX:
            raise RuntimeError(
                f"{INDEX} is not the index 025 created (it is on {live[0]}, "
                f"unique={live[1]}, partial-or-expression={live[3]}), so 025 "
                "will not drop it. Nothing has been changed."
            )
        op.drop_index(INDEX, table_name=TABLE)

    state = _column_state(bind)
    if state is not None:
        if state[3] != COLUMN_MARKER:
            raise RuntimeError(
                f"{TABLE}.{COLUMN} does not carry 025's comment marker "
                f"({COLUMN_MARKER!r}), so 025 did not create it and will not "
                "drop it. Nothing has been changed."
            )
        op.drop_column(TABLE, COLUMN)

    op.execute("RESET search_path")
