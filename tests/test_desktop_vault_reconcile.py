"""Desktop sync runs real git against synthetic local bare remotes."""
import os
import fcntl
import subprocess
from pathlib import Path

import pytest


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


def blocked_vault(server, desktop):
    """A recovery state that stays until a human resolves it."""
    (server / "Alpha.md").write_text("remote private version\n")
    git(server, "add", "-A")
    git(server, "commit", "-qm", "remote edit")
    git(server, "push", "-q")
    (desktop / "Alpha.md").write_text("local private version\n")


def said_at(marker, seconds_ago):
    when = marker.stat().st_mtime - seconds_ago
    os.utime(marker, (when, when))


def test_a_blocked_sync_says_so_again_once_the_window_is_up(world, tmp_path):
    """One alert per distinct state left a vault silent for 43 hours."""
    _, server, desktop = world
    env = notification_env(tmp_path)
    blocked_vault(server, desktop)
    marker = desktop / ".git/obsidian-desktop-recovery-notified"
    assert run_desktop(desktop, tmp_path, **env).returncode == 3
    assert len((tmp_path / "notifications").read_text().splitlines()) == 1

    # Still blocked, and nothing about the recovery state has changed.
    said_at(marker, 61 * 60)
    assert run_desktop(desktop, tmp_path, **env).returncode == 3

    alerts = (tmp_path / "notifications").read_text().splitlines()
    assert len(alerts) == 2
    assert "still has not synchronised" in alerts[1]
    # The repeat is as private as the first one.
    assert "Alpha" not in alerts[1] and "private version" not in alerts[1]
    assert str(desktop) not in alerts[1]
    # And the clock restarts, so the sweeps in between stay quiet.
    assert run_desktop(desktop, tmp_path, **env).returncode == 3
    assert len((tmp_path / "notifications").read_text().splitlines()) == 2


def test_the_repeat_window_is_configurable_and_can_be_turned_off(world, tmp_path):
    _, server, desktop = world
    env = notification_env(tmp_path)
    blocked_vault(server, desktop)
    marker = desktop / ".git/obsidian-desktop-recovery-notified"
    assert run_desktop(desktop, tmp_path, **env, VAULT_RECONCILE_RENOTIFY_MINUTES="0").returncode == 3
    said_at(marker, 40 * 24 * 60 * 60)
    assert run_desktop(desktop, tmp_path, **env, VAULT_RECONCILE_RENOTIFY_MINUTES="0").returncode == 3
    assert len((tmp_path / "notifications").read_text().splitlines()) == 1

    assert run_desktop(desktop, tmp_path, **env, VAULT_RECONCILE_RENOTIFY_MINUTES="5").returncode == 3
    assert len((tmp_path / "notifications").read_text().splitlines()) == 2


def test_a_changed_recovery_state_alerts_at_once_inside_the_window(world, tmp_path):
    """The window may delay a repeat; it must never delay news.

    A recovery state that changes — another displaced version, an editor saving
    into a retained inode — is new information, and waiting an hour for it
    would be a regression the two window tests above would not catch.
    """
    _, server, desktop = world
    env = notification_env(tmp_path)
    blocked_vault(server, desktop)
    marker = desktop / ".git/obsidian-desktop-recovery-notified"
    assert run_desktop(desktop, tmp_path, **env).returncode == 3
    first = (tmp_path / "notifications").read_text().splitlines()
    assert len(first) == 1
    assert "still has not synchronised" not in first[0]

    # Same instant, so the window is nowhere near up; different state.
    marker.write_text("a digest from some other recovery state")
    assert run_desktop(desktop, tmp_path, **env).returncode == 3

    alerts = (tmp_path / "notifications").read_text().splitlines()
    assert len(alerts) == 2
    assert "still has not synchronised" not in alerts[1]


@pytest.mark.parametrize("tool", ["stat", "date"])
def test_an_unusable_clock_or_stat_never_changes_the_exit_status(world, tmp_path, tool):
    """The sweep hands launchd the engine's status, whatever the alerting does.

    `date` is as unavailable as anything else when a fork fails under memory
    pressure, which is precisely when a false status is worst.
    """
    _, server, desktop = world
    env = notification_env(tmp_path)
    blocked_vault(server, desktop)
    assert run_desktop(desktop, tmp_path, **env).returncode == 3
    broken = Path(env["PATH"].split(os.pathsep)[0]) / tool
    broken.write_text(
        '#!/bin/sh\n'
        'printf "  File: \\"x\\"\\n    ID: deadbeef Namelen: 255\\n"\n'
        'exit 1\n'
    )
    broken.chmod(0o755)
    try:
        done = run_desktop(desktop, tmp_path, **env)
        assert done.returncode == 3, done.stderr
        assert "unbound variable" not in done.stderr
        # Unusable means "no record of having spoken", which repeats.
        assert len((tmp_path / "notifications").read_text().splitlines()) == 2
    finally:
        broken.unlink()


@pytest.mark.parametrize("value", ["abc", "-5", "008", "", "60 "])
def test_an_unusable_window_falls_back_to_repeating(world, tmp_path, value):
    """Every rejected value must land on the default, never on silence: a
    leading zero reads as octal in bash, and `008` aborted the comparison.
    """
    _, server, desktop = world
    env = notification_env(tmp_path)
    blocked_vault(server, desktop)
    marker = desktop / ".git/obsidian-desktop-recovery-notified"
    assert run_desktop(desktop, tmp_path, **env,
                       VAULT_RECONCILE_RENOTIFY_MINUTES=value).returncode == 3
    said_at(marker, 61 * 60)
    done = run_desktop(desktop, tmp_path, **env, VAULT_RECONCILE_RENOTIFY_MINUTES=value)
    assert done.returncode == 3, done.stderr
    assert len((tmp_path / "notifications").read_text().splitlines()) == 2


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
