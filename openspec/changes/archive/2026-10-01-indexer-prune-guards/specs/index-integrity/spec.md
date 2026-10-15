## MODIFIED Requirements

### Requirement: Indexer failures SHALL be counted per scope from every entrypoint, together with embedding failures and loss of the indexer task

The indexer SHALL keep, per scope, the number of consecutive failed index passes and the number of consecutive embed passes that failed — an embed pass counts as failed when it reports any per-note embedding failure in `EmbedPassResult.failures` (provider errors included) or when the embed stage raised — with the time of the last success and last failure, updated identically by the startup, periodic and manual-reindex entrypoints in single-user and multi-user modes. An index pass SHALL count as failed only if it raises after the quarantine recovery; a pass that quarantines a note SHALL count as successful. A successful pass (respectively an embed pass with no per-note failure that did not raise) SHALL reset its counter. A committed pass that could not list one or more directories beneath the root SHALL increment a per-scope `walk_incomplete` counter, reset by a committed pass that listed every directory, and it SHALL count toward degradation like the other counters. Per-tick work not attributable to one scope — user enumeration and overlap detection — SHALL be counted as its own consecutive-failure counter. The application SHALL record that the indexer background task is not running when that task ends by exception or returns while the application is not shutting down. A committed re-derive that is incomplete SHALL increment a separate per-scope `rederive_incomplete` counter, reset by a re-derive that records provenance or by the scope leaving re-derive; it SHALL count toward degradation like the other counters. In single-user mode a failed index stage SHALL NOT prevent the same tick's embed stage from running over committed rows, nor the token, authorization-code and unused-client cleanup, which SHALL run on every tick regardless of the index outcome. The CRITICAL "manual intervention required" log SHALL be emitted once when any counter first reaches `INDEXER_DEGRADED_AFTER_FAILURES` (default 3), and not again for that counter until it has been reset.

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

#### Scenario: An unlistable subdirectory is counted

- **WHEN** three consecutive passes for a scope commit while a subdirectory cannot be listed
- **THEN** the scope's `walk_incomplete` count SHALL be 3 and the CRITICAL log SHALL have been emitted exactly once

## ADDED Requirements

### Requirement: A pass SHALL NOT prune or move-pair a row beneath a directory it could not list

The walk SHALL record the vault-relative prefix of every directory it could not open or list, using the same failures it already records as skips, and of every entry whose type it could not determine. During the pass, a row whose path, read under the generation lock, equals such a prefix or lies beneath it (prefix followed by `/`) SHALL be excluded from deletion and from move-source pairing, and its quarantine entry SHALL NOT be cleared for being unseen; every other row SHALL be processed and committed as usual. The pass SHALL log one WARNING naming the unlisted directories (bounded, with a count of the remainder) and the number of rows protected, and its run record SHALL carry a line naming them (at most five, then a count of the remainder, rendered so the record is always storable), labelled as a failed run on the panel. A directory that no longer exists when the walk opens it, a symlink, and a non-directory SHALL keep their present treatment.

#### Scenario: Rows under an unreadable folder survive

- **WHEN** a folder holding ten indexed and embedded notes becomes unreadable to the server and the next pass runs
- **THEN** the ten rows and their embeddings SHALL remain unchanged, every other changed note in the vault SHALL be committed, and the run record SHALL name the folder

#### Scenario: A protected row is not paired as a move source

- **WHEN** a folder holding an indexed note cannot be listed and a new file elsewhere has the same content hash as that note
- **THEN** the new file SHALL be inserted as a new note and the protected row SHALL keep its path

#### Scenario: An entry whose type cannot be determined is protected

- **WHEN** determining whether the entry `sub` is a directory fails with an I/O error while other notes are discovered
- **THEN** the rows at `sub` and beneath `sub/` SHALL be neither pruned nor paired as a move source

#### Scenario: A sibling with a shared name prefix is not protected

- **WHEN** the folder `sub` cannot be listed and the note `subway.md` at the root has been deleted from disk
- **THEN** the row for `subway.md` SHALL be pruned and the rows under `sub/` SHALL be kept

#### Scenario: Recovery re-indexes nothing that did not change

- **WHEN** the folder becomes readable again with its notes unchanged
- **THEN** the next pass SHALL NOT re-insert or re-embed those notes

### Requirement: An unlistable root, or an empty root over an existing index, SHALL abort the pass with nothing deleted

An index pass SHALL raise before its locked transaction, writing nothing, when the vault root itself cannot be listed, or when the walk discovers no markdown file at all while the scope's index holds at least one row, unless the scope holds an unexpired empty-prune permission granted by an administrator. The same condition SHALL be re-evaluated against the rows read under the generation lock, and SHALL raise there before any mutation. The error SHALL state the condition and the number of indexed notes, SHALL name where the permission is granted, and SHALL NOT name any path. Such a pass SHALL be recorded as failed in its run record and counted as a failed index pass. A scope whose index holds no rows SHALL NOT be refused. A permission SHALL be single-use: the first pass of that scope that evaluates this condition SHALL take it atomically, whether or not the root is empty, and SHALL use that taken authorisation — never the registry — for its re-evaluation under the lock and across its own restarts; the authorisation SHALL NOT be returned after failure or cancellation. A permission SHALL expire 15 minutes after it was granted, and the pass SHALL re-check the expiry and that the scope's vault assignment is unchanged immediately before pruning, refusing if either fails. This requirement governs the pass's ordinary prune; a provenance discard decided because the vault assignment demonstrably changed is not affected by it. A permission SHALL NOT authorise pruning beneath an unlisted directory and SHALL NOT override an unlistable root.

#### Scenario: An empty mount deletes nothing

- **WHEN** a scope's index holds 50 notes and its vault root is replaced by an empty directory
- **THEN** the pass SHALL raise, no row or embedding SHALL be deleted, the run record SHALL be failed, and after `INDEXER_DEGRADED_AFTER_FAILURES` such passes `/health` SHALL report `degraded`

#### Scenario: A confirmed empty vault is pruned once

- **WHEN** an administrator confirms that the scope's vault is empty and the next pass finds no markdown file
- **THEN** that pass SHALL delete the scope's rows, consume the permission and log the number of rows deleted
- **AND** after the scope is indexed again and its root is emptied again, the next pass SHALL raise unless a new permission is granted

#### Scenario: A permission that expires during the lock wait authorises nothing

- **WHEN** a pass takes a permission and its 15-minute expiry passes before the pass reaches its prune
- **THEN** the pass SHALL raise with nothing deleted

#### Scenario: A permission is not kept for later

- **WHEN** an administrator confirms an empty vault but the next pass finds the vault populated
- **THEN** that pass SHALL index normally and the permission SHALL be consumed

#### Scenario: An unlistable root deletes nothing

- **WHEN** the vault root opens but cannot be listed
- **THEN** the pass SHALL raise with nothing written, even if an empty-prune permission is held

#### Scenario: A new or emptied scope is not refused

- **WHEN** a scope whose index holds no rows has an empty vault root
- **THEN** the pass SHALL complete without error

#### Scenario: One tenant's empty mount does not affect another

- **WHEN** in multi-user mode one user's vault root is empty over an existing index
- **THEN** only that user's pass SHALL raise, and every other user's pass SHALL index normally
