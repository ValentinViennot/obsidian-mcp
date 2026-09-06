"""Embed a git-backed vault's *history*, not just its present.

The ordinary embed pass reads a directory of files, so `note_embeddings` only
ever describes HEAD. Everything the author has since rewritten or deleted is
unfindable — a vault used as a single source of truth is at its least useful
exactly where a human's memory is worst ("I wrote this down somewhere and then
tidied it away"). Migration 025 gave each vector a validity interval; this
module is what fills those intervals in from the one place that actually
records when a note said what: the vault's git history.

## What a "version" is here

One **distinct content state** of one path, with the half-open interval over
which it was that path's content:

    valid_from = the authored date of the commit that introduced it
    valid_to   = the authored date of the commit that replaced or deleted it
                 (NULL when it is still the note's content)

`valid_to IS NULL` is what makes "current" queryable, and it is the same NULL
the ordinary embed pass writes — which is the join between the two writers and
is why the current version is *stamped* here rather than re-embedded (see
`plan_note_backfill`).

## Deduplication, and why it is not only ours

`git log` lists a path only in the commits that changed it, so a thousand
commits that did not touch a note produce one entry, not a thousand. What git
does not collapse is a change *back* to content the path already had — a
revert, a round-tripped formatter, an amend — and those would otherwise embed
the same bytes twice under two intervals. So the walk carries git's own blob
object id for each entry and drops an entry whose blob equals its predecessor's.
The blob id is the identity git itself uses for content, so this is exact: it
cannot merge two different texts and cannot miss two identical ones.

## `--first-parent`, and why the timeline is linear

History is a DAG and a validity interval is not; some linearisation has to be
chosen. `--first-parent` chooses the mainline: work merged from a side branch
enters the timeline at the merge, dated by the merge's authored date, rather
than being interleaved by side-branch dates that never described the mainline's
content. Any other choice produces intervals that overlap — two versions both
claiming to be the note's text at one instant — which is not a shape the schema
or a point-in-time query can represent.

`--no-renames` is deliberate for the same reason. A rename is a delete of one
path and an add of another, and the vault addresses notes by path: after a
rename the old path genuinely had no content, and following the content across
would claim it did.

## Authored dates, and what happens when they are not monotonic

`%aI` is the authored date, per this workstream's specification, and authored
dates are attacker- and rebase-controlled: they can run backwards along the
mainline. An interval whose end precedes its start is meaningless, and one
whose end *equals* its start is valid at no instant at all. Both are handled in
`plan_note_backfill` by clamping each version's start to its predecessor's,
then dropping any version whose interval collapses — a version that was
superseded within the same instant of authored time was never the note's text
at any queryable moment, and embedding it would only add a row no query can
return. Dropped versions are counted and reported, never silent.

## Resumability without a cursor

Nothing here records "where I got to". A version is skipped when the database
already holds rows for that `(note_id, valid_from, valid_to)`, which makes an
interrupted run resumable and a completed run a no-op, with no state that can
be stale, lost or wrong about work that was rolled back. A cursor would also
have needed a key in `indexer_state`, whose CHECK constraint pins the admitted
key set in migration 023 — so it would have cost a schema change to record
something the data already says.

## What this module does not do

- **A note that no longer exists at HEAD is skipped.** Its history is real and
  its versions are walked and counted, but a vector row hangs off
  `notes_metadata.id` and a deleted note has no such row; inventing one would
  put a deleted note into `list_notes`, `keyword_search` and every graph tool,
  which is a far worse regression than a missing slice of history. Reported as
  `notes_without_metadata` so the gap is a number an operator can see rather
  than a silence.
- **It never writes a current-version row.** Only the ordinary embed pass
  creates rows with `valid_to IS NULL`; this stamps `valid_from` onto the ones
  it already wrote. Two writers of the current slice would double every
  current vector and every search hit.
- **It is not incremental.** An ordinary edit after a backfill replaces the
  note's current vectors and clears the `valid_from` this stamped (the row is
  new); re-running the backfill re-stamps it. Making `embed_note` close the
  outgoing interval itself would turn every edit in the vault into permanent
  history growth, which is a product decision, not a mechanical one.
"""
from __future__ import annotations

import asyncio
import datetime
import fnmatch
import logging
import os
from dataclasses import dataclass, field, replace

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import MAX_CHUNKS_PER_NOTE, settings
from src.models.db import NoteEmbedding, NoteMetadata
from src.services.embeddings import (
    chunk_text_bounded,
    clean_for_embedding,
    get_embeddings_batch,
)
from src.services.filters import current_embedding_predicate

logger = logging.getLogger(__name__)

#: The executable. Named through a constant so the subprocess calls below read
#: as one dependency rather than eleven string literals, and so a deployment
#: that ships git somewhere unusual has one place to point at.
GIT = os.environ.get("OMCP_GIT_BINARY", "git")

#: Record separator inside the `--format` string, and the commit separator
#: prefixed to it. Both are control characters no commit hash or ISO-8601 date
#: can contain, which is what lets the `-z` stream be split without a parser.
_FIELD_SEP = "\x1f"
_COMMIT_SEP = "\x01"

#: The all-zero object id git prints for the missing side of an add or delete.
_NULL_BLOB = "0" * 40

#: How long any single git invocation may take. The walk is one process over
#: the whole history and a blob read is one process per version, so a hung git
#: must not hang the backfill for ever — but the bound is generous, because a
#: cold `git log --raw` over a decade of history on a cold page cache is slow
#: and killing it would make the tool unusable on exactly the repositories it
#: exists for.
GIT_TIMEOUT_SECONDS = 600.0


class GitError(RuntimeError):
    """A git invocation failed, or the vault is not inside a git repository."""


@dataclass(frozen=True)
class VersionRef:
    """One distinct content state of one path, and when it was that content.

    `blob` is git's own object id for the content — the identity used for
    deduplication — and is `None` only for the synthetic deletion entry, which
    exists to carry the instant a path stopped having content and never becomes
    a stored row.
    """

    path: str
    blob: str | None
    valid_from: datetime.datetime
    valid_to: datetime.datetime | None = None

    @property
    def is_current(self) -> bool:
        return self.valid_to is None


@dataclass
class BackfillStats:
    """What a run did, or (under `--dry-run`) would do.

    Every field is a count an operator can act on; in particular the two
    "skipped" counters exist so a run that does almost nothing can say *which*
    kind of nothing it did — already-backfilled, or unbackfillable.
    """

    notes_walked: int = 0
    versions_walked: int = 0
    #: Versions dropped because their interval collapsed — see the module
    #: docstring on non-monotonic authored dates.
    versions_dropped: int = 0
    #: Historical versions already present in the database from an earlier run.
    versions_already_stored: int = 0
    versions_embedded: int = 0
    chunks_embedded: int = 0
    #: Current versions whose existing rows got their `valid_from` stamped.
    current_versions_stamped: int = 0
    #: Paths with real history but no `notes_metadata` row — deleted notes,
    #: and notes the ordinary indexer has not reached yet.
    notes_without_metadata: int = 0
    #: Versions whose blob is not valid UTF-8.
    versions_unreadable: int = 0
    paths_excluded: int = 0

    def render(self) -> str:
        return (
            f"notes={self.notes_walked} versions={self.versions_walked} "
            f"embedded={self.versions_embedded} chunks={self.chunks_embedded} "
            f"stamped_current={self.current_versions_stamped} "
            f"already_stored={self.versions_already_stored} "
            f"dropped={self.versions_dropped} "
            f"no_metadata={self.notes_without_metadata} "
            f"unreadable={self.versions_unreadable} "
            f"excluded={self.paths_excluded}"
        )


# ── git ────────────────────────────────────────────────────────────────────


async def _git(repo_root: str, *args: str, binary: bool = False):
    """Run one git command in `repo_root` and return its stdout.

    `create_subprocess_exec`, never a shell: every argument here is a path or a
    revision that can come from a repository's own contents, and a shell would
    make a note called `$(…).md` an execution vector on the operator's machine.
    """
    proc = await asyncio.create_subprocess_exec(
        GIT,
        "-C",
        repo_root,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=GIT_TIMEOUT_SECONDS
        )
    except (asyncio.TimeoutError, TimeoutError):
        proc.kill()
        await proc.wait()
        raise GitError(
            f"`git {' '.join(args)}` did not finish within "
            f"{GIT_TIMEOUT_SECONDS:.0f}s in {repo_root}"
        )
    if proc.returncode != 0:
        raise GitError(
            f"`git {' '.join(args)}` failed in {repo_root} "
            f"(exit {proc.returncode}): {stderr.decode('utf-8', 'replace').strip()}"
        )
    return stdout if binary else stdout.decode("utf-8", "replace")


async def resolve_repo(vault_path: str) -> tuple[str, str]:
    """`(repo_root, prefix)` for a vault directory inside a git repository.

    `prefix` is the vault's path relative to the repository root, `""` when the
    vault *is* the root. It is what lets a vault that lives in a subdirectory of
    a larger repository be walked without dragging in the rest: the log is
    scoped to it, and paths come back vault-relative, which is the only form
    `notes_metadata.file_path` speaks.
    """
    root = (await _git(vault_path, "rev-parse", "--show-toplevel")).strip()
    if not root:
        raise GitError(f"{vault_path} is not inside a git repository")
    prefix = (await _git(vault_path, "rev-parse", "--show-prefix")).strip()
    return root, prefix


def _parse_authored_date(raw: str) -> datetime.datetime:
    """`%aI` is strict ISO-8601 with an offset; `Z` is the one spelling
    `fromisoformat` did not accept before 3.11 and git does emit it."""
    return datetime.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))


def parse_git_log(stream: str) -> list[tuple[datetime.datetime, str, str, str]]:
    """`(authored_date, status, path, blob)` for every markdown change, oldest
    first.

    Parses `git log --raw -z --format=%x01%H%x1f%aI`. The `-z` stream is
    NUL-separated, so after the commit header each raw entry is exactly two
    fields — the mode/blob/status line and the path — and no quoting,
    unquoting or `core.quotePath` handling is needed. That is the whole reason
    for `-z`: git's default raw output octal-escapes any path with a non-ASCII
    or unusual byte in it, and a vault is full of those.
    """
    out: list[tuple[datetime.datetime, str, str, str]] = []
    for chunk in stream.split(_COMMIT_SEP):
        if not chunk:
            continue
        fields = chunk.split("\x00")
        header = fields[0]
        if _FIELD_SEP not in header:
            continue
        _sha, _, raw_date = header.partition(_FIELD_SEP)
        when = _parse_authored_date(raw_date)
        i = 1
        while i + 1 < len(fields):
            meta = fields[i].lstrip("\n")
            path = fields[i + 1]
            i += 2
            if not meta.startswith(":"):
                continue
            parts = meta.split()
            # ":<srcmode> <dstmode> <srcblob> <dstblob> <status>"
            if len(parts) < 5:
                continue
            dst_blob, status = parts[3], parts[4]
            out.append((when, status[0], path, dst_blob))
    return out


async def walk_note_versions(
    repo_root: str,
    *,
    prefix: str = "",
    exclude_patterns: list[str] | None = None,
    stats: BackfillStats | None = None,
) -> dict[str, list[VersionRef]]:
    """Every markdown path's distinct content versions, keyed by vault-relative
    path, each list oldest first and already interval-chained.

    One `git log` for the whole repository, not one per path: `--follow` and
    per-path logs are O(paths) process spawns over O(history) each, which on a
    real vault is minutes of `fork` before a single vector is computed.
    """
    patterns = (
        settings.embedding_exclude_patterns
        if exclude_patterns is None
        else exclude_patterns
    ) or []
    stats = stats if stats is not None else BackfillStats()

    args = [
        "log",
        "--reverse",
        "--first-parent",
        "--raw",
        "--no-renames",
        "--no-abbrev",
        "-z",
        f"--format={_COMMIT_SEP}%H{_FIELD_SEP}%aI",
    ]
    # The pathspec is what keeps the log small on a repository with a decade of
    # history, and its two forms are not arbitrary.
    #
    # No prefix (the vault *is* the repository, the common case): a bare
    # `*.md`, whose default wildmatch semantics let `*` cross `/` so notes in
    # folders are included. **Not `:(glob)*.md`** — the `glob` magic word turns
    # on pathname semantics, under which `*` stops at a `/` and every note
    # outside the vault root silently disappears from the walk.
    #
    # With a prefix: `:(literal)` on the directory alone. A vault directory
    # containing `[`, `*` or `?` is not exotic — `Reading [2024]/` — and under
    # glob semantics such a name would match the wrong paths, or nothing.
    # `literal` and `glob` cannot be combined, so the directory is matched
    # literally and the `.md` test happens in Python below, where it is a claim
    # about the vault-relative name the rest of the system uses anyway.
    args += ["--", f":(literal){prefix}" if prefix else "*.md"]

    entries = parse_git_log(await _git(repo_root, *args))

    ordered: dict[str, list[VersionRef]] = {}
    excluded: set[str] = set()
    for when, status, repo_path, blob in entries:
        if prefix:
            if not repo_path.startswith(prefix):
                continue
            path = repo_path[len(prefix) :]
        else:
            path = repo_path
        if not path.endswith(".md"):
            # The pathspec already says `*.md`, but a pathspec is a glob over
            # the repository and this is the vault-relative name the rest of
            # the system uses. Cheap, and it keeps the two in agreement.
            continue
        if any(fnmatch.fnmatch(path, pat) for pat in patterns):
            # The same `EMBEDDING_EXCLUDE_PATTERNS` the indexer applies. An
            # excluded note must not be searchable through its history either,
            # which is also why the indexer's exclusion branch deletes a note's
            # vectors *unscoped* by validity.
            excluded.add(path)
            continue
        # `D` is carried as a version with no blob: it is not a row, it is the
        # instant the preceding version stopped being the note's content.
        ordered.setdefault(path, []).append(
            VersionRef(
                path=path,
                blob=None if status == "D" or blob == _NULL_BLOB else blob,
                valid_from=when,
            )
        )

    stats.paths_excluded += len(excluded)
    return {path: _chain(versions, stats) for path, versions in ordered.items()}


def _chain(
    versions: list[VersionRef], stats: BackfillStats
) -> list[VersionRef]:
    """Collapse repeats, enforce a monotonic timeline, and close each interval.

    Three passes, in this order and for these reasons:

    1. **Collapse** consecutive entries with the same blob. Git already omits
       commits that did not touch the path; this is for the ones that changed
       it back.
    2. **Clamp** each start to its predecessor's, so a rebased or forged
       authored date running backwards cannot produce an interval that ends
       before it begins.
    3. **Close** each interval at the next entry's start, and drop the ones
       that collapsed to zero length under step 2 — a version superseded within
       the same instant was the note's text at no queryable moment, so a row
       for it could never be returned.

    The deletion entry is consumed here: it closes the interval before it and
    contributes no row of its own.
    """
    collapsed: list[VersionRef] = []
    for version in versions:
        if collapsed and collapsed[-1].blob == version.blob:
            continue
        collapsed.append(version)

    monotonic: list[VersionRef] = []
    for version in collapsed:
        if monotonic and version.valid_from < monotonic[-1].valid_from:
            version = replace(version, valid_from=monotonic[-1].valid_from)
        monotonic.append(version)

    chained: list[VersionRef] = []
    for index, version in enumerate(monotonic):
        if version.blob is None:
            # A deletion. It has already served its purpose as the successor
            # that closes the previous interval.
            continue
        end = (
            monotonic[index + 1].valid_from
            if index + 1 < len(monotonic)
            else None
        )
        if end is not None and end <= version.valid_from:
            stats.versions_dropped += 1
            continue
        chained.append(replace(version, valid_to=end))
    return chained


async def read_version_content(repo_root: str, blob: str) -> str | None:
    """The text of one version, or `None` when it is not valid UTF-8.

    A binary or mis-encoded blob is a fact about one version, not a reason to
    abandon a note's history, so it is counted and stepped over exactly as the
    ordinary pass steps over a non-UTF-8 file.
    """
    raw = await _git(repo_root, "cat-file", "blob", blob, binary=True)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


# ── storage ────────────────────────────────────────────────────────────────


@dataclass
class NotePlan:
    """What a backfill would do to one note. Produced without touching the
    provider, which is what makes `--dry-run` honest and cheap."""

    note_id: int
    path: str
    historical: list[VersionRef] = field(default_factory=list)
    current: VersionRef | None = None


async def _existing_intervals(
    session: AsyncSession, note_id: int
) -> set[tuple[datetime.datetime, datetime.datetime]]:
    rows = await session.execute(
        select(NoteEmbedding.valid_from, NoteEmbedding.valid_to)
        .where(
            NoteEmbedding.note_id == note_id,
            NoteEmbedding.valid_to.is_not(None),
        )
        .distinct()
    )
    return {(vf, vt) for vf, vt in rows.all() if vf is not None}


async def plan_note_backfill(
    session: AsyncSession,
    note: NoteMetadata,
    versions: list[VersionRef],
    stats: BackfillStats,
) -> NotePlan:
    """Split a note's walked versions into "insert" and "stamp", skipping work
    an earlier run already did.

    The current version — the one with `valid_to IS NULL` — is **stamped, never
    inserted**. Its vectors already exist: the ordinary embed pass wrote them
    from the working tree, and they are the rows every search reads. Inserting
    a second copy from git would double every current vector, double every
    search hit for that note, and leave two sets that drift apart the moment
    the note is edited. What git contributes for the current version is the one
    thing the pass could not know — the instant it began — so that is all this
    writes.

    Historical versions are matched against what the database already holds by
    their exact `(valid_from, valid_to)` pair. That is the resumability
    mechanism, and it is derived from the data rather than from a cursor: an
    interrupted run repeats only the note it was in the middle of.
    """
    plan = NotePlan(note_id=note.id, path=note.file_path)
    stored = await _existing_intervals(session, note.id)
    for version in versions:
        if version.is_current:
            plan.current = version
            continue
        if (version.valid_from, version.valid_to) in stored:
            stats.versions_already_stored += 1
            continue
        plan.historical.append(version)
    return plan


async def store_version(
    session: AsyncSession,
    note_id: int,
    version: VersionRef,
    content: str,
    stats: BackfillStats,
) -> int:
    """Embed one historical version and add its rows. Returns the chunk count.

    Chunked and cleaned by exactly the functions the ordinary pass uses, and
    for exactly that reason: a historical vector that was produced by a
    different cleaner or a different chunk size sits in the same column and the
    same index as a current one, and cosine distance between them would be
    comparing two conventions rather than two texts.
    """
    cleaned = clean_for_embedding(content)
    chunks, _truncated = chunk_text_bounded(
        cleaned,
        chunk_size=settings.chunk_size,
        overlap=settings.chunk_overlap,
        max_chunks=MAX_CHUNKS_PER_NOTE,
    )
    if not chunks:
        return 0
    vectors = await get_embeddings_batch(chunks)
    if len(vectors) != len(chunks):
        # The same exactness `embed_note` demands. Partial coverage of a
        # version would store a fragment of a past that reads as the whole of
        # it, and nothing downstream could tell.
        raise RuntimeError(
            f"Embedding provider returned {len(vectors)} vectors for "
            f"{len(chunks)} chunks of {version.path} at {version.valid_from}"
        )
    for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
        session.add(
            NoteEmbedding(
                note_id=note_id,
                chunk_index=index,
                chunk_text=chunk,
                embedding=vector,
                valid_from=version.valid_from,
                valid_to=version.valid_to,
            )
        )
    stats.versions_embedded += 1
    stats.chunks_embedded += len(chunks)
    return len(chunks)


async def stamp_current_version(
    session: AsyncSession, note_id: int, version: VersionRef, stats: BackfillStats
) -> None:
    """Record when the note's *present* content began, on the rows that already
    hold it.

    Conditional on `valid_to IS NULL`, so it can only ever touch the current
    slice — a historical row's interval is a fact this must not overwrite. It
    is also why a note whose current vectors have not been written yet is
    simply a no-op here rather than an error: the next embed pass will write
    them, and the next backfill will stamp them.
    """
    result = await session.execute(
        update(NoteEmbedding)
        .where(NoteEmbedding.note_id == note_id, current_embedding_predicate())
        .values(valid_from=version.valid_from)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount:
        stats.current_versions_stamped += 1


async def backfill_note(
    session: AsyncSession,
    repo_root: str,
    note: NoteMetadata,
    versions: list[VersionRef],
    stats: BackfillStats,
    *,
    dry_run: bool = False,
) -> None:
    """Backfill one note, committing once at the end.

    **One transaction per note**, which is the unit an interrupt may split. A
    note is either wholly backfilled or wholly not, so a resumed run's
    interval-matching skip is exact — and no transaction is held open across
    more than one note's worth of provider calls.
    """
    plan = await plan_note_backfill(session, note, versions, stats)

    for version in plan.historical:
        content = await read_version_content(repo_root, version.blob)
        if content is None:
            stats.versions_unreadable += 1
            continue
        if dry_run:
            cleaned = clean_for_embedding(content)
            chunks, _ = chunk_text_bounded(
                cleaned,
                chunk_size=settings.chunk_size,
                overlap=settings.chunk_overlap,
                max_chunks=MAX_CHUNKS_PER_NOTE,
            )
            if chunks:
                stats.versions_embedded += 1
                stats.chunks_embedded += len(chunks)
            continue
        await store_version(session, note.id, version, content, stats)

    if plan.current is not None and not dry_run:
        await stamp_current_version(session, note.id, plan.current, stats)
    elif plan.current is not None:
        stats.current_versions_stamped += 1

    if dry_run:
        await session.rollback()
    else:
        await session.commit()


async def backfill_vault_history(
    session_factory,
    vault_path: str,
    *,
    user_id: int | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    progress_every: int = 25,
    on_progress=None,
) -> BackfillStats:
    """Walk a git-backed vault's history and embed every superseded version.

    `session_factory` is an async-session factory (`src.database.async_session`
    in production); one session is opened per note so an interrupt costs at
    most the note in flight, and so a long run cannot hold one connection and
    one snapshot open for hours against a live server.
    """
    stats = BackfillStats()
    repo_root, prefix = await resolve_repo(vault_path)
    logger.info(
        "Walking history of %s (repo %s, prefix %r)", vault_path, repo_root, prefix
    )
    by_path = await walk_note_versions(repo_root, prefix=prefix, stats=stats)
    stats.notes_walked = len(by_path)
    stats.versions_walked = sum(len(v) for v in by_path.values())

    processed = 0
    for path in sorted(by_path):
        versions = by_path[path]
        if not versions:
            continue
        if limit is not None and processed >= limit:
            break
        async with session_factory() as session:
            note = (
                await session.execute(
                    select(NoteMetadata).where(
                        NoteMetadata.file_path == path,
                        NoteMetadata.user_id.is_(None)
                        if user_id is None
                        else NoteMetadata.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if note is None:
                # Deleted at HEAD, or not yet indexed. Counted, never invented:
                # see the module docstring.
                stats.notes_without_metadata += 1
                continue
            await backfill_note(
                session, repo_root, note, versions, stats, dry_run=dry_run
            )
        processed += 1
        if progress_every and processed % progress_every == 0:
            message = f"[{processed}/{len(by_path)}] {stats.render()}"
            logger.info("History backfill %s", message)
            if on_progress is not None:
                on_progress(message)

    return stats


async def count_current_and_historical(session: AsyncSession) -> tuple[int, int]:
    """`(current_rows, historical_rows)` — the one-line answer to "did the
    backfill actually store anything, and did it disturb the present"."""
    current = (
        await session.execute(
            select(func.count(NoteEmbedding.id)).where(current_embedding_predicate())
        )
    ).scalar() or 0
    historical = (
        await session.execute(
            select(func.count(NoteEmbedding.id)).where(
                NoteEmbedding.valid_to.is_not(None)
            )
        )
    ).scalar() or 0
    return current, historical
