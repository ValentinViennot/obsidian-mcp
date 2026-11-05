## Why

GitHub #351. `get_recent` and `list_notes` order by file mtime and take only
`limit`. On 2026-10-25 a vault-wide cleanup pass touched 17 of the 30 project
home notes, so "what changed recently" returned that block and nothing else.
No call could page past it, and no call could ask for notes whose content
had actually changed. The index already knows the difference. Every pass
compares each file's `content_hash` with the stored one, and it pairs moves.
It keeps none of this, though: there is no column recording when a row's
content last changed and no record of a deletion once the row is gone. So an
agent starting a session cannot ask "what moved since my last cursor", which
is the first step of Anthropic's memory-tool prompt and of its long-running
agent harness. It has to re-read and diff, or trust mtime, and mtime is the
signal that just failed.

## What Changes

- **`notes_metadata.content_changed_at`** (migration **033**, `NOT NULL`).
  The index pass stamps it only when a row's `content_hash` differs from the
  stored one, so a touch, a re-derive or a stale-marker re-upsert that keeps
  the hash leaves it unchanged. A row the index sees for the first time takes
  the file's mtime. An id-preserving move keeps the stamp. The migration
  backfills `COALESCE(modified_at, indexed_at)`.
- **`since` / `until` / `order_by` / `cursor` on `get_recent` and `list_notes`.** Both
  bounds are UTC ISO 8601 dates or datetimes with an explicit offset. A
  datetime without an offset is refused. A date-only bound covers the whole
  UTC day. `since` is inclusive and `until` exclusive. They apply to the
  column `order_by` selects: `"mtime"` (the default, today's behaviour) or
  `"content_changed"`. The default stays `"mtime"` so existing calls are
  byte-identical, and both docstrings say that "what changed" is
  `order_by="content_changed"` with `since`. Calls that use any of the
  new parameters page with an opaque keyset `cursor` over `(time,
  file_path)`, carried in a `more: cursor=…` footer, so ties of any size are
  reachable. Calls that use none get no footer and today's exact bytes.
- **A durable change log, `note_events`** (same migration). Each row is one
  of `created`, `modified`, `moved` or `deleted`, or one of the meta kinds
  `gap` and `reset`. Rows are scoped by owner exactly like `notes_metadata`,
  through a plain `user_id` column with **no foreign keys**. An FK to `users`
  would close the documented deadlock with permanent user deletion, and
  events must outlive `notes_metadata` rows. Permanent user deletion deletes
  the scope's events explicitly. Two writers append to it, both through one
  helper:
  - **the MCP write tools**, for every markdown publication they perform. The
    row carries the caller's actor label and principal, written at call
    time. This covers `create_note`, `edit_note`, `set_frontmatter`,
    `move_note` (and the backlink sources it rewrites), `delete_note`, and
    `write_file` and `import_from_url` on a `.md` path, plus
    `PUT /transfer/upload` of a `.md` file. The upload is attributed to the
    principal that minted the link, reusing the rule the write-bucket charge
    already applies at redemption. `delete_file` refuses every `.md` path, so
    it never records one. `move_note` writes its event inside its
    existing metadata transaction (all or nothing). Every other tool writes
    after its file-only publication, and if that write fails the next pass
    rediscovers the change unattributed. A tool event is appended only
    while the caller is still bound to the root the tool ran under. Content
    hashes come from one canonical function shared with the indexer.
  - **the index pass**, for what it discovers that no tool recorded,
    including every row it drops from the index, as `deleted` with a reason
    (`removed`, `not_indexable`, `quarantined`). These rows have no actor
    and render `human/sync`. The pass writes them in its
    own transaction as its last statement, so they commit exactly when the
    row changes they describe commit. An event the pass would write is
    suppressed when the latest event for that path already describes the
    same state, so a tool's edit is not recorded twice.
- **`get_changes(since, folder=None, kinds=None, by="any", limit=50)`**, a
  new read tool. It returns one line per event, oldest first, giving the
  event's per-scope sequence number, UTC time, kind, path (and source path
  for a move), and actor:
  `api_key "<name>"` / `oauth "<client name>"`, marked `(you)` when it is the
  caller's own principal, or `human/sync`. It also returns `next_since`, an
  exclusive cursor (`ev:<seq>`, meaningful only within the caller's scope)
  the agent stores in its own notes. `since`
  takes that cursor or a UTC date/datetime. `by` is `any | me | agents |
  human`, and `me` means the caller's exact principal, `("api_key", id)` or
  `("oauth", grant_id)`. `gap` and `reset` lines are always shown, whatever
  the filters, because they are what tells an agent its view is incomplete.
- **Cursor safety by construction.** Every append for a scope runs under a
  per-scope transaction advisory lock, held to commit, and assigns the
  scope's next `seq` under it. Within a scope, seqs are therefore assigned in
  commit order and are gapless across tenants, so no number reveals another
  tenant's activity. A cursor `ev:N` (return `seq > N`) never loses an event
  committed later and never repeats one.
- **Bounded and prunable.** The pass itemises at most
  `CHANGE_FEED_MAX_EVENTS_PER_PASS` (2,000) events per scope and writes one
  `gap` line for the rest. A failed or lock-timed-out event write never fails
  the pass or a file-only tool. A pass records a durable `note_event_gaps`
  row, which surfaces as a `gap` line at the scope's next append. A tool's
  change is rediscovered by the next pass. Retention runs in the
  periodic cleanup, outside any pass, and prunes by age
  (`CHANGE_FEED_RETENTION_DAYS`, 90; `null` disables it, `0` is refused)
  and by a per-scope row cap (`CHANGE_FEED_MAX_EVENTS_PER_SCOPE`, 100,000),
  which always applies. It never removes a scope's
  newest event. It keeps a per-scope "pruned through" watermark on
  `note_event_scopes`, so `get_changes` says `history_truncated` to a cursor
  or a datetime `since` older than what survives.
- **The vault guide gets one line**, framed neutrally: at session start,
  `get_changes(since=<stored cursor>)` lists what changed, and the returned
  `next_since` is the cursor to store. It also points to
  `order_by="content_changed"` with `since` on `get_recent` / `list_notes`.

No breaking change. Existing `get_recent` / `list_notes` calls return the same
rows in the same order. The published tool listing gains one tool and four
optional parameters on each of two tools, and the schema fixture is
regenerated deliberately.

## Capabilities

### New Capabilities
- `change-feed`: the `note_events` log (writers, kinds, the serialisation
  that makes cursors safe, suppression, bounds, gap/reset semantics,
  retention), and the `get_changes` tool (arguments, `by` semantics, tenancy,
  rendering, what an actor label may reveal, `next_since`).

### Modified Capabilities
- `note-filters`: `get_recent` and `list_notes` gain `since` / `until` /
  `order_by` / `cursor` (keyset), with one shared UTC bound parser. `get_changes` joins the
  total owner-scoping requirement.
- `index-integrity`: the pass stamps `content_changed_at` only on a hash
  change and records discovered events in its own transaction without ever
  failing on them. This covers the full-hash, re-derive, quarantine,
  not-indexable, walk-incomplete, C5-deferral and provenance-discard paths.
- `vault-write`: a note publication records an attributed event after it has
  published. Recording never changes the tool's result or outcome.
- `schema-integrity`: migration 033's shape (reconciliation, backfill, marker
  comments, per-scope sequence uniqueness, no foreign keys) and the three
  feed tables classified as durable for backups.
- `vault-guide`: the guide names `get_changes` and the cursor convention in
  neutral framing.
- `mcp-request-routing`: the tool-metadata requirements cover 27 tools, add
  `get_changes` to the read-only set, append it to the additions fixture and
  record the two amended filter-tool schemas in an amendments fixture.

## Impact

- **Schema**: migration `033_content_changed_at.py`, which revises 032, the
  sibling `list-tasks` change (#350). It adds one column on `notes_metadata`
  and the new `note_events`, `note_event_scopes` and `note_event_gaps`
  tables with their indexes and marker comments.
  Models are in `src/models/db.py`.
- **Code**: `src/services/indexer.py` (upsert stamp, event collection, final
  append, baseline/reset, discard purge, retention call in the tick's
  `finally`); a new `src/services/note_hash.py` (the canonical note hash, which
  `read_note_at` is refactored onto); `src/control_panel/users.py`
  (permanent deletion purges the scope's feed rows); a new
  `src/services/change_feed.py` (append helper, lock,
  suppression, gap state, retention, cursor and bound parsing, rendering);
  `src/mcp_server/tools.py` (filters on two tools, the new tool, event
  recording in eight write paths); `src/transfer/routes.py` /
  `src/services/transfer.py` (upload redemption); `src/mcp_server/server.py`
  (registration, docstrings, title and annotations);
  `src/mcp_server/vault_guide_primer.md`; `src/config.py` (four settings, each documented in README and `.env.example`).
- **Tests**: integration tests on real Postgres for stamping, events,
  suppression, cursor safety under concurrent writers, tenancy, retention
  and pass-never-fails; offline tests for parsing, rendering (forged rows),
  metadata and docstring caps; `make test-schema`; `alembic check`;
  `tests/test_backup_derived_tables.py`.
- **Docs, in the same PR**: README (tool list, settings), `.env.example`,
  CLAUDE.md ("MCP tools" count and list, a key-decisions bullet),
  `docs/architecture/indexing-and-embeddings.md`,
  `docs/architecture/vault-tools.md`, `docs/architecture/search.md`
  (rendering), `docs/architecture/schema-and-migrations.md` (033, durable
  table list), `docs/architecture/usage-attribution.md` (the event actor
  columns), `docs/architecture/vault-roots-and-tenancy.md` (event scoping,
  discard purge).
- **Not touched**: `SERVER_INSTRUCTIONS` (owner decision: the guide line
  and the docstrings carry `get_changes`), the rate buckets, the quota, the
  embed pass, `usage_logs`.
