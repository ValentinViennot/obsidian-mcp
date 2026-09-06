# Runtime image for the MCP server.
#
# Two stages. The BUILDER resolves and installs every Python dependency into a
# self-contained virtualenv at /opt/venv; the RUNTIME stage copies that one
# directory and nothing else from it. The point is not tidiness: pip leaves
# build metadata, a wheel cache and its own tooling behind, and a single-stage
# image ships all of it plus whatever apt needed to unpack it. Copying the venv
# means the runtime filesystem contains exactly the interpreter, the packages,
# and the source.
#
# Layer order is deliberate and is the other half of the build's cost: the
# dependency install depends only on requirements.txt, so editing src/ rebuilds
# only the last few layers and re-uses the (slow) install. Copying src/ before
# installing would invert that and reinstall ~40 packages per source change.

# ---------------------------------------------------------------------------
# Stage 1 — builder
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# PIP_NO_CACHE_DIR: this stage is never re-entered, so a wheel cache inside it
# is weight that gets copied nowhere and helps nothing.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

# A venv rather than the system environment: it is a single relocatable
# directory the next stage can take wholesale, and it keeps the app's packages
# from being interleaved with the base image's own site-packages.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Copied alone, before any source, so this layer's cache key is the pin file.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# No build toolchain is installed in either stage: every pin resolves to a
# manylinux wheel today, and keeping gcc out means a future pin that needs to
# compile fails the build here rather than quietly doubling the image.
#
# pip/setuptools/wheel are installation-time tools. Nothing under src/ imports
# them, and leaving them in the runtime venv both adds weight and hands a
# compromised process a working package installer.
RUN pip uninstall --yes pip setuptools wheel

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Three runtime packages, each load-bearing:
#
#   curl  — the HEALTHCHECK below and the compose healthchecks call it. Kept
#           rather than replaced with a python one-liner because an operator
#           debugging a sick container reaches for it too.
#   git   — a hard runtime requirement for this fork, not a convenience: the
#           vault is a git repository, commit-on-write shells out to git after
#           every write-class tool call, and note_history / note_blame /
#           find_when_written are nothing but git plumbing. Without the binary
#           all four degrade *silently* — the write still lands and the tools
#           still answer, they just stop recording and reporting history, which
#           is the one failure nobody notices until they need the history.
#           tests/test_runtime_dependencies.py keeps this honest, and does it
#           against this file rather than a built image precisely so that a
#           restructuring like this multi-stage split cannot drop the package.
#   tini  — see ENTRYPOINT.
#
# `apt-get upgrade` stays: the deploy gate (`make trivy`, and the trivy-scan CI
# job) fails on fixable HIGH/CRITICAL findings, and the base image's own
# packages are a regular source of them.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        curl \
        git \
        tini \
    && rm -rf /var/lib/apt/lists/*

# The vault is a BIND MOUNT from the host and the write tools mutate it, so the
# container's uid must be able to write the host directory. uid/gid 1000 is the
# first non-system id on virtually every Linux distribution and therefore the
# usual owner of a single-admin VPS's files; `chown -R 1000:1000` on the vault
# is the one-line fix when it is not. The compose file exposes APP_UID/APP_GID
# so the id can be overridden at RUN time without rebuilding — the app never
# needs this account to exist in /etc/passwd, because git's ownership check is
# handled per-call with `-c safe.directory` (src/services/git_history.py) and
# every git config layer is pinned off, so there is no lookup of HOME either.
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin appuser

# The base image's own pip, which is a different one from the venv's (already
# removed in the builder). Nothing at runtime installs anything, and leaving a
# working package installer in an image is how a foothold becomes a payload.
RUN /usr/local/bin/python3 -m pip uninstall --yes pip \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The dependency layer: large, and invalidated only by requirements.txt.
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Source last, smallest-to-largest churn. alembic.ini and alembic/ change per
# migration; src/ changes constantly. scripts/ is imported by the maintenance
# targets (`python -m scripts.rebuild_tsvectors`) that run inside this image.
COPY alembic.ini ./
COPY alembic/ alembic/
COPY scripts/ scripts/
COPY src/ src/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# Byte-compiled ahead of time because PYTHONDONTWRITEBYTECODE is set and /app
# is root-owned anyway: without this the non-root process re-parses every
# module on every start and can never cache the result.
RUN python -m compileall -q /app/src /app/scripts || true \
    && chmod 0755 /usr/local/bin/entrypoint.sh

USER 1000:1000

EXPOSE 8000

# Hits the app's own /health, which reports liveness plus two startup verdicts
# (named-staging fallback, transfer mount-identity probe) — see src/main.py.
# start-period is generous because the lifespan runs the embedding-dimension
# check and publishes the vault-root snapshot before it serves.
#
# `localhost`, NOT 127.0.0.1. The Host header has to be in `allowed_hosts`, and
# once MCP_HOSTNAME is set that list is [hostname, "localhost"] — Starlette's
# TrustedHostMiddleware answers a literal-IP Host with 400, so an otherwise
# healthy container would report unhealthy forever (see src/config.py, where
# `allowed_hosts` is derived).
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["curl", "-fsS", "http://localhost:8000/health"]

# tini is PID 1 for two reasons, and both are real here rather than cargo cult.
# (1) Reaping: the git history tools spawn `git`, which itself spawns children
# (pager-less as configured, but `git log --follow` still forks internally);
# any process orphaned mid-kill reparents to PID 1, and a Python PID 1 does not
# reap, so the container would accumulate zombies against its own pids limit.
# (2) Signals: tini forwards SIGTERM to uvicorn so `docker stop` is a graceful
# shutdown that completes in-flight MCP requests instead of a 10s wait and a
# SIGKILL. entrypoint.sh then exec's the CMD, so uvicorn keeps its own pid.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]

# --workers 1 is load-bearing, not a default nobody revisited. Every /mcp rate
# control is IN-PROCESS state: the two per-principal token buckets, the
# per-address failed-authentication table and the refusal coalescer all live in
# this worker's memory and are deliberately not persisted or shared. A second
# worker therefore does not split the configured rates between them — it gives
# each worker a full set, multiplying every effective rate by the worker count,
# and splits the coalescer so the same key writes a row per worker. Raising it
# means revisiting docs/architecture/rate-limits.md first.
#
# It lives HERE and only here. The compose files deliberately do not override
# `command:` — a stack that repeated the uvicorn invocation to prepend a
# migration is exactly how this contract drifts, which is why the migration
# moved into entrypoint.sh (RUN_MIGRATIONS) instead.
#
# --forwarded-allow-ips: trust X-Forwarded-* only from private ranges (the
# reverse proxy lives in the Docker network), not from "*" which would let any
# client spoof its forwarded IP. Narrow to the specific compose subnet CIDR if
# desired.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "172.16.0.0/12,10.0.0.0/8"]
