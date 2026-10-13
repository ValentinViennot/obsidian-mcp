"""Actual native exchanges and Git histories, with real independent editors."""
import os
from pathlib import Path
import subprocess

import pytest

from src.services.vault_reconcile import Reconciler, ReconcileError


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE).decode().strip()


@pytest.fixture
def clones(tmp_path):
    bare, live, peer = (tmp_path / name for name in ("remote.git", "live", "peer"))
    subprocess.run(["git", "init", "--bare", "--quiet", "-b", "main", str(bare)], check=True)
    for root in (live, peer):
        subprocess.run(["git", "clone", "--quiet", str(bare), str(root)], check=True, capture_output=True)
        git(root, "config", "user.name", "Synthetic Human")
        git(root, "config", "user.email", "human@example.invalid")
    (live / "Note.md").write_text("baseline\n")
    git(live, "add", "-A")
    git(live, "commit", "-qm", "baseline")
    git(live, "push", "-qu", "origin", "main")
    git(peer, "pull", "-q", "origin", "main")
    return bare, live, peer


def incoming(peer, path="Note.md", value="incoming\n"):
    (peer / path).write_text(value)
    git(peer, "add", "-A")
    git(peer, "commit", "-qm", "incoming")
    git(peer, "push", "-q")


def versions(engine):
    return [version for tx in engine.status()["transactions"] for version in tx["versions"]]


def test_independent_changes_merge_without_rewriting_human_history(clones):
    bare, live, peer = clones
    incoming(peer)
    remote_head = git(peer, "rev-parse", "HEAD")
    (live / "Local.md").write_text("recent local edit\n")
    engine = Reconciler(live, desktop=True)
    engine.run()
    assert (live / "Note.md").read_text() == "incoming\n"
    assert (live / "Local.md").read_text() == "recent local edit\n"
    assert git(live, "log", "--format=%an", "--", "Local.md") == "Synthetic Human"
    assert git(live, "merge-base", "--is-ancestor", remote_head, "HEAD") == ""
    assert git(bare, "show", "main:Local.md") == "recent local edit"
    assert all("obsidian-recovery" not in path for path in git(live, "ls-files").splitlines())


def test_same_note_conflict_preserves_live_bytes_and_both_heads(clones):
    _, live, peer = clones
    incoming(peer)
    remote_head = git(peer, "rev-parse", "HEAD")
    (live / "Note.md").write_text("local recent edit\n")
    engine = Reconciler(live, desktop=True)
    with pytest.raises(ReconcileError, match="conflict retained"):
        engine.run()
    assert (live / "Note.md").read_text() == "local recent edit\n"
    assert git(live, "show", "HEAD:Note.md") == "local recent edit"
    assert remote_head in git(live, "show-ref")
    assert engine.status()["review_required"]
    assert not (live / ".git/MERGE_HEAD").exists()


@pytest.mark.parametrize("save", ["in_place_before", "fd_after", "rename_before", "rename_after"])
def test_editor_autosave_is_visible_or_on_exact_displaced_inode(clones, save):
    _, live, peer = clones
    incoming(peer)
    descriptor = os.open(live / "Note.md", os.O_RDWR)
    original_inode = os.fstat(descriptor).st_ino
    fired = False
    def observer(label, journal):
        nonlocal fired
        target = "before_publication" if save.endswith("before") else "after_publication"
        if fired or label != target:
            return
        fired = True
        if save.startswith("rename"):
            temporary = live / "editor.tmp"
            temporary.write_text("editor autosave\n")
            temporary.replace(live / "Note.md")
        else:
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.ftruncate(descriptor, 0)
            os.write(descriptor, b"editor autosave\n")
            os.fsync(descriptor)
    engine = Reconciler(live, desktop=True, observer=observer)
    try:
        with pytest.raises(ReconcileError, match="local edit|new local edit"):
            engine.run()
        recovered = versions(engine)
        assert (live / "Note.md").read_text() == "editor autosave\n" or any(
            Path(version["location"]).read_text() == "editor autosave\n" for version in recovered)
        if save == "fd_after":
            assert any(Path(version["location"]).stat().st_ino == original_inode for version in recovered)
        assert git(live, "show", "HEAD:Note.md") == "incoming"
    finally:
        os.close(descriptor)


def test_delayed_open_descriptor_save_is_detected_on_next_sweep(clones, tmp_path):
    _, live, peer = clones
    incoming(peer)
    fd = os.open(live / "Note.md", os.O_RDWR)
    engine = Reconciler(live, desktop=True)
    try:
        engine.run()
        assert not engine.status()["review_required"]
        os.ftruncate(fd, 0)
        os.write(fd, b"late autosave\n")
        os.fsync(fd)
        assert engine.status()["review_required"]
        before = git(live, "rev-parse", "HEAD")
        with pytest.raises(ReconcileError, match="recovery review"):
            engine.run()
        assert git(live, "rev-parse", "HEAD") == before
        version = versions(engine)[0]
        tx = engine.status()["transactions"][0]["id"]
        exported = tmp_path / "recovered.md"
        engine.export(tx, version["path"], exported)
        assert exported.read_text() == "late autosave\n"
        with pytest.raises(FileExistsError):
            engine.export(tx, version["path"], exported)
    finally:
        os.close(fd)


def test_deletion_retains_open_inode_and_late_edit(clones):
    _, live, peer = clones
    git(peer, "rm", "Note.md")
    git(peer, "commit", "-qm", "delete")
    git(peer, "push", "-q")
    fd = os.open(live / "Note.md", os.O_RDWR)
    engine = Reconciler(live, desktop=True)
    try:
        engine.run()
        assert not (live / "Note.md").exists()
        os.ftruncate(fd, 0)
        os.write(fd, b"late retained deletion edit\n")
        assert engine.status()["review_required"]
        assert Path(versions(engine)[0]["location"]).read_text() == "late retained deletion edit\n"
    finally:
        os.close(fd)


@pytest.mark.parametrize("boundary", ["prepared", "planned", "before_publication", "after_syscall", "before_head", "after_head"])
def test_real_process_kill_recovers_without_replaying_an_exchange(clones, boundary):
    import signal
    import sys
    _, live, peer = clones
    incoming(peer)
    original_inode = (live / "Note.md").stat().st_ino
    code = """
import os, signal, sys
from pathlib import Path
from src.services.vault_reconcile import Reconciler
root, target = sys.argv[1:]
def stop(label, journal):
    if label == target:
        os.kill(os.getpid(), signal.SIGKILL)
Reconciler(Path(root), desktop=True, observer=stop).run()
"""
    from src.services import vault_reconcile as module
    source_root = Path(module.__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, "-c", code, str(live), boundary],
                            capture_output=True, cwd=source_root,
                            env={**os.environ, "PYTHONPATH": str(source_root)})
    assert result.returncode == -signal.SIGKILL
    engine = Reconciler(live, desktop=True)
    state = engine.status()
    assert state["review_required"]
    tx = state["transactions"][0]["id"]
    with pytest.raises(ReconcileError, match="recovery review"):
        engine.run()
    engine.continue_recovery(tx, editors_quiesced=True)
    assert (live / "Note.md").read_text() == "incoming\n"
    assert git(live, "show", "HEAD:Note.md") == "incoming"
    assert git(live, "diff", "--cached", "--name-only") == ""
    retained = versions(engine)[0]
    assert Path(retained["location"]).stat().st_ino == original_inode
    assert Path(retained["location"]).read_text() == "baseline\n"


def test_failure_after_native_exchange_retains_ambiguous_checkpoint(clones):
    _, live, peer = clones
    incoming(peer)
    def failed_fsync(label, _):
        if label == "after_syscall":
            raise OSError("simulated publication fsync failure")
    engine = Reconciler(live, desktop=True, observer=failed_fsync)
    with pytest.raises(OSError, match="fsync failure"):
        engine.run()
    assert (live / "Note.md").read_text() == "incoming\n"
    engine = Reconciler(live, desktop=True)
    tx = engine.status()["transactions"][0]["id"]
    engine.continue_recovery(tx, editors_quiesced=True)
    assert (live / "Note.md").read_text() == "incoming\n"
    assert Path(versions(engine)[0]["location"]).read_text() == "baseline\n"


def test_unsupported_exchange_never_falls_back_to_overwrite(clones, monkeypatch):
    import errno
    import src.services.vault_reconcile as module
    _, live, peer = clones
    incoming(peer)
    def unsupported(*_, **__):
        raise OSError(errno.ENOTSUP, "exchange unsupported by filesystem")
    monkeypatch.setattr(module, "rename", unsupported)
    with pytest.raises(OSError):
        Reconciler(live, desktop=True).run()
    assert (live / "Note.md").read_text() == "baseline\n"
    assert git(live, "show", "HEAD:Note.md") == "baseline"
    assert Reconciler(live).status()["review_required"]


def test_addition_racing_editor_save_never_clobbers_it(clones):
    _, live, peer = clones
    incoming(peer, "Added.md")
    def save(label, _):
        if label == "before_publication":
            (live / "Added.md").write_text("editor addition\n")
    with pytest.raises(FileExistsError):
        Reconciler(live, desktop=True, observer=save).run()
    assert (live / "Added.md").read_text() == "editor addition\n"
    assert Reconciler(live).status()["review_required"]


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o660, 0o664])
def test_replacement_preserves_restrictive_and_shared_modes(clones, mode):
    _, live, peer = clones
    (live / "Note.md").chmod(mode)
    previous = (live / "Note.md").stat()
    incoming(peer)
    Reconciler(live, desktop=True).run()
    current = (live / "Note.md").stat()
    assert current.st_mode & 0o777 == mode
    assert (current.st_uid, current.st_gid) == (previous.st_uid, previous.st_gid)


def test_new_directories_and_notes_remain_shared_writable(clones):
    _, live, peer = clones
    live.chmod(0o2770)
    (peer / "daily").mkdir()
    incoming(peer, "daily/Today.md")
    Reconciler(live, desktop=True).run()
    assert (live / "daily").stat().st_mode & 0o777 == 0o770
    assert (live / "daily/Today.md").stat().st_mode & 0o777 == 0o660
    (live / "daily/Today.md").write_text("existing daily note edit after sync\n")
    assert (live / "daily/Today.md").read_text() == "existing daily note edit after sync\n"


def test_shared_git_executable_bit_is_deliberate(clones):
    _, live, peer = clones
    (live / "Note.md").chmod(0o660)
    (peer / "Note.md").chmod(0o755)
    git(peer, "add", "-A")
    git(peer, "commit", "-qm", "executable")
    git(peer, "push", "-q")
    Reconciler(live, desktop=True).run()
    assert (live / "Note.md").stat().st_mode & 0o777 == 0o770


def test_root_sweep_preserves_different_service_uid_and_additions(clones):
    if os.geteuid() != 0:
        pytest.skip("requires isolated root container for actual different-uid ownership")
    _, live, peer = clones
    os.chown(live, 1234, 1234)
    os.chown(live / "Note.md", 1234, 1234)
    (live / "Note.md").chmod(0o660)
    incoming(peer)
    (peer / "daily").mkdir()
    incoming(peer, "daily/Today.md")
    Reconciler(live, desktop=True).run()
    for path in (live / "Note.md", live / "daily", live / "daily/Today.md"):
        assert path.stat().st_uid == 1234
        assert path.stat().st_gid == 1234


def test_resolution_retains_both_versions_and_reopens_on_late_save(clones):
    _, live, peer = clones
    incoming(peer)
    fd = os.open(live / "Note.md", os.O_RDWR)
    engine = Reconciler(live, desktop=True)
    try:
        engine.run()
        os.ftruncate(fd, 0)
        os.write(fd, b"recovered editor version\n")
        version = versions(engine)[0]
        tx = engine.status()["transactions"][0]["id"]
        with pytest.raises(ReconcileError, match="close editors"):
            engine.resolve(tx, "Note.md", keep="recovered", expected_sha256=version["sha256"])
        engine.resolve(tx, "Note.md", keep="recovered", expected_sha256=version["sha256"], editors_quiesced=True)
        assert (live / "Note.md").read_text() == "recovered editor version\n"
        assert not engine.status()["review_required"]
        assert any(Path(item["location"]).read_text() == "incoming\n" for item in versions(engine))
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, b"descriptor still saved later\n")
        assert engine.status()["review_required"]
    finally:
        os.close(fd)


def test_failed_commit_restores_same_path_staged_version(clones):
    _, live, _ = clones
    (live / "Note.md").write_text("staged version\n")
    git(live, "add", "Note.md")
    (live / "Note.md").write_text("newer unstaged editor version\n")
    hook = live / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(ReconcileError, match="index restored"):
        Reconciler(live, desktop=True).run()
    assert git(live, "show", ":Note.md") == "staged version"
    assert (live / "Note.md").read_text() == "newer unstaged editor version\n"
    refs = git(live, "for-each-ref", "--format=%(refname)", "refs/obsidian-recovery").splitlines()
    assert any(git(live, "show", ref + ":Note.md") == "staged version" for ref in refs)


def test_conflict_candidate_resolution_never_rebases_live_vault(clones):
    _, live, peer = clones
    incoming(peer)
    (live / "Note.md").write_text("local conflict\n")
    engine = Reconciler(live, desktop=True)
    with pytest.raises(ReconcileError):
        engine.run()
    state = engine.status()["transactions"][0]
    candidate = Path(state["merge_worktree"])
    (candidate / "Note.md").write_text("both versions reviewed\n")
    git(candidate, "add", "Note.md")
    git(candidate, "commit", "-qm", "explicit merge resolution")
    engine.continue_recovery(state["id"], editors_quiesced=True)
    assert (live / "Note.md").read_text() == "both versions reviewed\n"
    assert Path(versions(engine)[0]["location"]).read_text() == "local conflict\n"
    assert not engine.status()["review_required"]



def test_large_attachment_streams_without_new_limit(clones):
    _, live, peer = clones
    payload = b"synthetic attachment bytes" * 700000
    (peer / "Attachment.bin").write_bytes(payload)
    git(peer, "add", "Attachment.bin")
    git(peer, "commit", "-qm", "attachment")
    git(peer, "push", "-q")
    Reconciler(live, desktop=True).run()
    assert (live / "Attachment.bin").read_bytes() == payload


def test_export_fsyncs_file_and_destination_directory(clones, tmp_path, monkeypatch):
    import src.services.vault_reconcile as module
    _, live, peer = clones
    incoming(peer)
    engine = Reconciler(live, desktop=True)
    engine.run()
    tx = engine.status()["transactions"][0]["id"]
    called = []
    actual = module.fsync_directory
    def observe(path):
        actual(path)
        called.append(path)
    monkeypatch.setattr(module, "fsync_directory", observe)
    destination = tmp_path / "exported.md"
    engine.export(tx, "Note.md", destination)
    assert destination.read_text() == "baseline\n"
    assert destination.parent in called


def test_desktop_bundle_contains_working_standard_library_engine(clones, tmp_path):
    _, live, peer = clones
    incoming(peer)
    from src.services import vault_reconcile as module
    root = Path(module.__file__).resolve().parents[2]
    bundle = tmp_path / "sync-bundle"
    subprocess.run(["bash", str(root / "deploy/desktop/install.sh"), str(bundle)], check=True)
    result = subprocess.run(["bash", str(bundle / "deploy/desktop/obsidian-vault-reconcile.sh")],
        capture_output=True, env={**os.environ, "VAULT_PATH": str(live), "LOG_PATH": str(tmp_path / "sync.log")})
    assert result.returncode == 0, (tmp_path / "sync.log").read_text()
    assert (live / "Note.md").read_text() == "incoming\n"



def test_git_crlf_materialization_is_preserved_without_false_recovery(clones):
    _, live, peer = clones
    (peer / ".gitattributes").write_text("*.md text eol=crlf\n")
    incoming(peer)
    engine = Reconciler(live, desktop=True)
    engine.run()
    assert (live / "Note.md").read_bytes() == b"incoming\r\n"
    (peer / "Note.md").write_bytes(b"next incoming\r\n")
    git(peer, "add", "-A")
    git(peer, "commit", "-qm", "CRLF incoming edit")
    git(peer, "push", "-q")
    engine.run()
    assert (live / "Note.md").read_bytes() == b"next incoming\r\n"
    assert not engine.status()["review_required"]


def test_real_clean_smudge_filter_preserves_materialized_bytes(clones):
    _, live, peer = clones
    for root in (live, peer):
        git(root, "config", "filter.synthetic.clean", "sed 's/^WORKING:/STORED:/'")
        git(root, "config", "filter.synthetic.smudge", "sed 's/^STORED:/WORKING:/'")
        git(root, "config", "filter.synthetic.required", "true")
    (peer / ".gitattributes").write_text("*.md filter=synthetic\n")
    incoming(peer, value="WORKING:incoming\n")
    assert git(peer, "show", "HEAD:Note.md") == "STORED:incoming"
    engine = Reconciler(live, desktop=True)
    engine.run()
    assert (live / "Note.md").read_text() == "WORKING:incoming\n"
    assert git(live, "status", "--porcelain") == ""
    incoming(peer, value="WORKING:next incoming\n")
    engine.run()
    assert (live / "Note.md").read_text() == "WORKING:next incoming\n"
    assert not engine.status()["review_required"]
    assert git(live, "status", "--porcelain") == ""


def test_replacement_does_not_copy_file_setid_bits(clones):
    _, live, peer = clones
    (live / "Note.md").chmod(0o4660)
    incoming(peer)
    Reconciler(live, desktop=True).run()
    assert (live / "Note.md").stat().st_mode & 0o7777 == 0o660



def test_real_git_invocations_enable_reference_and_object_durability(clones):
    _, live, _ = clones
    engine = Reconciler(live, desktop=True)
    configured = engine.git("config", "--get", "core.fsync").decode().strip().split(",")
    assert {"committed", "index", "reference"} <= set(configured)
    assert engine.git("config", "--get", "core.fsyncMethod").decode().strip() == "fsync"
