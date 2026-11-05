## ADDED Requirements

### Requirement: Migration 032 SHALL own the task table and the task-truncation column as marked units

Migration 032 (`down_revision` `"031"`) SHALL create `note_tasks` with these columns: `id` integer primary key; `note_id` integer NOT NULL, foreign key to `notes_metadata.id` `ON DELETE CASCADE`; `line` integer NOT NULL; `status_char` varchar(1) NOT NULL; `state` varchar(16) NOT NULL with a CHECK limiting it to `open`, `done`, `cancelled`; `text` text NOT NULL; `text_cut` boolean NOT NULL default false; `due`, `scheduled`, `start` and `done_on` as nullable `date`; `priority` varchar(8) NULL with a CHECK limiting it to `highest`, `high`, `medium`, `low`, `lowest`; `tags` varchar(128)[] NULL; `tags_cut` boolean NOT NULL default false; and `heading_path` text NULL. The table SHALL have a unique constraint on `(note_id, line)`, an index on `note_id` and an index on `(state, due)`. It SHALL carry the migration's ownership marker as its table comment. Migration 032 SHALL also add `notes_metadata.tasks_truncated BOOLEAN NOT NULL DEFAULT false` and `notes_metadata.tasks_failed BOOLEAN NOT NULL DEFAULT false`, each with its own column marker. Every name, type, constraint, index and marker SHALL be mirrored in `src/models/db.py` so that `alembic check` is clean. The migration SHALL write no data. A same-named table or column of a different shape SHALL be refused by name, and one of the exact shape carrying the marker SHALL be reconciled without change. It SHALL set `lock_timeout` and `statement_timeout` and `RESET` them before returning. `downgrade()` SHALL drop the table and the column only where they carry the marker. `note_tasks` SHALL be classified as a derived table: `make db-backup` and the Kubernetes backup Job SHALL dump it schema-only, and `tests/test_backup_derived_tables.py` SHALL list it.

#### Scenario: Fresh upgrade

- **WHEN** a database at 031 is upgraded to 032
- **THEN** `note_tasks` SHALL exist empty with its constraints, indexes and marker, and every `notes_metadata` row SHALL read `tasks_truncated = false` and `tasks_failed = false` without a table rewrite

#### Scenario: Stamp-back re-runs cleanly

- **WHEN** the schema gate stamps the database back to 031 and upgrades again
- **THEN** 032 SHALL reconcile the marked table and column without error and write nothing

#### Scenario: An impostor is refused

- **WHEN** a `note_tasks` table without the marker, or a `tasks_truncated` or `tasks_failed` column of another type, already exists
- **THEN** the migration SHALL fail naming the object and change nothing

#### Scenario: The cascade holds

- **WHEN** a `notes_metadata` row with task rows is deleted
- **THEN** its task rows SHALL be deleted in the same statement

#### Scenario: The schema agrees with the model

- **WHEN** `make test-schema` runs, and `make db-check` runs after deploy
- **THEN** both SHALL report "No new upgrade operations detected"

#### Scenario: Migrations run as a non-superuser

- **WHEN** `test_nonsuperuser_migrations_pg.py` runs 032 as the database-owning, non-superuser role
- **THEN** the migration SHALL succeed
