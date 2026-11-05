"""File helpers shared by the harness: canonical JSON, hashes, owner-only atomic writes.

Every file the harness writes for real data is tenant data (design D9), so it
is created `0600` inside a `0700` directory, and every publication is a
`.tmp` write followed by `os.replace`, so a reader never sees half a file.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    """Sorted keys, no insignificant whitespace, non-ASCII kept verbatim."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def pretty_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def ensure_private_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def atomic_write(path: Path, data: str | bytes) -> str:
    """Write `data` to `path` via a fresh temp file + rename, mode 0600. Returns its sha256.

    The temp file is created by `tempfile` in the destination directory with
    an unpredictable name and `O_EXCL`, so a pre-existing file or symlink at a
    guessable `<name>.tmp` is never opened or followed.
    """
    path = Path(path)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    tmp = tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
                                      delete=False)
    try:
        with tmp as fh:
            os.chmod(fh.name, 0o600)
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp.name, path)
    except BaseException:
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass
        raise
    return sha256_bytes(raw)


def read_jsonl(path: Path) -> list[Any]:
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out
