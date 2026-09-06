"""The `make backfill-history` entrypoint (`scripts/backfill_history.py`).

Thin by design — the work is in `src/services/history_indexer.py` — so what is
asserted here is the small set of things a thin script can still get wrong and
that an operator would only discover after running it against a real vault: a
`--dry-run` that quietly writes, a `--user-id` that silently scopes to the
wrong slice, and the current-row invariant the script exists to shout about.
"""
import os
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from scripts import backfill_history  # noqa: E402
from src.services.history_indexer import BackfillStats  # noqa: E402


def test_the_defaults_are_the_safe_ones():
    args = backfill_history._parse_args([])

    assert args.dry_run is False
    assert args.limit is None
    assert args.user_id is None, (
        "`None` is the NULL-owned slice, the same total mapping the read path "
        "uses — not 'every user'"
    )
    assert args.vault is None, "falls back to VAULT_PATH"


def test_the_flags_are_forwarded():
    args = backfill_history._parse_args(
        ["--dry-run", "--limit", "5", "--user-id", "3", "--vault", "/v"]
    )
    assert (args.dry_run, args.limit, args.user_id, args.vault) == (
        True,
        5,
        3,
        "/v",
    )


class _Recorder:
    def __init__(self, stats):
        self.stats = stats
        self.kwargs = None

    async def __call__(self, _factory, vault, **kwargs):
        self.vault = vault
        self.kwargs = kwargs
        return self.stats


@pytest.fixture
def stub_counts(monkeypatch):
    counts = {"value": (10, 0)}

    async def _counts(_session):
        return counts["value"]

    class _Session:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(backfill_history, "count_current_and_historical", _counts)
    monkeypatch.setattr(backfill_history, "async_session", lambda: _Session())
    return counts


@pytest.mark.asyncio
async def test_a_dry_run_reports_and_does_not_read_the_after_counts(
    monkeypatch, stub_counts, capsys
):
    stats = BackfillStats(versions_embedded=4, chunks_embedded=17)
    recorder = _Recorder(stats)
    monkeypatch.setattr(backfill_history, "backfill_vault_history", recorder)

    code = await backfill_history.run(
        backfill_history._parse_args(["--dry-run", "--vault", "/v"])
    )

    assert code == 0
    assert recorder.kwargs["dry_run"] is True
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "Would embed 4 versions (17 chunks)" in out
    assert "After:" not in out, "a dry run has no 'after' to report"


@pytest.mark.asyncio
async def test_a_change_in_the_current_row_count_is_shouted_about(
    monkeypatch, stub_counts, capsys
):
    """The one thing this tool must never do is change what an ordinary search
    returns. It only inserts closed intervals and stamps existing current rows,
    so the current-row count is an invariant of the run — and a violated
    invariant has to reach the operator, not a log file nobody reads."""
    recorder = _Recorder(BackfillStats(versions_embedded=1))
    monkeypatch.setattr(backfill_history, "backfill_vault_history", recorder)

    calls = {"n": 0}
    original = backfill_history.count_current_and_historical

    async def _drifting(session):
        calls["n"] += 1
        return (10, 0) if calls["n"] == 1 else (11, 3)

    monkeypatch.setattr(backfill_history, "count_current_and_historical", _drifting)
    assert original is not _drifting

    await backfill_history.run(backfill_history._parse_args(["--vault", "/v"]))

    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "10 -> 11" in captured.err


@pytest.mark.asyncio
async def test_unbackfillable_paths_are_reported_rather_than_silent(
    monkeypatch, stub_counts, capsys
):
    """A deleted note's history cannot be stored (it has no `notes_metadata`
    row). That gap must be a number an operator can see."""
    recorder = _Recorder(BackfillStats(notes_without_metadata=12))
    monkeypatch.setattr(backfill_history, "backfill_vault_history", recorder)

    await backfill_history.run(backfill_history._parse_args(["--vault", "/v"]))

    assert "12 path(s) had history but no" in capsys.readouterr().out
