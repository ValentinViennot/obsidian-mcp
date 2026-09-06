#!/usr/bin/env bash
# Run the test suite on Linux, from any host.
#
# Why this exists: src/services/vault_fs.py is built on openat2(2), which only
# Linux has. Running `pytest` directly on macOS fails 863 tests at import time,
# which looks like a broken branch and is really just the wrong kernel. This
# script runs the same suite in the same Python version against a Linux kernel,
# with the working tree bind-mounted so there is nothing to sync.
#
# Usage:
#   scripts/test-in-docker.sh                  # whole suite
#   scripts/test-in-docker.sh tests/test_x.py  # any pytest arguments
#   REBUILD=1 scripts/test-in-docker.sh        # force the image to rebuild
#   SKIP_BUILD=1 scripts/test-in-docker.sh     # never build; use the image as-is
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="obsidian-mcp-test:local"

cd "$REPO_ROOT"

needs_build=0
if [ "${SKIP_BUILD:-0}" = "1" ]; then
  # CI builds the image itself, with a registry-backed layer cache, and then
  # runs this script so that local and CI execute the same command against the
  # same image definition. The staleness check below would defeat that: a
  # cache-restored image carries its ORIGINAL creation timestamp, which is
  # older than the mtimes a fresh checkout stamps on the manifests, so every
  # run would discard the cache and rebuild from scratch.
  needs_build=0
  if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "SKIP_BUILD=1 but $IMAGE does not exist — build it first." >&2
    exit 1
  fi
elif [ "${REBUILD:-0}" = "1" ]; then
  needs_build=1
elif ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  needs_build=1
else
  # Rebuild when the dependency manifests are newer than the image, so a
  # requirements bump is never silently tested against stale packages.
  image_created="$(docker image inspect -f '{{.Created}}' "$IMAGE")"
  image_epoch="$(date -j -f '%Y-%m-%dT%H:%M:%S' "${image_created:0:19}" '+%s' 2>/dev/null \
    || date -d "$image_created" '+%s' 2>/dev/null || echo 0)"
  for manifest in requirements.txt requirements-dev.txt docker/test.Dockerfile; do
    manifest_epoch="$(stat -f '%m' "$manifest" 2>/dev/null || stat -c '%Y' "$manifest")"
    if [ "$manifest_epoch" -gt "$image_epoch" ]; then needs_build=1; fi
  done
fi

if [ "$needs_build" = "1" ]; then
  echo "==> building $IMAGE" >&2
  docker build -q -f docker/test.Dockerfile -t "$IMAGE" . >&2
fi

# The unit suite is hermetic and mocks every outbound call, so it runs with the
# network denied: an accidental real HTTP call becomes a loud failure instead of
# a slow, flaky, or silently-passing test. The integration suite genuinely needs
# to reach Postgres, so it opts back in by setting PGVECTOR_TEST_ADMIN_URL
# (the same variable the suite already uses to decide whether to run at all).
net_args=(--network none)
env_args=()
if [ -n "${PGVECTOR_TEST_ADMIN_URL:-}" ]; then
  net_args=()
  # host.docker.internal reaches a Postgres published on the host's loopback.
  env_args=(-e "PGVECTOR_TEST_ADMIN_URL=${PGVECTOR_TEST_ADMIN_URL}"
            --add-host=host.docker.internal:host-gateway)
fi

# The `${arr[@]+...}` guard is required: under `set -u`, expanding an empty
# array is an unbound-variable error on the bash 3.2 that ships with macOS.
exec docker run --rm \
  ${net_args[@]+"${net_args[@]}"} \
  ${env_args[@]+"${env_args[@]}"} \
  -v "$REPO_ROOT:/app" \
  -w /app \
  -e PYTHONDONTWRITEBYTECODE=1 \
  "$IMAGE" \
  pytest "$@"
