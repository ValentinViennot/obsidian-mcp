#!/bin/sh
# Container entrypoint: optionally migrate, then exec the image's CMD.
#
# This file exists so that no compose stack ever has to restate the uvicorn
# invocation. The bundled stacks used to run
#
#     command: sh -c "alembic upgrade head && uvicorn src.main:app ..."
#
# which is how `--workers 1` — a load-bearing contract for the in-process rate
# limiter, see docs/architecture/rate-limits.md — silently drifted out of a
# deployment: the override replaced the CMD that carried it, and nothing
# noticed because a second worker fails open (it multiplies every rate) rather
# than erroring. Migrations now hang off an env var instead, the CMD stays
# where it is documented, and `command:` overrides stay unnecessary.
#
# RUN_MIGRATIONS=true  — run `alembic upgrade head` before starting.
#   Correct for the bundled single-replica compose stacks, where the app
#   container is the only thing that owns the schema.
#   Leave it UNSET for the `make deploy` pipeline and for any deployment that
#   migrates as a separate, ordered step (that pipeline backs up *before* it
#   migrates; a container that migrates itself on start would run the upgrade
#   ahead of the backup and leave nothing to roll back to).
#
# Anything else in the environment is the app's; this script reads two vars and
# adds no other behaviour.
set -eu

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
    echo "entrypoint: RUN_MIGRATIONS=true — running alembic upgrade head" >&2
    alembic upgrade head
fi

# exec, so uvicorn inherits pid 2 under tini and receives SIGTERM directly.
# Without it this shell would hold the pid and swallow the signal.
exec "$@"
