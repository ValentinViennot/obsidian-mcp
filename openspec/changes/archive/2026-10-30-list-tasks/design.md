## Context

### What the code does today (verified on 6bcc9d9)

- **Links are the only per-note derived table.** `_update_links_for_changed` (`src/services/indexer.py:4279`) runs inside the index pass's transaction, after the batch upsert, the keyword vectors and the prune. It reads bodies **only** from `path_to_content`, the buffer the scan fills (`indexer.py:3361`), and never re-reads a file. For the changed set (`to_upsert` paths plus `moved_new_paths`) it deletes the old rows with one `DELETE … WHERE source_note_id IN (…)`, then extracts and inserts each note before moving to the next one (#203 D4). With `poisoned=` set, each note's inserts run in a savepoint (`_savepoint_poison`) and are confirmed by one retry (`_confirm_poison`, site `"links"`). A poison note is appended, and the caller raises `PoisonNote` so the attempt is restarted without it (`indexer.py:3893`). A changed path with no buffered body or no index row goes into `skips` and `skipped_paths` (`underived`), which withholds a re-derive's stamp. A capped note sets `links_truncated` in the same transaction, and the cap is not a skip.
- **A note's derived rows die with the note through `ON DELETE CASCADE`.** The prune (`indexer.py:3870`), the quarantine removal (a quarantined path becomes "not indexable", so its row is pruned), the provenance discard (`indexer.py:2558`) and a user delete all remove `notes_metadata` rows. `note_links.source_note_id` and `note_embeddings.note_id` cascade. Rows beneath a directory the walk could not list are protected from the prune (#309, `_walk_protected`), so their derived rows survive too.
- **Moves preserve the row id.** The pass's id-preserving move UPDATE (`indexer.py:~3530`) and `move_note`'s index UPDATE (`tools.py:5568`) both rewrite `file_path` in place. Anything keyed by `notes_metadata.id` follows the move without a write.
- **Backfill through the extraction marker.** `CURRENT_EXTRACTION_VERSION = 3` (`indexer.py:764`). A row stamped lower is treated as changed for the whole pass: it is parsed, re-tagged, re-linked, re-tsvectored and re-stamped. `clean_at_version` (`src/services/embeddings.py:213`) maps each version to its embedding cleaner. Versions 2 and 3 alias version 1's cleaner, so neither bump re-embedded anything. `move_note(rewrite_links=True)` refuses with `index_not_ready` while any of the caller's rows is behind (`_stale_extraction_error`, `tools.py:4827`).
- **The fence grammar** is `scan_fences` / `mask_code` in `src/services/links.py`. Masking is same-length, so offsets into the masked text are valid against the original. Inline code is masked as well, `BODY` context. 4-space-indented code blocks are deliberately not masked. Container blocks are not parsed, so a fence inside a list item is computed flat. The #218 rule: a link whose deciding span differs between the masked and the original text is not a link.
- **Shared derivation rules for tags and headings**: inline tags come from `extract_tags`'s regex `(?:^|\s)#([a-zA-Z][a-zA-Z0-9_/-]*)` over masked text, bounded by `MAX_TAG_BYTES` (`src/services/vault.py:4031`). Headings come from `_ATX_HEADING_RE`, column zero only (`vault.py:3630`).
- **Read-tool shape**: `_tracked(name, params, …)` resolves the vault root and runs the admission gates. `_note_owner_predicate(uid)` is the total owner mapping. `_clamp_limit` caps at `_MAX_QUERY_RESULTS` (500). `settings.max_read_response_chars` (40,000) bounds read output. `_one_line` collapses every whitespace run to one space so that a note cannot forge a server-authored line (#344, Codex C-2). Refusals end with `MCP-REFUSAL {"code":…}`, and `invalid_argument` and `index_not_ready` already exist in `src/services/refusals.py`.
- **Tool metadata** (#343): `_READ_ONLY` in `server.py:115`. `tests/test_issue_343_tool_metadata.py` pins 25 tools and the fifteen-name `READ_TOOLS` set. `concurrency.TOOL_CLASSES` must name every tool. A change to it bumps `CLASS_MAPPING_VERSION` (2), which is part of the readiness epoch.
- **The vault guide**: `src/mcp_server/vault_guide_primer.md` has a `## Tasks` section showing checkbox syntax and nothing about querying. The primer does **not** tell agents to use Grep. The issue's "the vault guide tells agents to use Grep" must refer to the owner's vault `CLAUDE.md` (the vault-specific half of `get_vault_guide`), which this repo does not ship.

### Tasks-plugin syntax (https://publish.obsidian.md/tasks/Getting+Started/Dates)

Date fields are a signifier emoji followed by `YYYY-MM-DD`, the only recognised form: due 📅, scheduled ⏳, start 🛫, created ➕, done ✅, cancelled ❌. Priorities are 🔺 highest, ⏫ high, 🔼 medium, 🔽 low, ⏬ lowest, and no emoji means "normal". The plugin reads fields from the end of the line. Statuses: `[ ]` todo, `[x]` done, `[/]` in progress, `[-]` cancelled, plus user-defined symbols, which the plugin treats as todo unless configured.

## Goals / Non-Goals

**Goals:**

- Answer "what is open / due / scheduled" for a whole vault or folder in one bounded call that never reads a file.
- Make task rows derived state with exactly the lifecycle of `note_links`. They are written and removed in the same transaction as the note row and can never be more current, or less current, than it.
- Leave a seam that a third per-note derivation (`change-feed`, #351) can join without editing the link or task code paths.
- Never report "no open tasks" when the truth is "not yet scanned".

**Non-Goals:**

- Toggling, creating or editing tasks. `edit_note` remains the only way to change one.
- Evaluating Tasks-plugin or Dataview query blocks, recurrence (🔁) expansion, dependencies (🆔 ⛔), and the created or cancelled dates.
- Task-plugin global filters, custom status configuration, and per-vault settings.
- Parent/child task structure (nesting is visible only through the line number).
- Freshness better than the index: `list_tasks` reflects the last committed pass (D9).

## Decisions

### D1. Table shape: keyed by note id, no path, no owner

```
note_tasks
  id             integer PK
  note_id        integer NOT NULL FK notes_metadata.id ON DELETE CASCADE
  line           integer NOT NULL        -- 1-based line in the file
  status_char    varchar(1) NOT NULL     -- the raw marker character
  state          varchar(16) NOT NULL    -- 'open' | 'done' | 'cancelled' (CHECK)
  text           text NOT NULL           -- ≤ TASK_TEXT_MAX_CHARS code points
  text_cut       boolean NOT NULL DEFAULT false
  due            date NULL
  scheduled      date NULL
  start          date NULL
  done_on        date NULL
  priority       varchar(8) NULL         -- 'highest'|'high'|'medium'|'low'|'lowest' (CHECK)
  tags           varchar(128)[] NULL     -- first 64 distinct inline tags, document order
  tags_cut       boolean NOT NULL DEFAULT false
  heading_path   text NULL               -- "H1 > H2 > H3", ≤ 512 chars
  INDEX ix_note_tasks_note (note_id)
  INDEX ix_note_tasks_state_due (state, due)
  UNIQUE (note_id, line)
notes_metadata.tasks_truncated boolean NOT NULL DEFAULT false   -- per-note cap bit
notes_metadata.tasks_failed    boolean NOT NULL DEFAULT false   -- derivation failed (D4)
```

**Why no `file_path` or `user_id` column**: the owner asked that a task row never outlive or precede its note. Duplicated path or owner columns would need their own writers on every move path (the pass's id-preserving branch and `move_note`'s), and any writer that forgot one would leave a task listed under a path that no longer exists, or worse, under another tenant's owner. Joining `notes_metadata` on `note_id` makes both impossible by construction. The FK cascade covers prune, quarantine removal, provenance discard and user deletion. The FK itself means a task cannot be inserted before its note. The join costs nothing at this scale (thousands of rows). **Alternative considered**: denormalised `file_path` with the trigram index `list_notes` would want. Rejected because it adds a third move writer.

**Why `content_hash` is not stored on the task**: every task write happens in the same transaction as the upsert that wrote the note's `content_hash`, from the same buffered body. The rows therefore always describe the row's committed hash, and a second copy of the hash could only disagree. The owner's "keyed by path and content hash" is satisfied through the parent row: a derivation is a pure function of `(body, extraction version)`, and it is redone exactly when the row's hash or version changes.

**Why `tasks_failed` is a boolean on the note, not a hash**: it is written only by the task stage, in the same transaction as the derivation attempt for the row's current hash. It is set when that attempt failed (D4) and cleared when a later attempt succeeds. A row's hash cannot change without the row entering the changed set, and entering the changed set always runs the task stage, which either clears the bit or sets it again. So the bit always describes the current hash, and a stored hash would only duplicate that.

`UNIQUE (note_id, line)` holds because there is at most one checkbox per line. It also catches a double-insert bug in the seam.

### D2. Status mapping

| Marker | `state` | Why |
| --- | --- | --- |
| `[ ]` | `open` | todo |
| `[x]`, `[X]` | `done` | the plugin and Obsidian both treat these as complete |
| `[-]` | `cancelled` | the plugin's CANCELLED type. It is neither open nor done, so "what is open" excludes it |
| `[/]` | `open` | the plugin's IN_PROGRESS is a todo-type status, and an agent asking what is open wants in-progress work |
| any other single non-whitespace character except `[` and `]` | `open` | the plugin's default for an unconfigured symbol is TODO. Common customs (`[>]` forwarded, `[!]`, `[?]`) are listed as open with their raw marker shown |

`status_char` keeps the raw marker, and every rendered line shows it (`[/]`, `[>]`), so an agent can tell in-progress from todo without a second call. The tool's `status` argument is `open` | `done` | `cancelled` | `all`. **Alternative considered**: a fourth `in_progress` state. Rejected because it would split "what is open" for no benefit when the marker is already displayed. A whitespace marker other than U+0020 (`[\t]`, an NBSP) is not a checkbox.

### D3. Extraction grammar (`src/services/tasks.py`)

1. **Input**: the post-frontmatter body from `path_to_content`, plus that note's `body_line_offset` (the number of line terminators in the frontmatter block, under the universal-newline rule). The scan stores the offset beside the body at `indexer.py:3361`. `line = body_line + body_line_offset`, so the line number counts from the file's first line.
2. `masked = mask_code(body, context=BODY)`. Lines are split under the universal-newline rule (`\r\n`, `\n`, `\r`), the same split the fence grammar uses.
3. **Checkbox shape**, matched on the **masked** line at its start with possessive quantifiers only (linear, no backtracking into a class):
   `^(?:[ \t]*+>)*+[ \t]*+(?:[-*+]|[0-9]{1,9}[.)])[ \t]++\[(?P<s> |[^\s\[\]])\](?=[ \t]|$)`
   That is: optional blockquote or callout prefixes, any indentation (4+ spaces and tabs included, because nested subtasks are indented that way and the fence grammar does not mask indented code), a bullet or ordered marker, whitespace, and a one-code-point status in brackets followed by whitespace or end of line.
4. **The #218 rule for tasks**: the original and masked text must be identical from the line start through the closing `]`. Otherwise the marker was decided by masking (for example `` `- [ ]` `` inside inline code, or a fenced span) and the line is not a task.
5. **Text**: the original line after the `]` and its following whitespace, through the line's last character, with trailing whitespace stripped. The cap is `TASK_TEXT_MAX_CHARS` (1,000) code points, and `text_cut` records whether it bit. Fields are kept inside the text, so the agent sees what the person wrote.
6. **Fields** are parsed from the **masked** slice of the same span, so an emoji inside inline code is not a field. For each family, the **last** occurrence on the line wins, which matches the plugin's end-of-line parse on well-formed lines:
   - due `[📅📆🗓]`, scheduled `[⏳⌛]`, start `🛫`, done `✅`. Each may be followed by U+FE0F and horizontal whitespace, then `([0-9]{4})-([0-9]{2})-([0-9]{2})` not followed by a digit. The value is validated with `datetime.date`, and an invalid or out-of-range value (`2026-02-30`, `0000-01-01`) **stores NULL and keeps the text**. It is never an error and never quarantines the note. A signifier with no date stores NULL.
   - priority `🔺→highest ⏫→high 🔼→medium 🔽→low ⏬→lowest`, with an optional U+FE0F. If none is present, the column is NULL ("normal").
   - tags: the shared inline-tag regex over the masked slice. A tag longer than `TASK_TAG_MAX_CHARS` (128; the regex is ASCII-only, so this is also under `MAX_TAG_BYTES`) is not stored. The **first 64 distinct tags in document order** are kept, in document order. If any tag was dropped for either reason, the row's `tags_cut` is true. The rule is deterministic, so every implementation stores the same set, and a cut is never silent (D6, D7).
7. **Heading path**: column-zero ATX headings (`_ATX_HEADING_RE`, imported rather than copied) found in the masked text above the task, kept as a stack by level. The path is joined with ` > `, each heading's text is cut at 200 code points, the whole at 512, and it is NULL when the task is above every heading. **Stored, not derived**: deriving it at query time means reading the file, which the tool must never do (D9), or storing the heading list anyway.
8. **Per-note cap**: `MAX_TASKS_PER_NOTE` (5,000) in document order. When it bites, the call returns `(tasks, truncated=True)`, mirroring `extract_links_bounded`. Peak memory is one note's tasks.
9. Pure and synchronous, run via `asyncio.to_thread` like `extract_links_bounded`. The input is NUL-free by the #308 decode rule, and the extractor asserts that rather than repairing it.

Any change to this grammar is a `CURRENT_EXTRACTION_VERSION` bump, the same rule the fence and link grammars carry. The module docstring says so.

### D4. The seam in the pass: a second per-note derivation

`_update_tasks_for_changed(session, changed_paths, *, paths_to_id, bodies, line_offsets, skips, skipped_paths)` is called **immediately after `_update_links_for_changed`** at `indexer.py:3893`, after the caller has checked for link poison. It runs with the same changed list, inside the same `if to_upsert or deleted_paths or moved_new_paths:` block, and before the re-derive marks. It follows the link rebuild in shape, and departs from it in one place: **a task-derivation failure never fails, restarts or prunes anything but that note's task rows.**

- **Per note, in its own savepoint**, with batched inserts of 1,000: delete that note's `note_tasks` rows, then insert the extracted ones, then clear `tasks_failed` and set or clear `tasks_truncated`. Both flag UPDATEs are predicated on the value changing. A per-note DELETE replaces the link rebuild's single up-front DELETE, because the savepoint has to cover the delete as well. Otherwise rolling back a failed insert would leave the old rows, which describe the previous hash.
- **The failure rule.** A *derivation failure* is either an exception raised by the extractor (any `Exception`, which is a bug, since the grammar is total), or a database error with a poison SQLSTATE (`poison_sqlstate`: class 22 or 54000) raised by that note's task statements. On a derivation failure the savepoint is rolled back, so no partial task rows survive. The note's write is then run once more in a fresh savepoint, mirroring `_confirm_poison`'s one retry. If the retry succeeds, its result stands. If it fails too, a third savepoint deletes the note's task rows and sets `tasks_failed = true`, and one ERROR line names the path, the exception class and the SQLSTATE, never content or the driver's message.
  - The note's row, keyword vector, links, embeddings, quarantine status and the pass's success are **unchanged**. A derivation failure is **not** a poison note, is not added to any `poisoned` list, raises no `PoisonNote`, and is not a skip. A permanently failing note must not hold its owner in re-derive mode for ever (#311), which is the same reasoning that makes the link cap not a skip. It is a declared degradation carried on the row, and `list_tasks` reports it in every header.
  - The bit is cleared only when a later derivation succeeds. Derivations happen when the content changes, on an extraction-version bump, or on a re-derive.
  - **Any other database error** (connection, interface, serialization, a savepoint that cannot be rolled back) propagates. The pass fails as an ordinary failure with nothing committed, exactly as for any other statement. Nothing is pruned by a pass that commits nothing.
- **No file read.** A changed path with no buffered body, or with no row in the pass's `paths_to_id`, appends to `skips` and `skipped_paths`, so a re-derive is withheld (#311), exactly as the link rebuild records the same two cases. The pass's `paths_to_id` map comes from the link rebuild (which built it in this transaction). To pass it on, `_update_links_for_changed` returns its `paths_to_id`, a signature change that touches nothing else.
- A capped note logs one ERROR line and sets `tasks_truncated`. It is not a skip.
- The link backfill (`link_backfill_pass`) is **not** extended. It exists because `note_links` predates the backfill-by-version mechanism. Tasks arrive with version 4 (D5).
- **Entry points.** `_index_vault_attempt` is reached from the startup pass, the periodic pass and the panel's manual reindex (`index_vault`, `index_scope_now`). All three therefore get the same isolation, and all three are tested (tasks §3).

**Composition rule, written into the module docstring for #351.** A per-note derivation (1) is keyed by `notes_metadata.id` with `ON DELETE CASCADE`, (2) is a pure function of the buffered body and the extraction version, (3) runs after the link rebuild in the same transaction, for the same changed list, (4) records a missing buffered body or id in the shared `skips` and `skipped_paths` lists, (5) never reads a file, and (6) **isolates its own failure**: a per-note savepoint, one retry, then an owner-visible failure bit on the note's row, never the quarantine and never a pass failure. A third derivation is one more call after this one. **Alternative considered**: refactoring links and tasks into a generic `PerNoteDerivation` driver now. Rejected for this change because it would move the link rebuild, a mutating hot path with its own adversarial history, inside a feature PR.

**Why not the quarantine** (Codex spec round 1, BLOCKER): the quarantine exists for writes that *are* the note (its row, its keyword vector, its links). Task rows are an optional view of the note. Quarantining on their account would remove an otherwise searchable note from every search tool, and that removed note would also drop out of `list_tasks`'s own counts, making the loss silent.

Moved notes are in the changed list and are re-derived. The result is identical (same body), but re-deriving keeps one rule ("every changed path is delete-and-reinsert") rather than a move exception.

### D5. Backfill: `CURRENT_EXTRACTION_VERSION` 3 → 4

`clean_at_version` gains `4: clean_for_embedding`, so `_grammar_changed_the_embedding_text` is False for every v3 row and **nothing is re-embedded**. The first pass after deploy, the startup full-hash pass, re-derives every note's tags, links, keyword vector and tasks, then re-stamps 4. This is the same cost as the v2 and v3 bumps. A constant `TASKS_SINCE_EXTRACTION_VERSION = 4` in `src/services/tasks.py` lets the tool count the caller's rows that have **not yet been scanned for tasks** (`extraction_version < 4`). The header always reports this count (D7). The tool never refuses on it, because a partial answer that says it is partial is more useful than a refusal during a window of about one pass.

During that window `move_note(rewrite_links=True)` refuses with `index_not_ready`, as it does after every bump. That is existing behaviour, and it is stated in the upgrade note.

### D6. The tool: signature and query

```
list_tasks(folder: str | None = None, status: str = "open",
           due_by: str | None = None, scheduled_by: str | None = None,
           tags: list[str] | None = None, limit: int = 100,
           cursor: str | None = None) -> str
```

- `status` is one of `open|done|cancelled|all`. Anything else is refused with `invalid_argument`. `open` excludes cancelled (`[-]`) tasks (owner decision, 2026-10-29). `cancelled` returns only those, and `all` returns every state. Every line shows the raw marker, so a cancelled task in an `all` listing reads `- [-] …`.
- `due_by` and `scheduled_by` are ISO `YYYY-MM-DD` and **inclusive**: `due_by=<today>` returns overdue tasks and tasks due today. A task with no date of that kind is excluded when the filter is given. A malformed date is `invalid_argument`. **The names deviate from the issue's `due_before` / `scheduled_before`**: "before" reads as exclusive, and an agent asking "what is due today" would pass today and miss today's tasks. Owner decision (2026-10-29): inclusive, named `_by`. The docstring's Args lines say "on or before" for both.
- `folder`: the same prefix semantics and LIKE escaping as `apply_note_filters` (`src/services/filters.py`), applied to the joined `notes_metadata.file_path`. The string is capped by `_tracked`'s `arg_char_caps` (`MAX_PATH_CHARS`).
- `tags`: AND containment on the **task's** inline tags (`note_tasks.tags @> ARRAY[…]`). A leading `#` on an argument is stripped. Note-level tags are not consulted, because a task's own tags are what the plugin filters on. **The tool body validates the list explicitly** (`_tracked`'s `arg_char_caps` covers only named top-level strings, not list elements): at most `LIST_TASKS_MAX_TAG_ARGS` (32) entries, each between 1 and `TASK_TAG_MAX_CHARS` (128) characters after stripping `#`, and each a string. A violation is refused with `invalid_argument`, naming the bound, before any query runs.
- `limit` is clamped to 1–500 by `_clamp_limit`.
- **Ordering**: `(coalesce(due, 'infinity'::date), file_path, line)` ascending: dated tasks first, earliest first, then by path and line. This is a total order within an owner scope because `file_path` is unique per owner and `(note_id, line)` is unique. Round 1 briefly keyed on `note_id` to keep the cursor short. Round 2 reverted that, because a database-global id in a cursor discloses another tenant's activity. The cursor length is now handled by D8's compact form instead.
- **Tenancy**: `JOIN notes_metadata m ON m.id = t.note_id WHERE _note_owner_predicate(uid)`, the total mapping, as part of the join. A caller with no user id sees only NULL-owned rows. Every statement of a call runs in one `REPEATABLE READ, READ ONLY` transaction (the #344 pattern), so the page, the counts and the cursor agree.
- **Header counts**, all owner-scoped and all in the same snapshot:
  - `not yet scanned for tasks`: owner rows with `extraction_version < 4`.
  - `tasks could not be indexed`: owner rows (in `folder`) with `tasks_failed`.
  - `over the task cap`: owner rows (in `folder`) with `tasks_truncated`.
  - **Only when `tags=` is given**: `tasks with cut tags`, the count of task rows that match every filter except `tags` and have `tags_cut = true`. Any one of those might have carried a requested tag. The count is conservative (it over-reports), and it is stated even when zero, so a filter miss caused by the cap is never silent.
- Registered with `title="List tasks"` and `annotations=_READ_ONLY`, **after every existing registration**, so that the first 25 entries of `tools/list` are unchanged in order (D12). Decorated `_tracked("list_tasks", ["folder","status","due_by","scheduled_by","tags","limit","cursor"], arg_char_caps={"cursor": 2048, "folder": MAX_PATH_CHARS})`. `TOOL_CLASSES["list_tasks"] = "scan"`, with `CLASS_MAPPING_VERSION = 3`.

### D7. Rendering: one line per task, note-controlled fields on that line

```
Tasks — status: open — shown: 37 — more: no — not yet scanned for tasks: 0 notes — tasks could not be indexed: 0 notes — over the task cap: 0 notes
- [ ] `Projects/Alpha.md`:42 — due 2026-11-01 — scheduled 2026-10-30 — priority high — tags #work #alpha — under: Sprint > Backend — text: Ship the migration 📅 2026-11-01 ⏳ 2026-10-30 ⏫ #work #alpha
- [/] `Projects/Beta.md`:7 — text: Draft the spec
next: eyJ2IjoxLC…
```

- **The header line is always present** and always carries `shown`, `more`, and the three note counts from D6. With `tags=` it also carries `tasks with cut tags`. Every count is stated even when it is zero (#200: an absent token is not evidence of absence). Filters are echoed, with each echoed value shown through `_one_line` and cut at 64 characters with `…`. The echo is display only, and the filter itself is applied exactly. When the not-yet-scanned count or the could-not-be-indexed count is above zero, a closing notice says the list is incomplete and why.
- Each task is exactly one line. Fields are server-rendered, empty ones are omitted, and `text:` comes **last**. `text`, `heading_path` and each tag go through `_one_line`, so whitespace runs (line breaks and U+2028/9 included) become one space, and a note body cannot start a new line. The marker character is `[^\s\[\]]` by grammar and cannot break the line. A path is rendered with any character `_one_line` would collapse shown as an escape (`\n`, `\r`, `\t`, or `\uXXXX`) and a literal backslash doubled (`\\`), so the rendering is injective and the path stays exact and still on one line. That is stricter than `keyword_search`, which renders the path raw (a pre-existing residual, not changed here). A stored `text_cut` renders as `text (cut at 1000 chars):`, and a stored `tags_cut` renders as `— tags cut`.
- **Size bound, with guaranteed progress.** Let `B = settings.max_read_response_chars` (≥ 1,000 by its `Field` bound). The renderer reserves the header (at its worst-case length), the closing incomplete-list notice when there is one, and the footer first, then fills rows:
  - **Fixed bounds** (each pinned by a unit test):
    - The header is at most 480 characters: fixed text and up to five counts of at most 10 digits, plus echoes of at most 64 characters each.
    - The footer is at most 200 characters when it carries the compact cursor form (D8). The full form is used only where it fits the room that is actually left.
    - The closing incomplete-list notice is at most 85 characters.
    - An abbreviated row's structure is at most 120 characters: marker, line number of at most 10 digits, labels (`tags omitted`, `tags cut`, `heading omitted`, `path cut`), the `— abbreviated` tag, and the `text (cut):` label. The dates and priority it keeps are on top of that, at most 96 characters (four dates and `priority highest`).
  - The arithmetic, counting everything (corrected at verification; the first draft left out the notice and the kept fields): 480 + 85 + 200 + 120 + 96 = 981 ≤ 1,000 in the bounds, and the worst case actually rendered (a 1,024-character folder echo, 32 × 128-character tags, 10-digit counts, the notice, four dates and `priority highest`, a 10-digit line, a 1,024 × `é` path, a cut tag list) is 969 characters with 61 of path and 40 of text. So **at least 40 characters each of path and text remain for an abbreviated first row** at the minimum cap, with its dates and priority kept. `test_the_worst_case_at_the_minimum_cap_keeps_40_of_path_and_text` pins it.
  - **Full rows are appended while each fits.** The first row that does not fit ends the page with `more: yes` and a cursor from the last rendered row, unless it is the page's **first** row. The first row is then rendered **abbreviated**: dates and priority are kept, tags and heading are dropped (with `— tags omitted` / `— heading omitted` noted only if present; a stored `tags_cut` also counts as present and adds `— tags cut`), and the text is cut to the remaining room with `text (cut):`. If the escaped path alone exceeds the room left for path and text minus 40 characters, the path is cut too. It is rendered as its first and last characters around `…`, with `— path cut`, so the agent can still recognise the note and find it with `list_notes`. No index id is ever rendered. The row ends with `— abbreviated`.
  - Every page therefore renders at least one row when one matches, and the cursor always advances. The response never exceeds `B`.
- **Empty result**: the header with `shown: 0` and no `next`. Not an error.

### D8. Cursor: an owner-bound keyset over the caller's own values

**Payload.** `next` is base64url (no padding) of compact JSON (`ensure_ascii=False`, UTF-8), in one of two forms:

- full form: `{"v":1,"o":"<owner>","f":"<filters>","d":"<YYYY-MM-DD or infinity>","p":"<file_path>","l":<line>}`
- compact form: the same, with `"h":"<32 hex>"` in place of `"p"`.

The fields:

- `o` is the **owner discriminator**: the first 16 hex characters of SHA-256 over `owner:<user id>`, or over `owner:null` for the NULL scope. It is bound to the owner scope, not to the credential, so a cursor keeps working across the same owner's keys and grants. No raw id appears in it.
- `f` is the 16-hex digest of the call's normalised filters (`folder`, `status`, `due_by`, `scheduled_by`, sorted `tags`; `limit` excluded).
- `d` is **never null**. An undated row's key is the string `infinity`, which binds to PostgreSQL's `'infinity'::date`, the same value `coalesce` produces in the ORDER BY.
- `p` is the boundary row's path, which is the caller's own value.
- `h` (compact form only) is the first 32 hex characters of SHA-256 over the boundary path's UTF-8 bytes.

No `notes_metadata.id` and no other global sequence value appears in a cursor or in any rendered line (Codex spec round 2). Every key value is the caller's own data, so a cursor exposes nothing they could not read from the page itself.

**Resume predicate**, exactly (both sides compare under the column's collation, which is also what the ORDER BY uses):

```
(coalesce(t.due, 'infinity'::date), m.file_path, t.line) > (CAST(:d AS date), CAST(:p AS varchar), :l)
```

**Which form.** The renderer issues the full form when its encoding fits both the footer room left on the page and the argument cap of 2,048 characters. A 1,024-character ASCII path fits with room. Otherwise it issues the compact form, which is at most 200 characters by construction. That form is what makes the D7 guarantee hold at the 1,000-character minimum response cap, and for a long non-ASCII path whose UTF-8 encoding would overflow 2,048. To decode a compact cursor, the call resolves `h` to `:p` inside its own snapshot, against the caller's own rows only (`_note_owner_predicate(uid) AND substr(encode(sha256(convert_to(file_path,'UTF8')),'hex'),1,32) = :h`). If no row matches (the boundary note was deleted or moved since) or more than one does, the cursor is refused with `invalid_argument` ("the note this cursor stopped at is no longer indexed; start without a cursor").

**Validation.** Decoding checks:

- the version and the shape (exactly one of `p` / `h`);
- that `d` is `infinity` or a valid ISO date;
- that `l ≥ 1` is an integer and `p` is a string of at most `MAX_PATH_CHARS`;
- **that `o` equals this caller's owner discriminator, and `f` equals this call's filters digest**.

Any failure is refused with `invalid_argument` ("cursor does not belong to this query; start without one"). The `o` check is what stops one tenant's boundary being applied to another tenant's rows (Codex spec round 2). A cursor forged with a valid `o` can only move its forger within their own rows, which the `due_by` / `folder` filters already let them do, so the cursor needs no MAC or encryption. Decided by the owner on 2026-10-29 as simpler than Codex's proposed fix with the same guarantees. The argument is capped at 2,048 characters by `arg_char_caps`.

An integration test pins the predicate with a page boundary **between two undated tasks** (round 1). Further integration tests cover a cross-owner cursor refused, a filters-changed cursor refused, and neither a cursor nor any rendered line containing a `notes_metadata.id` (round 2).

**Why a cursor rather than `limit` alone**: the response cap can stop a page before `limit`, so a bounded tool without paging would leave the tail unreachable. **Why keyset rather than offset**: the index changes between calls, and an offset would skip or repeat rows when an earlier task is ticked. A keyset can still miss or repeat a task edited between pages, or a note moved mid-pagination (its path, and so its position, changes). That is accepted (L4).

### D9. Freshness

The tool reads only the index. A task changed through `edit_note` appears changed after the next index pass (default `INDEX_INTERVAL_SECONDS` = 300). The docstring says this in its contract lead. **Alternative considered**: re-deriving the touched note inside `edit_note`. Rejected, because the write tools do not index today and adding a derivation writer outside the pass would break D1's "written only with the row".

### D10. Coordination with `change-feed` (#351, migration 033)

- Migration numbers: 032 here, 033 there. 033's `down_revision` is `"032"` if this change merges first. Otherwise the second to merge rebases its `down_revision`.
- Both changes add one call after `_update_links_for_changed`. D4's composition rule is the shared contract, and the second to merge adds its call after the first's.
- `CLASS_MAPPING_VERSION`: each tool-adding change bumps it, and the second to merge takes the next value.
- **Hazard for #351**: the v4 bump makes every row "changed" for one pass (`marker_stale`). A feed derived from `to_upsert` would report every note as changed at deploy. The feed must key on a content-hash change, not on membership in the changed set. This is recorded here so the #351 reviewer sees it, and it is not enforced by this change.
- Tool surface: this change takes the tool count to 26 and appends `list_tasks` as the first entry of the additions fixture (D12). A tool #351 adds lands after it as the 27th, appended to the same fixture.

### D11. Caps, as constants

`MAX_TASKS_PER_NOTE = 5_000`, `TASK_TEXT_MAX_CHARS = 1_000`, `TASK_TAG_MAX_CHARS = 128`, `TASK_MAX_TAGS = 64` and `LIST_TASKS_MAX_TAG_ARGS = 32` live in `src/config.py` beside `MAX_LINKS_PER_NOTE`. They are constants, not settings, so there is nothing for `.env.example` or the README settings tables. Changing any of the extraction caps (the first four) is a grammar change (D3) and needs a version bump.

### D12. The published tool surface: 26 tools, a deliberate fixture addition

`mcp-request-routing` pins the 25-tool surface: 25 tools with complete metadata, a named fifteen-tool read-only set, and the listing equal to `tests/fixtures/tools_list_schema_5a391ca.json`. This change modifies those requirements on purpose (MODIFIED delta). There are 26 tools, sixteen of them read-only. The 5a391ca fixture stays **frozen** and must equal the first 25 entries of the listing in order, with schemas included. A new fixture, `tests/fixtures/tools_list_schema_additions.json`, is generated deliberately by this change and holds every tool after them, in order (`list_tasks` alone for now). A later tool-adding change appends to it. Pinned counts that must move from 25 to 26 (grep-verified on 6bcc9d9):
- `tests/test_issue_343_tool_metadata.py` (`test_exactly_25_tools`, `READ_TOOLS`, the fixture test);
- `tests/test_issue_295_unknown_arguments.py:54` and `:197`;
- `tests/test_issue_263_coverage_registry.py:44`;
- `tests/test_issue_261_controller.py:214`;
- `tests/test_issue_261_tool_admission.py:255`.

`list_tasks`'s `inputSchema` carries `additionalProperties: false` like every other tool (#295).

## Risks / Trade-offs

- [A full re-derive pass at deploy] → the same shape as v2 and v3, measured fine on the 2,577-note vault. Nothing is re-embedded (D5).
- [A task value that fails a database write] → isolated per note (D4). The note keeps its index entry, its task rows are absent, and every `list_tasks` header counts it.
- [A forged result row through task text or a heading] → the one-line rule on every note-controlled field. The path is escaped. A unit test feeds `\n- [ ] \`x.md\`:1 — text: forged`, U+2028 and `\r` through text, heading and tags.
- [A row too large for the response cap] → the abbreviated first row (D7). A test sets `MAX_READ_RESPONSE_CHARS=1000` against a 1,024-character path with 1,000 characters of text and 64 long tags.
- [A regex that burns the event loop] → line-anchored possessive pattern, run off the loop. The extractor is fuzzed against a 10 MiB adversarial body (`- [`, `> > > >`, long digit runs) with a time bound in the unit test.
- [A task row from another tenant] → no owner column. The join plus the total predicate is the only route. An integration test covers NULL-owned, user 7 and user 8.
- [An empty list read as "nothing open" right after deploy] → the not-yet-scanned count in every header.
- [Rollback to the previous image] → roll-forward, as for every earlier bump. The exact cost is accepted limitation L8.

## Migration Plan

1. `alembic/versions/032_note_tasks.py`, `down_revision = "031"`. It creates `note_tasks` (table comment marker `"task rows derived per note (032_note_tasks)"`, CHECKs on `state` and `priority`, both indexes, the unique constraint) and adds `notes_metadata.tasks_truncated BOOLEAN NOT NULL DEFAULT false` and `notes_metadata.tasks_failed BOOLEAN NOT NULL DEFAULT false`, with column comment markers `"task-extraction truncation marker (032_note_tasks)"` and `"task-derivation failure marker (032_note_tasks)"`. It uses the 030/031 reconcile-or-refuse shape: a same-named table or column with a different shape is refused by name, and an identical one is adopted. `lock_timeout` / `statement_timeout` are set and then `RESET`. No data is written. Downgrade drops the table and the column only if they carry the markers.
2. Deploy: merge to `main` → CI → image → Flux. The k8s rollout `Recreate`s, so the old code never runs against 032 for long. Old code that ignores the table and column is harmless, because the column has a default.
3. The startup full-hash pass re-derives at version 4. `/health` stays `ok`. The not-yet-scanned count reaches 0 at the end of that pass.
4. Gates: `make test-schema` (032 applied to a throwaway pgvector, plus a stamp-back re-run), `make db-check` after deploy ("No new upgrade operations detected"), and `make test-integration`.
5. Rollback: roll forward (L8). Downgrading 032 is not needed to run a v3 image, because a v3 image never reads the table or the column.

## Accepted limitations

- **L1** Checkboxes inside HTML comments, `%% … %%` comments and fenced blocks nested in blockquotes or callouts are listed as tasks. The flat fence grammar does not parse containers or comments, the same limitation links have.
- **L2** Field signifiers are recognised anywhere on the line (last occurrence wins), while the plugin reads only trailing fields. A date emoji in the middle of a description counts as the task's date.
- **L3** An invalid date stores NULL, so that task is invisible to `due_by` / `scheduled_by` filters. It still appears without a date filter, with its text intact.
- **L4** Keyset paging over a live index can miss or repeat a task that changes between pages. A note moved mid-pagination (so its path, and with it its sort position, changes) may be skipped or repeated on the next page. A compact-form cursor whose boundary note was deleted or moved is refused, and the caller restarts.
- **L5** Freshness is one index pass (D9).
- **L6** Recurrence, created and cancelled dates, dependencies, global filters and custom status configuration are not modelled. Custom markers map to `open`.
- **L7** Nesting is not stored. A subtask's parent is visible only by line order.
- **L8** Rollback is roll-forward, consistent with the version 2 and 3 bumps. This is what a bare redeploy of the previous (version-3) image does to rows stamped 4, verified against the code on 6bcc9d9:
  - **It re-extracts every note.** `marker_stale` is `4 != 3`, so the next pass treats every row as changed. It re-parses every note and rewrites its tags, links and keyword vector, then re-stamps it 3. That is one full re-derive pass, the same cost as the deploy itself.
  - **It also invalidates every note's embedding certification.** `_grammar_changed_the_embedding_text(4, body)` calls `clean_at_version(4, …)`. A v3 build has no version-4 cleaner, so the call returns None, which that build treats as *differs*. So the v3 image does more than re-extract: it clears `embedded_content_hash` on every unchanged note.
  - **That costs a re-chunk, not a re-embed.** The embed pass then reselects every note. Because version 4's cleaner is version 3's, every chunk's text is byte-identical to a stored chunk, so chunk-vector reuse (#281) reuses every vector. No provider call is made and no embed budget is debited. Each note's `note_embeddings` rows are deleted and reinserted, though. Until the embed pass has worked through the vault (it rotates tenants, at a note boundary), `semantic_search` annotates those notes as stale (#200) rather than certified.
  - **Task rows are left untouched.** The v3 image never reads, writes or prunes `note_tasks` or `tasks_truncated`. The FK cascade still removes the tasks of a note it deletes, so no orphan can form. Tasks of notes edited during the v3 window go stale but are never served, because the v3 image has no `list_tasks`.
  - **Rolling forward again is one more full re-derive at version 4,** which rewrites every note's task rows. A v4 build knows version 3's cleaner, so this step clears no certification and re-embeds nothing.

## Decisions taken (owner, 2026-10-29)

- **Inclusive `due_by` / `scheduled_by`**, docstring "on or before": "what is due today" must include today.
- **Cancelled stays out of `status="open"`**, and `all` includes it: a cancelled task is neither work to do nor work done, and the shown marker `[-]` identifies it.
- **The vault's own `CLAUDE.md` (the source of the "use Grep" advice) is outside this repo**: a post-deploy operator chore in tasks.md, and nothing in the spec.
- **Rollback is roll-forward** (L8): consistent with the earlier extraction-version bumps, and the cost is bounded (one re-derive pass, a re-chunk with full vector reuse).

## Codex spec review, round 1 (2026-10-29): six findings, all folded

1. **BLOCKER**: a task-insert failure quarantined and pruned the whole note, silently. Now it is isolated per note: a savepoint, one retry, `tasks_failed`, and a header count (D4, D6). The quarantine requirement is untouched, and the `index-integrity` delta is an ADDED isolation requirement instead.
2. **MAJOR**: a NULL due key in the cursor made the resume predicate UNKNOWN. The key is now `"infinity"`, bound as `'infinity'::date`, and an integration test puts a page boundary between two undated tasks (D8).
3. **MAJOR**: when the first row did not fit, no row was shown and the cursor could not advance. There is now an abbreviated first row, with fixed bounds proving it fits at the 1,000-character minimum, and a zero-rows-fit regression test (D7).
4. **MAJOR**: the 25-tool surface was pinned by `mcp-request-routing`. There is now a MODIFIED delta, the frozen fixture plus an additions fixture, and every pinned count is enumerated (D12).
5. **MAJOR**: the 64-tag cap was nondeterministic and silent. It is now the first 64 distinct tags in document order, plus `tags_cut` persisted and rendered, plus a conservative header count whenever `tags=` is used (D3, D6, D7).
6. **MINOR**: the claim that `_tracked` bounds list elements was false. The body now validates at most 32 tags of 1–128 characters each, with `invalid_argument` (D6).

## Codex spec review, round 2 (2026-10-29): one new MAJOR, folded

- **MAJOR**: the cursor was plain base64 carrying a database-global `notes_metadata.id`, and nothing bound it to an owner. One tenant's boundary could be applied to another's rows, and the id leaked sequence information. **Fold (owner decision, simpler than Codex's encrypt-and-MAC fix, with the same guarantees):** the keyset returns to `(due, file_path, line)`, so every key value is the caller's own data. The cursor carries an owner-scope discriminator `o` and the filters digest `f`, and the server refuses a cursor whose `o` or `f` does not match. No note id appears in any cursor or rendered line, including the abbreviated row. The cursor argument cap is 2,048. A compact form (a path hash resolved within the caller's own rows) keeps the D7 minimum-cap guarantee and covers long non-ASCII paths. The moved-note case is added to L4.

## Verification (2026-10-29)

The independent `openspec-verifier` pass on dc7cb34 found **0 blocking** gaps and three MINORs, all addressed: the D7 arithmetic is corrected above (the first draft promised 200 characters of path and text but did not count the closing notice or the dates and priority an abbreviated row keeps; the promise is now at least 40 characters each, pinned by a worst-case render test, and the spec's path-cut sentence now names the 40-character text reserve as D7 does); the `vault-guide` delta's two scenarios gained tests (`test_guide_points_at_list_tasks_in_its_tasks_section`, `test_the_instructions_name_list_tasks_outside_the_lead`); and a stale "25 tools" docstring was updated. One implementation detail outside D12's list: `tests/test_issue_263_coverage_registry.py` carries an `ADDED_AFTER_INVENTORY = {"list_tasks"}` exemption, because the outcome inventory it checks lives in an archived change (`2026-09-06-typed-tool-outcomes`) that is a historical record and is not edited; for `list_tasks` the test instead requires `list_tasks_impl` to exist and names its behavioural tests.

## Codex implementation review, round 1 (2026-10-29): FAIL, three findings, all fixed

1. **MAJOR**: an abbreviated row ignored `tags_cut`, so a task whose tags were capped (or stored NULL with `tags_cut`) rendered with neither `tags cut` nor `tags omitted` and looked complete. **Fixed:** either stored tags or `tags_cut` produces `— tags omitted`, and `tags_cut` always adds `— tags cut`; the structure stays within 120 (measured 86). Regression: `test_an_abbreviated_row_says_its_tags_were_cut`.
2. **MINOR**: a hand-edited full cursor could carry a NUL (or an unpaired surrogate) in `p`, which reached the varchar bind and failed as a database error rather than the `invalid_argument` refusal. **Fixed:** decoding refuses both before any session opens; both mutations are in the tampered-cursor test.
3. **MINOR**: path escaping was not injective (a literal `\` followed by `n` rendered like a line feed). **Fixed:** literal backslashes are doubled in the same pass as the whitespace escapes; `test_path_escaping_is_injective`. The closing incomplete-list notice was also shortened (now at most 85 characters) so the corrected D7 minimum holds with the extra `tags cut` label.

## Codex implementation review, round 2 (2026-10-29): PASS

All three round-1 fixes VERIFIED; no BLOCKER or MAJOR. One MINOR and one NIT, fixed: the "Five tool classes" table in `docs/architecture/rate-limits.md` now lists `list_tasks` in the `scan` row, and a trailing blank line at the end of `tests/test_note_tasks_extractor.py` is gone (`git diff --check` clean).
