FROM python:3.12-slim

WORKDIR /app

# Install system dependencies.
#
# `git` is a hard runtime requirement for this fork, not a convenience: the
# vault is a git repository, commit-on-write shells out to git after every
# write-class tool call, and note_history / note_blame / find_when_written are
# nothing but git plumbing. Without the binary all four degrade *silently* —
# the write still lands and the tools still answer, they just stop recording
# and reporting history, which is the one failure nobody notices until they
# need the history. tests/test_runtime_dependencies.py keeps this honest.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Add non-root user
RUN useradd -m -u 1000 -s /bin/bash appuser

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY alembic.ini .
COPY alembic/ alembic/
COPY src/ src/
COPY scripts/ scripts/

USER appuser

EXPOSE 8000

# --workers 1 is load-bearing, not a default nobody revisited. Every /mcp rate
# control is IN-PROCESS state: the two per-principal token buckets, the
# per-address failed-authentication table and the refusal coalescer all live in
# this worker's memory and are deliberately not persisted or shared. A second
# worker therefore does not split the configured rates between them — it gives
# each worker a full set, multiplying every effective rate by the worker count,
# and splits the coalescer so the same key writes a row per worker. Raising it
# means revisiting docs/architecture/rate-limits.md first.
#
# --forwarded-allow-ips: trust X-Forwarded-* only from private ranges (the
# reverse proxy lives in the Docker network), not from "*" which would let any
# client spoof its forwarded IP. Narrow to the specific compose subnet CIDR if
# desired.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "172.16.0.0/12,10.0.0.0/8"]
