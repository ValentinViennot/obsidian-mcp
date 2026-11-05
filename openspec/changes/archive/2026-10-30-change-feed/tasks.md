## 0. Prerequisites (before any code)

- [x] 0.1 Confirm `list-tasks` (#350) is merged: `alembic/versions/032_*.py` exists on `origin/main`, and the branch is rebased onto it. If 032 is not merged, stop. Do not take 032.
- [x] 0.2 Rebase this change's `specs/mcp-request-routing/spec.md` MODIFIED blocks onto the text list-tasks archived: the tool count (26 here, one more for each tool list-tasks added), the read-only list (add `get_changes` to whatever list-tasks left), and the fixture name and diff (diff against list-tasks' fixture, not 5a391ca's). Then re-run `openspec validate change-feed --strict`.
- [x] 0.3 Read `docs/architecture/indexing-and-embeddings.md` (C1–C8, #308 quarantine, #309, #311), `usage-attribution.md` (actor columns), `vault-roots-and-tenancy.md` (owner predicate, provenance discard), `search.md` (one-line rendering), `schema-and-migrations.md` (026/030 reconciliation shape, #348 derived vs durable), and this change's `design.md` D1–D10.

## 1. Schema: migration 033 and models

- [x] 1.1 `src/models/db.py`: `NoteMetadata.content_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), comment=_CONTENT_CHANGED_AT_MARKER)`, with a comment block covering D1 (stamped only on a hash change, `now()` for a change, mtime for a first-seen row, never moved by moves or re-upserts).
- [x] 1.2 `src/models/db.py`: `NoteEvent` (`note_events`), `NoteEventScope` (`note_event_scopes`) and `NoteEventGap` (`note_event_gaps`), exactly as design D2.
  - `note_events` has a bigserial surrogate `id`, a plain `user_id` with **no `ForeignKey`**, `seq`, `UNIQUE (user_id, seq)` with `postgresql_nulls_not_distinct=True`, two plain ascending indexes `ix_note_events_user_path_seq` (`user_id`, `path`, `seq`) and `ix_note_events_user_from_path_seq` (`user_id`, `from_path`, `seq`, not partial), and `ix_note_events_occurred_at`.
  - `note_event_scopes` has `user_id` unique `NULLS NOT DISTINCT`, `last_seq`, `pruned_through_seq` and `pruned_at`.
  - `note_event_gaps` is as D2.
  - Each table carries a marker comment, and a comment block explains why there are no FKs (D2).
- [x] 1.3 `alembic/versions/033_content_changed_at.py` (`revision="033"`, `down_revision="032"`), in the 026/030 shape.
  - Pin and assert `search_path`; set and `RESET` `lock_timeout` / `statement_timeout`.
  - Add the column nullable, backfill `COALESCE(modified_at, indexed_at)`, then `SET NOT NULL`, `SET DEFAULT now()` and `COMMENT`.
  - Create the three tables with their constraints, indexes and comments, and no FKs.
  - On a re-run, reconcile the marked shapes and refuse any other shape by name. `downgrade()` drops only marked objects.
- [x] 1.4 Extend the schema gate (`make test-schema` and its test module) to assert, through `pg_constraint`, the CHECK predicates, the two `NULLS NOT DISTINCT` uniques, and the absence of any FK on the three tables. Alembic compares none of these.
- [x] 1.5 `tests/test_backup_derived_tables.py`: classify all three tables as **durable**. Update the durable list in `docs/architecture/schema-and-migrations.md` "Derived vs durable tables (#348)" from twelve to fifteen.
- [x] 1.6 Run `make test-schema` (it shares `SCHEMA_TEST_CONTAINER` / `SCHEMA_TEST_PORT` with `test-integration`, so never run both at once). Then confirm `alembic check` reports "No new upgrade operations detected".

## 2. Shared helpers

- [x] 2.1 `src/services/note_hash.py`: `canonical_note_text(data: bytes) -> str | None` does a strict UTF-8 decode (`None` on failure), translates `\r\n` and lone `\r` to `\n`, and removes U+0000. `canonical_note_hash(data) -> str | None` returns the SHA-256 hex of that text's UTF-8.
  - Refactor `indexer.read_note_at` to read bytes from the same descriptor and call `canonical_note_text`, keeping the NUL WARNING and the `UnicodeDecodeError` / not-indexable behaviour. `_content_hash(text)` must equal `canonical_note_hash(bytes)`.
  - Add a parity test over LF, CRLF, lone CR, NUL, BOM and invalid UTF-8 against the previous text-mode implementation (kept in the test as the reference).
- [x] 2.2 `src/services/change_feed.py`: register `NOTE_EVENTS_LOCK_NS` beside the advisory constants in `src/oauth/grants.py`, with the wire-constant comment. `_scope_key(user_id) -> int` returns `user_id` or 0.
- [x] 2.3 `async append_events(session, user_id, events, *, suppress=False, bounded=True) -> int`, implementing design D4's ordered steps under `pg_advisory_xact_lock(NS, scope)`:
  1. the user-exists and root-binding check: `SELECT vault_path FROM users WHERE id = :uid`, multi-user only. If the user is gone write nothing. For tool events, if the current assignment's canonical-root digest differs from the `root_binding` the tool passed, write nothing and log `change_feed_root_changed` (add it to the security-event catalogue in `docs/architecture/security-event-logging.md`);
  2. the `note_event_scopes` upsert and read of `last_seq`;
  3. consuming `note_event_gaps` rows into one `write_failed` `gap` event;
  4. the suppression lookup when `suppress=True`;
  5. one multi-row INSERT with consecutive `seq`, then storing `last_seq`.

  With `bounded=True` it runs in a savepoint with `SET LOCAL lock_timeout`. On any exception it rolls back the savepoint, logs one WARNING (scope and count, no path), and returns `-n`. With `bounded=False` (only `move_note`'s metadata transaction) it lets exceptions propagate so the surrounding transaction rolls back. It is the only `insert(NoteEvent)` in `src/`.
- [x] 2.4 `async record_tool_events(events, root_binding)`: for file-only tools, with the binding taken from the root `_tracked` resolved for the call (a digest of its canonical root path; never rendered). `move_note`'s in-transaction append passes the same binding. It opens its own session and transaction, fills `user_id` from `current_user_id`, the actor from `actor_columns()` (kind and label only), the principal from `current_principal`, and the tool from `_current_tool_name`, then calls `append_events` and commits. It swallows every exception, including the session open, logs it, and writes **no** durable marker (the next pass rediscovers the change, design D3).
- [x] 2.5 `async latest_events_at(session, user_id, paths)` returns the highest-seq event whose `path` **or** `from_path` equals each path, chunked at 1,000 paths and served by the two indexes. `suppress(candidate, latest_at) -> bool` implements design D3's rules (mcp source only): content events by hash, or NULL hash with `not_indexable`; `deleted` with the same reason; `moved` only when the same MCP move is the latest at both endpoints.
- [x] 2.6 `parse_utc_bound(value, role) -> datetime`, `parse_since(value) -> Seq | datetime`, and the `get_recent` / `list_notes` keyset cursor codec (`encode_note_cursor` / `decode_note_cursor`: versioned base64url JSON with `o`, `t` in microseconds, `p`, the filter digest `f`, and the owner discriminator `w` = first 16 hex of SHA-256 over `owner:<user_id>` / `owner:null`), following the note-filters requirements. It shares list-tasks' envelope (`_b64url` and the decode) and its owner discriminator: import them, and do not write a second copy. Refusals go through `_body_refusal(..., "invalid_argument")`.
- [x] 2.7 `render_event_line(row, caller_principal) -> str` and `render_changes(...)`, following change-feed's rendering requirement. Render `ev:<seq>`, never `id`. A `_quote()` JSON-encodes with `ensure_ascii=False` and additionally escapes U+0080–U+009F, U+2028 and U+2029.
- [x] 2.8 `async prune(now)` (design D9): per scope, batches of 5,000.
  - Each batch takes the scope's feed lock and applies the age and cap rules, keeping the newest event, and raises `pruned_through_seq` / `pruned_at` in the delete's transaction.
  - The backstop deletes feed rows of `user_id`s with no `users` row.
  - Catch and log per scope.
- [x] 2.9 `src/config.py`: the four `CHANGE_FEED_*` settings with the bounds in design D10, validated at load.
  - `CHANGE_FEED_RETENTION_DAYS` is `int | None`: `null` disables the age limit, as `OAUTH_CLIENT_UNUSED_EXPIRY_DAYS` does, and `0` is refused.
  - `CHANGE_FEED_MAX_EVENTS_PER_SCOPE` cannot be disabled.
  - Settings tests cover `null`, `0` and the bounds.

## 3. Index pass: stamp, collect, suppress, append

- [x] 3.1 Upsert in `_index_vault_attempt`: the insert values carry `content_changed_at = modified_at` (the file's mtime). `on_conflict_do_update` sets `content_changed_at = CASE WHEN notes_metadata.content_hash IS DISTINCT FROM excluded.content_hash THEN now() ELSE notes_metadata.content_changed_at END`. Confirm the move UPDATE, stat refresh, keyword-vector writers, re-derive tail and link backfill never name the column (grep, then the guard in 6.3).
- [x] 3.2 Collect events in memory: `created` for `to_upsert` paths not in `existing` and not moved; `modified` for `to_upsert` paths in `existing` with `existing[p] != h`; `moved` for the committed `moves`; `deleted` for every row removed: the final `deleted_paths` as `reason=removed`, `not_indexable_rows` as `reason=not_indexable`, and the rows `quarantined_now` removed as `reason=quarantined`. Add `created reason=not_indexable` for a not-indexable path with no row whose latest event is not already a `not_indexable` one. Order and cap them per design D5 (`CHANGE_FEED_MAX_EVENTS_PER_PASS`, with the overflow `gap`).
- [x] 3.3 Baseline: if `locked` (the scope's rows at the attempt's start) is empty, discard the collected events and append a single `reset`.
- [x] 3.4 Final statement: immediately before `await session.commit()`, and after every derivation stage (keyword vectors, links, the re-derive tail, list-tasks' extraction), call `append_events(..., suppress=True)`.
  - If it returns a negative count, insert one `note_event_gaps` row (`gap_count = n`) in the pass transaction. A failure of that INSERT propagates like any database error.
  - Leave a comment at the call saying that any future stage goes above it.
- [x] 3.5 `_reconcile_provenance` discard branch: in the discard's transaction, delete the scope's `note_events`, set `note_event_scopes.pruned_through_seq = last_seq` and `pruned_at`, and append `reset` through `append_events`.
- [x] 3.6 Periodic tick `finally`: call `change_feed.prune(now)` after `cleanup_expired_tokens`, separately guarded so neither failure masks the other.

## 4. Write tools and user deletion

- [x] 4.1 `create_note`, `edit_note`, `set_frontmatter`: after a confirmed, non-dry-run publication, call `record_tool_events` with the kind, `canonical_note_hash` of the published bytes (a NULL hash plus `reason=not_indexable` when it returns `None`), and the call's root binding. Do not call it on refusal, dry run or `publication_uncertain`.
- [x] 4.2 `write_file`, `import_from_url`: only when the destination ends in `.md`. Use `created` or `modified` according to whether an incumbent existed, with the hash from `canonical_note_hash`.
- [x] 4.3 `move_note`:
  - Inside its existing metadata transaction, as the last statement before commit, call `append_events(..., bounded=False)` with `moved` (with the hash when the tool holds the bytes). A failure rolls the transaction back and takes the existing "DB update failed; reindex will reconcile" branch.
  - After the backlink rewrites, `record_tool_events` the `modified` events for each source it published. On `partial_completion`, record only what was published.
- [x] 4.4 `delete_note`: `record_tool_events` with `deleted reason=removed` after the confirmed delete, soft or permanent. (`delete_file` refuses every `.md` path and records nothing.)
- [x] 4.5 `PUT /transfer/upload` redemption of a `.md` path: record with the token row's `actor_kind` / `actor_label`, the minting principal (`transfer.minting_principal`), `user_id` of the token's owner, `tool = "transfer_upload"`, and `canonical_note_hash` of the stored bytes.
  - This reuses the minting-principal rule the write-bucket charge already applies at redemption (owner decision 4); do not add a second rule.
  - Do not go through `current_*`, which the public route does not set.
- [x] 4.6 Grep every `_confirmed_publication(` call site and every database write inside a write tool.
  - Each markdown-publishing tool records an event.
  - A tool that writes the database for its mutation does so in that transaction (today only `move_note`).
  - `BodyOutcome` and the returned text are untouched.
- [x] 4.7 `src/control_panel/users.py` `delete_user` permanent branch: after the `users` DELETE and its cascade, take the scope's feed lock (`pg_advisory_xact_lock(NS, user_id)`), then DELETE the scope's rows from the three feed tables, all in its transaction. Add a comment naming the lock order and why there is no FK (design D4).

## 5. Read tools

- [x] 5.1 `list_notes_impl` / `get_recent_impl`: `since`, `until`, `order_by`, `cursor`.
  - A call with none of the four runs the exact current statement and output, with no footer.
  - Any of the four switches to the bounded path: predicates, the keyset predicate `col < t OR (col = t AND file_path > p)`, a `limit + 1` fetch and the `more: cursor=…` footer.
  - Add the `@_tracked` param names.
  - Update the docstrings in `server.py`, each ≤ 4,000 characters. The default stays `"mtime"` (owner decision 1), and each docstring says that "what changed" is `order_by="content_changed"` with `since`.
- [x] 5.2 `get_changes_impl` in `tools.py` with `@_tracked("get_changes", ["since", "folder", "kinds", "by", "limit"])`:
  - argument validation, including refusal of a cursor above the scope's `last_seq`;
  - one `REPEATABLE READ, READ ONLY` transaction for the page, `last_seq`, `pruned_through_seq` and `pruned_at`;
  - the total owner predicate over `note_events`, and the folder LIKE-escape on `path OR from_path`;
  - `kinds` / `by` filters that never filter meta kinds;
  - the `limit` clamp and the `MAX_READ_RESPONSE_CHARS` cut;
  - `history_truncated` for both cursor and datetime starts (design D6);
  - `next_since`.
- [x] 5.3 Register `get_changes` in `src/mcp_server/server.py` with `title="Change feed"`, `annotations=_READ_ONLY`, and a docstring covering the change-feed metadata requirement's statements, at ≤ 4,000 characters.
- [x] 5.4 `src/mcp_server/vault_guide_primer.md`: one neutral line in "Summary for agents" naming `get_changes(since=<stored cursor>)` and `next_since`, and pointing to `order_by="content_changed"` with `since` on `get_recent` / `list_notes`. `SERVER_INSTRUCTIONS` stays unchanged (owner decision 2).

## 6. Tests

- [x] 6.1 Offline parsing:
  - `parse_utc_bound`: date-only in both roles, `Z`, `+02:00`, naive refused, garbage refused, `since >= until` refused, and `ev:N` refused by the two filter tools.
  - `parse_since`: cursor and datetime.
  - The note cursor codec: round trip, version, `order_by` and filter-digest mismatch refused, garbage refused.
- [x] 6.2 Offline rendering:
  - the forged-row scenario (a path containing `\n- ev:999 …`);
  - U+2028 / U+2029 / C1 in a label;
  - `(you)` only on principal equality;
  - no `principal_ref`, prefix, client_id, grant id, user id or surrogate `id` in output, asserted by planting sentinel values in every non-rendered column;
  - the `gap` / `reset` lines;
  - the character-cap cut moving `next_since`.
- [x] 6.3 Offline guards: exactly one `insert(NoteEvent)` / `INSERT INTO note_events` in `src/`; no writer other than the upsert names `content_changed_at`; no `ForeignKey` on the three feed models; one definition of the note hash (`canonical_note_hash`), with no other `sha256` over note text in `src/services/indexer.py` or the write tools.
- [x] 6.4 Metadata and guide:
  - `tests/test_issue_343_tool_metadata.py` gets `get_changes` in the read-only set, and the count rises from 26 to 27.
  - Append `get_changes` to `tests/fixtures/tools_list_schema_additions.json` (after `list_tasks`), record the `get_recent` and `list_notes` replacement entries in `tests/fixtures/tools_list_schema_amendments.json`, and add the diff-only test from the `mcp-request-routing` delta (four optional properties per filter tool; the frozen 5a391ca fixture is not edited).
  - Check the description cap.
  - Add a vault-guide primer test for the new line and its neutral framing.
- [x] 6.5 Integration, `tests/integration/test_change_feed_pg.py` (real Postgres):
  - stamping: touch, edit, re-derive, marker bump, full-hash-only edit, moves of both kinds, quarantine re-entry, backfill values;
  - pass events for created, modified, moved and deleted (a not-indexable path gives its one `created`/`deleted reason=not_indexable` event, see 6.5a), and none for protected and C5-deferred rows;
  - baseline `reset`;
  - the discard purge with `history_truncated`;
  - overflow `gap`;
  - suppression: agent then pass gives one event; agent then human gives two; a CRLF `write_file` and a NUL transfer upload each give one;
  - a `PoisonNote` restart writing events once;
  - `get_recent` / `list_notes` keyset paging over 501 rows sharing one instant with `limit=500`, returning all 501 exactly once;
  - a no-new-parameter call over 21 rows, byte-identical to the pre-change output.
- [x] 6.5a Integration, round-2 cases:
  - An invalid-UTF-8 overwrite of an indexed `.md` and a new invalid-UTF-8 `.md`, each with a forced append failure and a simulated restart, give `deleted reason=not_indexable` and `created reason=not_indexable`, the latter only once across passes.
  - Quarantine removal gives `deleted reason=quarantined`.
  - The exact move cycle A→B (`move_note`), B→A (external), A→B (external), with a pass after each external move, gives three `moved` lines.
  - The reassignment interleaving: publish under root A, reassign to B, B's discard plus `reset`, then the delayed append. Nothing from A enters B's feed, and one `change_feed_root_changed` security event is logged.
  - The opposite order: the append commits first and the discard purges it.
  - `list_notes` cursor from user 9 passed by user 7 under identical filters is refused.
- [x] 6.6 Integration, cursor safety:
  - Two concurrent appenders to one scope, with a reader interleaved between their commits (explicit sessions control commit order). Assert no event is ever at or below a returned `next_since` after commit, and that paging over 120 events returns each exactly once.
  - Interleaved writes in two scopes give gapless per-scope seqs.
  - A cursor above `last_seq` is refused.
  - A lock held past `CHANGE_FEED_LOCK_TIMEOUT_MS` makes the pass commit with a `note_event_gaps` row. That row survives a simulated restart (fresh process state) and becomes a `write_failed` gap before the next append.
- [x] 6.7 Integration, deletion and moves:
  - Interleave a pass's append with permanent user deletion of the same user, both orders. Assert no deadlock victim and no remaining feed rows for that user.
  - An append after the deletion committed writes nothing.
  - `move_note` with a forced append failure, then a restart, then a pass. Assert the metadata transaction rolled back and the pass records `moved`. On success, exactly one attributed `moved` and no pass event.
- [x] 6.8 Integration, tenancy and `by`:
  - Two users: user 9's events, labels and seqs are invisible to user 7 and to an ownerless caller.
  - `by=me` matches the same API key and the same OAuth grant after a refresh, does not match a second key of the same user, and refuses a principal-less call.
  - `by=agents` / `by=human` select by source.
- [x] 6.9 Integration, write tools:
  - Each tool in 4.1–4.5 records the expected kind, hash, label and principal.
  - A dry run, a refusal and a non-`.md` `write_file` record nothing.
  - A failing file-only event insert (monkeypatched) leaves the tool's result and usage outcome identical, and the next pass records the change as `human/sync`.
- [x] 6.10 Integration, retention: the age and cap rules, newest event kept, `pruned_through_seq` / `pruned_at`, a stale cursor and `since="1970-01-01"` both reading `history_truncated`, the orphan backstop, and a prune failure not affecting the pass.
- [x] 6.11 `make test-integration SCHEMA_TEST_CONTAINER=omcp-cf-test SCHEMA_TEST_PORT=55443` passes, then the full offline suite, `make audit`, `make test-schema`, and `alembic check` clean.

## 7. Docs (same PR, last commits before review)

- [x] 7.1 Public-repo rule: every new setting is documented in README **and** `.env.example` in this same PR. That means all four `CHANGE_FEED_*` settings, with defaults, bounds and `CHANGE_FEED_RETENTION_DAYS`' `null`-disables / `0`-refused rule. Grep-check both files for each name before opening the PR. README also gets the tool list (27 tools, `get_changes`) and the new `get_recent` / `list_notes` parameters.
- [x] 7.2 CLAUDE.md: the "MCP tools" count and the "Search and read" list gain `get_changes`. Add one key-decisions bullet covering `note_events` as source of truth, the per-scope `seq` assigned under the per-scope append lock (why cursors are exact and private), no FKs and the explicit purge on permanent user deletion, `move_note`'s in-transaction event, the canonical note hash, tool-written attribution with index-discovered `human/sync`, suppression, baseline and discard `reset`, the bounds and retention, and `content_changed_at` stamped only on a hash change.
- [x] 7.3 `docs/architecture/indexing-and-embeddings.md`: a "Change feed (#351)" section covering the stamp, event collection, the final-statement rule and composition with other derivations, baseline, discard, overflow, the durable gap rows, the `read_note_at` refactor onto `canonical_note_hash`, and the lock-order exception to "advisory before row lock".
- [x] 7.4 `docs/architecture/vault-tools.md`: write tools record events after publication, which tools, the `.md` rule, transfer attribution, and the `get_changes` contract text moved out of the docstring.
- [x] 7.5 `docs/architecture/search.md`: the event-line rendering rule (JSON-quoted paths and labels, not `_one_line`) beside #344's snippet rule, and the `get_recent` / `list_notes` bounds and the keyset `cursor` footer (only when a new parameter is used).
- [x] 7.6 `docs/architecture/schema-and-migrations.md`: a "033" section (backfill, per-scope `seq`, why there are no FKs (the user-deletion deadlock), markers, the three durable tables).
- [x] 7.7 `docs/architecture/usage-attribution.md`: the event actor columns (no `actor_ref`, never for authorization) and `principal_ref` (equality only, never rendered).
- [x] 7.8 `docs/architecture/vault-roots-and-tenancy.md`: event scoping, the root binding on tool appends (a same-user stale-root path, not a cross-tenant leak, closed anyway), per-scope seqs (no cross-tenant volume), the discard purge, the permanent-deletion purge, and what a label reveals.
- [x] 7.9 Copy the accepted-limitations list below into the archived spec's design notes when archiving. — Done: design.md "Accepted limitations (L1–L14)" holds the full list.

## 8. Gates

- [x] 8.1 `openspec-verifier` subagent against this change. Iterate to zero blocking gaps. — Result: 0 blocking, 0 major, 10 minor, all folded in (e1e2f97).
- [x] 8.2 Adversarial Codex pass (mandatory: the indexer pass, the write tools and silently wrong answers are all in scope). Frame "wrong" as an event lost behind a cursor, a cross-tenant line, a forged line, a pass or tool made to fail, or a misattributed write. Triage per the workflow file, two rounds by default. — Result: round 1 FAIL (3 fixed, 2 declined as L11/L12); round 2 FAIL → its one new MAJOR declined as L14, and the 3 round-1 fixes VERIFIED.
- [ ] 8.3 End-to-end against the live server after deploy: call `get_changes`, `get_recent(order_by="content_changed", since=…)`, `list_notes(since=…, until=…)` and one `edit_note`, then `get_changes(by="me")`. Name the tools called in the report.

## Accepted limitations

- L1: Changes made before migration 033 have no events, and the first pass after a restore or a discard is a baseline (`reset`), not a history.
- L2: Edits outside MCP (Obsidian, sync clients, git, a shell) are `human/sync`. The index cannot tell a person from a sync client, or one device from another.
- L3: `created` means first seen at this path. A rename the hash pairing cannot prove (a duplicate hash, or rename plus edit) is `deleted` + `created`. A note that becomes indexable, or re-enters after quarantine, is `created`.
- L4: A pass that observes a tool's publication in the milliseconds before the tool's own event commits records an extra `human/sync` event. That is a duplicate, never a loss.
- L5: Several edits inside one tick are one `modified` event, and an edit reverted before the pass is none. The feed reports the states the index observed.
- L6: A failed event append never loses the change, but it can lose the attribution. `move_note`'s event is in its metadata transaction, so a failure leaves the row at the old path, and the pass records the move. A file-only tool's failure leaves the row's old state, and the next pass records the change. In both cases the event is `human/sync` instead of the agent's label. A failed pass append leaves a durable `note_event_gaps` row, which surfaces as a `gap` line. Nothing about feed completeness is held only in process memory.
- L7: Every row the index drops is a `deleted` line with its reason (`removed`, `not_indexable`, `quarantined`), so the feed never omits a note leaving the index. It does not distinguish successive states of a note that stays unindexable: one `created`/`deleted reason=not_indexable` line, then nothing until it becomes indexable again. `content_changed_at` is lost on a restore (`notes_metadata` is schema-only in backups) and comes back as mtime.
- L8: Feed history (events, seq counters, watermarks) is durable and restored with backups, but a restore rolls `last_seq` back to the dump. A cursor taken after the dump is then above `last_seq` and is refused, and the agent restarts from a datetime `since`.
- L9: A datetime `since` is approximate (`occurred_at` is a transaction's start time). Only an `ev:` cursor is exact.
- L10: A `move_note` of a note that was never indexed and whose bytes the tool did not read may be followed by an unsuppressed `created` from the pass (no hash to match).
- L11: An `ev:` cursor is a plain per-scope seq, not a signed token; a lower cursor from elsewhere is an earlier position in the caller's own feed, and only one above `last_seq` is refused.
- L12: Near the 1,000-character `MAX_READ_RESPONSE_CHARS` floor, one event line with a very long escaped path can exceed the cap; the first event is always rendered so `next_since` advances.
- L13: A file-only tool's post-publication append waits for a pool connection outside `lock_timeout`, the same exposure as `_log_usage`; the tool's result is unaffected.
- L14: In a scope with no indexed notes, a markdown publication whose bytes are not UTF-8 and whose tool-side event append also failed is not rediscovered by the pass: `_collect_feed_events` returns before the not-indexable candidates when `locked` and `to_upsert` are both empty (indexer.py ~4304), so no `created reason=not_indexable` and no `reset` is written until the first indexable note. Declined 2026-10-30: the input needs a fresh scope whose first note is unreadable and a simultaneous append failure; the cost is one feed line for a file no agent can read; the file itself is untouched.
