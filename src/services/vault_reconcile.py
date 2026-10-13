"""Merge away from the live vault and retain exact displaced editor inodes.

Only supported git writers share the kernel lock. Editors need no lock: their
published bytes stay visible or remain on retained inodes in private recovery
storage. Recovery refs/journals are intentionally never automatically pruned.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import tempfile
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from src.services.vault_exchange import rename


class ReconcileError(RuntimeError):
    def __init__(self, message: str, code: int = 3):
        super().__init__(message)
        self.code = code


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def inode_at(fd: int, name: str):
    try:
        value = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(value.st_mode):
            raise ReconcileError("publication/recovery name is not a regular file")
        return [value.st_dev, value.st_ino]
    except FileNotFoundError:
        return None


def durable_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        if os.geteuid() == 0:
            owner = path.parent.stat()
            os.fchown(fd, owner.st_uid, owner.st_gid)
        payload = json.dumps(value, ensure_ascii=True, indent=2).encode()
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def make_directory(fd: int, name: str, mode: int):
    """Use a child umask so shared bits survive without explicit set-ID chmod.

    The engine does not change the caller's process umask; the kernel can
    inherit a parent directory's setgid bit under RestrictSUIDSGID=yes.
    """
    code = "import os,sys; os.umask(0); os.mkdir(sys.argv[2], int(sys.argv[3]), dir_fd=int(sys.argv[1]))"
    result = subprocess.run([sys.executable, "-c", code, str(fd), name, str(mode & 0o777)],
                            pass_fds=(fd,), capture_output=True, timeout=15)
    if result.returncode:
        if inode_directory_exists(fd, name):
            raise FileExistsError(name)
        raise ReconcileError("could not create shared note directory; nothing published")


def inode_directory_exists(fd, name):
    try:
        return stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode)
    except FileNotFoundError:
        return False


@contextmanager
def parent_fd(root: Path, relative: str, *, create: bool = False):
    parts = relative.split("/")
    if any(part in ("", ".", "..", ".git") for part in parts):
        raise ReconcileError("unsafe publication path")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    make_directory(fd, part, stat.S_IMODE(os.fstat(fd).st_mode))
                    created = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        parent = os.fstat(fd)
                        os.fchown(created, parent.st_uid if os.geteuid() == 0 else -1, parent.st_gid)
                        os.fsync(created)
                    finally:
                        os.close(created)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        yield fd, parts[-1]
    finally:
        os.close(fd)


def retained_digest(path: Path) -> str:
    """Read the latest inode bytes, never follow a replacement symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ReconcileError("recovery version is not a regular file")
        digest = hashlib.sha256()
        while data := os.read(fd, 1024 * 1024):
            digest.update(data)
        return digest.hexdigest()
    finally:
        os.close(fd)


class Reconciler:
    def __init__(self, root: Path, *, desktop: bool = False, observer=None):
        self.root = root.resolve()
        self.git_dir = self.root / ".git"
        if not self.git_dir.is_dir() or self.git_dir.is_symlink():
            raise ReconcileError("vault must be a regular working clone", 2)
        self.storage = self.git_dir / "obsidian-recovery"
        self.desktop = desktop
        self.observer = observer or (lambda *_: None)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="", SSH_ASKPASS="", GIT_EDITOR="true", LC_ALL="C")
        if not desktop:
            name = os.environ.get("GIT_AGENT_NAME", "obsidian-mcp agent")
            email = os.environ.get("GIT_AGENT_EMAIL", "agent@obsidian-mcp.invalid")
            self.env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
                            GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
        self.timeout = float(os.environ.get("VAULT_RECONCILE_NET_TIMEOUT", "120"))

    def git(self, *args, root=None, check=True, optional=False) -> bytes:
        command = self.command(args, root)
        process = subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        try:
            output, _ = process.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise ReconcileError("git command timed out; local versions retained") from None
        if optional and process.returncode == 1:
            return b""
        if (check or optional) and process.returncode:
            # Provider output may contain credentials or note contents.
            raise ReconcileError(f"git {args[0]} failed; inspect the local repository")
        return output if not process.returncode else b""

    def command(self, args, root=None):
        return ["git", "-c", f"safe.directory={self.root}",
                "-c", "core.fsync=committed,index,reference", "-c", "core.fsyncMethod=fsync",
                "-C", str(root or self.root), *args]

    def blob_to(self, oid: str, fd: int, *, path: str | None = None):
        """Git streams attachments to disk: no size cap or whole-blob allocation."""
        args = ["cat-file", "blob", oid] if path is None else ["cat-file", "--filters", f"--path={path}", oid]
        process = subprocess.Popen(self.command(args), env=self.env,
                                   stdout=fd, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            if process.wait(timeout=self.timeout):
                raise ReconcileError("could not materialize a pinned blob")
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise ReconcileError("blob read timed out") from None

    def blob_digest(self, oid, path=None):
        if oid is None:
            return hashlib.sha256(b"").hexdigest()
        with tempfile.TemporaryFile() as stream:
            self.blob_to(oid, stream.fileno(), path=path)
            stream.seek(0)
            digest = hashlib.sha256()
            while data := stream.read(1024 * 1024):
                digest.update(data)
            return digest.hexdigest()

    def pin(self, transaction, kind, commit):
        ref = f"refs/obsidian-recovery/{transaction}/{kind}"
        self.git("update-ref", ref, commit)
        directory = self.git_dir / Path(ref).parent
        while directory != self.git_dir.parent:
            fsync_directory(directory)
            directory = directory.parent

    def new_directory(self, transaction):
        self.storage.mkdir(mode=0o700, exist_ok=True)
        if self.storage.is_symlink():
            raise ReconcileError("recovery directory must not be a symlink")
        owner = self.git_dir.stat()
        if os.geteuid() == 0:
            os.chown(self.storage, owner.st_uid, owner.st_gid)
        os.chmod(self.storage, 0o700)
        directory = self.storage / transaction
        directory.mkdir(mode=0o700)
        if os.geteuid() == 0:
            os.chown(directory, owner.st_uid, owner.st_gid)
        # The journal's directory entry must survive along with its content.
        for path in (directory, self.storage, self.git_dir, self.root):
            fsync_directory(path)
        return directory

    @contextmanager
    def locked(self):
        fd = os.open(self.git_dir / "obsidian-sync.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.geteuid() == 0:
                owner = self.git_dir.stat()
                os.fchown(fd, owner.st_uid, owner.st_gid)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def journals(self):
        if self.storage.exists():
            for path in sorted(self.storage.glob("*/journal.json")):
                yield path, json.loads(path.read_text())

    def status(self) -> dict:
        transactions = []
        pending = False
        for path, journal in self.journals():
            versions = []
            for op in journal["operations"]:
                retained = path.parent / op["retained"]
                if retained.exists():
                    digest = retained_digest(retained)
                    changed = digest != op["baseline_sha256"] if self.operation_state(path.parent, op) == "applied" else False
                    acknowledged = op.get("acknowledged_sha256") == digest
                    pending |= changed and not acknowledged
                    versions.append({"path": op["path"], "location": str(retained),
                                     "sha256": digest, "local_edit_recovered": changed,
                                     "resolved": acknowledged, "position": self.operation_state(path.parent, op)})
            incomplete = journal["state"] not in ("complete", "acknowledged")
            pending |= incomplete
            transactions.append({"id": journal["id"], "date": journal["date"],
                                 "incoming_commit": journal["incoming"], "state": journal["state"],
                                 "versions": versions, "merge_worktree": str(path.parent / "merge"),
                                 "staged_snapshot": journal.get("staged_snapshot")})
        return {"review_required": pending, "transactions": transactions,
                "notice": "Displaced inodes remain retained: an editor descriptor can save there later. "
                          "Use recovery export before resolution; no automatic cleanup."}

    def tree(self, commit):
        result = {}
        for record in self.git("ls-tree", "-r", "-z", commit).split(b"\0"):
            if record:
                metadata, path = record.split(b"\t", 1)
                mode, kind, oid = metadata.decode().split()
                result[os.fsdecode(path)] = (mode, kind, oid)
        return result

    def checkpoint(self, label, journal):
        durable_json(self.storage / journal["id"] / "journal.json", journal)
        self.observer(label, journal)

    def run(self):
        with self.locked():
            return self._run()

    def _run(self):
        if self.status()["review_required"]:
            raise ReconcileError("recovery review required; run status and recovery export before syncing")
        if any((self.git_dir / name).exists() for name in
               ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")):
            raise ReconcileError("interrupted git operation; recover before retrying")
        branch = self.git("symbolic-ref", "--quiet", "--short", "HEAD", optional=True).decode().strip()
        if not branch:
            raise ReconcileError("HEAD is detached; refusing to sync", 2)
        if self.git("ls-files", "-u"):
            raise ReconcileError("unresolved index conflicts; recover before retrying")
        if self.git("rev-parse", "--show-toplevel").decode().strip() != str(self.root):
            raise ReconcileError("vault must be the repository root", 2)
        # Preserve pre-existing staging, including a different version of a note.
        staged = self.git("write-tree").decode().strip()
        staged_snapshot = None
        if self.git("diff", "--cached", "--name-only", "-z"):
            staged_snapshot = self.git("commit-tree", staged, "-p", "HEAD", "-m",
                                       "vault: retained pre-sync staging").decode().strip()
            self.pin(uuid.uuid4().hex, "staged", staged_snapshot)
        # Capture out-of-band edits without any worktree checkout. Hooks on the
        # desktop remain meaningful; a failed commit stops before the fetch.
        self.git("add", "-A")
        if self.git("diff", "--cached", "--name-only", "-z"):
            changed = len(self.git("diff", "--cached", "--name-only", "-z").rstrip(b"\0").split(b"\0"))
            message = "vault: desktop sync" if self.desktop else (
                f"vault(sweep): {changed} path(s) changed outside the MCP server\n\n"
                "Tool: reconcile-sweep\nPrincipal: out-of-band")
            options = [] if self.desktop else ["--no-verify", "--no-gpg-sign"]
            try:
                self.git("commit", "--quiet", *options, "-m", message)
            except ReconcileError:
                self.git("read-tree", staged)
                raise ReconcileError("could not commit local edits; index restored; nothing was fetched") from None
        original = self.git("rev-parse", "HEAD").decode().strip()
        remote = self.git("config", "--get", f"branch.{branch}.remote", optional=True).decode().strip()
        ref = self.git("config", "--get", f"branch.{branch}.merge", optional=True).decode().strip()
        if not remote:
            return {"state": "local_only"}
        if remote == "." or not ref.startswith("refs/heads/"):
            raise ReconcileError("configure a remote branch upstream before syncing", 2)
        self.git("fetch", "--quiet", "--", remote, ref)
        incoming = self.git("rev-parse", "FETCH_HEAD").decode().strip()
        if original == incoming:
            if self.git("status", "--porcelain"):
                raise ReconcileError("new local edit remains visible; retry before pushing")
            self.git("push", "--quiet", "--", remote, f"HEAD:{ref}")
            return {"state": "complete"}
        transaction = uuid.uuid4().hex
        directory = self.new_directory(transaction)
        scratch = directory / "merge"
        journal = {"id": transaction, "date": datetime.now(timezone.utc).isoformat(),
                   "original": original, "incoming": incoming, "merged": None,
                   "state": "preparing", "operations": [], "staged_snapshot": staged_snapshot, "branch_ref": f"refs/heads/{branch}"}
        self.pin(transaction, "local", original)
        self.pin(transaction, "incoming", incoming)
        self.checkpoint("prepared", journal)
        # All checkout and merge machinery operates here, outside the live vault.
        self.git("-c", "core.hooksPath=/dev/null", "worktree", "add", "--quiet", "--detach", str(scratch), original)
        try:
            try:
                self.git("-c", "core.hooksPath=/dev/null", "merge", "--no-edit", "--no-gpg-sign", incoming, root=scratch)
            except ReconcileError:
                if not self.git("ls-files", "-u", root=scratch):
                    raise ReconcileError("candidate merge failed without conflicts; inspect the private worktree") from None
                journal["state"] = "merge_conflict"
                self.checkpoint("merge_conflict", journal)
                raise ReconcileError("incoming conflict retained; live notes unchanged; recovery review required") from None
            merged = self.git("rev-parse", "HEAD", root=scratch).decode().strip()
            self.plan(journal, merged)
            self.publish_and_finalize(journal)
            if self.status()["review_required"]:
                raise ReconcileError("local edit recovered; incoming visible; review required before push")
            if self.git("status", "--porcelain"):
                raise ReconcileError("new local edit remains visible; retry before pushing")
            try:
                self.git("push", "--quiet", "--", remote, f"HEAD:{ref}")
            except ReconcileError:
                raise ReconcileError("push failed; both histories retained locally", 4) from None
            return self.status()
        finally:
            if journal["state"] != "merge_conflict":
                self.git("worktree", "remove", "--force", str(scratch), check=False)

    def plan(self, journal, merged):
        journal["merged"] = merged
        self.pin(journal["id"], "merged", merged)
        old_tree, new_tree = self.tree(journal["original"]), self.tree(merged)
        for path in sorted(old_tree.keys() | new_tree.keys()):
            old, new = old_tree.get(path), new_tree.get(path)
            if old == new:
                continue
            if any(item and item[0] not in ("100644", "100755") for item in (old, new)):
                raise ReconcileError("symlink/submodule transition needs manual review")
            with parent_fd(self.root, path, create=new is not None) as (fd, name):
                incumbent = inode_at(fd, name)
                if old is None and incumbent is not None:
                    raise ReconcileError("incoming addition collides with a local file; retained unchanged")
                if old is not None and incumbent is None:
                    raise ReconcileError("local deletion arrived during sync; retained unchanged")
            journal["operations"].append({"path": path, "old_blob": old[2] if old else None,
                "new_blob": new[2] if new else None, "mode": new[0] if new else None,
                "baseline_sha256": self.blob_digest(old[2] if old else None, path),
                "retained": f"version-{len(journal['operations'])}", "published": False})
        journal["state"] = "publishing"
        self.checkpoint("planned", journal)

    def stage(self, directory, op, recovery_fd):
        with parent_fd(self.root, op["path"], create=bool(op["new_blob"])) as (fd, name):
            incumbent = inode_at(fd, name)
            op["incumbent_inode"] = incumbent
            if incumbent is None and op["old_blob"]:
                raise ReconcileError("local deletion arrived; review required")
            if incumbent is not None and not op["old_blob"]:
                raise ReconcileError("incoming addition collides with a local save; review required")
            info = os.stat(name, dir_fd=fd, follow_symlinks=False) if incumbent else os.fstat(fd)
            op["stage_inode"] = None
            if op["new_blob"]:
                try:
                    # No recorded identities means this stage never reached a
                    # publication syscall. Retain its abandoned inode anyway.
                    os.stat(op["retained"], dir_fd=recovery_fd, follow_symlinks=False)
                    rename(recovery_fd, op["retained"], recovery_fd,
                           "abandoned-" + uuid.uuid4().hex, exchange=False)
                except FileNotFoundError:
                    pass
                output = os.open(op["retained"], os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                                 0o600, dir_fd=recovery_fd)
                try:
                    candidate = directory / "merge"
                    if candidate.is_dir():
                        # Publish exactly Git's materialized worktree bytes,
                        # including CRLF/smudge attributes, streaming attachments.
                        with parent_fd(candidate, op["path"]) as (source_parent, source_name):
                            source = os.open(source_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_parent)
                            try:
                                with os.fdopen(output, "wb", closefd=False) as stream:
                                    while data := os.read(source, 1024 * 1024):
                                        stream.write(data)
                                    stream.flush()
                            finally:
                                os.close(source)
                    else:
                        self.blob_to(op["new_blob"], output)
                    # Preserve restrictive and shared permission bits, owner and
                    # group. Only the tracked Git executable bit changes deliberately.
                    mode = stat.S_IMODE(info.st_mode) & 0o777 if incumbent else stat.S_IMODE(info.st_mode) & 0o666
                    mode = mode & ~0o111
                    if op["mode"] == "100755":
                        mode |= (mode & 0o444) >> 2
                    os.fchown(output, info.st_uid if incumbent or os.geteuid() == 0 else os.geteuid(), info.st_gid)
                    os.fchmod(output, mode)
                    os.fsync(output)
                    op["stage_inode"] = [os.fstat(output).st_dev, os.fstat(output).st_ino]
                finally:
                    os.close(output)
                os.fsync(recovery_fd)

    def operation_state(self, directory, op):
        if "incumbent_inode" not in op:
            return "unstaged"
        recovery_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            retained = inode_at(recovery_fd, op["retained"])
        finally:
            os.close(recovery_fd)
        old, staged = op["incumbent_inode"], op["stage_inode"]
        if old and retained == op.get("displaced_inode", old):
            return "applied"
        try:
            with parent_fd(self.root, op["path"]) as (fd, name):
                live = inode_at(fd, name)
        except OSError:
            return "ambiguous"
        if old and live == staged and retained is not None and retained != staged:
            return "applied"
        if old and live == old and retained == staged:
            return "not_applied"
        if not old and staged and live == staged and retained is None:
            return "applied"
        if not old and staged and live is None and retained == staged:
            return "not_applied"
        return "ambiguous"

    def publish_and_finalize(self, journal):
        directory = self.storage / journal["id"]
        if not journal.get("resolution") and not (directory / "merge").exists() and any(
                "incumbent_inode" not in op and op["new_blob"] for op in journal["operations"]):
            self.git("-c", "core.hooksPath=/dev/null", "worktree", "add", "--quiet", "--detach",
                     str(directory / "merge"), journal["merged"])
        recovery_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for op in journal["operations"]:
                freshly_staged = "incumbent_inode" not in op
                if freshly_staged:
                    self.stage(directory, op, recovery_fd)
                    # Inode identities persist BEFORE the syscall. A success
                    # followed by fsync failure/death cannot be replayed blindly.
                    self.checkpoint("before_publication", journal)
                position = self.operation_state(directory, op)
                if position == "ambiguous" and not freshly_staged:
                    raise ReconcileError("publication names changed unexpectedly; no operation replayed; inspect recovery")
                if position == "not_applied" or freshly_staged:
                    with parent_fd(self.root, op["path"], create=bool(op["new_blob"])) as (fd, name):
                        callback = lambda: self.observer("after_syscall", journal)
                        if op["new_blob"]:
                            rename(recovery_fd, op["retained"], fd, name,
                                   exchange=bool(op["old_blob"]), after=callback)
                        else:
                            rename(fd, name, recovery_fd, op["retained"], exchange=False, after=callback)
                elif position != "applied":
                    raise ReconcileError("publication staging incomplete; inspect recovery")
                op["displaced_inode"] = inode_at(recovery_fd, op["retained"]) if op["old_blob"] else None
                op["published"] = True
                self.checkpoint("after_publication", journal)
        finally:
            os.close(recovery_fd)
        journal["state"] = "finalizing"
        self.checkpoint("before_head", journal)
        if journal.get("branch_ref") and self.git("symbolic-ref", "HEAD").decode().strip() != journal["branch_ref"]:
            raise ReconcileError("branch changed during sync; explicit review required")
        current = self.git("rev-parse", "HEAD").decode().strip()
        if current == journal["original"]:
            self.git("update-ref", "HEAD", journal["merged"], journal["original"])
        elif current != journal["merged"]:
            raise ReconcileError("HEAD changed outside synchronization; explicit review required")
        self.observer("after_head", journal)
        if not journal.get("resolution"):
            self.git("read-tree", journal["merged"])  # index only: deliberately no -u
        journal["state"] = "complete"
        self.checkpoint("complete", journal)

    def transaction(self, transaction):
        for path, journal in self.journals():
            if journal["id"] == transaction:
                return path, journal
        raise ReconcileError("recovery transaction not found")

    def continue_recovery(self, transaction, *, editors_quiesced=False):
        if not editors_quiesced:
            raise ReconcileError("close editors and pause independent Git writers, then pass --editors-quiesced")
        with self.locked():
            path, journal = self.transaction(transaction)
            if journal["state"] in ("complete", "acknowledged"):
                return self.status()
            scratch = path.parent / "merge"
            if journal["merged"] is None and journal["state"] != "merge_conflict":
                if not scratch.exists():
                    self.git("-c", "core.hooksPath=/dev/null", "worktree", "add", "--quiet", "--detach", str(scratch), journal["original"])
                if self.git("ls-files", "-u", root=scratch):
                    journal["state"] = "merge_conflict"
                    self.checkpoint("merge_conflict", journal)
                else:
                    try:
                        self.git("-c", "core.hooksPath=/dev/null", "merge", "--no-edit", "--no-gpg-sign", journal["incoming"], root=scratch)
                    except ReconcileError:
                        if not self.git("ls-files", "-u", root=scratch):
                            raise
                        journal["state"] = "merge_conflict"
                        self.checkpoint("merge_conflict", journal)
                    if journal["state"] != "merge_conflict":
                        self.plan(journal, self.git("rev-parse", "HEAD", root=scratch).decode().strip())
            if journal["state"] == "merge_conflict":
                if self.git("ls-files", "-u", root=scratch):
                    raise ReconcileError("resolve and commit the candidate merge worktree shown by status first")
                candidate = self.git("rev-parse", "HEAD", root=scratch).decode().strip()
                for head in (journal["original"], journal["incoming"]):
                    # Genuine conflict versus process failure stays distinguishable.
                    self.git("merge-base", "--is-ancestor", head, candidate)
                self.plan(journal, candidate)
            if journal["merged"] is None:
                raise ReconcileError("preparation interrupted; no live publication; inspect pinned heads and candidate worktree")
            self.publish_and_finalize(journal)
            self.git("worktree", "remove", "--force", str(path.parent / "merge"), check=False)
            return self.status()

    def resolve(self, transaction, path, *, keep, expected_sha256, editors_quiesced=False):
        if not editors_quiesced:
            raise ReconcileError("close editors and pause independent Git writers, then pass --editors-quiesced")
        with self.locked():
            journal_path, journal = self.transaction(transaction)
            if journal["state"] not in ("complete", "acknowledged"):
                raise ReconcileError("continue interrupted recovery before resolving versions")
            op = next((item for item in journal["operations"] if item["path"] == path), None)
            if op is None or self.operation_state(journal_path.parent, op) != "applied":
                raise ReconcileError("displaced recovery version not found")
            retained = journal_path.parent / op["retained"]
            current_digest = retained_digest(retained)
            if current_digest != expected_sha256:
                raise ReconcileError("recovery bytes changed; inspect/export the latest version before resolution")
            if keep not in ("live", "recovered"):
                raise ReconcileError("choose live or recovered")
            if keep == "recovered":
                # A resolution is itself a journaled publication. It retains the
                # incumbent live inode; the original recovery inode stays too.
                object_id = self.git("hash-object", "--no-filters", "-w", "--", str(retained)).decode().strip()
                head = self.git("rev-parse", "HEAD").decode().strip()
                identity = uuid.uuid4().hex
                self.new_directory(identity)
                resolution = {"id": identity, "date": datetime.now(timezone.utc).isoformat(),
                    "original": head, "incoming": head, "merged": head, "state": "publishing", "operations": [],
                    "branch_ref": self.git("symbolic-ref", "HEAD").decode().strip(), "resolution": True}
                live_oid = self.tree(head).get(path)
                with parent_fd(self.root, path, create=True) as (fd, name):
                    live_inode = inode_at(fd, name)
                resolution["operations"].append({"path": path, "old_blob": live_oid[2] if live_inode and live_oid else (object_id if live_inode else None),
                    "new_blob": object_id, "mode": live_oid[0] if live_oid else "100644",
                    "baseline_sha256": self.blob_digest(live_oid[2], path) if live_oid else hashlib.sha256(b"").hexdigest(),
                    "retained": "version-0", "published": False})
                self.pin(identity, "chosen", head)
                # Pin selected blob through an explicit tree so GC cannot remove it.
                self.pin(identity, "blob", object_id)
                self.checkpoint("planned", resolution)
                self.publish_and_finalize(resolution)
                for version in resolution["operations"]:
                    location = self.storage / identity / version["retained"]
                    if location.exists():
                        version["acknowledged_sha256"] = retained_digest(location)
                self.checkpoint("resolved", resolution)
            op["acknowledged_sha256"] = current_digest
            self.checkpoint("resolved", journal)
            return self.status()

    def export(self, transaction: str, path: str, destination: Path):
        with self.locked():
            for journal_path, journal in self.journals():
                if journal["id"] == transaction:
                    for op in journal["operations"]:
                        if op["path"] == path:
                            source = journal_path.parent / op["retained"]
                            source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
                            try:
                                output = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                                with os.fdopen(output, "wb") as stream:
                                    while data := os.read(source_fd, 1024 * 1024):
                                        stream.write(data)
                                    stream.flush()
                                    os.fsync(stream.fileno())
                            finally:
                                os.close(source_fd)
                            fsync_directory(destination.parent)
                            return
            raise ReconcileError("recovery version not found")
