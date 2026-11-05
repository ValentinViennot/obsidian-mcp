"""Per-connection PostgreSQL session settings shared by every engine.

**The session TimeZone is pinned to UTC.** Every DateTime column is
`timestamptz` and the application reasons in UTC throughout — the quota's
accounting day (`quotas.utc_day`), the usage chart's buckets, the dashboard's
"Requests Today". But SQL `now()::date`, `date_trunc` on a `timestamptz` and
`timestamptz`-to-text casts follow the *session's* `TimeZone`, which otherwise
defaults to the server's `timezone` setting — and an external, distro-installed
Postgres usually carries the host's zone (the bundled `pgvector` image happens
to default to UTC, which is why this went unnoticed). Pinning the session makes
any SQL time arithmetic agree with the Python side.

It is a backstop, not the only defence: the queries whose result depends on the
zone also say UTC explicitly (`date_trunc(..., 'UTC')`, a Python-computed UTC
day start bound as a parameter), so they stay correct on a connection that did
not come through one of these engines.

Imported by `src/database.py` and `alembic/env.py`; it imports nothing from the
application, so alembic can read it without constructing the app's engine.
"""
from __future__ import annotations

#: The zone every application session runs in.
SESSION_TIMEZONE = "UTC"

#: Merged into asyncpg's `server_settings` (sent in the startup packet, so it is
#: the session default — `SET LOCAL timezone = DEFAULT` restores it).
SESSION_TIMEZONE_SETTINGS: dict[str, str] = {"timezone": SESSION_TIMEZONE}
