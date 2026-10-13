"""Desktop sync runs real git against synthetic local bare remotes."""
import os
import subprocess
from pathlib import Path


from tests.test_vault_reconcile_script import git, world  # noqa: F401

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/desktop/obsidian-vault-reconcile.sh"


def run_desktop(vault, tmp_path):
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, check=False,
        env={**os.environ, "VAULT_PATH": str(vault), "LOG_PATH": str(tmp_path / "desktop.log")},
    )


def test_tracks_configured_remote_branch(world, tmp_path):
    bare, server, desktop = world
    git(desktop, "remote", "rename", "origin", "hosted")
    git(desktop, "push", "hosted", "HEAD:notes")
    git(desktop, "config", "branch.main.merge", "refs/heads/notes")
    (desktop / "Local.md").write_text("local recent edit\n")
    result = run_desktop(desktop, tmp_path)
    assert result.returncode == 0, result.stderr
    assert git(bare, "show", "notes:Local.md").stdout == "local recent edit\n"
    assert git(bare, "log", "--format=%s", "main").stdout.strip() == "seed"


def test_failed_commit_never_pulls_or_pushes(world, tmp_path):
    bare, server, desktop = world
    (server / "Alpha.md").write_text("remote version\n")
    git(server, "add", "-A")
    git(server, "commit", "-qm", "remote edit")
    git(server, "push", "-q")
    (desktop / "Alpha.md").write_text("recent local edit\n")
    hook = desktop / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    before = git(desktop, "rev-parse", "HEAD").stdout
    result = run_desktop(desktop, tmp_path)
    assert result.returncode != 0
    assert git(desktop, "rev-parse", "HEAD").stdout == before
    assert (desktop / "Alpha.md").read_text() == "recent local edit\n"
    assert "nothing was fetched" in (tmp_path / "desktop.log").read_text()
    assert git(desktop, "stash", "list").stdout == ""


def test_interrupted_operation_leaves_everything_untouched(world, tmp_path):
    bare, server, desktop = world
    (desktop / ".git/rebase-merge").mkdir()
    (desktop / "Alpha.md").write_text("unresolved recent edit\n")
    before = git(desktop, "status", "--porcelain").stdout
    result = run_desktop(desktop, tmp_path)
    assert result.returncode != 0
    assert git(desktop, "status", "--porcelain").stdout == before
    assert (desktop / "Alpha.md").read_text() == "unresolved recent edit\n"
    assert (desktop / ".git/rebase-merge").is_dir()


def test_conflict_preserves_local_commit_and_allows_retry(world, tmp_path):
    bare, server, desktop = world
    (server / "Alpha.md").write_text("remote version\n")
    git(server, "add", "-A")
    git(server, "commit", "-qm", "remote edit")
    git(server, "push", "-q")
    (desktop / "Alpha.md").write_text("recent local edit\n")
    assert run_desktop(desktop, tmp_path).returncode != 0
    assert (desktop / "Alpha.md").read_text() == "recent local edit\n"
    assert not (desktop / ".git/rebase-merge").exists()
    assert git(desktop, "show", "HEAD:Alpha.md").stdout == "recent local edit\n"
    git(desktop, "fetch", "origin")
    assert git(desktop, "show", "origin/main:Alpha.md").stdout == "remote version\n"


def test_detached_head_does_not_commit_local_edits(world, tmp_path):
    bare, server, desktop = world
    git(desktop, "checkout", "--detach", "HEAD")
    (desktop / "Recent.md").write_text("keep me\n")
    before = git(desktop, "rev-parse", "HEAD").stdout
    assert run_desktop(desktop, tmp_path).returncode != 0
    assert git(desktop, "rev-parse", "HEAD").stdout == before
    assert (desktop / "Recent.md").read_text() == "keep me\n"
