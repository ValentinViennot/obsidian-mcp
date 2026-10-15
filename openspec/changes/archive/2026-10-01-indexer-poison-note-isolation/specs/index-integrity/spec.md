## MODIFIED Requirements

### Requirement: Keyword indexing attempts full content and degrades per-note without aborting the pass

The tsvector build (incremental pass and full rebuild alike) SHALL attempt the note's full content. Each attempt SHALL run inside its own savepoint, entered such that a database error unwinds the savepoint through the context manager's rollback before any retry (the failure handling sits outside the savepoint context, so the outer transaction is never left in the aborted state). On failure the build SHALL retreat by halving the content, one fresh savepoint per attempt, down to a floor of exactly 100,000 characters — the pre-change statement. A failure at the floor SHALL propagate out of the build, and the two call sites SHALL provide different — but individually stated — guarantees: in the **incremental pass** a floor failure whose SQLSTATE marks it a poison failure SHALL quarantine that note and the pass SHALL be re-run without it, committing every other note (see "A note that fails at a per-note database boundary SHALL be quarantined and removed from the index"), while any other floor failure aborts the pass with nothing committed; the **full rebuild** SHALL be atomic — no intermediate commits — so a floor failure rolls the entire rebuild back and the error surfaces to the operator who invoked it, never leaving a keyword index half-built under two FTS configurations that no periodic pass would repair.

Verification of the savepoint behavior SHALL include a real-PostgreSQL integration test (mocks cannot prove the driver's aborted-transaction state clears): induce a genuine statement failure, observe the bounded retry succeed within the same outer transaction, perform a further update, commit, and verify both rows.

**The full rebuild SHALL update only rows it certifies.** It snapshots the table once and then reads the vault note by note, so both the rows and the files move underneath it — and a keyword vector is only ever rewritten again when a note's `content_hash` changes, because both move paths preserve `content_tsvector` and the ordinary scan skips a row whose hash is unchanged. A row the rebuild steps over, or writes the wrong bytes into, therefore stays on the previous configuration with nothing that would ever revisit it.

The rebuild's snapshot SHALL therefore retain each row's owner, relative path and content hash; the bytes it reads SHALL be verified to hash to that retained content hash before anything is written; and its UPDATE SHALL be conditional on all four of id, owner, relative path and content hash, and SHALL require that exactly one row matched. A zero-row update, a read failure, or a hash mismatch SHALL NOT be committed around and SHALL NOT be routed through the size-halving retreat, which addresses a size failure and cannot fix a stale target. Each SHALL instead trigger a bounded re-read of the current owner-scoped row: a row that is gone is safely absent and SHALL be skipped; a row whose path or hash has changed SHALL be retried against those fresh values within a bounded number of attempts; and a row that still records the path and hash the rebuild acted on — an unreadable file, or bytes the scan has not caught up with — SHALL abort the whole rebuild, which being one transaction rolls every other note back with it.

#### Scenario: A note moved mid-rebuild is repaired, never stepped over

- **WHEN** a note's `file_path` changes (by either move path) after the rebuild's snapshot and before it reads that row, so the read at the snapshotted path fails
- **THEN** the rebuild SHALL re-read the row, retry against its current path, and write that row's tsvector under the current configuration
- **AND** it SHALL NOT commit the remaining notes while leaving that row on the previous configuration

#### Scenario: A stale write does not land when the content hash advances

- **WHEN** a concurrent index pass commits a new `content_hash` and a matching `content_tsvector` for a row between the rebuild's read of the earlier content and its UPDATE
- **THEN** the certified UPDATE SHALL match no row and the earlier content's tsvector SHALL NOT be written
- **AND** the rebuild SHALL re-read the row and rebuild it against the committed content, so the stored hash and the stored tsvector describe the same content

#### Scenario: A row it cannot certify aborts the whole rebuild

- **WHEN** the rebuild cannot read a note, or the bytes it reads do not hash to the row's `content_hash`, and a re-read shows the row still records that path and that hash
- **THEN** the rebuild SHALL abort and its transaction SHALL roll back, leaving every note's `content_tsvector` unchanged
- **AND** the error SHALL surface to the operator who invoked it, rather than the rebuild committing around that row

#### Scenario: A row deleted mid-rebuild is safely absent

- **WHEN** the row is deleted (or leaves the rebuild's owner scope) between the snapshot and the write
- **THEN** the rebuild SHALL skip it without aborting, because no row remains in scope to leave on the previous configuration

#### Scenario: Terms beyond the former 100K slice are searchable when the full build succeeds

- **WHEN** a valid note carries a distinctive term past 100,000 characters and its full-content tsvector build succeeds
- **THEN** after the next index of that note, `keyword_search` for that term SHALL return the note

#### Scenario: A pathological note degrades alone, and the degradation is bounded and logged

- **WHEN** a note's full-content tsvector exceeds PostgreSQL's size limit
- **THEN** the pass SHALL retreat to a bounded prefix for that note only, log the retreat with the prefix length, index the remaining notes normally, and commit
- **AND** terms present only beyond the successful prefix are accepted as unsearchable for that note — the declared degradation

#### Scenario: A poison floor failure in the incremental pass costs only that note

- **WHEN** even the 100,000-character floor attempt fails for a note during an incremental index pass with a data-exception (class 22) or program-limit (54000) SQLSTATE
- **THEN** that note SHALL be quarantined, and any existing row at its path SHALL be removed together with its embeddings and outgoing links, so no hash is committed beside derived data from other content
- **AND** the pass SHALL be re-run without that note and every other note in the scope SHALL be committed

#### Scenario: A non-poison floor failure still aborts the incremental pass

- **WHEN** the floor attempt fails for a reason outside the poison SQLSTATE set (for example a connection loss or lock timeout)
- **THEN** the error SHALL propagate and the pass SHALL abort with nothing committed, and the failure SHALL count toward the scope's consecutive failures

#### Scenario: A floor failure during a full rebuild rolls the whole rebuild back

- **WHEN** `rebuild_tsvectors` is processing a vault of more than 500 notes and a note's floor attempt fails after what would previously have been an intermediate commit boundary
- **THEN** the entire rebuild SHALL roll back — no note's tsvector changes — and the error SHALL surface to the invoking operator
- **AND** the keyword index SHALL never be left half-built under two FTS configurations

### Requirement: A re-derive that skipped any file is incomplete, and an incomplete re-derive is not recorded
A per-file skip during a re-deriving pass SHALL make that re-derive **incomplete** when the skip could leave a row the pass did not derive, and an incomplete re-derive SHALL NOT record provenance for that user. Such a skip is: a discovered file the pass could not open, stat or read **whose path has a row** in the pass's locked snapshot; a directory it could not open or list (anything beneath it may have a row); or a changed note already selected for upsert whose links it could not extract. A skipped path with **no** row cannot certify a foreign row and SHALL NOT withhold the record. A path that is **present but not indexable** — a quarantined note, a file whose content is not valid UTF-8, or a path that cannot be encoded or exceeds the stored path length (see "Paths and contents the index can never hold are present but not indexable") — SHALL NOT withhold the record either, because any row at that path is deleted in the pass's transaction. A note whose link extraction was **truncated at the declared cap** (`MAX_LINKS_PER_NOTE`) is NOT a skip: the cap is a bounded, deterministic, logged degradation, the rows the pass wrote are exactly the rows it derived, and the note is marked `links_truncated` so the truncation is durably visible. An incomplete pass SHALL still perform every repair it can, SHALL log the paths that kept it unrecorded, and the next pass SHALL re-derive again.

Without this rule the pass's structural claim is false. The scan continues past a file it cannot read, and ordinary pruning keeps a row whose relative path exists under the assigned root — which is exactly the row a re-derive exists to replace. A vault that supplies a note at the same relative path as the previous vault, but which cannot be read, therefore leaves the previous vault's metadata row and its link rows untouched while the pass completes and records the new directory over them. One such skip is enough to certify a foreign row.

The rule fails toward re-work rather than toward wrongness for an unreadable file, because the alternative — deleting the row behind every unreadable path — destroys a row that may be the correct row for a file that was merely unreadable at that moment. A file whose bytes were read in full but are not valid UTF-8 is not in that position: its bytes are provably not those the row was derived from, so its row is deleted rather than kept.

A re-derive re-runs every tick until it completes, and each re-run upserts every file with its keyword vector and links, so an incomplete re-derive is a full-scope rewrite per tick. It SHALL therefore be withheld only by a skip that could hide a foreign row, as above, and a scope whose re-derive stays incomplete for `INDEXER_DEGRADED_AFTER_FAILURES` consecutive passes SHALL be counted as degraded (see the failure-accounting requirement), in addition to the pass naming the offending paths in its log.

#### Scenario: An unreadable file with a row behind it withholds the record

- **WHEN** a re-deriving pass discovers a file it cannot read, the locked snapshot has a row at that path, and the pass completes the rest of its work
- **THEN** the pass SHALL record no provenance for that user
- **AND** the next pass SHALL re-derive again

#### Scenario: An unreadable file with no row does not withhold the record

- **WHEN** a re-deriving pass discovers a file it cannot read, no row exists at that path, and nothing else is skipped
- **THEN** the pass SHALL record the provenance of the directory it scanned
- **AND** the next tick SHALL NOT re-derive or re-upsert the scope's unchanged notes

#### Scenario: A foreign row behind an undecodable path is deleted, not certified

- **WHEN** a user was indexed from one vault, is assigned another, and the newly assigned vault holds a file at the same relative path whose bytes are not valid UTF-8
- **THEN** the pass SHALL delete the previous vault's row at that path, with its embeddings and outgoing links, in its transaction
- **AND** if nothing else withholds the record it SHALL record the newly assigned root's provenance, and no later pass SHALL take the keep branch over a row from the previous vault at that path

#### Scenario: A file that disappears during the scan withholds the record only if it has a row

- **WHEN** a file is discovered by a re-deriving pass and can no longer be read when the pass reaches it
- **THEN** the pass SHALL treat that path as a skip, and SHALL record no provenance for that user if the locked snapshot has a row at that path

#### Scenario: A directory that cannot be listed withholds the record

- **WHEN** a re-deriving pass cannot open or list a directory beneath the root
- **THEN** the pass SHALL record no provenance for that user

#### Scenario: Every link-extraction skip is recorded, including the unreachable one

- **WHEN** a re-deriving pass reaches a changed note it cannot extract links for — because it holds no buffered body for that path, or because that path has no index row to attach the links to
- **THEN** both cases SHALL be recorded as skips, so the record is withheld
- **AND** neither SHALL be dropped silently, whatever its likelihood, because the record is a claim that every surviving link row was written by that pass

#### Scenario: A capped note does not withhold the record

- **WHEN** a re-deriving pass reaches a changed note with more than `MAX_LINKS_PER_NOTE` links and processes every other discovered file without a skip
- **THEN** the first `MAX_LINKS_PER_NOTE` links SHALL be written, `links_truncated` SHALL be set on the note, an ERROR line SHALL be logged, and the pass SHALL record the provenance of the directory it scanned

#### Scenario: A quarantined note does not withhold the record, and leaves no foreign row

- **WHEN** a user was indexed from one vault, is assigned another, and a re-deriving pass quarantines the newly assigned vault's note at a relative path the previous vault also had
- **THEN** the row at that path from the previous vault SHALL be deleted in the pass's transaction, together with its embeddings and outgoing links
- **AND** if the pass has no other withholding skip it SHALL record the provenance of the directory it scanned, and the next tick SHALL NOT re-derive or re-upsert the scope's unchanged notes

#### Scenario: The skipped paths are named

- **WHEN** a re-deriving pass is incomplete
- **THEN** it SHALL log the paths responsible, bounded to a stated number with a count of the remainder

#### Scenario: A complete re-derive is recorded

- **WHEN** a re-deriving pass leaves no withholding skip and raises nothing
- **THEN** it SHALL record the provenance of the directory it scanned, after its last write

## ADDED Requirements

### Requirement: The indexer SHALL derive every indexed value from NUL-free text and SHALL NOT modify the note file

The indexer SHALL remove every U+0000 character from a note's text at the single point where the file's bytes are decoded, before the content hash is computed, so that the content hash, the keyword vector, the title, tags, links, chunk text and every re-verification by the embed, reconciliation, link-backfill and rebuild passes are derived from the same NUL-free text. A note containing no NUL SHALL produce exactly the text and content hash it produced before this change. The indexer SHALL additionally remove U+0000 from every string key and value written to the `frontmatter` JSONB column, and the shared title and tag derivations (used by the indexer, `read_note` metadata and `move_note` alike) SHALL remove it from their results, because a YAML escape can produce a NUL from NUL-free bytes. The note's bytes on disk SHALL NOT be modified, and the parsed frontmatter mapping used by `set_frontmatter` SHALL NOT be altered. The shared tag derivation SHALL drop any tag whose UTF-8 encoding exceeds 1,024 bytes, so one tag cannot exceed the tags index's row limit; the shared derivation SHALL drop it silently, because it also serves request paths, which may not log per call (#190), and the indexer SHALL log one WARNING naming the note whose tags it dropped. A link carrying U+0000 SHALL NOT be extracted as a link, because removing the character would name a different file: a markdown link SHALL be refused when its decoded target or its label contains U+0000, and a wikilink when its target, anchor or alias contains U+0000. A markdown link's anchor is not decoded and SHALL be stored as written. Each read that removes a NUL SHALL log one WARNING naming the vault-relative path and the number removed, and SHALL NOT log note content.

#### Scenario: A note with a NUL in its body indexes (#308 repro)

- **WHEN** a note containing `hello\x00world\n` is discovered by an index pass
- **THEN** the pass SHALL complete and commit, the note's row SHALL be stored with the content hash of `helloworld\n`, and `keyword_search` for `helloworld` SHALL return the note
- **AND** the file SHALL still contain the NUL byte

#### Scenario: A NUL-bearing note embeds

- **WHEN** the embed pass processes a note whose file contains NUL bytes
- **THEN** its chunks SHALL be stored without NUL and the note SHALL be certified, without a stale-certification mismatch against the hash the scan committed

#### Scenario: A YAML-escaped NUL in frontmatter indexes

- **WHEN** a note's frontmatter contains `title: "a\0b"` and `tags: ["x\0y"]` and a key `"k\0": 1`
- **THEN** the pass SHALL complete, the stored title SHALL be `ab`, the stored tags SHALL include `xy`, and the stored JSONB SHALL carry the key `k`

#### Scenario: An over-long tag is dropped, not fatal

- **WHEN** a note's frontmatter carries a 3,000-byte tag and an ordinary tag
- **THEN** the note SHALL index with the ordinary tag only

#### Scenario: A percent-encoded NUL in a link target does not invent a link

- **WHEN** a note contains `[x](bad%00target.md)` and a note `badtarget.md` exists
- **THEN** the pass SHALL complete, no link row SHALL be written for that href, and `get_backlinks("badtarget.md")` SHALL NOT include the note

#### Scenario: NUL-free notes keep their hashes across the deploy

- **WHEN** the first index pass after deploying this change runs over a vault whose notes contain no NUL
- **THEN** no note's `content_hash` SHALL change and no note SHALL be re-upserted on account of this change

### Requirement: A note that fails at a per-note database boundary SHALL be quarantined and removed from the index

A **poison failure** SHALL be a database error whose SQLSTATE is in class 22 (data exception) or is 54000 (program limit exceeded) — including a client-side encode failure that the driver raises with a class-22 SQLSTATE before the statement reaches the server — raised during an incremental index pass by a write attributable to exactly one note: its id-preserving move update, its row in the batch upsert, its keyword-vector build failing at the floor, or its link inserts. Each of these write sites SHALL run inside a savepoint so that a failure leaves the pass's transaction usable; a batch statement that fails with a poison SQLSTATE SHALL be replayed one row at a time, each row in its own savepoint, to identify the note, **every** row that fails alone SHALL be quarantined in the same restart, and if no single row fails alone the error SHALL NOT be treated as a poison failure. The classification SHALL be decided by the exception raised, the driver exception it wraps and its explicit cause chain only, never by an exception it was merely raised while handling, and a connection, interface or session-state failure SHALL NOT be a poison failure whatever it wraps; a savepoint that cannot be rolled back SHALL fail the pass rather than be treated as a recovered poison failure.

Before a single-note failure is treated as poison, that note's write SHALL be executed once more in a fresh savepoint. The note SHALL be quarantined only if the retry also fails with a poison SQLSTATE. If the retry succeeds, the failure was not caused by the note's data: the note SHALL NOT be quarantined, and the pass SHALL fail with the original error, as an ordinary failure retried on the next tick; a retry that fails with any other error SHALL fail the pass with that error.

On a poison failure the pass's transaction SHALL roll back in full, the note SHALL be recorded in an in-process quarantine keyed by owner and vault-relative path together with the content hash that failed, and the pass SHALL be re-run for the same scope from classification, at most `INDEXER_QUARANTINE_RETRIES_PER_TICK` times per scope per invocation; beyond that bound the pass SHALL fail as an ordinary failure. While a file's current content hash equals its quarantined hash, every pass SHALL treat it as present on disk but not indexable: it SHALL be excluded from upsert and from move detection, and any existing row at that path SHALL be deleted in the pass's transaction together with its embeddings and outgoing links, so that no search tool serves the note's superseded content and no committed content hash is paired with derived data from other content. A quarantine SHALL NOT count as a read failure: it SHALL NOT keep the scope due for a full-hash pass. The entry SHALL be cleared when the note next indexes successfully, when its file is deleted, or when its scope's index is discarded. Quarantining SHALL log one ERROR naming the path and SQLSTATE and no content, and the pass's run record SHALL name the quarantined paths (at most five, then an ellipsis, each rendered so that an unencodable character cannot make the record itself unstorable) in its error field without the pass counting as failed, and the control panel SHALL label such a run "quarantined", distinctly from a failed run. The startup pass, the periodic pass and the panel's manual reindex SHALL all apply this recovery. The full keyword rebuild is excluded from this requirement and SHALL remain atomic.

#### Scenario: One poison note does not stop its owner's index

- **WHEN** a scope contains 50 changed notes and one of them raises a genuine PostgreSQL poison failure on its keyword-vector write, after another note in the same pass was moved
- **THEN** the other 49 notes and the move SHALL be committed in the same invocation
- **AND** the poison note SHALL have no row, and the next tick SHALL NOT retry it while its content is unchanged

#### Scenario: A poison row in the batch upsert is identified and quarantined

- **WHEN** one row of a 100-row upsert batch raises a class-22 error
- **THEN** the pass SHALL identify that row by per-row replay, quarantine it, and commit the other 99

#### Scenario: A failure the retry does not reproduce is not quarantined

- **WHEN** a note's write at any attributable site fails once with a poison SQLSTATE and succeeds when executed again alone
- **THEN** the note SHALL NOT be quarantined and the pass SHALL fail with the original error, committing nothing
- **AND** the next tick SHALL index the note normally

#### Scenario: A poison failure on a move is quarantined

- **WHEN** an indexed note is moved on disk to a valid-length path and the id-preserving move update raises a genuine server-side poison failure
- **THEN** the destination SHALL be quarantined, the source row SHALL be deleted as for a vanished file, and every other note in the scope SHALL be committed

#### Scenario: A previously indexed note that becomes poison is removed, not served stale

- **WHEN** a note indexed and embedded with content A is edited to content B, and B raises a poison failure
- **THEN** after the pass the note SHALL have no row, keyword vector, embeddings or outgoing links
- **AND** neither `semantic_search` nor `keyword_search` SHALL return content A for that path

#### Scenario: Editing a quarantined note retries it

- **WHEN** a quarantined note's content is edited so that it no longer fails
- **THEN** the next index pass SHALL index it and clear its quarantine entry

#### Scenario: The same recovery applies to startup and manual reindex

- **WHEN** a poison note is first encountered by the startup pass or by the panel's manual reindex
- **THEN** that invocation SHALL quarantine it and commit the rest, exactly as the periodic pass does

#### Scenario: The retry bound is honoured

- **WHEN** recovering one scope in one invocation would require more restarts than `INDEXER_QUARANTINE_RETRIES_PER_TICK`
- **THEN** that invocation SHALL fail with nothing committed for its last attempt and the failure SHALL count toward the scope's consecutive failures, while the notes already quarantined stay quarantined for the next tick

### Requirement: Indexer failures SHALL be counted per scope from every entrypoint, together with embedding failures and loss of the indexer task

The indexer SHALL keep, per scope, the number of consecutive failed index passes and the number of consecutive embed passes that failed — an embed pass counts as failed when it reports any per-note embedding failure in `EmbedPassResult.failures` (provider errors included) or when the embed stage raised — with the time of the last success and last failure, updated identically by the startup, periodic and manual-reindex entrypoints in single-user and multi-user modes. An index pass SHALL count as failed only if it raises after the quarantine recovery; a pass that quarantines a note SHALL count as successful. A successful pass (respectively an embed pass with no per-note failure that did not raise) SHALL reset its counter. Per-tick work not attributable to one scope — user enumeration and overlap detection — SHALL be counted as its own consecutive-failure counter. The application SHALL record that the indexer background task is not running when that task ends by exception or returns while the application is not shutting down. A committed re-derive that is incomplete SHALL increment a separate per-scope `rederive_incomplete` counter, reset by a re-derive that records provenance or by the scope leaving re-derive; it SHALL count toward degradation like the other counters. In single-user mode a failed index stage SHALL NOT prevent the same tick's embed stage from running over committed rows, nor the token, authorization-code and unused-client cleanup, which SHALL run on every tick regardless of the index outcome. The CRITICAL "manual intervention required" log SHALL be emitted once when any counter first reaches `INDEXER_DEGRADED_AFTER_FAILURES` (default 3), and not again for that counter until it has been reset.

#### Scenario: A failing tenant is counted in multi-user mode

- **WHEN** in multi-user mode one user's index pass fails on three consecutive ticks while other users' passes succeed
- **THEN** that user's scope SHALL have an index failure count of 3 and the CRITICAL log SHALL have been emitted exactly once

#### Scenario: A provider outage is counted even though indexing succeeds

- **WHEN** metadata indexing succeeds on three consecutive ticks while every embedding request fails
- **THEN** the scope's embedding failure count SHALL be 3 and its index failure count SHALL be 0

#### Scenario: A single-user index failure does not stop embedding or cleanup

- **WHEN** in single-user mode the index stage fails on a tick
- **THEN** the embed stage and the expired-token and unused-client cleanup SHALL still run on that tick

#### Scenario: A dead indexer task is recorded

- **WHEN** the indexer background task raises during startup user enumeration and ends while the HTTP application keeps serving
- **THEN** the indexer SHALL be recorded as not running
- **AND** a lifespan shutdown that cancels the task SHALL NOT record it as not running

### Requirement: Paths and contents the index can never hold SHALL be present but not indexable, decided at the scan

The scan (and the locked re-read) SHALL classify as **present but not indexable** a discovered file whose vault-relative path cannot be encoded as UTF-8 or exceeds the stored path length, and a file whose bytes were read in full but are not valid UTF-8. Such a path SHALL NOT be upserted, SHALL NOT take part in move detection, and any existing row at that path SHALL be deleted in the pass's transaction together with its embeddings and outgoing links, so no search tool serves content the file no longer holds. Each SHALL be logged at WARNING with the path rendered so that it can always be logged and stored, and SHALL NOT cause the pass to fail, to restart, or to withhold a re-derive's record. A file that cannot be opened, statted or read SHALL NOT be classified this way: its row, if any, SHALL be kept as before.

#### Scenario: A filename that is not valid UTF-8 does not wedge the pass

- **WHEN** the vault contains `caf\xe9.md` (a Latin-1 byte in the name) beside 100 ordinary changed notes
- **THEN** the pass SHALL commit the 100 notes, write no row for that file, log one WARNING, and the next tick SHALL NOT repeat any write

#### Scenario: An over-long path does not wedge the pass

- **WHEN** a note sits at a path longer than the `file_path` column allows
- **THEN** the pass SHALL skip it at the scan, commit every other note, and SHALL NOT restart

#### Scenario: A note rewritten as non-UTF-8 is removed, not served stale

- **WHEN** an indexed and embedded note is overwritten with Latin-1 bytes
- **THEN** after the next pass it SHALL have no row, keyword vector, embeddings or outgoing links, and neither search tool SHALL return its previous content

#### Scenario: An unreadable file keeps its row

- **WHEN** an indexed note becomes unreadable (permission denied)
- **THEN** its row SHALL be kept unchanged, as before this change

