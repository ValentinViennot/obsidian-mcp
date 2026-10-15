"""`docker/db-restore.sh` — `make db-restore` must verify the whole dump before
sending a single statement, and must restore all-or-nothing.

`docker` is a shim on PATH that records its argv and saves the SQL it was fed,
so the tests see exactly what would have reached psql (nothing, on a refusal).
"""

import gzip
import os
import stat
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "docker", "db-restore.sh")
MARKER = "--\n-- PostgreSQL database dump complete\n--\n"
GOOD_SQL = "--\n-- PostgreSQL database dump\n--\nDROP TABLE IF EXISTS notes;\nCREATE TABLE notes (id int);\n" + MARKER


def _run(tmp_path, dump_path, *, psql_rc=0):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    argv_log = tmp_path / "argv.log"
    fed = tmp_path / "fed.sql"
    shim = bindir / "docker"
    shim.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> "{argv_log}"\n'
        f'cat > "{fed}"\n'
        f"exit {psql_rc}\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "DB_BACKEND": "docker",
        "DB_CONTAINER": "postgres",
        "RESTORE_DELAY": "0",
    }
    result = subprocess.run(
        ["bash", SCRIPT, str(dump_path)], env=env, capture_output=True, text=True, timeout=30
    )
    calls = argv_log.read_text().splitlines() if argv_log.exists() else []
    return result, calls, (fed.read_text() if fed.exists() else None)


def test_a_verified_plain_dump_is_restored_all_or_nothing(tmp_path):
    f = tmp_path / "backup.sql"
    f.write_text(GOOD_SQL)
    result, calls, fed = _run(tmp_path, f)
    assert result.returncode == 0, result.stderr
    assert calls == [
        "exec -i postgres psql -U postgres -d obsidian_mcp -v ON_ERROR_STOP=1 --single-transaction -q"
    ]
    assert fed == GOOD_SQL


def test_a_verified_gzip_dump_is_restored(tmp_path):
    f = tmp_path / "backup.sql.gz"
    f.write_bytes(gzip.compress(GOOD_SQL.encode()))
    result, calls, fed = _run(tmp_path, f)
    assert result.returncode == 0, result.stderr
    assert fed == GOOD_SQL
    assert "--single-transaction" in calls[0] and "ON_ERROR_STOP=1" in calls[0]


def _refused(result, calls, fed):
    assert result.returncode != 0
    assert calls == [], "nothing may reach psql before the input verifies"
    assert fed is None
    assert "Nothing was sent to the database" in result.stderr


def test_a_missing_file_is_refused(tmp_path):
    _refused(*_run(tmp_path, tmp_path / "nope.sql.gz"))


def test_an_empty_file_is_refused(tmp_path):
    f = tmp_path / "empty.sql"
    f.write_text("")
    _refused(*_run(tmp_path, f))


def test_an_unreadable_file_is_refused(tmp_path):
    if os.geteuid() == 0:
        return  # root reads anything
    f = tmp_path / "locked.sql"
    f.write_text(GOOD_SQL)
    f.chmod(0)
    _refused(*_run(tmp_path, f))


def test_a_truncated_gzip_archive_is_refused(tmp_path):
    whole = gzip.compress((GOOD_SQL * 200).encode())
    f = tmp_path / "trunc.sql.gz"
    f.write_bytes(whole[: len(whole) // 2])
    _refused(*_run(tmp_path, f))


def test_a_truncated_dump_that_still_gzips_cleanly_is_refused(tmp_path):
    """The dangerous one: a --clean dump cut after its DROPs, re-gzipped whole."""
    f = tmp_path / "cut.sql.gz"
    f.write_bytes(gzip.compress(GOOD_SQL.split("CREATE TABLE")[0].encode()))
    _refused(*_run(tmp_path, f))


def test_a_truncated_plain_dump_is_refused(tmp_path):
    f = tmp_path / "cut.sql"
    f.write_text(GOOD_SQL.split("CREATE TABLE")[0])
    _refused(*_run(tmp_path, f))


def test_a_psql_failure_fails_the_restore(tmp_path):
    f = tmp_path / "backup.sql"
    f.write_text(GOOD_SQL)
    result, calls, fed = _run(tmp_path, f, psql_rc=3)
    assert result.returncode != 0
    assert "nothing was committed" in result.stderr
    assert "Restored from" not in result.stdout
