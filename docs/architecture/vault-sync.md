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

A transaction that merely stopped part-way is dealt with by the next sweep
without a human, because it is the engine's own work left half-done rather than
a judgement call. A retained version holding an unacknowledged editor save, or
an ambiguous operation, keeps one out of this entirely. Otherwise three rules,
in order, each provable from the journal:

- **Nothing was published** (no operation published, no version retained): the
  journal is marked `abandoned`. `preparing` is this state by construction — it
  reaches disk at one place, with `operations` empty, because `plan` writes the
  journal only once it is already `publishing`.
- **HEAD is neither `original` nor `merged`**: the plan was computed against a
  tree the vault no longer has, so it is `abandoned` too. Publishing it would
  put stale bytes over newer notes.
- **Otherwise** the publication continues, which is what `recovery-continue`
  performs, through the same `publish_and_finalize` and the same refusals; only
  the audience differs.

Abandonment deletes nothing: journal, every retained version and both pinned
heads stay, and the same sweep re-derives from the vault as it is — `git add -A`
records whatever was already published, the incoming commit is fetched again and
the merge covers the remainder, so a half-published import converges instead of
jamming. Two costs are accepted rather than fixed. The already-published subset
lands as that sweep's own commit instead of HEAD advancing to the import's
merge, so `git blame` names the sweep for those notes rather than whoever pushed
them — resumption keeps the original authorship, abandonment cannot. And an
ignored-but-tracked path is published but unrecordable (`git add -A` will not
stage it), which is why `plan` treats an untracked incumbent whose bytes already
equal the incoming blob as published rather than as a collision: without that,
each sweep abandoned and opened another transaction, about 720 a day, for ever.
Different bytes still refuse. The only thing it can lose sight of is an `incoming` the remote no
longer has (a force-push, a pruned branch); the commit survives as
`refs/obsidian-recovery/<id>/incoming`, and every sweep that heals anything
names the transaction and the reason in its result, so neither is silent. A
`recovery-continue` on an abandoned transaction is refused: re-planning it would
checkpoint `publishing` against the base the vault has left, and the publication
guard would then refuse it forever — one wrong command, one permanent jam.

Publication proves HEAD is `original` or `merged` **before** it touches a note
as well as after the loop. Without that, resuming a transaction whose base the
vault had left published its whole stale diff over newer committed bytes and
only then refused: the destructive write this engine exists to prevent.

A merge conflict and a recovered local edit remain explicit, quiesced
operations.

This exists because the opposite was worse, three times over. One 896-file
import whose publication was killed by `TimeoutStartSec` stopped every later
sweep — and, through the same gate in `vault_sync.committing`, every agent
commit — for two hours. A desktop vault sat on a void `preparing` transaction
for 43 hours and 510 sweeps, with one notification at the start of it. And a
half-published import plus one ordinary `git add -A` in the vault — a human's,
or an Obsidian Git "commit all" — was a dead end that no command could clear.

Publication is bounded by its own budget (`VAULT_RECONCILE_PUBLISH_BUDGET`,
210 s) rather than by the unit's `TimeoutStartSec` (300 s), and `SIGTERM`/
`SIGINT` set a flag instead of ending the process. Both stop the loop between
two operations, where the journal is exact, and exit 76: not a failure, no
staleness marker, resumed by the next sweep. The candidate worktree is kept for
an unfinished publication, so a resume publishes from it instead of checking the
merge out again. An import larger than one sweep therefore converges in slices
with nobody watching, instead of being killed mid-note.

Latest displaced bytes are compared on status, subsequent sweeps and every MCP
commit. A recovered local edit or a merge conflict pauses automatic Git commits
and pushes; an interrupted transaction is finished or abandoned instead, as
above. Note writes remain available, without rolling back their
published bytes. Explicit resolution can keep the live version or republish a
recovered version; republishing journals and retains the incumbent again. A
later descriptor save changing an acknowledged hash reopens review. Recovery
export refuses an existing destination and fsyncs its file and parent directory.
No automatic deletion/pruning of recovery files or references is provided.

Replacing a file preserves its POSIX owner, group and restrictive/shared mode;
only Git's executable bit changes deliberately. New files/directories inherit
parent permissions and group. Group write is preserved across the umask by
`fchmod` on the created directory's own descriptor — the umask masks `mkdir`'s
mode argument and nothing else — so the engine never changes the process umask,
which is process-global and shared with the async server, and never forks. It
used to fork unconditionally: a `python3 -c "os.umask(0); os.mkdir(...)"` per
path component, at three call sites per operation, which profiled on the
production host as 149 s of a 154 s 400-note import (96%) and is how an import
outran its unit timeout. Publication is a per-note loop; nothing in it may start
a process other than Git, and `test_publication_creates_directories_without_forking_a_process_per_note`
asserts that. **A set-ID parent is the exception and still uses that child.**
The kernel puts the parent's setgid bit on the new directory, and
`RestrictSUIDSGID=yes` denies this process any `fchmod` carrying set-ID bits,
while one without them would clear what the kernel just set — so when a set-ID
parent meets a umask that masks bits the directory needs, the mode can only be
got right at creation, under a umask of 0 only a child can have. The umask is
read from `/proc/self/status`, never by calling `os.umask`. File set-ID bits are
never copied; directory setgid inheritance comes from the kernel, without an
explicit set-ID chmod — `test_a_setid_parent_never_gets_an_explicit_setid_chmod`
asserts there is no chmod there at all. A root
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
recoverable. Each operation rewrites the whole journal twice, so a single
transaction's checkpointing is quadratic in its own size: tolerable at the
thousands of notes an import realistically carries (≈ 9 ms per note measured),
and the reason the budget and the automatic resume above exist rather than a
larger timeout. Native primitives must work on the actual mounted filesystem; an
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
output — both texts are fixed literals. A private digest marker inside `.git`
suppresses repeated alerts for the same recovery state; changed retained bytes
trigger a new alert.

**A state that does not change alerts again anyway, once an hour.** The marker's
own mtime is when the sweep last said anything, and
`VAULT_RECONCILE_RENOTIFY_MINUTES` (60; `0` restores one alert per state and
never again) is how long it may stay quiet. The repeat says the vault still has
not synchronised, and a successful sync clears the marker as before. Suppressing
by state alone was not enough: a desktop vault stopped syncing on 2026-11-24 at
03:21, said so once — at twenty past three in the morning — and then nothing for
43 hours and 510 refused sweeps. The server side has the opposite shape, a
Telegram alert on every failed run plus a staleness timer, which is why the same
outage was noticed there in minutes and here not at all. Successful sync
clears the notification marker, without deleting recovery versions. A busy writer
lock stays a silent retry. Failed notification delivery leaves the next sweep
eligible to retry; the original engine exit status is preserved. Notification
availability follows macOS user permissions, so use `status` when diagnosing sync.

## Named connection authors

Every vault-mutating MCP tool, including upload-capability minting, requires a
validated connection author before its body can run, even when Git support is
disabled. Missing or invalid names produce `connection_name_required`; reads
remain available. No unnamed request acquires a fallback author. Names are
NFC-normalized, stripped, 1–128 characters, and reject Unicode Cc/Cf and angle
brackets. OAuth uses the grant's chosen alias; API keys use their name.

The Git AUTHOR is the connection name plus a stable synthetic email formed
from SHA-256 of credential kind, a NUL separator and the stable grant/key ID
(first 32 hex characters), under `connections.obsidian-mcp.invalid`. Renaming
a connection leaves its email unchanged. The server's configured identity
remains COMMITTER. Both travel only in each subprocess environment; no global
or repository Git configuration changes. Request ContextVars and explicit
author snapshots isolate concurrent clients.

Uploads store the minter's author name/email on the capability. Redemption
refuses legacy unnamed capabilities before reading bytes, and commits the
published path under that snapshot while holding the existing vault sync
lock, including confirmed publication followed by a completion-record failure.
A request cannot override the snapshot with a header or request context. Git
failure never rolls back published bytes or changes a successful upload reply.

## Working-clone backups

`scripts/vault_backup.py` creates an exclusive, private tar outside the vault
while holding the same kernel lock as supported writers. It retains the whole
working tree and `.git`, including uncommitted files and displaced editor inodes,
then reads every archived file and emits a SHA-256. It never overwrites a backup
or deletes recovery data. Independent editors must be closed by the operator.
A deployment snapshot additionally stops the application and restores its full
PostgreSQL dump into an isolated instance before running any migration.
