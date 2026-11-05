## ADDED Requirements

### Requirement: Migration 033 SHALL add `content_changed_at` and the feed tables as marked, reconcilable objects
Migration 033 (revising 032) SHALL add `notes_metadata.content_changed_at timestamptz`, backfill it with `COALESCE(modified_at, indexed_at)`, and then make it `NOT NULL` with `DEFAULT now()`. It SHALL create three tables, with the columns, CHECK constraints, unique constraints and indexes of the design, and **no foreign key on any of them**:
- `note_events`, with a `bigserial` surrogate key, a plain nullable `user_id`, a `seq bigint NOT NULL`, and `UNIQUE (user_id, seq) NULLS NOT DISTINCT`;
- `note_event_scopes`, with `user_id` unique `NULLS NOT DISTINCT`, `last_seq`, `pruned_through_seq` and `pruned_at`;
- `note_event_gaps`.

It SHALL put a marker comment byte-identical to the model's on the new column and on each table. A re-run against a database already carrying a same-named object SHALL verify its shape and refuse an unmarked or differently shaped one by name. `downgrade()` SHALL drop only marked objects. After upgrade, `alembic check` SHALL report no new upgrade operations.

#### Scenario: Backfill
- **WHEN** 033 runs over rows with `modified_at` set and rows with `modified_at` NULL
- **THEN** every row SHALL have `content_changed_at` equal to its `modified_at`, or to its `indexed_at` where `modified_at` was NULL, and the column SHALL be `NOT NULL`

#### Scenario: Constraints asserted by the schema gate
- **WHEN** the schema gate inspects `pg_constraint` for the three feed tables
- **THEN** `UNIQUE (user_id, seq)` and the scope uniqueness SHALL be present with `NULLS NOT DISTINCT`, the CHECK predicates SHALL match the design, and no foreign key SHALL exist

#### Scenario: Clean drift check
- **WHEN** `make test-schema` runs migrations to head and `alembic check`
- **THEN** both SHALL pass with no new upgrade operations

### Requirement: The three feed tables SHALL be classified as durable for backups
`note_events`, `note_event_scopes` and `note_event_gaps` SHALL be dumped with their rows by `make db-backup` and the Kubernetes backup Job. They SHALL appear in the durable list that `tests/test_backup_derived_tables.py` pins: their history, attributions, sequence counters and retention watermarks cannot be rebuilt from the vault, and a cursor stays valid across a restore only if events and counters are restored together.

#### Scenario: Backup classification
- **WHEN** `tests/test_backup_derived_tables.py` runs
- **THEN** it SHALL pass with the three tables in the durable list and none in the schema-only list
