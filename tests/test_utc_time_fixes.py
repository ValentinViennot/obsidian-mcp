"""Date/time presentation and SQL time math are UTC, and say so.

Offline halves of the UTC fixes; the zone-independence of the real SQL is in
`tests/integration/test_utc_time_pg.py`.

1. Both engines pin the session TimeZone to UTC (`src/services/db_session.py`;
   the engine probes in `test_internal_transport_config.py` assert the
   `server_settings` each one actually dials with).
2. The usage chart truncates in UTC explicitly and emits ISO instants; the
   page renders hour buckets locally and labels day buckets as UTC.
3. "Requests Today" binds a Python-computed UTC midnight, and says "UTC".
4. `get_recent` prints its times in UTC with the zone.
"""
from __future__ import annotations

import ast
import datetime as dt
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "src" / "control_panel" / "templates"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UTC = dt.timezone.utc


class _Result:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def fetchall(self):
        return self._rows

    def scalar(self):
        return self._scalar


class _Session:
    """Records each statement and its parameters; returns canned results."""

    def __init__(self, result):
        self._result = result
        self.calls = []

    async def execute(self, stmt, params=None):
        self.calls.append((str(stmt), dict(params or {})))
        return self._result


# ── 1. the session pin ─────────────────────────────────────────────────────


def test_the_shared_session_settings_pin_utc():
    from src.services.db_session import SESSION_TIMEZONE_SETTINGS

    assert SESSION_TIMEZONE_SETTINGS == {"timezone": "UTC"}


@pytest.mark.parametrize("path", ["src/database.py", "alembic/env.py"])
def test_every_engine_uses_the_shared_session_settings(path):
    source = (ROOT / path).read_text()
    assert "SESSION_TIMEZONE_SETTINGS" in source, (
        f"{path} builds an engine without the UTC session pin"
    )


def _code_strings():
    """(file, text) for every string literal in `src/` that is not a docstring
    — i.e. the SQL, not the prose about it. An f-string's holes read `{}`."""
    out = []
    for p in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(p.read_text())
        docstrings = {
            id(n.value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
        }
        inner = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.JoinedStr):
                parts = []
                for v in n.values:
                    inner.add(id(v))
                    parts.append(v.value if isinstance(v, ast.Constant) else "{}")
                out.append((p.name, "".join(parts)))
        for n in ast.walk(tree):
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and id(n) not in docstrings and id(n) not in inner):
                out.append((p.name, n.value))
    return out


def test_no_session_zone_day_truncation_remains_in_src():
    """`date_trunc('day', now())` is the session zone's midnight."""
    pattern = re.compile(r"date_trunc\(\s*'day'\s*,\s*now\(\)\s*\)", re.I)
    assert [name for name, s in _code_strings() if pattern.search(s)] == []


def test_every_date_trunc_in_src_names_utc():
    """A two-argument `date_trunc` over a timestamptz truncates in the session
    zone; every call in the application names UTC as its third argument."""
    calls = [
        (name, m.group(1))
        for name, s in _code_strings()
        for m in re.finditer(r"date_trunc\(([^()]*(?:\([^()]*\)[^()]*)*)\)", s)
    ]
    assert calls, "expected at least the usage chart's date_trunc"
    for name, args in calls:
        assert args.rstrip().endswith("'UTC'"), (name, args)


# ── 2. the usage chart ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chart_series_truncates_in_utc_and_emits_instants():
    from src.services.usage_filters import Filters, chart_series

    rows = [
        # Whatever zone a driver hands back, the label is the UTC rendering.
        SimpleNamespace(bucket=dt.datetime(2026, 10, 22, 5, 30, tzinfo=IST), cnt=3),
        SimpleNamespace(bucket=dt.datetime(2026, 10, 23, 0, 0, tzinfo=UTC), cnt=4),
    ]
    session = _Session(_Result(rows=rows))
    series = await chart_series(session, Filters("7d", None, None, None, None))
    (sql, _), = session.calls
    assert "date_trunc('day', ul.created_at, 'UTC')" in sql
    assert series == {
        "labels": ["10/22", "10/23"],
        "values": [3, 4],
        "instants": ["2026-10-22T00:00:00Z", "2026-10-23T00:00:00Z"],
        "bucket": "day",
    }


@pytest.mark.asyncio
async def test_hour_buckets_carry_utc_instants():
    from src.services.usage_filters import Filters, chart_series

    rows = [SimpleNamespace(bucket=dt.datetime(2026, 10, 23, 14, 0, tzinfo=UTC), cnt=2)]
    session = _Session(_Result(rows=rows))
    series = await chart_series(session, Filters("24h", None, None, None, None))
    (sql, _), = session.calls
    assert "date_trunc('hour', ul.created_at, 'UTC')" in sql
    assert series["labels"] == ["14:00"]
    assert series["instants"] == ["2026-10-23T14:00:00Z"]
    assert series["bucket"] == "hour"


def _render_usage(chart_data):
    from jinja2 import ChainableUndefined, ChoiceLoader, DictLoader, Environment, FileSystemLoader

    env = Environment(
        loader=ChoiceLoader([
            DictLoader({"base.html": "{% block title %}{% endblock %}{% block content %}{% endblock %}"}),
            FileSystemLoader(str(TEMPLATES)),
        ]),
        undefined=ChainableUndefined,
        autoescape=True,
    )
    return env.get_template("usage.html").render(
        logs=[], chart_data=chart_data, window_label="Last 7 days"
    )


def test_the_daily_chart_is_labelled_utc():
    html = _render_usage({"labels": ["10/22"], "values": [1],
                          "instants": ["2026-10-22T00:00:00Z"], "bucket": "day"})
    assert "Last 7 days · per UTC day" in html


def test_the_hourly_chart_says_it_is_local_and_localises_from_instants():
    html = _render_usage({"labels": ["14:00"], "values": [1],
                          "instants": ["2026-10-23T14:00:00Z"], "bucket": "hour"})
    assert "per hour, your time" in html
    # The page reads the instants and formats them with Intl, like the
    # request log's data-localize cells.
    assert "data.instants" in html
    assert "Intl.DateTimeFormat" in html


def test_a_chart_without_bucket_metadata_still_renders():
    html = _render_usage({"labels": [], "values": []})
    assert "per UTC day" not in html
    assert "your time" not in html


# ── 3. Requests Today ──────────────────────────────────────────────────────


def test_utc_day_start_is_utc_midnight_of_the_quota_day():
    from src.services.quotas import utc_day, utc_day_start

    # 02:00 on the 23rd at +05:30 is 20:30 UTC on the 22nd.
    now = dt.datetime(2026, 10, 23, 2, 0, tzinfo=IST)
    assert utc_day(now) == dt.date(2026, 10, 22)
    assert utc_day_start(now) == dt.datetime(2026, 10, 22, tzinfo=UTC)
    # Naive input is UTC, as for `utc_day`.
    assert utc_day_start(dt.datetime(2026, 10, 23, 23, 59)) == dt.datetime(2026, 10, 23, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize("uid", [None, 7])
async def test_requests_today_binds_utc_midnight(uid):
    from src.control_panel.routes import _requests_today

    session = _Session(_Result(scalar=5))
    now = dt.datetime(2026, 10, 23, 2, 0, tzinfo=IST)
    assert await _requests_today(session, uid, now=now) == 5
    (sql, params), = session.calls
    assert "now()" not in sql and "date_trunc" not in sql
    assert params["day_start"] == dt.datetime(2026, 10, 22, tzinfo=UTC)
    if uid is None:
        assert "user_id" not in sql
    else:
        assert params["uid"] == 7 and "user_id = :uid" in sql


def test_the_dashboard_card_names_the_utc_day():
    source = (TEMPLATES / "dashboard.html").read_text()
    assert "Requests Today (UTC)" in source


# ── 4. get_recent ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_recent_prints_utc_with_the_zone(monkeypatch):
    import src.mcp_server.tools as tools

    rows = [SimpleNamespace(file_path="a.md", title="A", tags=[],
                            modified_at=dt.datetime(2026, 10, 23, 2, 0, tzinfo=IST))]

    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, _stmt):
            return SimpleNamespace(all=lambda: rows)

    monkeypatch.setattr(tools, "async_session", lambda: _S())
    out = await tools.get_recent_impl(limit=5)
    assert out.endswith("- `a.md` — A (modified 2026-10-22 20:30 UTC)")


def test_the_get_recent_tool_description_says_utc():
    tree = ast.parse((ROOT / "src" / "mcp_server" / "server.py").read_text())
    (fn,) = [n for n in ast.walk(tree)
             if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_recent"]
    assert "UTC" in ast.get_docstring(fn)
