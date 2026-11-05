"""SQL time arithmetic is UTC whatever the session's TimeZone is.

Every DateTime column is `timestamptz` and the application reasons in UTC, but
`date_trunc` on a `timestamptz` and `date_trunc('day', now())` follow the
session's `TimeZone`. The engines pin it to UTC (`src/services/db_session.py`);
the queries that depend on a zone also say UTC themselves. These tests run the
real queries on sessions deliberately set to a zone with a half-hour offset
east of UTC (`Asia/Kolkata`, +05:30) and one west of it
(`America/Los_Angeles`), so a regression to session-zone truncation shows up as
a moved bucket, a `:30` hour label or a wrong "today" count.
"""
from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
from src.control_panel.routes import _requests_today
from src.models.db import UsageLog
from src.services.db_session import SESSION_TIMEZONE_SETTINGS
from src.services.quotas import utc_day_start
from src.services.usage_filters import Filters, chart_series

pytestmark = [_harness.requires_pgvector, pytest.mark.asyncio(loop_scope="module")]

UTC = dt.timezone.utc
ZONES = ["Asia/Kolkata", "America/Los_Angeles"]


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("utc_time", 64)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def zoned(migrated_url):
    """One sessionmaker per non-UTC session zone."""
    engines = {
        zone: create_async_engine(
            migrated_url,
            poolclass=None,
            connect_args={"server_settings": {"timezone": zone}},
        )
        for zone in ZONES
    }
    yield {
        zone: async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        for zone, engine in engines.items()
    }
    for engine in engines.values():
        await engine.dispose()


async def _reset(sessionmaker, *instants):
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM usage_logs"))
        session.add_all(
            [UsageLog(tool="read_note", created_at=at, duration_ms=1) for at in instants]
        )
        await session.commit()


async def _assert_session_zone(session, zone):
    # The premise: this session really is not UTC.
    assert (await session.execute(text("SHOW timezone"))).scalar() == zone


@pytest.mark.parametrize("zone", ZONES)
async def test_day_buckets_are_utc_days(zoned, zone):
    sm = zoned[zone]
    day = (dt.datetime.now(UTC) - dt.timedelta(days=3)).date()
    start = dt.datetime.combine(day, dt.time.min, tzinfo=UTC)
    # 00:10 and 23:50 UTC on `day` — the same UTC day, but different local
    # days in both zones (LA puts 00:10 on the previous day, Kolkata puts
    # 23:50 on the next) — plus 23:50 UTC the day before.
    await _reset(
        sm,
        start + dt.timedelta(minutes=10),
        start + dt.timedelta(hours=23, minutes=50),
        start - dt.timedelta(minutes=10),
    )
    async with sm() as session:
        await _assert_session_zone(session, zone)
        series = await chart_series(session, Filters("7d", None, None, None, None))

    prev = day - dt.timedelta(days=1)
    assert series["bucket"] == "day"
    assert series["labels"] == [prev.strftime("%m/%d"), day.strftime("%m/%d")]
    assert series["values"] == [1, 2]
    assert series["instants"] == [
        f"{prev.isoformat()}T00:00:00Z",
        f"{day.isoformat()}T00:00:00Z",
    ]


@pytest.mark.parametrize("zone", ZONES)
async def test_hour_buckets_are_utc_hours(zoned, zone):
    sm = zoned[zone]
    hour = (dt.datetime.now(UTC) - dt.timedelta(hours=3)).replace(
        minute=0, second=0, microsecond=0
    )
    # :10 and :50 of one UTC hour. In Kolkata a session-zone truncation would
    # split them across two buckets labelled :30.
    await _reset(sm, hour + dt.timedelta(minutes=10), hour + dt.timedelta(minutes=50))
    async with sm() as session:
        await _assert_session_zone(session, zone)
        series = await chart_series(session, Filters("24h", None, None, None, None))

    assert series["bucket"] == "hour"
    assert series["labels"] == [hour.strftime("%H:%M")]
    assert series["values"] == [2]
    assert series["instants"] == [hour.strftime("%Y-%m-%dT%H:00:00Z")]


@pytest.mark.parametrize("zone", ZONES)
async def test_requests_today_counts_from_utc_midnight(zoned, zone):
    sm = zoned[zone]
    # A fixed clock, so the UTC day and the session zone's day are known to
    # differ: 03:00 UTC is still the previous day in Los Angeles, and 08:30 in
    # Kolkata, whose midnight was 18:30 UTC the day before.
    now = dt.datetime.now(UTC).replace(hour=3, minute=0, second=0, microsecond=0)
    midnight = utc_day_start(now)
    assert midnight == now.replace(hour=0)
    await _reset(
        sm,
        midnight - dt.timedelta(hours=6),     # yesterday (UTC), today in Kolkata
        midnight - dt.timedelta(minutes=1),   # yesterday (UTC)
        midnight,                             # today, on the boundary
        midnight + dt.timedelta(hours=2),     # today (UTC), yesterday in LA
    )
    async with sm() as session:
        await _assert_session_zone(session, zone)
        assert await _requests_today(session, None, now=now) == 2


async def test_the_pinned_setting_overrides_a_non_utc_database_default(migrated_url):
    """The application's `server_settings` beat the database's own default —
    the shape of an external Postgres installed with the host's zone."""
    plain = create_async_engine(migrated_url, poolclass=None)
    try:
        async with plain.connect() as conn:
            db = (await conn.execute(text("SELECT current_database()"))).scalar()
            await conn.execute(text(f'ALTER DATABASE "{db}" SET timezone = \'Asia/Kolkata\''))
            await conn.commit()
    finally:
        await plain.dispose()

    unpinned = create_async_engine(migrated_url, poolclass=None)
    pinned = create_async_engine(
        migrated_url,
        poolclass=None,
        connect_args={"server_settings": dict(SESSION_TIMEZONE_SETTINGS)},
    )
    try:
        async with unpinned.connect() as conn:
            assert (await conn.execute(text("SHOW timezone"))).scalar() == "Asia/Kolkata"
        async with pinned.connect() as conn:
            assert (await conn.execute(text("SHOW timezone"))).scalar() == "UTC"
    finally:
        await unpinned.dispose()
        await pinned.dispose()
        cleanup = create_async_engine(migrated_url, poolclass=None)
        try:
            async with cleanup.connect() as conn:
                await conn.execute(text(f'ALTER DATABASE "{db}" RESET timezone'))
                await conn.commit()
        finally:
            await cleanup.dispose()
