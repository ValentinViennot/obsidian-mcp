"""Desktop sync runs real git against synthetic local bare remotes."""
import os
import fcntl
import subprocess
from pathlib import Path


from tests.test_vault_reconcile_script import git, world  # noqa: F401

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/desktop/obsidian-vault-reconcile.sh"


def run_desktop(vault, tmp_path, **overrides):
    overrides = {**notification_env(tmp_path), **overrides}
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, check=False,
        env={**os.environ, "VAULT_PATH": str(vault), "LOG_PATH": str(tmp_path / "desktop.log"), **overrides},
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


def notification_env(tmp_path):
    """Intercept only the macOS notification side effect; Git/engine stay real."""
    tools = tmp_path / "notification-bin"
    tools.mkdir(exist_ok=True)
    command = tools / "osascript"
    command.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$NOTIFICATION_CAPTURE"\nexit "${NOTIFICATION_EXIT:-0}"\n')
    command.chmod(0o755)
    return {"PATH": str(tools) + os.pathsep + os.environ["PATH"],
            "NOTIFICATION_CAPTURE": str(tmp_path / "notifications")}


def test_recovery_notifies_once_without_private_note_details(world, tmp_path):
    bare, server, desktop = world
    env = notification_env(tmp_path)
    (server / "Alpha.md").write_text("remote private version\n")
    git(server, "add", "-A")
    git(server, "commit", "-qm", "remote edit")
    git(server, "push", "-q")
    (desktop / "Alpha.md").write_text("local private version\n")
    assert run_desktop(desktop, tmp_path, **env).returncode == 3
    captured = (tmp_path / "notifications").read_text()
    assert "review" in captured.lower()
    assert "Alpha" not in captured and "private version" not in captured
    assert str(desktop) not in captured
    marker = desktop / ".git/obsidian-desktop-recovery-notified"
    assert marker.stat().st_mode & 0o777 == 0o600
    assert run_desktop(desktop, tmp_path, **env).returncode == 3
    assert (tmp_path / "notifications").read_text() == captured
    assert (desktop / "Alpha.md").read_text() == "local private version\n"
    assert git(bare, "show", "main:Alpha.md").stdout == "remote private version\n"


def test_busy_retry_never_notifies(world, tmp_path):
    _, _, desktop = world
    env = notification_env(tmp_path)
    with (desktop / ".git/obsidian-sync.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run_desktop(desktop, tmp_path, **env).returncode == 75
    assert not (tmp_path / "notifications").exists()
    assert run_desktop(desktop, tmp_path, **env).returncode == 0
    assert not (tmp_path / "notifications").exists()


def test_failed_notification_retries_and_success_clears_marker(world, tmp_path):
    _, server, desktop = world
    env = notification_env(tmp_path)
    marker = desktop / ".git/obsidian-desktop-recovery-notified"
    marker.write_text("old recovery digest")
    assert run_desktop(desktop, tmp_path, **env).returncode == 0
    assert not marker.exists()
    (server / "Alpha.md").write_text("remote version\n")
    git(server, "add", "-A")
    git(server, "commit", "-qm", "remote edit")
    git(server, "push", "-q")
    (desktop / "Alpha.md").write_text("local version\n")
    assert run_desktop(desktop, tmp_path, **env, NOTIFICATION_EXIT="1").returncode == 3
    assert not marker.exists()
    assert run_desktop(desktop, tmp_path, **env).returncode == 3
    assert marker.exists()
    assert len((tmp_path / "notifications").read_text().splitlines()) == 2
