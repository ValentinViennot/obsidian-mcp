#!/bin/bash
# DB_EXEC test shim for tests/integration/test_search_eval_pg.py (#356).
#
# `make search-eval-export` runs `$(DB_EXEC) -i psql -U postgres -d <db> ...`.
# In production DB_EXEC is docker/db-exec.sh, which runs the command next to
# the server. Here the throwaway server is reached over TCP instead: this drops
# the leading `-i` (stdin is inherited anyway) and execs the rest locally, with
# PGHOST, PGPORT and PGPASSWORD taken from the environment the test sets.
# Only the transport is swapped; the Make recipe, psql and the SQL are real.
set -u
if [ "${1:-}" = "-i" ]; then
    shift
fi
exec "$@"
