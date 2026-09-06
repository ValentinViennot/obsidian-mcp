# Test runner image.
#
# The vault containment layer (src/services/vault_fs.py) is built on openat2(2),
# a Linux syscall. On macOS every test that touches it fails at import time —
# 863 of them — so a bare `pytest` on a Mac is not a meaningful signal. This
# image gives the suite the Linux kernel it actually targets, while the source
# stays bind-mounted from the host so the edit/test loop is unchanged.
#
# Dependencies are installed into the image (not a mounted venv) so that a
# host-side .venv built for macOS is simply ignored.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copied separately from the source so the dependency layer is cached and only
# rebuilds when the requirements actually change.
COPY requirements.txt requirements-dev.txt /app/
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

# git refuses to operate on a repository owned by another uid, which is exactly
# what a bind mount from the host looks like.
RUN git config --global --add safe.directory '*'

# Run as an unprivileged user, matching CI.
#
# As root, every permission-dependent assertion in this suite is vacuous: root
# ignores file modes, so a test that makes a directory 0500 and expects a write
# to fail sees it succeed. That is not a hypothetical — a hook test passed here
# for exactly this reason while failing on GitHub's unprivileged runner, and the
# runner was right. A gate that cannot reproduce CI's failures is not a gate.
#
# uid 1000 matches the typical host user, so a bind-mounted tree stays writable.
RUN useradd -m -u 1000 -s /bin/bash runner \
    && git config --system --add safe.directory '*'
USER runner
ENV HOME=/home/runner

CMD ["pytest", "-q"]
