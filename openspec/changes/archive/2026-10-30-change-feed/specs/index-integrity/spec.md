## ADDED Requirements

### Requirement: `content_changed_at` SHALL move only when the stored content hash changes
The index pass SHALL write `notes_metadata.content_changed_at` in the same statement that writes `content_hash`. On conflict it SHALL set the column to `now()` exactly when the stored `content_hash` is distinct from the new one and keep it unchanged otherwise. A row inserted for a path with no row SHALL take the file's mtime. No other writer SHALL change the column. The stat refresh, the indexer's id-preserving move, `move_note`, the keyword-vector writers, the re-derive marker and the link backfill SHALL leave it unchanged.

#### Scenario: Touch keeps the stamp
- **WHEN** a file's mtime changes, its bytes do not, and a pass (stat-shortcut or full-hash) re-reads it
- **THEN** `content_changed_at` SHALL be unchanged

#### Scenario: Re-derive and stale-marker re-upserts keep the stamp
- **WHEN** a re-deriving pass, or a pass after an extraction-version bump, re-upserts every row with an unchanged hash
- **THEN** no row's `content_changed_at` SHALL change

#### Scenario: Edit found only by the full-hash pass
- **WHEN** an edit keeps all four stat fields and is detected by the next full-hash pass
- **THEN** that pass SHALL set `content_changed_at` to its own `now()`

#### Scenario: A move keeps the stamp
- **WHEN** a note is moved, by `move_note` or externally with its hash paired by the pass
- **THEN** the row's `content_changed_at` SHALL be unchanged

#### Scenario: A quarantined note re-enters
- **WHEN** a quarantined note's hash changes, so that it is indexed again as a new row
- **THEN** its `content_changed_at` SHALL be the file's mtime

### Requirement: The change-feed append SHALL be the attempt's final statement and SHALL NOT change the pass's outcome
The index pass SHALL collect its events from its own classification (`existing`, `to_upsert`, the committed moves and the final `deleted_paths`), SHALL append them as the last statement of `_index_vault_attempt` before `session.commit()`, after every derivation stage (keyword vectors, links, the re-derive tail, and any per-note derivation added later), and SHALL treat an append failure as a durable `note_event_gaps` row committed with the pass, never as a skip, a read failure, a quarantine, a withholding of the provenance stamp or a failed pass. An attempt that rolls back, whether for a `PoisonNote` restart, `IndexIndeterminate`, cancellation or any other failure, SHALL leave no events.

#### Scenario: Poison restart writes events once
- **WHEN** an attempt raises `PoisonNote`, the note is quarantined, and the re-run commits
- **THEN** `note_events` SHALL hold the re-run's events only, and none for the quarantined note

#### Scenario: Append failure does not degrade health
- **WHEN** the append fails on three consecutive passes
- **THEN** no `indexer_health` failure counter SHALL be incremented by it and `/health` SHALL NOT report `degraded` on its account

#### Scenario: Protected and deferred rows emit nothing
- **WHEN** a directory the walk could not list holds indexed rows, or a row changed mid-walk and is deferred (C5)
- **THEN** the pass SHALL record no event for those rows

### Requirement: The indexer's content hash SHALL be computed by the shared canonical note hash
`read_note_at` SHALL read the note's bytes and derive its text through `canonical_note_text`, and the pass's `content_hash` SHALL equal `canonical_note_hash` of those bytes. The refactor SHALL NOT change any existing note's hash: the text SHALL be identical to the previous text-mode read with universal-newline translation and U+0000 removal.

#### Scenario: Hashes unchanged by the refactor
- **WHEN** notes with LF, CRLF, lone CR, U+0000 and a UTF-8 BOM are hashed by the previous implementation and by `canonical_note_hash`
- **THEN** each pair of hashes SHALL be equal

#### Scenario: Invalid UTF-8
- **WHEN** a note's bytes are not valid UTF-8
- **THEN** `canonical_note_text` SHALL return `None` and the pass SHALL treat the path as not indexable, exactly as before
