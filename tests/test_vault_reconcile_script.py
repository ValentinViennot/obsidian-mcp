"""`deploy/vault-reconcile.sh`, driven as a shell script against real repos.

Not a mock in sight: every test builds a bare repo and one or two working
clones under `tmp_path` with real `git`, then runs the real script. A sweep is
the thing that stands between an out-of-band edit and it being lost, and the
failure modes worth pinning — a rebase left half-applied, a conflict swallowed
as success, two sweeps racing — are all properties of the shell, not of
anything Python can stand in for.

All content here is synthetic and invented.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "vault-reconcile.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None
    or shutil.which("flock") is None
    or shutil.which("bash") is None,
    reason="the reconcile sweep needs git, flock and bash",
)

# The script's own exit codes, mirrored from its `readonly` block. A test that
# merely asserted "non-zero" would pass for a script that failed to start.
EXIT_MISCONFIGURED = 2
EXIT_CONFLICT = 3
EXIT_PUSH_FAILED = 4

AGENT = ("Sweep Agent", "sweep@test.invalid")
HUMAN = ("A Human", "human@test.invalid")


def git(repo, *args, check=True, who=HUMAN):
    return subprocess.run(
        [
            "git",
            "-c", f"user.name={who[0]}",
            "-c", f"user.email={who[1]}",
            "-c", f"safe.directory={repo}",
            "-C", str(repo),
            *args,
        ],
        capture_output=True, text=True, check=check,
    )


def run_sweep(vault, tmp_path, **env_overrides):
    env = {
        **os.environ,
        "VAULT_DIR": str(vault),
        "GIT_AGENT_NAME": AGENT[0],
        "GIT_AGENT_EMAIL": AGENT[1],
        "VAULT_RECONCILE_LOCK": str(tmp_path / "reconcile.lock"),
        "VAULT_RECONCILE_FLAG": str(tmp_path / "reconcile.requested"),
        "VAULT_RECONCILE_NET_TIMEOUT": "30",
        # The clones live under a `tmp_path` whose ownership the test host
        # decides; `safe.directory=*` keeps that from being what a failure
        # means. Production passes the vault root explicitly instead.
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "*",
        **env_overrides,
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, check=False
    )


def subjects(repo, ref="HEAD"):
    return git(repo, "log", "--format=%s", ref).stdout.strip().splitlines()


@pytest.fixture
def world(tmp_path):
    """A bare repo, the server's working clone, and the "desktop" clone.

    Returns `(bare, server, desktop)`. Both clones start from one seed commit,
    so a divergence in a test is one the test created.
    """
    bare = tmp_path / "vault.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "Alpha.md").write_text("original\n", encoding="utf-8")
    (seed / ".gitignore").write_text(".trash/\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True,
        capture_output=True,
    )

    server = tmp_path / "server-vault"
    desktop = tmp_path / "desktop-vault"
    for clone in (server, desktop):
        subprocess.run(
            ["git", "clone", "-q", str(bare), str(clone)], check=True,
            capture_output=True,
        )
        git(clone, "config", "user.name", HUMAN[0])
        git(clone, "config", "user.email", HUMAN[1])
    return bare, server, desktop


# ── The happy paths ─────────────────────────────────────────────────────────


def test_an_out_of_band_edit_is_committed_and_pushed(world, tmp_path):
    """An Obsidian client wrote straight into the mounted vault."""
    bare, server, desktop = world
    (server / "Alpha.md").write_text("edited in Obsidian\n", encoding="utf-8")
    (server / "Beta.md").write_text("brand new\n", encoding="utf-8")

    result = run_sweep(server, tmp_path)

    assert result.returncode == 0, result.stderr
    assert subjects(server)[0].startswith("vault(sweep): 2 path(s) changed outside")
    # The agent identity, not the human's — that separation is the whole point.
    who = git(server, "log", "-1", "--format=%an <%ae>").stdout.strip()
    assert who == f"{AGENT[0]} <{AGENT[1]}>"
    # And it reached the bare repo, so the desktop can see it.
    git(desktop, "pull", "-q")
    assert (desktop / "Beta.md").read_text(encoding="utf-8") == "brand new\n"


def test_the_sweep_commit_carries_the_same_trailers_as_a_write_commit(world, tmp_path):
    """One `git log --format='%(trailers…)'` covers both kinds of commit."""
    _bare, server, _desktop = world
    (server / "Beta.md").write_text("new\n", encoding="utf-8")

    assert run_sweep(server, tmp_path).returncode == 0

    body = git(server, "log", "-1", "--format=%B").stdout
    assert "Tool: reconcile-sweep" in body
    assert "Principal: out-of-band" in body


def test_a_desktop_push_is_pulled_into_the_server_clone(world, tmp_path):
    bare, server, desktop = world
    (desktop / "FromLaptop.md").write_text("typed on the train\n", encoding="utf-8")
    git(desktop, "add", "-A")
    git(desktop, "commit", "-q", "-m", "laptop note")
    git(desktop, "push", "-q")

    assert run_sweep(server, tmp_path).returncode == 0

    assert (server / "FromLaptop.md").read_text(encoding="utf-8") == "typed on the train\n"


def test_local_and_remote_work_are_both_kept(world, tmp_path):
    """The rebase puts the server's commit on top of the desktop's. Nothing is
    discarded to make the merge succeed — which is precisely what a
    `post-receive` `checkout -f` would have done to the local change."""
    bare, server, desktop = world
    (desktop / "FromLaptop.md").write_text("laptop\n", encoding="utf-8")
    git(desktop, "add", "-A")
    git(desktop, "commit", "-q", "-m", "laptop note")
    git(desktop, "push", "-q")
    # Meanwhile, an agent (or Obsidian) wrote a different file on the server.
    (server / "FromServer.md").write_text("server\n", encoding="utf-8")

    assert run_sweep(server, tmp_path).returncode == 0

    assert (server / "FromLaptop.md").exists()
    assert (server / "FromServer.md").exists()
    git(desktop, "pull", "-q")
    assert (desktop / "FromServer.md").read_text(encoding="utf-8") == "server\n"


def test_an_uncommitted_local_write_survives_a_pull(world, tmp_path):
    """The `--autostash` case, which is why this is not a checkout.

    An agent's write can be on disk and not yet committed — the commit is a
    separate step and is allowed to fail. A sweep that arrives at that instant
    must not lose it. (The sweep commits it first, so what this really pins is
    that a file written between `add -A` and the rebase is not destroyed.)
    """
    bare, server, desktop = world
    (desktop / "FromLaptop.md").write_text("laptop\n", encoding="utf-8")
    git(desktop, "add", "-A")
    git(desktop, "commit", "-q", "-m", "laptop note")
    git(desktop, "push", "-q")
    (server / "InFlight.md").write_text("an agent just wrote this\n", encoding="utf-8")

    assert run_sweep(server, tmp_path).returncode == 0

    assert (server / "InFlight.md").read_text(encoding="utf-8") == (
        "an agent just wrote this\n"
    )
    assert (server / "FromLaptop.md").exists()


def test_a_run_with_nothing_to_do_is_a_no_op(world, tmp_path):
    _bare, server, _desktop = world
    before = subjects(server)

    first = run_sweep(server, tmp_path)
    second = run_sweep(server, tmp_path)

    assert (first.returncode, second.returncode) == (0, 0)
    assert subjects(server) == before
    assert "nothing to commit" in first.stderr


def test_the_gitignore_is_honoured_by_the_sweep(world, tmp_path):
    """`add -A` is the catch-all, so the ignore file is the only bound on it."""
    _bare, server, _desktop = world
    (server / ".trash").mkdir()
    (server / ".trash" / "Deleted-20240101-000000.md").write_text("x\n", encoding="utf-8")
    (server / "Kept.md").write_text("kept\n", encoding="utf-8")

    assert run_sweep(server, tmp_path).returncode == 0

    names = git(server, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert names == ["Kept.md"]


# ── The conflict path ───────────────────────────────────────────────────────


def test_a_conflict_aborts_the_rebase_and_exits_loudly(world, tmp_path):
    """Both sides changed the same line. Nothing is resolved automatically."""
    bare, server, desktop = world
    (desktop / "Alpha.md").write_text("the laptop's version\n", encoding="utf-8")
    git(desktop, "add", "-A")
    git(desktop, "commit", "-q", "-m", "laptop edit")
    git(desktop, "push", "-q")
    (server / "Alpha.md").write_text("the server's version\n", encoding="utf-8")

    result = run_sweep(server, tmp_path)

    assert result.returncode == EXIT_CONFLICT
    assert "did not sync" in result.stderr
    assert "rebase aborted" in result.stderr

    # The tree is USABLE: no rebase in progress, nothing in conflict, and the
    # server's own commit still on the branch. An agent calling `edit_note` a
    # second later must not meet a repository mid-rebase.
    git_dir = Path(git(server, "rev-parse", "--absolute-git-dir").stdout.strip())
    assert not (git_dir / "rebase-merge").exists()
    assert not (git_dir / "rebase-apply").exists()
    assert git(server, "status", "--porcelain").stdout.strip() == ""
    assert (server / "Alpha.md").read_text(encoding="utf-8") == "the server's version\n"
    assert subjects(server)[0].startswith("vault(sweep):")


def test_a_writable_vault_survives_a_conflict(world, tmp_path):
    """After the abort, git still accepts a commit. The abort is not a wedge."""
    _bare, server, desktop = world
    (desktop / "Alpha.md").write_text("laptop\n", encoding="utf-8")
    git(desktop, "add", "-A")
    git(desktop, "commit", "-q", "-m", "laptop edit")
    git(desktop, "push", "-q")
    (server / "Alpha.md").write_text("server\n", encoding="utf-8")
    assert run_sweep(server, tmp_path).returncode == EXIT_CONFLICT

    (server / "After.md").write_text("written after the conflict\n", encoding="utf-8")
    git(server, "add", "After.md")
    git(server, "commit", "-q", "-m", "a later write")

    assert subjects(server)[0] == "a later write"


def test_an_unreachable_remote_fails_without_starting_a_rebase(world, tmp_path):
    bare, server, _desktop = world
    shutil.rmtree(bare)
    (server / "Beta.md").write_text("new\n", encoding="utf-8")

    result = run_sweep(server, tmp_path)

    assert result.returncode == EXIT_CONFLICT
    assert "pull --rebase failed" in result.stderr
    # Nothing to abort, so nothing pretends to have been aborted.
    assert "rebase aborted" not in result.stderr
    # The out-of-band edit is committed locally regardless: the commit happens
    # before the network step precisely so an unreachable remote never costs it.
    assert subjects(server)[0].startswith("vault(sweep):")


def test_a_push_race_is_reported_rather_than_swallowed(world, tmp_path):
    """A non-fast-forward push. Transient and self-healing — and still loud."""
    bare, server, desktop = world
    (server / "Beta.md").write_text("server\n", encoding="utf-8")

    # Land a desktop commit on the bare repo *after* the sweep's pull would
    # have run: a `pre-receive` hook that lets the pull through and rejects the
    # push is the deterministic stand-in for that race.
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    result = run_sweep(server, tmp_path)

    assert result.returncode == EXIT_PUSH_FAILED
    assert "push to" in result.stderr
    # Safe locally, which is what the message promises.
    assert subjects(server)[0].startswith("vault(sweep):")


# ── Concurrency, the flag, and misconfiguration ─────────────────────────────


def test_a_second_sweep_finding_the_lock_held_exits_zero(world, tmp_path):
    _bare, server, _desktop = world
    lock = tmp_path / "reconcile.lock"
    lock.touch()
    (server / "Beta.md").write_text("new\n", encoding="utf-8")

    # Hold the lock the way a running sweep would, then start another.
    holder = subprocess.Popen(
        ["flock", str(lock), "sleep", "10"],
    )
    try:
        result = run_sweep(server, tmp_path)
    finally:
        holder.kill()
        holder.wait()

    assert result.returncode == 0
    assert "another reconcile holds" in result.stderr
    # It really did nothing: the edit is still uncommitted for the holder.
    assert subjects(server) == ["seed"]


def test_the_flag_is_cleared_at_the_start_of_a_run(world, tmp_path):
    _bare, server, _desktop = world
    flag = tmp_path / "reconcile.requested"
    flag.touch()

    result = run_sweep(server, tmp_path)

    assert result.returncode == 0
    assert not flag.exists()
    assert "a push requested this sweep" in result.stderr


def test_a_missing_flag_is_not_an_error(world, tmp_path):
    _bare, server, _desktop = world
    result = run_sweep(server, tmp_path)
    assert result.returncode == 0
    assert "a push requested this sweep" not in result.stderr


def test_a_vault_with_no_remote_commits_locally_and_succeeds(tmp_path):
    """A local-only vault repo is a legitimate, useful setup."""
    solo = tmp_path / "solo"
    solo.mkdir()
    git(solo, "init", "-q", "-b", "main")
    (solo / "Alpha.md").write_text("one\n", encoding="utf-8")
    git(solo, "add", "-A")
    git(solo, "commit", "-q", "-m", "seed")
    (solo / "Beta.md").write_text("two\n", encoding="utf-8")

    result = run_sweep(solo, tmp_path)

    assert result.returncode == 0
    assert "tracks no remote" in result.stderr
    assert subjects(solo)[0].startswith("vault(sweep):")


def test_a_non_repository_is_refused(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = run_sweep(plain, tmp_path)
    assert result.returncode == EXIT_MISCONFIGURED
    assert "not a git working tree" in result.stderr


def test_a_missing_vault_dir_is_refused(tmp_path):
    result = run_sweep(tmp_path / "nowhere", tmp_path)
    assert result.returncode == EXIT_MISCONFIGURED


def test_an_unset_vault_dir_is_refused(tmp_path):
    result = run_sweep("", tmp_path, VAULT_DIR="")
    assert result.returncode == EXIT_MISCONFIGURED
    assert "VAULT_DIR is not set" in result.stderr


def test_a_subdirectory_of_a_repository_is_refused(world, tmp_path):
    """Pointing this at a subdirectory would sweep the enclosing repository."""
    _bare, server, _desktop = world
    inner = server / "Projects"
    inner.mkdir()

    result = run_sweep(inner, tmp_path)

    assert result.returncode == EXIT_MISCONFIGURED
    assert "is inside the repository rooted at" in result.stderr


def test_a_detached_head_is_refused(world, tmp_path):
    _bare, server, _desktop = world
    git(server, "checkout", "-q", "--detach", "HEAD")
    (server / "Beta.md").write_text("new\n", encoding="utf-8")

    result = run_sweep(server, tmp_path)

    assert result.returncode == EXIT_MISCONFIGURED
    assert "HEAD is detached" in result.stderr


# ── The post-receive hook ───────────────────────────────────────────────────


HOOK = Path(__file__).resolve().parent.parent / "deploy" / "hooks" / "post-receive"


def test_the_post_receive_hook_touches_the_flag(tmp_path):
    flag = tmp_path / "requested"
    result = subprocess.run(
        ["sh", str(HOOK)],
        input="0000 1111 refs/heads/main\n",
        capture_output=True, text=True,
        env={**os.environ, "VAULT_RECONCILE_FLAG": str(flag)},
        check=False,
    )
    assert result.returncode == 0
    assert flag.exists()


def test_the_post_receive_hook_never_fails_a_push(tmp_path):
    """A hook exiting non-zero after the refs moved reports an error for a push
    that succeeded. Nothing it cannot do is worth that."""
    unwritable = tmp_path / "nope"
    unwritable.mkdir()
    unwritable.chmod(0o500)
    try:
        result = subprocess.run(
            ["sh", str(HOOK)],
            input="0000 1111 refs/heads/main\n",
            capture_output=True, text=True,
            env={
                **os.environ,
                "VAULT_RECONCILE_FLAG": str(unwritable / "sub" / "requested"),
            },
            check=False,
        )
    finally:
        unwritable.chmod(0o700)

    assert result.returncode == 0
    if "could not touch" in result.stderr:
        # The interesting branch — only reachable when the test is not running
        # as a user that ignores directory permissions.
        assert "the push is fine" in result.stderr


def test_the_hook_and_the_sweep_agree_on_the_default_flag_path():
    """Two files, one path. A drift here is a flag nobody ever reads."""
    hook = HOOK.read_text(encoding="utf-8")
    sweep = SCRIPT.read_text(encoding="utf-8")
    unit = (
        Path(__file__).resolve().parent.parent
        / "deploy" / "systemd" / "obsidian-vault-reconcile.path"
    ).read_text(encoding="utf-8")
    service = (
        Path(__file__).resolve().parent.parent
        / "deploy" / "systemd" / "obsidian-vault-reconcile.service"
    ).read_text(encoding="utf-8")

    assert 'VAULT_RECONCILE_FLAG:-/run/obsidian-vault-reconcile/requested' in hook
    assert 'VAULT_RECONCILE_FLAG:-/run/obsidian-vault-reconcile' in sweep
    assert "PathExists=/run/obsidian-vault-reconcile/requested" in unit
    assert (
        "Environment=VAULT_RECONCILE_FLAG=/run/obsidian-vault-reconcile/requested"
        in service
    )


def test_the_scripts_are_executable():
    """They are invoked by systemd `ExecStart=` and by git as a hook."""
    assert os.access(SCRIPT, os.X_OK)
    assert os.access(HOOK, os.X_OK)
