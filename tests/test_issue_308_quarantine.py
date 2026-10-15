"""#308, design D3/D5 — the poison classifier, the quarantine registry, the
run-record line and the health wiring, without a database.

The behaviour against a real PostgreSQL (savepoints, restarts, every write
site) is in `tests/integration/test_issue_308_quarantine_pg.py`.
"""
import asyncpg
import pytest
from sqlalchemy.exc import DBAPIError

from src.services import indexer, indexer_health


def wrapped(orig: BaseException) -> DBAPIError:
    return DBAPIError("UPDATE notes_metadata ...", {}, orig)


# ── poison_sqlstate ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "exc_type,expected",
    [
        (asyncpg.exceptions.CharacterNotInRepertoireError, "22021"),
        (asyncpg.exceptions.InvalidTextRepresentationError, "22P02"),
        (asyncpg.exceptions.StringDataRightTruncationError, "22001"),
        (asyncpg.exceptions.ProgramLimitExceededError, "54000"),
        (asyncpg.exceptions.DataError, "22000"),
    ],
)
def test_class_22_and_54000_are_poison(exc_type, expected):
    assert indexer.poison_sqlstate(exc_type("x")) == expected
    assert indexer.poison_sqlstate(wrapped(exc_type("x"))) == expected


@pytest.mark.parametrize(
    "exc_type",
    [
        asyncpg.exceptions.UniqueViolationError,          # 23505
        asyncpg.exceptions.SerializationError,            # 40001
        asyncpg.exceptions.LockNotAvailableError,         # 55P03
        asyncpg.exceptions.QueryCanceledError,            # 57014
        asyncpg.exceptions.TooManyConnectionsError,       # 53300
        asyncpg.exceptions.StatementTooComplexError,      # 54001, not 54000
    ],
)
def test_other_sqlstates_are_not_poison(exc_type):
    assert indexer.poison_sqlstate(wrapped(exc_type("x"))) is None


def test_the_first_sqlstate_in_the_chain_decides():
    """A wrapper that names a non-poison state is not reclassified by a
    class-22 error somewhere deeper in its context."""
    inner = asyncpg.exceptions.CharacterNotInRepertoireError("x")
    outer = asyncpg.exceptions.SerializationError("y")
    outer.__context__ = inner
    assert indexer.poison_sqlstate(outer) is None


def test_the_client_side_encode_failure_is_poison():
    """What asyncpg raises for a lone surrogate at bind (verified against a
    real server in the integration module): `DataError`, sqlstate 22000,
    caused by `UnicodeEncodeError`."""
    data_error = asyncpg.exceptions.DataError("invalid input for query argument $1")
    data_error.__cause__ = UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates")
    assert indexer.poison_sqlstate(wrapped(data_error)) == "22000"


def test_a_bare_encode_error_with_no_sqlstate_is_treated_as_22021():
    class DriverError(Exception):
        pass

    err = DriverError("bind failed")
    err.__cause__ = UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates")
    assert indexer.poison_sqlstate(err) == "22021"


@pytest.mark.parametrize(
    "exc", [ValueError("x"), RuntimeError("x"), OSError(5, "EIO")]
)
def test_non_database_errors_are_not_poison(exc):
    assert indexer.poison_sqlstate(exc) is None


# ── the registry ────────────────────────────────────────────────────────────


def candidate(path, h="h1", code="22021", site="upsert"):
    return indexer.PoisonCandidate(path, h, code, site)


def test_quarantine_is_keyed_by_owner_and_path(caplog):
    with caplog.at_level("ERROR", logger="src.services.indexer"):
        indexer._quarantine_note(None, candidate("a.md"))
        indexer._quarantine_note(7, candidate("a.md", h="h2"))
    assert indexer.quarantine_count() == 2
    assert indexer._quarantined_hash(None, "a.md") == "h1"
    assert indexer._quarantined_hash(7, "a.md") == "h2"
    assert indexer._quarantined_hash(8, "a.md") is None
    assert indexer.quarantined_paths(7) == ["a.md"]
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 2, "one ERROR per quarantine"
    assert "22021" in errors[0] and "a.md" in errors[0]


def test_a_new_failure_replaces_the_entry():
    indexer._quarantine_note(None, candidate("a.md", h="h1"))
    indexer._quarantine_note(None, candidate("a.md", h="h2", code="54000"))
    assert indexer.quarantine_count() == 1
    assert indexer._quarantine[(None, "a.md")].sqlstate == "54000"


def test_clear_by_path_by_scope_and_all():
    indexer._quarantine_note(None, candidate("a.md"))
    indexer._quarantine_note(None, candidate("b.md"))
    indexer._quarantine_note(3, candidate("a.md"))
    indexer.clear_quarantine(None, "a.md")
    assert indexer.quarantined_paths(None) == ["b.md"]
    indexer.clear_quarantine(3)
    assert indexer.quarantined_paths(3) == []
    assert indexer.quarantine_count() == 1
    indexer.clear_quarantine()
    assert indexer.quarantine_count() == 0


def test_inactive_users_are_forgotten_single_user_is_kept():
    indexer._quarantine_note(None, candidate("a.md"))
    indexer._quarantine_note(1, candidate("a.md"))
    indexer._quarantine_note(2, candidate("a.md"))
    indexer.retain_quarantine_scopes([1])
    assert sorted(k[0] or 0 for k in indexer._quarantine) == [0, 1]


def test_the_quarantine_log_renders_an_unencodable_path(caplog):
    with caplog.at_level("ERROR", logger="src.services.indexer"):
        indexer._quarantine_note(None, candidate("caf\udce9.md"))
    message = caplog.records[-1].getMessage()
    message.encode("utf-8")  # must not raise
    assert "caf\\udce9.md" in message


# ── D5: the run record ──────────────────────────────────────────────────────


def test_the_run_record_names_at_most_five_paths_then_an_ellipsis():
    paths = [f"n{i}.md" for i in range(7)]
    line = indexer.format_quarantined(paths)
    assert line == "quarantined 7 note(s): n0.md, n1.md, n2.md, n3.md, n4.md, …"
    assert indexer.format_quarantined(["a.md"]) == "quarantined 1 note(s): a.md"
    rendered = indexer.format_quarantined(["caf\udce9.md"])
    rendered.encode("utf-8")
    assert "caf\\udce9.md" in rendered


def test_pass_stats_records_the_quarantine_as_text_not_as_a_failure():
    stats = indexer.PassStats()
    stats.record_index(
        indexer.IndexPassResult(3, 2, quarantined=("Bad.md",))
    )
    assert (stats.notes_scanned, stats.notes_indexed) == (3, 2)
    assert stats.error_text == "quarantined 1 note(s): Bad.md"

    clean = indexer.PassStats()
    clean.record_index(indexer.IndexPassResult(3, 2))
    assert clean.error_text is None
    plain = indexer.PassStats()
    plain.record_index((3, 2))  # a test stub's bare tuple
    assert plain.error_text is None


# ── health wiring ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "verdict,expected",
    [
        (None, None),
        (indexer.REDERIVE_RECORDED, True),
        (indexer.REDERIVE_INCOMPLETE, False),
        (indexer.REDERIVE_UNRECORDED, False),
    ],
)
def test_record_index_outcome_passes_the_rederive_verdict(
    monkeypatch, verdict, expected
):
    seen = []
    monkeypatch.setattr(
        indexer_health, "record_rederive", lambda scope, c: seen.append((scope, c))
    )
    indexer.record_index_outcome(
        5, True, indexer.IndexPassResult(1, 1, rederive=verdict)
    )
    assert seen == [(5, expected)]


def test_a_failed_pass_records_no_rederive_verdict(monkeypatch):
    seen = []
    monkeypatch.setattr(
        indexer_health, "record_rederive", lambda scope, c: seen.append(c)
    )
    indexer.record_index_outcome(5, False)
    assert seen == []


def test_three_unrecorded_re_derives_reach_degraded():
    for _ in range(3):
        indexer.record_index_outcome(
            4, True,
            indexer.IndexPassResult(1, 1, rederive=indexer.REDERIVE_UNRECORDED),
        )
    assert indexer_health.snapshot()["status"] == "degraded"
    indexer.record_index_outcome(
        4, True, indexer.IndexPassResult(1, 1, rederive=indexer.REDERIVE_RECORDED)
    )
    assert indexer_health.snapshot()["status"] == "ok"


def test_the_quarantine_count_reaches_health_without_the_path():
    indexer_health.set_quarantine_count_provider(indexer.quarantine_count)
    assert indexer_health.snapshot()["quarantined_notes"] == 0
    indexer._quarantine_note(9, candidate("Secret/plan.md"))
    snap = indexer_health.snapshot()
    assert snap["quarantined_notes"] == 1 and snap["status"] == "degraded"
    assert "plan" not in repr(snap) and "9" not in repr(
        {k: v for k, v in snap.items() if k != "max_consecutive_failures"}
    )


def test_the_provider_is_installed_by_the_indexer_loop(monkeypatch):
    """`run_indexer_loop` installs it before the startup pass, so a process
    whose loop runs always reports its quarantine."""
    import asyncio

    class Stop(Exception):
        pass

    async def stop(*_a, **_k):
        raise Stop

    monkeypatch.setattr(indexer, "detect_root_overlaps", stop)
    indexer_health.set_quarantine_count_provider(None)
    with pytest.raises(Stop):
        asyncio.run(indexer.run_indexer_loop())
    indexer._quarantine_note(None, candidate("a.md"))
    assert indexer_health.snapshot()["quarantined_notes"] == 1


def test_the_retry_setting_defaults_to_five():
    from src.config import Settings

    assert Settings.model_fields["indexer_quarantine_retries_per_tick"].default == 5


# ── C4: the classifier never follows unrelated context ──────────────────────


def test_an_interface_error_raised_while_handling_a_poison_error_is_not_poison():
    """A savepoint rollback that dies on the connection after a class-22
    statement carries that statement in `__context__` only. It is not a
    property of the note."""
    poison = asyncpg.exceptions.CharacterNotInRepertoireError("x")
    outer = asyncpg.exceptions.InterfaceError("connection is closed")
    outer.__context__ = poison
    assert indexer.poison_sqlstate(outer) is None
    assert indexer.poison_sqlstate(wrapped(outer)) is None


def test_context_alone_never_makes_an_error_poison():
    class DriverError(Exception):
        pass

    outer = DriverError("cleanup failed")
    outer.__context__ = asyncpg.exceptions.CharacterNotInRepertoireError("x")
    assert indexer.poison_sqlstate(outer) is None


@pytest.mark.parametrize(
    "make_outer",
    [
        lambda: asyncpg.exceptions.InterfaceError("connection is closed"),
        lambda: asyncpg.exceptions.ConnectionDoesNotExistError("gone"),
        lambda: OSError(104, "Connection reset by peer"),
        lambda: __import__("sqlalchemy.exc").exc.PendingRollbackError("rollback"),
    ],
)
def test_a_connection_or_cleanup_failure_is_not_poison_even_if_it_wraps_one(
    make_outer,
):
    outer = make_outer()
    outer.__cause__ = asyncpg.exceptions.CharacterNotInRepertoireError("x")
    assert indexer.poison_sqlstate(outer) is None


def test_an_invalidated_connection_wrapper_is_not_poison():
    err = DBAPIError(
        "UPDATE", {}, asyncpg.exceptions.CharacterNotInRepertoireError("x"),
        connection_invalidated=True,
    )
    assert indexer.poison_sqlstate(err) is None


# ── _savepoint_poison / _confirm_poison against a fake session ──────────────


class _Nested:
    def __init__(self, exit_exc=None):
        self.exit_exc = exit_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None and self.exit_exc is not None:
            raise self.exit_exc
        return False


class _FakeSession:
    def __init__(self, exit_exc=None):
        self.exit_exc = exit_exc

    def begin_nested(self):
        return _Nested(self.exit_exc)


def _raiser(exc):
    async def run():
        raise exc
    return run


async def _ok():
    return None


def test_savepoint_poison_returns_a_poison_failure_it_rolled_back():
    import asyncio

    exc = asyncpg.exceptions.CharacterNotInRepertoireError("x")
    got = asyncio.run(indexer._savepoint_poison(_FakeSession(), _raiser(exc)))
    assert got is exc
    assert asyncio.run(indexer._savepoint_poison(_FakeSession(), _ok)) is None


@pytest.mark.parametrize(
    "rollback_exc",
    [
        asyncpg.exceptions.InterfaceError("connection is closed"),
        # Even a rollback failure that itself classifies as poison is not the
        # note's failure: what left the savepoint is not what the body raised.
        asyncpg.exceptions.CharacterNotInRepertoireError("rollback"),
    ],
)
def test_savepoint_poison_propagates_a_failed_savepoint_rollback(rollback_exc):
    import asyncio

    body = asyncpg.exceptions.CharacterNotInRepertoireError("x")
    with pytest.raises(type(rollback_exc)) as excinfo:
        asyncio.run(indexer._savepoint_poison(
            _FakeSession(exit_exc=rollback_exc), _raiser(body)
        ))
    assert excinfo.value is rollback_exc


def test_confirm_poison_reraises_the_original_when_the_retry_succeeds(caplog):
    import asyncio

    first = wrapped(asyncpg.exceptions.InvalidTextRepresentationError(
        "invalid input syntax: SECRET NOTE TEXT"
    ))
    with caplog.at_level("ERROR", logger="src.services.indexer"):
        with pytest.raises(DBAPIError) as excinfo:
            asyncio.run(indexer._confirm_poison(
                _FakeSession(), _ok, first, site="links"
            ))
    assert excinfo.value is first
    assert indexer.quarantine_count() == 0
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and "22P02" in errors[0]
    assert "SECRET" not in errors[0]


def test_confirm_poison_returns_the_confirming_sqlstate():
    import asyncio

    first = asyncpg.exceptions.CharacterNotInRepertoireError("x")
    again = asyncpg.exceptions.ProgramLimitExceededError("y")
    code = asyncio.run(indexer._confirm_poison(
        _FakeSession(), _raiser(again), first, site="upsert"
    ))
    assert code == "54000"


def test_confirm_poison_propagates_a_non_poison_retry_failure():
    import asyncio

    first = asyncpg.exceptions.CharacterNotInRepertoireError("x")
    again = asyncpg.exceptions.SerializationError("y")
    with pytest.raises(asyncpg.exceptions.SerializationError):
        asyncio.run(indexer._confirm_poison(
            _FakeSession(), _raiser(again), first, site="upsert"
        ))


# ── the panel's run outcome (D5) ────────────────────────────────────────────


@pytest.mark.parametrize(
    "error,expected",
    [
        (None, "ok"),
        ("", "ok"),
        ("quarantined 1 note(s): Bad.md", "quarantined"),
        (indexer.format_quarantined([f"n{i}.md" for i in range(9)]), "quarantined"),
        (indexer.format_quarantined(["a\nindex: boom.md"]), "quarantined"),
        ("quarantined 1 note(s): Bad.md\nembed failures: 2 of 3 — first: x",
         "failed"),
        ("index: OSError: no space left", "failed"),
        ("embed failures: 1 of 1 — first: quarantined 1 note(s): x", "failed"),
    ],
)
def test_run_outcome(error, expected):
    assert indexer_health.run_outcome(error) == expected


def test_format_quarantined_keeps_the_record_one_line():
    line = indexer.format_quarantined(["a\nb.md", "c\rd.md"])
    assert "\n" not in line and "\r" not in line
    assert line.startswith(indexer_health.QUARANTINED_RUN_PREFIX)
