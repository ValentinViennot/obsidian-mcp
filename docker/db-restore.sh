#!/bin/bash
# Restore a plain-SQL `pg_dump` of obsidian_mcp through docker/db-exec.sh.
# Called by `make db-restore FILE=<path>`.
#
#   db-restore.sh <dump.sql | dump.sql.gz>
#
# The whole input is verified BEFORE a single statement is sent, because a
# partial restore of a `--clean` dump can drop tables and then stop:
#   - the file must exist, be a regular readable file and be non-empty;
#   - a .gz must pass `gzip -t` over the WHOLE archive (a truncated archive fails);
#   - the SQL must end with pg_dump's end-of-dump marker
#     ("PostgreSQL database dump complete"). A truncated dump, even one that
#     gzips to a valid file, lacks it.
# Then psql runs with ON_ERROR_STOP=1 and --single-transaction: the first error
# rolls back everything, so the database is either fully restored or untouched.
# pipefail makes a decompression failure fail the restore.
#
#   DB_NAME        default obsidian_mcp
#   RESTORE_DELAY  seconds to wait (Ctrl+C window) after verification, default 5
#   DB_BACKEND / DB_CONTAINER / CNPG_* / KUBECTL  passed through to db-exec.sh
set -uo pipefail

RED='\033[0;31m'; YELLOW='\033[0;33m'; GREEN='\033[0;32m'; NC='\033[0m'
FILE="${1:-}"
DB_NAME="${DB_NAME:-obsidian_mcp}"
DELAY="${RESTORE_DELAY:-5}"
DB_EXEC_SCRIPT="$(dirname "${BASH_SOURCE[0]}")/db-exec.sh"
MARKER='PostgreSQL database dump complete'

die() { echo -e "${RED}db-restore: $*${NC}" >&2; echo -e "${RED}Nothing was sent to the database.${NC}" >&2; exit 1; }

[ -n "$FILE" ] || { echo -e "${RED}Usage: make db-restore FILE=<path>${NC}" >&2; exit 2; }
[ -e "$FILE" ] || die "$FILE does not exist"
[ -f "$FILE" ] || die "$FILE is not a regular file"
[ -r "$FILE" ] || die "$FILE is not readable"
[ -s "$FILE" ] || die "$FILE is empty"

case "$FILE" in
    *.gz)
        gzip -t "$FILE" 2>/dev/null || die "$FILE failed gzip -t (truncated or corrupt archive)"
        TAIL=$(gzip -dc "$FILE" | tail -n 20) || die "could not decompress $FILE"
        READER=(gzip -dc "$FILE")
        ;;
    *)
        TAIL=$(tail -n 20 "$FILE") || die "could not read $FILE"
        READER=(cat "$FILE")
        ;;
esac
grep -qF "$MARKER" <<< "$TAIL" || die "$FILE does not end with pg_dump's '$MARKER' marker (truncated or not a pg_dump)"

echo -e "${YELLOW}Verified $FILE. WARNING: this will restore it into the $DB_NAME database (one transaction).${NC}"
if [ "$DELAY" -gt 0 ] 2>/dev/null; then
    echo "Press Ctrl+C to cancel, waiting ${DELAY}s..."
    sleep "$DELAY"
fi

if ! "${READER[@]}" | bash "$DB_EXEC_SCRIPT" -i psql -U postgres -d "$DB_NAME" \
        -v ON_ERROR_STOP=1 --single-transaction -q; then
    echo -e "${RED}Restore FAILED: nothing was committed (ON_ERROR_STOP + --single-transaction)${NC}" >&2
    exit 1
fi
echo -e "${GREEN}Restored from $FILE${NC}"
