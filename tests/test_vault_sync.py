"""Kernel lock exclusion and interrupted-operation preservation."""
import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.services import vault_sync


@pytest.fixture
def clone(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.setattr(vault_sync.settings, "git_vault_enabled", True)
    return tmp_path


@pytest.mark.asyncio
async def test_writer_lock_excludes_another_process(clone):
    async with vault_sync.writing(clone):
        probe = subprocess.run([
            sys.executable, "-c",
            "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)",
            str(clone / ".git/obsidian-sync.lock"),
        ], capture_output=True)
        assert probe.returncode != 0
    async with vault_sync.writing(clone):
        pass  # Released even when a caller leaves the context.


@pytest.mark.asyncio
async def test_busy_sync_refuses_before_publication(clone, monkeypatch):
    monkeypatch.setattr(vault_sync.settings, "git_commit_timeout_seconds", 0.01)
    fd = os.open(clone / ".git/obsidian-sync.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(RuntimeError, match="No edit was published"):
            async with vault_sync.writing(clone):
                (clone / "Recent.md").write_text("must not publish")
        assert not (clone / "Recent.md").exists()
    finally:
        os.close(fd)


@pytest.mark.asyncio
async def test_interrupted_git_keeps_writes_available_but_pauses_commits(clone):
    (clone / ".git/rebase-merge").mkdir()
    async with vault_sync.writing(clone):
        (clone / "Recent.md").write_text("recent edit remains available")
        with vault_sync.committing(clone) as allowed:
            assert not allowed
    assert (clone / ".git/rebase-merge").is_dir()
    assert (clone / "Recent.md").read_text() == "recent edit remains available"


@pytest.mark.asyncio
async def test_disabled_git_keeps_plain_vault_writable(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_sync.settings, "git_vault_enabled", False)
    async with vault_sync.writing(tmp_path):
        (tmp_path / "Recent.md").write_text("plain vault")
    assert (tmp_path / "Recent.md").read_text() == "plain vault"


def test_clone_helper_preserves_existing_vault(tmp_path):
    script = Path(__file__).resolve().parents[1] / "deploy/clone-vault.py"
    (tmp_path / "Recent.md").write_text("recent local edit")
    result = subprocess.run([sys.executable, str(script)], capture_output=True,
        env={**os.environ, "VAULT_PATH": str(tmp_path), "VAULT_GIT_REMOTE": "does-not-exist"})
    assert result.returncode == 2
    assert (tmp_path / "Recent.md").read_text() == "recent local edit"


def test_clone_helper_uses_configured_local_fixture_remote(tmp_path):
    from tests.test_vault_reconcile_script import git
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "notes")
    (seed / "Recent.md").write_text("synthetic note")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "seed")
    bare = tmp_path / "hosted.git"
    git(seed, "clone", "--bare", str(seed), str(bare))
    clone = tmp_path / "clone"
    script = Path(__file__).resolve().parents[1] / "deploy/clone-vault.py"
    result = subprocess.run([sys.executable, str(script)], capture_output=True,
        env={**os.environ, "VAULT_PATH": str(clone), "VAULT_GIT_REMOTE": str(bare),
             "VAULT_GIT_BRANCH": "notes"})
    assert result.returncode == 0, result.stderr
    assert (clone / "Recent.md").read_text() == "synthetic note"
    assert git(clone, "config", "branch.notes.merge").stdout.strip() == "refs/heads/notes"
