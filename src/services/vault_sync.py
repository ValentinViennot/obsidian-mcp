"""Cross-process exclusion between supported vault writers and reconciliation.

The lock covers publication through commit and the supported Git writers.
Independent editor saves are protected by retained inodes, not this lock.
Locks are kernel-owned: a crash releases them without deleting a live lock.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from pathlib import Path

from src.config import settings


class VaultSyncBusy(RuntimeError):
    """Nothing published: the supported sync writer still owns the lock."""


_held_root = ContextVar("vault_writer_root", default=None)


@asynccontextmanager
async def writing(root):
    """Coordinate publication; recovery pauses Git commits, never note writes."""
    git_dir = Path(root) / ".git"
    if not settings.git_vault_enabled or not git_dir.is_dir():
        yield
        return
    fd = os.open(git_dir / "obsidian-sync.lock", os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + settings.git_commit_timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise VaultSyncBusy("Vault sync is busy. No edit was published; retry shortly.") from None
                await asyncio.sleep(0.05)
        token = _held_root.set(str(Path(root).resolve()))
        try:
            yield
        finally:
            _held_root.reset(token)
    finally:
        os.close(fd)


@contextmanager
def committing(root):
    """All direct and tracked Git commits share the sweep lock/recovery gate."""
    root = Path(root).resolve()
    fd = None
    allowed = True
    try:
        if (root / ".git").is_dir():
            if _held_root.get() != str(root):
                fd = os.open(root / ".git/obsidian-sync.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            from src.services.vault_reconcile import Reconciler
            allowed = not Reconciler(root).status()["review_required"]
            allowed &= not any((root / ".git" / name).exists() for name in
                   ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD"))
    except (OSError, ValueError, RuntimeError):
        allowed = False
    try:
        yield allowed
    finally:
        if fd is not None:
            os.close(fd)
