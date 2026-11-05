import fcntl
import tarfile

import pytest

from scripts.vault_backup import backup


def test_private_backup_keeps_uncommitted_and_recovery(tmp_path):
    vault = tmp_path / "source"
    recovery = vault / ".git" / "obsidian-recovery"
    recovery.mkdir(parents=True)
    (recovery / "saved").write_text("retained synthetic editor save")
    (vault / "draft.md").write_text("uncommitted synthetic draft")
    target = tmp_path / "backup.tar"
    assert len(backup(vault, target)) == 64
    assert target.stat().st_mode & 0o777 == 0o600
    with tarfile.open(target) as archive:
        assert archive.extractfile("vault/draft.md").read() == b"uncommitted synthetic draft"
        assert archive.extractfile("vault/.git/obsidian-recovery/saved").read() == b"retained synthetic editor save"
    with pytest.raises(FileExistsError):
        backup(vault, target)
    with pytest.raises(ValueError):
        backup(vault, vault / "backup.tar")


def test_backup_refuses_active_supported_writer(tmp_path):
    vault = tmp_path / "source"
    (vault / ".git").mkdir(parents=True)
    with (vault / ".git/obsidian-sync.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            backup(vault, tmp_path / "backup.tar")
    assert not (tmp_path / "backup.tar").exists()
