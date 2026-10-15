#!/bin/bash
# Run one command next to the PostgreSQL server, as its local superuser.
#
#   db-exec.sh [-i] <command> [args...]      e.g.  db-exec.sh pg_dump -U postgres obsidian_mcp
#
# This is THE channel `make db-backup`, `make db-restore` and
# `docker/record-backup.sh` use, so a dump and its `backups_log` row always go
# through the same database handle. `-i` keeps stdin attached (a restore, or
# SQL fed on stdin).
#
# DB_BACKEND selects where the server lives:
#
#   docker (default)  `docker exec [-i] $DB_CONTAINER <command…>` — the bundled
#                     compose stacks and a shared host container.
#   cnpg              the PRIMARY instance of a CloudNativePG cluster:
#                     `kubectl -n $CNPG_NAMESPACE exec [-i] <primary> -c postgres -- <command…>`.
#                     The command runs in the instance's own container and reaches
#                     the server over its local socket as the operator's `postgres`
#                     user (peer authentication), which is how `kubectl cnpg psql`
#                     works too. No password and no network path are involved, and
#                     it works with `enableSuperuserAccess: false`.
#
# The primary is resolved on every call from the operator's own label
# (`cnpg.io/cluster=<name>,cnpg.io/instanceRole=primary`); anything other than
# exactly one Running pod is refused rather than guessed, because after a
# failover `<cluster>-1` is no longer the primary and a dump of a replica that
# is behind would look healthy.
#
#   DB_CONTAINER     docker backend: the container name (required)
#   CNPG_NAMESPACE   cnpg backend: the cluster's namespace   (default: db)
#   CNPG_CLUSTER     cnpg backend: the Cluster name          (default: pg)
#   KUBECTL          cnpg backend: the kubectl binary        (default: kubectl)
#                    KUBECONFIG is honoured from the environment as usual.
set -u

DB_BACKEND="${DB_BACKEND:-docker}"

STDIN_FLAG=()
if [ "${1:-}" = "-i" ]; then
    STDIN_FLAG=(-i)
    shift
fi

if [ $# -eq 0 ]; then
    echo "db-exec.sh: usage: db-exec.sh [-i] <command> [args...]" >&2
    exit 2
fi

case "$DB_BACKEND" in
    docker)
        if [ -z "${DB_CONTAINER:-}" ]; then
            echo "db-exec.sh: DB_BACKEND=docker needs DB_CONTAINER" >&2
            exit 2
        fi
        exec docker exec "${STDIN_FLAG[@]}" "$DB_CONTAINER" "$@"
        ;;
    cnpg)
        KUBECTL="${KUBECTL:-kubectl}"
        NS="${CNPG_NAMESPACE:-db}"
        CLUSTER="${CNPG_CLUSTER:-pg}"
        if ! PODS=$("$KUBECTL" -n "$NS" get pods \
                -l "cnpg.io/cluster=${CLUSTER},cnpg.io/instanceRole=primary" \
                --field-selector=status.phase=Running \
                -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'); then
            echo "db-exec.sh: could not list the primary of CNPG cluster '$CLUSTER' in namespace '$NS'" >&2
            exit 1
        fi
        # `grep -c .` counts non-empty lines; an empty answer is 0, not 1.
        COUNT=$(printf '%s' "$PODS" | grep -c . || true)
        if [ "$COUNT" -ne 1 ]; then
            echo "db-exec.sh: expected exactly one Running primary of CNPG cluster '$CLUSTER' in '$NS', found $COUNT: ${PODS//$'\n'/ }" >&2
            exit 1
        fi
        exec "$KUBECTL" -n "$NS" exec "${STDIN_FLAG[@]}" "$PODS" -c postgres -- "$@"
        ;;
    *)
        echo "db-exec.sh: unknown DB_BACKEND '$DB_BACKEND' (expected docker or cnpg)" >&2
        exit 2
        ;;
esac
