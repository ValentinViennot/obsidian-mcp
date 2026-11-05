## ADDED Requirements

### Requirement: A task-derivation failure SHALL be isolated to that note's task rows and SHALL NOT quarantine, prune or fail anything else

A task-derivation failure (an exception raised by the task extractor, or a database error with a poison SQLSTATE, class 22 or 54000, raised by one note's task statements) SHALL roll back only that note's task savepoint and SHALL be retried once in a fresh savepoint. If the retry also fails, the note's task rows SHALL be deleted and `notes_metadata.tasks_failed` SHALL be set in the pass's transaction, with one ERROR line naming the path, the exception class and the SQLSTATE and no content. The failure SHALL NOT quarantine the note, SHALL NOT remove or alter its row, keyword vector, links or embeddings, SHALL NOT restart or fail the pass, and SHALL NOT be a skip that withholds a re-derive's record. `tasks_failed` SHALL be cleared only when a later task derivation of that note succeeds. Any other database error during the task stage SHALL fail the pass as an ordinary failure with nothing committed. The quarantine's set of attributable write sites (move update, batch upsert row, keyword vector at the floor, link inserts) is unchanged by this requirement. The startup pass, the periodic pass and the panel's manual reindex SHALL all apply this isolation.

#### Scenario: The periodic pass commits around a failing task derivation

- **WHEN** a periodic incremental pass has 50 changed notes and one note's task inserts fail with a poison SQLSTATE twice
- **THEN** all 50 notes SHALL be committed with their rows, keyword vectors, links and embeddings eligible, and 49 SHALL have their task rows
- **AND** the failing note SHALL have no task rows, `tasks_failed = true`, no quarantine entry, and SHALL still be returned by `keyword_search` and `read_note`

#### Scenario: The startup pass applies the same isolation

- **WHEN** the startup full-hash pass meets a note whose task extraction raises an exception
- **THEN** the pass SHALL commit, the run SHALL be recorded as successful, the note SHALL be indexed with `tasks_failed = true`, and no row SHALL be pruned on its account

#### Scenario: The manual reindex applies the same isolation

- **WHEN** the panel's manual reindex meets a note whose task insert fails with SQLSTATE 54000 twice
- **THEN** the reindex SHALL commit with that note indexed and `tasks_failed = true`, and `/health` SHALL NOT count a quarantined note for it

#### Scenario: A failure the retry does not reproduce leaves no mark

- **WHEN** a note's task insert fails once with a poison SQLSTATE and succeeds on the retry
- **THEN** the note SHALL have its task rows and `tasks_failed = false`

#### Scenario: A later success clears the mark

- **WHEN** a note with `tasks_failed = true` is edited so that its task derivation succeeds, and the next pass commits
- **THEN** its task rows SHALL be present and `tasks_failed` SHALL be false

#### Scenario: A persistent task failure does not hold a re-derive open

- **WHEN** a re-deriving pass (#311) derives every note and one note's task derivation fails twice
- **THEN** that note SHALL still be marked derived under the current root, and the re-derive SHALL be recorded if nothing else withheld it
