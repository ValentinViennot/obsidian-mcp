# Vault synchronization and saved editor versions

Both reconciliation entry points call the same standard-library Python engine.
It fetches the branch's configured remote, commits out-of-band local edits, and
merges histories in a detached worktree inside private Git recovery storage.
No checkout, rebase, reset or abort runs against the live vault. Merge conflicts
retain both commit histories and leave live notes unchanged. The candidate
worktree remains available for explicit resolution.

The kernel lock coordinates MCP publication/commit, uploads and supported Git
writers. Ordinary Obsidian saves do not acquire that lock. Existing files are
published with Linux `renameat2(RENAME_EXCHANGE)` or macOS
`renameatx_np(RENAME_SWAP)`: the recovery name retains the exact displaced
inode. An already-open editor descriptor can save to that inode later. Retain
all displaced inodes, even when their initial bytes match the original commit;
copying a file beforehand or deleting a seemingly unchanged recovery version
would lose that protection. Additions use native no-replace publication;
deletions move the incumbent inode into recovery. No unsafe fallback exists.

The transaction journal is fsynced before publication, together with its
containing directory ancestry. It records original/incoming/merged commits,
paths, baseline hashes and incumbent/staged inode identities. Git object and
reference writes use per-invocation `core.fsync=committed,index,reference` and
`core.fsyncMethod=fsync`; recovery reference directories are fsynced too. A
crash between exchange and the next journal checkpoint is classified by inode
identity, never by an unchanged hash or a `published` flag. Ambiguous names are
not blindly replayed. Explicit recovery requires editors and other Git writers
to be quiesced. HEAD/index finalization changes metadata only.

Latest displaced bytes are compared on status, subsequent sweeps and every MCP
commit. A recovered local edit or incomplete transaction pauses automatic Git
commits and pushes. Note writes remain available, without rolling back their
published bytes. Explicit resolution can keep the live version or republish a
recovered version; republishing journals and retains the incumbent again. A
later descriptor save changing an acknowledged hash reopens review. Recovery
export refuses an existing destination and fsyncs its file and parent directory.
No automatic deletion/pruning of recovery files or references is provided.

Replacing a file preserves its POSIX owner, group and restrictive/shared mode;
only Git's executable bit changes deliberately. New files/directories inherit
parent permissions and group, with group write preserved across umask through a short child-process mkdir.
Only that child changes its umask. File set-ID bits are never copied; directory
setgid inheritance comes from the kernel, without an explicit set-ID chmod. A root
sweep inherits the service owner's UID for additions; prefer running the whole
sweep as the service owner. Unavailable ownership/permission changes stop before
publication. These guarantees concern POSIX modes, not preservation of extended
ACLs or platform-specific metadata. Use an appropriate editor backup for those.

The explicit `reference` component is required: Git's `committed` aggregate
includes objects, not reference updates. See [Git configuration](https://git-scm.com/docs/git-config#Documentation/git-config.txt-corefsync).

A busy sync lock returns a retryable upload503 with `Retry-After` only after
the committed capability claim is restored. The same capability can then be
retried; a failed release receives the normal unavailable-capability response.
No payload bytes are read or published while waiting for the lock.

A pre-existing staged version is pinned as a private Git snapshot before the
sweep stages current disk bytes. Failed commits restore the original index
tree, including an older staged version of the same note. Commit-on-write also
restores its initial index on failure. Inspect `refs/obsidian-recovery/*/staged`
when retrieving a pre-sync staging snapshot.

The supported guarantee is filesystem-published local edits remaining visible
or durably recoverable. It excludes unsaved application buffers, editor-caused
destructive overwrites, disk failure, and independent destructive Git commands.
Publication across multiple notes is not atomic; the journal makes interruption
recoverable. Native primitives must work on the actual mounted filesystem; an
unsupported mount leaves local bytes intact and raises recovery review.
Recovery storage grows until an operator performs a separately planned backup
and maintenance operation with editors closed. It is private, outside the
vault index, and must be included in vault filesystem backups.

The primary regression suite uses actual bare remotes, actual native macOS and
Linux syscalls, and independent in-place/atomic-save/open-descriptor writes.
The application suite remains Linux-only because its vault containment uses
`openat2`; desktop engine qualification additionally runs natively on macOS.

### Desktop recovery alerts

The desktop sweep shows a generic macOS notification when recovery review is
required. Note names, saved content and vault paths stay in private status/log
output. A private digest marker inside `.git` suppresses repeated alerts for the
same recovery state; changed retained bytes trigger a new alert. Successful sync
clears the notification marker, without deleting recovery versions. A busy writer
lock stays a silent retry. Failed notification delivery leaves the next sweep
eligible to retry; the original engine exit status is preserved. Notification
availability follows macOS user permissions, so use `status` when diagnosing sync.
