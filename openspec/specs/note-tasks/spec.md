# note-tasks Specification

## Purpose
Checkbox tasks in note bodies as derived index state (`note_tasks`, #350): the shared fence-aware extraction grammar, the task rows' lifecycle beside the link rebuild, and the `list_tasks` tool's arguments, ordering, owner-bound cursor, rendering and bounds.
## Requirements
### Requirement: The indexer SHALL extract checkbox list items from note bodies with one shared, fence-aware grammar

The indexer SHALL derive a note's tasks from its post-frontmatter body with a single extractor in `src/services/tasks.py`. A line is a task when, at its start, it holds optional blockquote prefixes (`>`), any horizontal indentation, a list marker (`-`, `*`, `+`, or 1–9 digits followed by `.` or `)`), horizontal whitespace, and a bracketed one-code-point status marker that is either U+0020 or a non-whitespace character other than `[` and `]`, followed by horizontal whitespace or the end of the line. Lines SHALL be split under the universal-newline rule. Recognition SHALL run on the text produced by the shared code-masking grammar (`mask_code`, `BODY` context), and a line SHALL NOT be a task when the original and masked text differ anywhere from the line start through the closing `]`. The extractor SHALL NOT evaluate Tasks-plugin or Dataview query blocks, which are literal text to it. Its patterns SHALL be linear-time (possessive or line-anchored, with no class that can be re-entered by backtracking), and it SHALL run off the event loop. Any change to this grammar SHALL bump `CURRENT_EXTRACTION_VERSION` in the same change.

#### Scenario: Open and done checkboxes are tasks

- **WHEN** a note body holds `- [ ] write report` on line 3 and `* [x] send invoice` on line 4
- **THEN** the extractor SHALL return two tasks, with markers ` ` and `x` and texts `write report` and `send invoice`

#### Scenario: A checkbox in a fenced block is not a task

- **WHEN** a note body holds a fenced code block (backtick or tilde) whose content includes `- [ ] not a task`
- **THEN** that line SHALL NOT produce a task

#### Scenario: A checkbox decided by inline-code masking is not a task

- **WHEN** a line reads `` `- [ ]` example syntax ``
- **THEN** it SHALL NOT produce a task

#### Scenario: Indented, ordered and callout tasks are tasks

- **WHEN** a body holds `    - [ ] subtask`, `\t- [ ] tabbed`, `1. [ ] numbered` and `> - [ ] in a callout`
- **THEN** each SHALL produce one task

#### Scenario: Shapes that are not checkboxes

- **WHEN** a body holds `- [ ]no space`, `[ ] no bullet`, `- [xx] two chars`, `- [\t] tab marker` and `-[ ] no gap`
- **THEN** none SHALL produce a task

#### Scenario: A Tasks-plugin query block is not evaluated

- **WHEN** a note contains a fenced `tasks` query block reading `not done` and wikilinks to other notes
- **THEN** the extractor SHALL return no task for that note, and no task SHALL be derived from the linked notes on its account

#### Scenario: The extractor is linear on adversarial input

- **WHEN** the extractor runs on a 10 MiB body of repeated `> > - [` or `- [ ] ` fragments
- **THEN** it SHALL complete within the bound the unit test sets, and the task count SHALL respect the per-note cap

### Requirement: A task's status marker SHALL map to exactly one of open, done or cancelled, and the raw marker SHALL be kept

Each task SHALL store its raw marker character and a derived state: ` ` (space), `/` and every other permitted marker not listed below SHALL map to `open`; `x` and `X` SHALL map to `done`; `-` SHALL map to `cancelled`.

#### Scenario: Status mapping

- **WHEN** a note holds `- [ ] a`, `- [x] b`, `- [X] c`, `- [-] d`, `- [/] e` and `- [>] f`
- **THEN** their states SHALL be `open`, `done`, `done`, `cancelled`, `open`, `open` respectively, with the raw markers stored as written

### Requirement: Tasks-plugin fields SHALL be parsed from the task line, and an unparseable value SHALL NOT fail the note

For each task the extractor SHALL parse the due (📅, 📆, 🗓), scheduled (⏳, ⌛), start (🛫) and done (✅) dates, each written as the signifier, an optional U+FE0F, optional horizontal whitespace, and `YYYY-MM-DD` not followed by a digit. It SHALL parse the priority (🔺 highest, ⏫ high, 🔼 medium, 🔽 low, ⏬ lowest, optional U+FE0F) and the inline tags (the shared inline-tag rule): a tag longer than 128 characters SHALL NOT be stored, the first 64 distinct remaining tags in document order SHALL be stored in document order, and the row's `tags_cut` SHALL be true exactly when any tag on the line was not stored for either reason. Fields SHALL be read from the masked text of the line, so a signifier inside inline code is not a field. When a family occurs more than once, the last occurrence SHALL win. A date that is not a valid calendar date, or a signifier with no date, SHALL store NULL for that field, and the task SHALL still be stored with its full text. A field value SHALL never cause an extraction error or a quarantine. With no priority signifier the priority SHALL be NULL.

#### Scenario: Dates, priority and tags are parsed

- **WHEN** a task reads `- [ ] ship 📅 2026-11-01 ⏳ 2026-10-30 🛫 2026-10-28 ⏫ #work #alpha`
- **THEN** due SHALL be 2026-11-01, scheduled 2026-10-30, start 2026-10-28, priority `high`, and tags `work`, `alpha` in that order, with `tags_cut` false

#### Scenario: An invalid date stores NULL and keeps the text

- **WHEN** a task reads `- [ ] pay 📅 2026-02-30`
- **THEN** the task SHALL be stored with due NULL and text `pay 📅 2026-02-30`
- **AND** the note SHALL be indexed normally, with no quarantine and no skip

#### Scenario: A signifier inside inline code is not a field

- **WHEN** a task reads ``- [ ] document the `📅 2026-01-01` syntax``
- **THEN** its due date SHALL be NULL

#### Scenario: The tag cap keeps the first 64 distinct tags and says so

- **WHEN** a task line carries 65 distinct inline tags `#t01` … `#t65`, with `#t01` repeated at the end
- **THEN** the stored tags SHALL be `t01` … `t64` in document order, `tags_cut` SHALL be true, and `t65` SHALL NOT be stored

#### Scenario: An over-long tag is not stored and is flagged

- **WHEN** a task line carries `#ok` and a 129-character tag
- **THEN** the stored tags SHALL be `ok` and `tags_cut` SHALL be true

#### Scenario: The last occurrence wins

- **WHEN** a task reads `- [ ] move 📅 2026-01-01 then 📅 2026-02-01`
- **THEN** its due date SHALL be 2026-02-01

### Requirement: Task text, heading path and per-note count SHALL be bounded, and a capped note SHALL be marked

Each task's text SHALL be the original line after the marker and its following whitespace, with trailing whitespace removed and with fields left in place, cut at `TASK_TEXT_MAX_CHARS` (1,000) code points, and a cut SHALL be recorded on the row. Each task SHALL store the path of column-zero ATX headings above it in the masked body (the shared heading rule), joined by ` > `, each heading cut at 200 code points and the whole at 512, or NULL when no heading precedes it. Each task SHALL store its 1-based line number counted from the first line of the file, frontmatter included. Extraction SHALL keep at most `MAX_TASKS_PER_NOTE` (5,000) tasks per note in document order. When the cap bites, `notes_metadata.tasks_truncated` SHALL be set in the same transaction and one ERROR line SHALL name the note. When a later extraction completes under the cap, the marker SHALL be cleared. A capped note SHALL NOT be a skip and SHALL NOT withhold a re-derive's record.

#### Scenario: Line numbers count the frontmatter

- **WHEN** a note has a 4-line frontmatter block (delimiters included) and a task on the second line of the body
- **THEN** the task's line SHALL be 6

#### Scenario: Heading path

- **WHEN** a task follows `# Sprint`, then `## Backend`, then `### API` and then `## Frontend`
- **THEN** its heading path SHALL be `Sprint > Frontend`

#### Scenario: A note over the cap

- **WHEN** a note holds 5,001 checkbox lines
- **THEN** 5,000 task rows SHALL be stored, `tasks_truncated` SHALL be true, and the pass SHALL complete without a skip

### Requirement: Task rows SHALL be derived state that is written only with their note's row and can never outlive it

The index pass SHALL store tasks in `note_tasks`, keyed by `note_id` with a foreign key to `notes_metadata.id` `ON DELETE CASCADE`, and SHALL NOT store a path or owner on the task row. For every changed path of a pass (the upserted and the moved paths), in the same transaction as the note's row and immediately after that note's link rebuild, the pass SHALL delete the note's task rows and insert the rows extracted from the body the scan buffered. It SHALL NOT read the file again. A changed path with no buffered body or no index row SHALL be recorded as a skip that withholds a re-derive's record. Each note's task deletion and insertion SHALL run in their own savepoint, and a task-derivation failure SHALL be isolated to that note's task rows as the `index-integrity` requirement on task-derivation isolation states. Deleting a note's row by any route (prune, quarantine removal, provenance discard, user deletion) SHALL remove its task rows. A row protected from the prune by an unlistable directory SHALL keep its task rows. An id-preserving move SHALL leave the rows attached to the moved note. `CURRENT_EXTRACTION_VERSION` SHALL be 4, with version 4 sharing version 3's embedding cleaner so that the bump re-embeds nothing.

#### Scenario: Editing a note replaces its tasks

- **WHEN** a note with tasks A and B is edited to hold tasks B and C, and the next pass commits
- **THEN** `note_tasks` SHALL hold exactly B and C for that note

#### Scenario: Deleting a note removes its tasks

- **WHEN** a note with tasks is deleted from disk and the next pass commits
- **THEN** no task row SHALL reference it

#### Scenario: A moved note keeps its tasks under the new path

- **WHEN** a note with tasks is renamed on disk (same content) and the pass's id-preserving move runs
- **THEN** `list_tasks` SHALL list those tasks under the new path and never under the old one

#### Scenario: A failing task insert costs only that note's task rows

- **WHEN** one note's task insert fails with a poison SQLSTATE on the attempt and on its retry
- **THEN** that note SHALL keep its row, keyword vector, links and embeddings, SHALL have no task rows and `tasks_failed = true`, SHALL NOT be quarantined, and every other note of the pass SHALL be committed with its tasks

#### Scenario: An unlistable directory protects its tasks

- **WHEN** a directory holding indexed notes with tasks cannot be listed during a pass
- **THEN** those notes' rows and task rows SHALL remain

#### Scenario: The version bump backfills without re-embedding

- **WHEN** a vault indexed at version 3 is indexed by this build
- **THEN** every note SHALL gain its task rows and be stamped 4, and no note's `embedded_content_hash` SHALL be cleared on account of the bump

### Requirement: `list_tasks` SHALL answer task queries from the index in one bounded, owner-scoped call

The MCP server SHALL expose a read tool `list_tasks(folder=None, status="open", due_by=None, scheduled_by=None, tags=None, limit=100, cursor=None)`, registered with a title and the shared read-only annotations and wrapped by `_tracked`. It SHALL read only the index and never a file. `status` SHALL be one of `open`, `done`, `cancelled`, `all`, where `open` excludes cancelled tasks, `cancelled` returns only them, and `all` returns every state. `due_by` and `scheduled_by` SHALL mean on or before the given date. `due_by` and `scheduled_by` SHALL be `YYYY-MM-DD` dates, inclusive, and SHALL exclude tasks that have no date of that kind. `folder` SHALL be a path prefix with the same semantics as the shared note filter. `tags` SHALL require every listed tag (a leading `#` stripped) among the task's own tags, and the tool body SHALL refuse with `invalid_argument`, before any query, a `tags` list of more than 32 entries, or any entry that is not a string or is empty or longer than 128 characters after the `#` is stripped. `limit` SHALL be clamped to 1–500. Any invalid argument SHALL be refused with an `MCP-REFUSAL` line carrying `invalid_argument`. Results SHALL be ordered by due date ascending with undated tasks last, then by path, then by line. Rows SHALL be restricted by joining `notes_metadata` under the total owner mapping, so that a caller with no user identifier sees only NULL-owned notes' tasks, and a named user only their own. The page, the counts and the cursor SHALL be read in one consistent snapshot.

#### Scenario: Open tasks across the vault in one call

- **WHEN** 21 notes hold 120 open and 300 done tasks and `list_tasks()` is called
- **THEN** one call SHALL return the first 100 open tasks in order with a `next` cursor, and no `read_note` SHALL be needed

#### Scenario: Cancelled tasks are excluded from open and included in all

- **WHEN** a note holds `- [ ] a`, `- [/] b`, `- [-] c` and `- [x] d`
- **THEN** `list_tasks(status="open")` SHALL return `a` and `b` only
- **AND** `list_tasks(status="cancelled")` SHALL return `c` only, rendered with the marker `[-]`
- **AND** `list_tasks(status="all")` SHALL return all four, each line showing its raw marker (`[ ]`, `[/]`, `[-]`, `[x]`)

#### Scenario: Due filter is inclusive

- **WHEN** tasks are due 2026-10-28, 2026-10-29 and 2026-10-30, and one has no due date, and `list_tasks(due_by="2026-10-29")` is called
- **THEN** exactly the first two SHALL be returned

#### Scenario: Invalid arguments are refused

- **WHEN** `list_tasks(status="pending")` or `list_tasks(due_by="tomorrow")` is called
- **THEN** the response SHALL end with `MCP-REFUSAL {"code":"invalid_argument"…}`

#### Scenario: Tag argument bounds are enforced in the body

- **WHEN** `list_tasks` is called with 33 tags, or with a tag of 129 characters, or with `tags=["#"]`
- **THEN** each call SHALL be refused with `invalid_argument` naming the bound, and no query SHALL run
- **AND** a call with 32 tags of 128 characters each SHALL be accepted

#### Scenario: A tag filter miss caused by the cap is reported

- **WHEN** a task carries 65 distinct tags and `list_tasks(tags=["t65"])` is called
- **THEN** that task SHALL NOT be returned, and the header SHALL report `tasks with cut tags: 1`
- **AND** a `tags=` call where no matching-scope row has cut tags SHALL report `tasks with cut tags: 0`

#### Scenario: Tenancy

- **WHEN** NULL-owned, user-7 and user-8 notes all hold open tasks, and `list_tasks` runs as user 7
- **THEN** only user 7's tasks SHALL appear, and running with no user identifier SHALL return only the NULL-owned tasks

#### Scenario: Folder and tag filters

- **WHEN** `list_tasks(folder="Projects/", tags=["#work"])` is called
- **THEN** only tasks in notes under `Projects/` that carry the inline tag `work` SHALL be returned

### Requirement: `list_tasks` SHALL page with a keyset cursor bound to its filters and SHALL never exceed the read-response cap

When more rows match than were rendered, the response SHALL say so and SHALL end with a `next:` cursor that encodes an owner-scope discriminator, a digest of the call's normalised filters, and the sort key `(due, path, line)` of the last rendered row; the due component SHALL never be null, an undated row's due key SHALL be PostgreSQL `'infinity'::date` (encoded as `infinity`) on both the cursor and the ordering side, the path SHALL be carried either verbatim or, when the verbatim form would not fit the remaining response room or the 2,048-character argument cap, as a hash resolved only against the caller's own rows, and neither the cursor nor any rendered line SHALL contain a `notes_metadata.id` or other database-global identifier. A call with a cursor SHALL return rows strictly after that key. A cursor that does not decode, has the wrong version or shape, carries an owner discriminator other than the caller's, was issued for different filters, or (in hashed form) names a path the caller no longer has indexed SHALL be refused with `invalid_argument`. The cursor argument SHALL be capped at 2,048 characters. Rendering SHALL stop before the response would exceed `MAX_READ_RESPONSE_CHARS`, and the cursor SHALL then be issued from the last row actually rendered, so that no matching row is skipped between pages of an unchanged index.

#### Scenario: Paging reaches every row

- **WHEN** 1,234 open tasks exist and a caller follows `next` until none is returned
- **THEN** the union of the pages SHALL be exactly the 1,234 tasks, each once, on an index that did not change between calls

#### Scenario: A page boundary between two undated tasks

- **WHEN** three undated open tasks exist and `list_tasks(limit=1)` is called, followed by its `next` cursor twice
- **THEN** the three calls SHALL return the three tasks, one each, in order, and the third response SHALL carry no cursor

#### Scenario: The character cap stops a page early

- **WHEN** 500 tasks of 900-character text match and `limit=500`
- **THEN** the response SHALL be at most `MAX_READ_RESPONSE_CHARS` characters, SHALL say `more: yes`, and its cursor SHALL resume at the first unrendered task

#### Scenario: A cursor from another owner is refused

- **WHEN** user 7 obtains a cursor from `list_tasks(limit=1)` and user 8 (or an ownerless caller) passes it with the same filters
- **THEN** the call SHALL be refused with `invalid_argument` and SHALL return no rows

#### Scenario: No global identifier is exposed

- **WHEN** a cursor is decoded and every line of a response is inspected, including an abbreviated row with a cut path
- **THEN** neither SHALL contain the `notes_metadata.id` of any note

#### Scenario: A long path still pages

- **WHEN** the boundary task's path is 1,024 characters of three-byte UTF-8 characters, or `MAX_READ_RESPONSE_CHARS` is 1,000
- **THEN** the issued cursor SHALL be at most 2,048 characters and fit the response, and following it SHALL return the next task

#### Scenario: A cursor from another query is refused

- **WHEN** a cursor issued for `status="open"` is passed with `status="done"`
- **THEN** the call SHALL be refused with `invalid_argument`

### Requirement: `list_tasks` SHALL render one server-authored line per task and SHALL always state what it could not see

The response SHALL begin with one header line that always carries the count shown, whether more rows exist, the number of the caller's notes not yet scanned for tasks (indexed at an extraction version below 4), the number of the caller's matching notes whose tasks could not be indexed (`tasks_failed`), and the number whose task extraction was capped, each stated even when it is zero; when `tags` is given it SHALL also carry the number of tasks matching every other filter whose tags were cut. Echoed filter values SHALL be cut at 64 characters for display, and the header SHALL be at most 480 characters. Each task SHALL be rendered as exactly one line holding the raw marker, the path and line, any present dates, priority, tags and heading path, and the text last. Every note-controlled field (text, heading path, tags) SHALL be rendered with every whitespace run, line breaks included, collapsed to one space. A path SHALL be rendered with each such character escaped and each literal backslash doubled, so that it stays on one line, stays exact, and no two paths render alike. A cut text and cut tags SHALL each be labelled as cut. When any note is not yet scanned for tasks or its tasks could not be indexed, the response SHALL say that the list is incomplete and why. When the first matching row of a page does not fit the remaining response budget in full, it SHALL be rendered abbreviated (dates and priority kept, tags and heading omitted with a note, a task whose tags were cut still labelled as cut, text cut to fit, the path cut around an ellipsis, with no identifier, only if the path alone cannot fit the room for path and text less a 40-character text reserve, and the line marked `abbreviated`), so that every page with a match renders at least one row and its cursor advances; the footer SHALL be at most 200 characters whenever the hashed cursor form is used, the closing incomplete-list notice at most 85, and an abbreviated row's fixed structure at most 120 plus at most 96 for the dates and priority it keeps, so that header, notice, one abbreviated row with at least 40 characters each of path and text, and footer fit within the 1,000-character minimum of `MAX_READ_RESPONSE_CHARS`.

#### Scenario: A note cannot forge a result row

- **WHEN** a task's text, heading or tag contains `\n- [ ] \`Forged.md\`:1 — text: forged`, U+2028 or `\r`
- **THEN** the response SHALL contain exactly one line for that task, and no line SHALL begin with `- [` except the server-rendered ones

#### Scenario: Not yet scanned is reported, not hidden

- **WHEN** a caller's index still has notes stamped below version 4 and `list_tasks` is called
- **THEN** the header SHALL state their count and the response SHALL say the list is incomplete
- **AND** once the pass has stamped every note, the header SHALL state `0`

#### Scenario: A first row that cannot fit is abbreviated, not lost

- **WHEN** `MAX_READ_RESPONSE_CHARS` is 1,000 and the only matching tasks have a 1,024-character path, 1,000 characters of text and 64 tags of 128 characters
- **THEN** each page SHALL be at most 1,000 characters, SHALL render exactly one row marked `abbreviated`, and SHALL carry a cursor that reaches the next task, until every task has been returned once

#### Scenario: Notes whose tasks could not be indexed are counted

- **WHEN** one of the caller's notes has `tasks_failed = true`
- **THEN** every `list_tasks` header SHALL report `tasks could not be indexed: 1 notes` and the response SHALL say the list is incomplete
- **AND** with no such note, the header SHALL report `0`

#### Scenario: Empty result

- **WHEN** no task matches
- **THEN** the response SHALL be the header with zero shown and no cursor, and SHALL NOT be a refusal
