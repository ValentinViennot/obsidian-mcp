"""Report current/pending without upgrading; refuse ambiguous Alembic output.

Run inside the candidate image with RUN_MIGRATIONS=false. Both subprocesses
are read-only: a deploy must decide whether to stop the old application before
any migration can execute. Failure is never interpreted as an empty database.
"""
from __future__ import annotations

import re
import subprocess
import sys


def revisions(command: str) -> list[str]:
    result = subprocess.run(
        ["alembic", command], capture_output=True, text=True, check=True, timeout=60,
    )
    rows = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-f]+)(?:\s+\([^)]*\))*", line.strip())
        if match is None:
            raise ValueError("unexpected Alembic revision output")
        rows.append(match[1])
    return rows


def main() -> int:
    try:
        current, heads = revisions("current"), revisions("heads")
        if len(current) > 1 or len(heads) != 1:
            raise ValueError("expected one migration head and at most one current revision")
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Cannot establish migration status; refusing deployment.", file=sys.stderr)
        return 1
    print("current" if current == heads else "pending")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
