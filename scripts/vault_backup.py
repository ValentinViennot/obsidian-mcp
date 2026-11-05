"""Private, lock-coordinated backup of a working vault and its Git recovery data.

Run as the vault owner (or root). Independent editors must be quiesced by the
operator; the shared lock coordinates the MCP and the supported sync engine.
The destination must be outside the vault and must not exist. No pruning.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import tarfile


def backup(vault: Path, destination: Path) -> str:
    vault = vault.resolve(strict=True)
    destination = destination.absolute()
    if destination.resolve().is_relative_to(vault):
        raise ValueError("backup destination must be outside the vault")
    git = vault / ".git"
    if git.is_symlink() or not git.is_dir():
        raise ValueError("a working clone with a local .git directory is required")
    fd = os.open(git / "obsidian-sync.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.geteuid() == 0:
            owner = git.stat()
            os.fchown(fd, owner.st_uid, owner.st_gid)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Exclusive creation and private mode, including on shared staging mounts.
        out = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(out, "wb") as stream:
            with tarfile.open(fileobj=stream, mode="w", dereference=False) as archive:
                archive.add(vault, arcname="vault")
            stream.flush()
            os.fsync(stream.fileno())
        # Read every regular member back; merely listing a tar misses truncation.
        with tarfile.open(destination, "r") as archive:
            for member in archive:
                if member.isfile():
                    with archive.extractfile(member) as stream:
                        while stream.read(1024 * 1024):
                            pass
        with destination.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        parent = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        return digest
    finally:
        os.close(fd)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vault", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(backup(args.vault, args.destination))
