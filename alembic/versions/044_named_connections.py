"""Name OAuth connections and snapshot transfer Git authors.

Existing OAuth credentials are intentionally invalidated once: their holders
must consent again with a connection name. API keys and their permissions are
unchanged. Stamp-back/replay revokes only unnamed rows, never a newly named
connection. Downgrade cannot resurrect revoked credentials.
"""
from alembic import op
import sqlalchemy as sa

revision = "044"
down_revision = "043"
branch_labels = None
depends_on = None

MARKER = "named connection identity (044_named_connections)"
COLUMNS = (
    ("oauth_codes", "connection_alias", 128),
    ("oauth_tokens", "connection_alias", 128),
    ("transfer_tokens", "git_author_name", 128),
    ("transfer_tokens", "git_author_email", 255),
)


def _column(bind, table, name):
    return bind.execute(sa.text("""
        SELECT format_type(a.atttypid, a.atttypmod) AS type,
               a.attnotnull AS not_null, col_description(c.oid,a.attnum) AS comment,
               pg_get_expr(d.adbin,d.adrelid) AS default_value
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE n.nspname='public' AND c.relname=:table AND a.attname=:name
          AND a.attnum > 0 AND NOT a.attisdropped
    """), {"table":table,"name":name}).mappings().first()


def upgrade():
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
    bind.execute(sa.text("SET LOCAL statement_timeout = '60s'"))
    # Child tables first; stop credential issuance while the cutover commits.
    bind.execute(sa.text("LOCK TABLE public.transfer_tokens, public.oauth_codes, public.oauth_tokens IN ACCESS EXCLUSIVE MODE"))
    for table, name, length in COLUMNS:
        row = _column(bind, table, name)
        if row is None:
            op.add_column(table, sa.Column(name, sa.String(length), nullable=True, comment=MARKER), schema="public")
        elif (row['type'] != f'character varying({length})' or row['not_null']
              or row['comment'] != MARKER or row['default_value'] is not None):
            raise RuntimeError(f"044 refuses incompatible or unowned column {table}.{name}")
    bind.execute(sa.text("UPDATE public.oauth_tokens SET revoked=true WHERE connection_alias IS NULL AND NOT revoked"))
    bind.execute(sa.text("UPDATE public.oauth_codes SET used=true WHERE connection_alias IS NULL AND NOT used"))
    bind.execute(sa.text("RESET lock_timeout"))
    bind.execute(sa.text("RESET statement_timeout"))


def downgrade():
    bind = op.get_bind()
    for table, name, _ in reversed(COLUMNS):
        row = _column(bind, table, name)
        if row is not None and row['comment'] == MARKER:
            op.drop_column(table, name, schema="public")
