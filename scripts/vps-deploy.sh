#!/usr/bin/env bash
# Roll the production stack to one image digest, or put back the one it was on.
#
# This runs ON the VPS. `.github/workflows/deploy.yml` pipes it in over SSH
# (`ssh host bash -s -- <ref>`), so nothing is left on the host afterwards and
# the script that ran is always the one from the commit being deployed.
#
# WHY IT DISCOVERS THE STACK INSTEAD OF BEING TOLD WHERE IT IS
#
# The compose project belongs to a private GitOps repository that Komodo clones
# onto the host; this repository is public and must not carry host paths (see
# CLAUDE.md, "Public repo — host paths live outside the tree"). Docker already
# knows where the project is: compose stamps the project name, working
# directory and config-file list onto every container it creates. Reading them
# back off the running container means no path here, no third secret to keep in
# sync, and no way for CI to point at a stack that is not the live one.
#
# WHAT IT DOES NOT DO
#
# It does not create the stack. A container must already exist — the first
# deploy is a Komodo DeployStack and stays manual, which is the fleet's rule
# for every new stack, not a limitation of this script.
#
# WHAT A ROLLBACK HERE IS AND IS NOT
#
# Rolling back restores the IMAGE. It does not undo a migration: `alembic
# downgrade` is not safe to run unattended, and the daily restic snapshot is
# the real way back from a bad one. So a release whose migration is not
# backward-compatible will fail health, roll the image back, and STILL be
# broken — that case wants a human and the runbook, which is why the failure
# path says so out loud.
#
# The pin this script writes also lives outside the Komodo clone, so the next
# Komodo DeployStack drops it and the stack returns to the floating `latest`
# tag. After a rollback that is exactly the broken image again. Fix forward, or
# pin the good digest in the GitOps repo — the failure path prints this too.
#
# Usage:
#   vps-deploy.sh ghcr.io/owner/name@sha256:<64 hex>  [--skip-backup]

set -euo pipefail

CONTAINER="obsidian-mcp"
SERVICE="obsidian-mcp"
STATE_DIR="/var/lib/obsidian-mcp-deploy"
PIN_FILE="$STATE_DIR/pin.yaml"

# The container's own healthcheck IS `curl -f http://localhost:8000/health`, so
# polling its status is polling /health — from inside the network the service
# actually listens on, which the host cannot reach (the port is `expose`d, not
# published). `start_period` is 60s, so nothing before then counts as failure.
HEALTH_TIMEOUT_SECONDS=240
ROLLBACK_HEALTH_TIMEOUT_SECONDS=180
# A crash loop is the failure this whole path exists for (AUTH_MODE=pocketid
# without MULTI_USER_MODE crash-looped exactly like this). Docker restarts the
# container faster than the healthcheck can ever go unhealthy, so restarts are
# counted separately and abort early rather than waiting out the timeout.
MAX_RESTARTS=3

die() { echo "vps-deploy: $*" >&2; exit 1; }
note() { echo "vps-deploy: $*"; }

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

NEW_REF="${1:-}"
SKIP_BACKUP=0
shift || true
for arg in "$@"; do
  case "$arg" in
    --skip-backup) SKIP_BACKUP=1 ;;
    *) die "unknown argument: $arg" ;;
  esac
done

# A digest, never a tag. Pinning a tag would make "roll back to the previous
# digest" meaningless — both sides of the rollback would name the same moving
# target.
[ -n "$NEW_REF" ] || die "usage: vps-deploy.sh <image>@sha256:<digest> [--skip-backup]"
case "$NEW_REF" in
  *@sha256:*) : ;;
  *) die "refusing to deploy '$NEW_REF': an image reference pinned by digest is required." ;;
esac

# ---------------------------------------------------------------------------
# Discover the compose project from the live container
# ---------------------------------------------------------------------------

docker container inspect "$CONTAINER" >/dev/null 2>&1 || die \
  "no container named '$CONTAINER' on this host. The stack has never been
  deployed here, and this script rolls an existing stack rather than creating
  one. Run DeployStack in Komodo once, then re-run the deploy workflow."

label() { docker container inspect -f "{{index .Config.Labels \"$1\"}}" "$CONTAINER"; }

PROJECT="$(label com.docker.compose.project)"
WORKDIR="$(label com.docker.compose.project.working_dir)"
CONFIG_FILES="$(label com.docker.compose.project.config_files)"

[ -n "$PROJECT" ] && [ -n "$WORKDIR" ] && [ -n "$CONFIG_FILES" ] || die \
  "'$CONTAINER' carries no compose project labels — it was not created by
  docker compose, so there is no project to roll. Refusing to guess."
[ -d "$WORKDIR" ] || die "compose project directory '$WORKDIR' does not exist."

# Config files are recorded comma-separated and may be relative to the working
# directory.
COMPOSE_ARGS=(--project-name "$PROJECT" --project-directory "$WORKDIR")
IFS=',' read -r -a _config_files <<<"$CONFIG_FILES"
for file in "${_config_files[@]}"; do
  case "$file" in
    /*) path="$file" ;;
    *) path="$WORKDIR/$file" ;;
  esac
  [ -f "$path" ] || die "compose file '$path' (from the container's labels) is missing."
  COMPOSE_ARGS+=(-f "$path")
done

# Komodo writes the interpolated secrets to a `.env` beside the compose file.
# `--project-directory` makes compose find it, but naming it explicitly means a
# future change to compose's lookup order cannot silently deploy a stack with
# every `${VAR:?}` unset.
if [ -f "$WORKDIR/.env" ]; then
  COMPOSE_ARGS+=(--env-file "$WORKDIR/.env")
fi

# ---------------------------------------------------------------------------
# Record what is running now — this is the rollback target
# ---------------------------------------------------------------------------

# `.Config.Image` is the reference as the compose file spelled it, which may be
# a floating tag; the digest has to come from the image the container actually
# resolved to. An image can carry several RepoDigests when it has been pulled
# under more than one name, so the one belonging to the repository we are
# deploying is preferred over whatever happens to be first.
NEW_REPO="${NEW_REF%@*}"
CURRENT_IMAGE_ID="$(docker container inspect -f '{{.Image}}' "$CONTAINER")"
PREVIOUS_REF="$(
  docker image inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' "$CURRENT_IMAGE_ID" 2>/dev/null \
    | grep -x -- "$NEW_REPO@sha256:[0-9a-f]*" | head -n 1 || true
)"
if [ -z "$PREVIOUS_REF" ]; then
  PREVIOUS_REF="$(docker image inspect -f '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' \
    "$CURRENT_IMAGE_ID" 2>/dev/null || true)"
fi

[ -n "$PREVIOUS_REF" ] || die \
  "the running container's image has no registry digest (it was built on this
  host, or its digest was pruned), so there is nothing exact to roll back to.
  Refusing to deploy: a deploy with no way back is the failure this script
  exists to prevent."

note "project          $PROJECT"
note "project dir      $WORKDIR"
note "currently running $PREVIOUS_REF"
note "deploying         $NEW_REF"

if [ "$PREVIOUS_REF" = "$NEW_REF" ]; then
  note "already on this digest — continuing anyway (migrations and the health
  check are the point of a re-run, and both are idempotent)."
fi

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

write_pin() {
  cat >"$PIN_FILE" <<EOF
# Written by scripts/vps-deploy.sh from the obsidian-mcp repository.
# Do not edit: the next deploy overwrites it. It exists so the stack runs an
# immutable digest instead of the floating \`latest\` tag its compose file
# names, which is what makes an exact rollback possible.
services:
  $SERVICE:
    image: $1
EOF
}

compose() { docker compose "${COMPOSE_ARGS[@]}" -f "$PIN_FILE" "$@"; }

# The pin on disk always describes what SHOULD be running, so it starts out
# naming the digest that IS running. It moves to the new one only at the point
# of no return below; anything that fails before then leaves a pin an operator
# can apply by hand without accidentally deploying the release that failed.
write_pin "$PREVIOUS_REF"

# ---------------------------------------------------------------------------
# Pull first: an image that is not there must not take the stack down
# ---------------------------------------------------------------------------

note "pulling $NEW_REF"
docker pull --quiet "$NEW_REF" >/dev/null || die "could not pull $NEW_REF"

# ---------------------------------------------------------------------------
# Back up before migrating
# ---------------------------------------------------------------------------
#
# `make deploy` refuses to migrate without a backup, for the reason it states:
# the backup is the only way back from a bad migration. The same rule holds
# here. The stack's own `backup` profile is used rather than an ad-hoc pg_dump,
# so CI takes the same backup the runbook and the nightly Komodo procedure take
# — one mechanism, one thing to test.

if [ "$SKIP_BACKUP" = "1" ]; then
  note "WARNING: --skip-backup — migrating with no fresh backup."
else
  backup_services="$(compose --profile backup config --services 2>/dev/null || true)"
  if grep -qx 'backup-prepare' <<<"$backup_services" && grep -qx 'backup' <<<"$backup_services"; then
    note "taking a pre-migration backup"
    compose --profile backup run --rm -T backup-prepare \
      || die "backup-prepare failed — refusing to migrate unprotected."
    compose --profile backup run --rm -T backup \
      || die "backup failed — refusing to migrate unprotected."
  else
    # Not fatal: a stack may legitimately back up out of band. Loud, because
    # the deploy is about to migrate and the operator should know what net is
    # under it.
    note "WARNING: this project has no backup-prepare/backup services; the last
  net under the migration below is the nightly snapshot, not a fresh one."
  fi
fi

# ---------------------------------------------------------------------------
# Migrate with the new image, before the new image serves traffic
# ---------------------------------------------------------------------------
#
# Same order as `make deploy`, for the same reason: migrations are written to
# be backward-compatible, so the old container keeps serving correctly against
# the new schema for the few seconds between here and the recreate. The reverse
# order would put new code in front of an old schema, which is the window
# nobody tests.
write_pin "$NEW_REF"
note "running alembic upgrade head with the new image"
if ! compose run --rm -T "$SERVICE" alembic upgrade head; then
  write_pin "$PREVIOUS_REF"
  die "migration failed. The stack is untouched and still on $PREVIOUS_REF."
fi

# ---------------------------------------------------------------------------
# Roll the service
# ---------------------------------------------------------------------------

restart_count() { docker container inspect -f '{{.RestartCount}}' "$CONTAINER" 2>/dev/null || echo 0; }
health_status() {
  docker container inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "$CONTAINER" 2>/dev/null || echo missing
}

# Returns 0 when the container reports healthy, 1 on anything else. Takes the
# restart count from *before* the recreate so a crash loop is detected as new
# restarts rather than as a nonzero absolute number.
wait_for_health() {
  local timeout="$1" baseline="$2" deadline status restarts
  deadline=$(( $(date +%s) + timeout ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    status="$(health_status)"
    restarts="$(restart_count)"
    case "$status" in
      healthy) note "healthy after $restarts restart(s)"; return 0 ;;
      missing) note "the container disappeared while waiting for health"; return 1 ;;
      none)
        # No healthcheck declared on this service, so there is no status to
        # poll and the loop would otherwise time out and roll back a perfectly
        # good deploy. Probe /health directly instead — from inside the
        # container, because the port is `expose`d and not published.
        if docker exec "$CONTAINER" curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
          note "no healthcheck on this service; /health answered directly"
          return 0
        fi
        ;;
    esac
    if [ "$(( restarts - baseline ))" -ge "$MAX_RESTARTS" ]; then
      note "crash loop: $(( restarts - baseline )) restarts since the recreate"
      return 1
    fi
    sleep 5
  done
  note "timed out after ${timeout}s waiting for health (last status: $(health_status))"
  return 1
}

BASELINE_RESTARTS="$(restart_count)"

# No --force-recreate: when the digest has not changed, compose leaves the
# container alone and the whole script is a no-op plus a health check. That is
# what makes a re-run safe.
note "bringing up $SERVICE on the new digest"
compose up -d "$SERVICE"

if wait_for_health "$HEALTH_TIMEOUT_SECONDS" "$BASELINE_RESTARTS"; then
  note "deployed $NEW_REF"
  # Advisory. A schema that disagrees with the models is a real problem, but it
  # is not one a rollback fixes, and the deploy that just went healthy should
  # not be reverted over it. ci.yml's schema-gate is where this fails a change.
  if ! compose exec -T "$SERVICE" alembic check >/dev/null 2>&1; then
    note "WARNING: 'alembic check' is not clean — the live schema and the ORM
  models disagree. See docs/architecture/schema-and-migrations.md."
  fi
  docker logs --tail 20 "$CONTAINER" 2>&1 | sed 's/^/  | /' || true
  exit 0
fi

# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

note "DEPLOY FAILED — rolling back to $PREVIOUS_REF"
docker logs --tail 60 "$CONTAINER" 2>&1 | sed 's/^/  | /' || true

write_pin "$PREVIOUS_REF"
BASELINE_RESTARTS="$(restart_count)"
compose up -d "$SERVICE" || die "ROLLBACK FAILED to start $PREVIOUS_REF. The
  service is DOWN and needs a human — see docs/operations/runbooks.md."

if wait_for_health "$ROLLBACK_HEALTH_TIMEOUT_SECONDS" "$BASELINE_RESTARTS"; then
  cat >&2 <<EOF
vps-deploy: rolled back to $PREVIOUS_REF and the service is healthy again.

  Two things are NOT undone and need a person:
    * The migration that ran before the failed release is still applied. If it
      was not backward-compatible, this rollback did not fix anything.
    * The digest pin lives outside the Komodo clone, so the next DeployStack
      returns the stack to the floating 'latest' tag — which is the image that
      just failed. Fix forward, or pin $PREVIOUS_REF in the GitOps repo.
EOF
  exit 1
fi

die "ROLLBACK FAILED: $PREVIOUS_REF did not come back healthy either. The
  service is DOWN. See docs/operations/runbooks.md."
