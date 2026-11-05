# Design: change feed (#351)

## Context

What the index can know today, read from the code at 6bcc9d9:

- **`notes_metadata` has no first-seen time.** `indexed_at` has
  `server_default now()`, but the upsert's `ON CONFLICT … SET indexed_at =
  now()` and the indexer's move UPDATE both rewrite it, so it means "last
  rewritten by the index". `modified_at` is the file's mtime at the last
  upsert. No column records when a row's content last changed, and none
  records when a row first appeared.
- **Change detection is per pass, in memory.** `_index_vault_attempt`
  compares `existing[rel_path]` (the locked row's `content_hash`) with the
  scanned hash. A path not in `existing` is new. A path whose hash differs is
  changed. A path that is equal but has a stale extraction marker, or sits
  under a re-derive, is re-upserted *with the same hash*. The upsert writes
  every column without remembering which of these cases it was.
- **Moves are hash pairing.** After classification, deleted paths
  (`existing − seen − protected`, each passing the C5 snapshot test) and new
  paths are grouped by `content_hash`. Only a group that is exactly one old
  and one new path becomes an id-preserving `UPDATE … SET file_path = :new`.
  Anything else (a duplicate hash, a rename combined with an edit) is
  delete plus insert. Nothing about a move is recorded beyond the log line
  `Detected N file move(s)`.
- **Deletions leave no trace.** The prune is `DELETE FROM notes_metadata
  WHERE file_path IN (…)`. Rows at present-but-not-indexable paths (D7) and
  quarantined paths go through the same DELETE although their files still
  exist. Rows the walk could not see (#309 `protected`) and rows changed
  mid-walk (C5) are not pruned at all.
- **The pass is one transaction per scope**, committed at one point
  (`await session.commit()` at the end of `_index_vault_attempt`). A
  `PoisonNote` rolls the whole attempt back, and `_index_vault_pinned`
  re-runs it without the quarantined note.
- **Write tools do not touch `notes_metadata`**, with one exception:
  `move_note` updates the row's `file_path`, title, keyword head and link
  targets in its own transaction after the rename, so the next pass sees no
  move. `create_note`, `edit_note`, `set_frontmatter`, `delete_note`,
  `write_file`, `import_from_url` and the transfer upload
  publish bytes (`delete_file` refuses every `.md` path) and leave the row to the next pass.
- **Attribution exists at call time, not at index time.**
  `src.auth.session.actor_columns()` gives `(actor_kind, actor_label,
  actor_ref)` from `current_actor`, and `current_principal` gives
  `("api_key", api_keys.id)` or `("oauth", grant_id)`, the identity the rate
  buckets are keyed on. The `usage_logs` row is written *after* the tool body
  by `_tracked`, with `synchronous_commit = off` (it can be lost in a crash,
  #279). For write tools its `params` hold the arguments, not a
  `result_paths` list: `result_paths` is search-result telemetry (#161) and
  only the search tools write it.
- **Backups treat `notes_metadata` as derived** (#348). It is dumped
  schema-only, so after a restore the first pass sees an empty index.

The expensive failures for this product are **silently wrong answers an agent
acts on** and **destructive writes**. A change feed that silently drops an
event is the first kind: the agent's cursor moves past a change it never saw.
The design ranks "never lose an event behind a cursor" above "always
attribute" and both above write volume.

## Goals / Non-Goals

**Goals**

- A `content_changed_at` that moves only on a content-hash change, and
  `since`/`until`/`order_by` filters that use it.
- A feed in which a cursor is exact: no event committed after a read can sit
  at or below the `next_since` that read returned.
- Attribution recorded at the moment the server knows it (the tool call),
  with no time-window joins.
- Every event bounded, prunable and invisible across tenants. An event write
  never fails, rolls back or meaningfully delays an index pass or a write
  tool.
- Composing with `list-tasks` (#350, migration 032), which adds a second
  per-note derivation to the same pass.

**Non-Goals**

- Reconstructing history from before the migration, or from changes made
  while the server was down that were overwritten before the next pass.
- Attributing out-of-band edits to anyone. Obsidian, a sync client, `git` and
  a shell are all `human/sync`.
- Content diffs. The feed says *that* a note changed, not *how*.
- Events for non-markdown files.
- A push or subscription mechanism. This is a pull feed.

## D1: `content_changed_at`, stamped in the upsert statement

A new `notes_metadata.content_changed_at timestamptz NOT NULL`. In the batch
upsert:

```sql
INSERT … VALUES (…, :content_changed_at /* = the file's mtime */)
ON CONFLICT (user_id, file_path) DO UPDATE SET
  …,
  content_changed_at = CASE
    WHEN notes_metadata.content_hash IS DISTINCT FROM excluded.content_hash
    THEN now()
    ELSE notes_metadata.content_changed_at
  END
```

The comparison runs in the same statement that overwrites `content_hash`, so
the stamp cannot drift from the hash it describes. Every same-hash re-upsert
leaves the stamp alone: a `touch`, a stale extraction marker, a re-derive, a
stat refresh (which is a separate UPDATE that never names the column). The
id-preserving move in the indexer and `move_note` never name the column, so a
move keeps it.

**Time for a change is `now()`, the pass transaction's start, not the new
mtime.** Mtime is the signal #351 shows to be wrong. A sync client that
preserves mtime (`rsync -t`, many mobile sync apps) would stamp a real change
in the past and bury it under exactly the ordering this column exists to fix.
The cost is a lag of up to one tick (and up to
`INDEX_FULL_HASH_INTERVAL_HOURS` for an edit that kept all four stat fields,
perf-L3). For a row the index has never seen there is no previous content and
so no observed change. It takes the file's mtime, which matches the backfill
and keeps a fresh install's first pass (or the first pass after a restore)
from stamping every note "changed just now".

Alternatives considered: (a) stamping in Python by comparing `existing`
before the upsert. This is equivalent today, but it is a second copy of the
comparison, and it is wrong for a row another writer changed between the
locked read and the statement. (b) A trigger. It is invisible to the code
reader, and it fires for `move_note`'s UPDATE too, which would then need
exclusions. (c) Nullable with no backfill. Then `order_by=content_changed`
would sort every pre-033 row as unknown, which is useless for months.

**The full-hash pass** is an ordinary pass that reads every file, so a hash
difference it finds is stamped like any other. **Quarantine**: a
quarantined note's row is deleted (D7/D3 of #308). When its hash changes it
re-enters as a new row with the mtime stamp. **Provenance discard** deletes
the rows, so the re-derived rows are new rows.

## D2: Event source of truth is a table, `note_events`, not `.trash`

The issue offers "deletions from `.trash`". That covers one case only:
Obsidian configured to move deletions into the vault's `.trash`, plus our own
soft `delete_note`. It misses OS-trash deletes, permanent deletes, sync-client
deletes and `git checkout`. It has no time beyond mtime and no actor. Its
contents are emptied by the user. And it cannot tell a move into `.trash`
from a deletion followed by an unrelated file. `.trash` is also a dot
directory the walk never enters. A deletion record must outlive the row, so
it needs its own table.

```
note_events
  id             bigserial PRIMARY KEY  -- surrogate; never rendered, never accepted
  user_id        integer NULL            -- scope; NULL = single-user. NO foreign key (below)
  seq            bigint NOT NULL         -- per-scope sequence; the cursor
  occurred_at    timestamptz NOT NULL DEFAULT now()
  kind           varchar(16) NOT NULL    -- created|modified|moved|deleted|gap|reset
  path           varchar(1024) NULL      -- destination for a move; NULL for meta kinds
  from_path      varchar(1024) NULL      -- moves only
  content_hash   varchar(64) NULL        -- canonical note hash (D3) the event leaves at `path`, when known
  source         varchar(8)  NOT NULL    -- 'mcp' | 'index'
  tool           varchar(100) NULL       -- server-authored tool name, mcp rows only
  actor_kind     varchar(20)  NULL       -- usage_logs' denormalised actor, same widths
  actor_label    varchar(255) NULL
  principal_kind varchar(16)  NULL       -- 'api_key' | 'oauth'
  principal_ref  varchar(64)  NULL       -- str(api_keys.id) | oauth grant_id; never rendered
  gap_reason     varchar(32)  NULL       -- gap rows: pass_overflow|write_failed
  gap_count      integer      NULL
  reason         varchar(16)  NULL       -- removed|not_indexable|quarantined (D5); NULL otherwise
  CHECKs: kind and source enumerations; meta kinds have NULL path;
          moved has non-NULL from_path; principal_kind and principal_ref both set or both NULL;
          an index row has no actor, principal or tool (a tool row may lack a principal)
  UNIQUE (user_id, seq) NULLS NOT DISTINCT
Indexes: (user_id, path, seq); (user_id, from_path, seq) -- plain ascending, scanned backwards; not partial
         (occurred_at)

note_event_scopes                         -- one row per scope that has ever had an event
  user_id            integer NULL UNIQUE NULLS NOT DISTINCT  -- no foreign key
  last_seq           bigint NOT NULL      -- highest seq ever assigned in the scope (never decreases)
  pruned_through_seq bigint NOT NULL DEFAULT 0  -- highest seq removed by retention or a discard purge
  pruned_at          timestamptz NULL     -- occurred_at of the newest event removed

note_event_gaps                           -- durable record of a failed pass append (D5)
  id bigserial PK, user_id integer NULL, gap_count integer NOT NULL, recorded_at timestamptz
```

**No foreign keys, on purpose.** Events must outlive their `notes_metadata`
rows, so there is no FK to that table. There is none to `users` either. The
INSERT's FK check would take `FOR KEY SHARE` on the `users` row *after* the
pass already holds `notes_metadata` row locks. A permanent user deletion
takes the `users` row first and then cascades into exactly those rows. Each
waits for the other, and PostgreSQL aborts one, possibly the operator's
delete. The indexer already documents this cycle (`ProvenanceLockUnavailable`,
`_assert_still_assigned(nowait=True)`) and avoids it by never waiting on
`users` after its child locks. A foreign key would bring the wait back on
every pass. Cleanup on deletion is explicit instead (D4, "Scope lifecycle").

`actor_ref` (key prefix / `client_id`) is deliberately **not** copied. The
feed never renders it (D7), and a column that is never read is a column
someone later renders.

**A `created` event means "first seen at this path"**, which is what the
index can actually know. It is honest about its cases: a new note; one half
of a rename the pairing could not prove (a duplicate hash, or rename plus
edit), rendered as `deleted` + `created`; a note that became indexable; a
quarantined note that re-entered. The tool docstring says this.

## D3: Who writes which event, and how attribution is linked

**Tools write attributed events at call time. The pass writes only what it
discovers unattributed.** Why not join `usage_logs`: the usage row is
written after the body, asynchronously committed (it can be lost), carries
no path list for write tools, and is pruned on its own schedule. A time join
between a pass's observation and a usage row is fuzzy by up to a tick, and
it is wrong whenever a human and an agent edit within the same tick. Writing
the actor onto the event when the tool publishes is a fact, not an
inference. It is the same reason `usage_logs.actor_*` is denormalised
(#77).

| Writer | Event | Transaction |
|---|---|---|
| `move_note` | `moved` (from, to, hash when the tool read the bytes) | **inside its existing metadata transaction** (the `notes_metadata` path update), all or nothing |
| `move_note` backlink rewrites | `modified` per rewritten source (hash of the rewritten bytes) | own short transaction after the rewrites (file-only publications) |
| `create_note` | `created` (hash) | own short transaction after the confirmed publication |
| `edit_note`, `set_frontmatter` | `modified` (hash) | same |
| `write_file`, `import_from_url`, `PUT /transfer/upload` on a `.md` path | `created` if there was no incumbent, else `modified` (hash) | same; the upload is attributed to the principal that minted the link (the rule the write-bucket charge already applies at redemption), from `transfer_tokens.actor_*` |
| `delete_note` | `deleted reason=removed` | own short transaction after the confirmed delete (`delete_file` refuses every `.md` path and records nothing) |
| index pass | `created` / `modified` / `moved` / `deleted` for row changes it commits, unless suppressed | its final statement |

**Rule: where a tool already writes the database for the mutation, the event
is in that transaction.** Today that is only `move_note`'s metadata update.
It updates the row's path, so the next pass sees no move, and a separately
committed event that failed would be lost for good. In the same transaction,
an event failure rolls back the path update too (no savepoint). The tool
takes its existing "DB update failed; reindex will reconcile" branch, and the
next pass pairs the move by hash and records it as `moved human/sync`. That
is a misattribution, never a loss. A grep task (4.6) confirms no other write
tool touches the database for its mutation. Any that does later follows the
same rule.

**Every other tool's publication is file-only**, so its event goes in its own
short transaction after the publication. A failed append there needs no
durable marker, because the next pass always observes the resulting index
transition and records it unattributed. If the row still holds the old hash,
it records `modified`. If the path is new, it records `created`. If the new
bytes are not indexable (invalid UTF-8, an over-long path), it records the
row's removal as `deleted reason=not_indexable`, or for a path that never
had a row, a first-seen `created reason=not_indexable` (D5). That last case
is the one Codex round 2 found the round-1 text missed. The consequence is `human/sync` instead of the agent's label,
never a missing event. No event is written for a publication the tool
reports as uncertain (`publication_uncertain`), and the next pass discovers
whatever happened. A `partial_completion` records what the tool knows it
published.

**One canonical note hash.** A new `src/services/note_hash.py` exposes
`canonical_note_text(data: bytes) -> str | None` and
`canonical_note_hash(data: bytes) -> str | None`. The text function does a
strict UTF-8 decode and returns `None` if that fails. It then applies
universal-newline translation (`\r\n` and `\r` become `\n`, as Python text
mode does) and removes U+0000 (#308 D1). The hash is SHA-256 of that text's
UTF-8. The indexer's `read_note_at` is refactored to read bytes and call
`canonical_note_text`, so its `content_hash` *is*
`canonical_note_hash(file bytes)`, with no second copy. A test pins that the
refactor yields the same text as the old text-mode read. Tools compute the
event hash with `canonical_note_hash` over the bytes they published. Bytes
that are not UTF-8 get `content_hash = NULL`. The index will not index them,
so there is nothing to suppress against.

**Suppression (the pass):** after taking the feed lock (D4), the pass reads
the latest event *at* each candidate path in one statement. An event is at a
path when the path is its `path` or its `from_path`, served by the two
`(user_id, path|from_path, seq)` indexes (scanned backwards); latest means highest `seq`.
It drops its own event when the latest event is `source='mcp'` and one of
these holds:

- both are content events (`created`/`modified`, in either combination) and
  either the same `content_hash` is non-NULL, or both have a NULL hash with
  `reason=not_indexable` (a tool that published non-UTF-8 bytes records its
  content event with that reason);
- both are `deleted` with the same `reason`;
- both are `moved` with the same `from_path` and `path`, **and** that MCP
  move is the latest event at **both** endpoints. Any later event at either
  endpoint defeats suppression. Without the second condition, the cycle
  A→B (agent), B→A (human), A→B (human) would match the agent's first move
  at `B` and silently drop the third transition.

So "agent edits, pass notices" yields one attributed line. "Agent edits,
human edits again before the pass" yields the agent's line and a
`human/sync` line. Reading under the lock means every tool event committed
before the pass's append is visible to the check.

## D4: Per-scope sequence, serialised appends, scope lifecycle

**The cursor is a per-scope sequence, `seq`, never the global id.** A global
identity lets a tenant read other tenants' write volume from the gaps between
its own ids. With `seq`, each scope sees a gapless 1, 2, 3, … whatever any
other scope does. `ev:<seq>` means something only within the caller's scope.
The surrogate `id` is never rendered or accepted.

**Every append for a scope is serialised:** the one helper
`change_feed.append_events(session, scope, events)` runs `SELECT
pg_advisory_xact_lock(<NOTE_EVENTS_LOCK_NS>, <scope key>)`, held to commit.
Under that lock it does the following, in order:

1. Check that the scope still exists, and for a tool event that it is
   still bound to the root the tool ran under: `SELECT vault_path FROM users
   WHERE id = :uid`, a plain read with no row lock, skipped for single-user.
   The tool passes the binding of the root `_tracked` resolved for the call,
   a digest of its canonical root path, never the path. If the user is gone,
   or the current assignment's digest differs, the helper writes nothing for
   that tool and logs one security event (`change_feed_root_changed`; the
   subject is the user, with no path, root or digest).
2. Upsert the `note_event_scopes` row and take `last_seq`. The advisory lock
   already serialises the scope, so this row never contends.
3. Consume any `note_event_gaps` rows for the scope into one `gap` event.
4. Run the suppression read (pass only).
5. Insert the events with `seq = last_seq + 1 …`, and store the new
   `last_seq`.

Seq assignment happens only under the lock, and the lock is held until
commit. So within a scope seq order is commit order, and **every event not
visible to a reader's snapshot has a seq greater than every visible event of
that scope**. `last_seq` never decreases, even when retention or a discard
removes the events that held it, so a seq is never reused. The two-key lock
uses a namespace constant registered beside the existing ones in
`src/oauth/grants.py`, with the scope key being `user_id` or 0 for
single-user. It is a wire constant, for the same rolling-deploy reason as
`USER_BOOTSTRAP_LOCK_KEY`.

**Reassignment between publication and append.** A tool can confirm and
publish under user 7's root A. An administrator then reassigns user 7 to root
B, and B's first pass discards the scope and appends `reset`. The tool's
delayed append would then land A's path in B's feed, where no later pass of B
would ever purge it. The harm is a stale path from the **same user's former
root** surviving in that user's own feed. It is not a cross-tenant leak:
scope is still `user_id`, and only that user reads it. The root binding in
step 1 closes it anyway. The reassignment commits before the discard, and
the discard takes the feed lock, so an append that runs after it sees the
new assignment and drops its event. An append that runs before it is purged
by the discard. Pass events need no binding: passes of one scope are
serialised, so a pass's events always commit before the next pass's
discard, which purges them.

**Lock order.** The feed lock is the **last** lock a pass or tool transaction
takes. After it come only rows no other transaction can hold: the scope row
(serialised by the lock itself), new event rows, gap rows of this scope, and
the commit. No FK means no wait on `users`. This is the one deliberate
exception to the "advisory before any row lock" ordering in `embeddings.py`,
and the docs say so.

**Scope lifecycle (permanent user deletion).** `delete_user`'s permanent
branch deletes the scope's `note_events`, `note_event_scopes` and
`note_event_gaps` rows in its own transaction, **after** its cascade
statement and taking the feed lock **last**. Within that transaction the
order is: the `users` row, then the cascade into `notes_metadata` and the
rest, then the feed lock, then the three explicit DELETEs, then the commit.
This matches the order the indexer documents as safe. The deletion waits on
a pass's row locks, then on the pass's feed lock, and the pass never waits on
the deletion. An appender that takes the feed lock after the deletion has
committed fails step 1 (user gone) and writes nothing, so no orphan events
appear. As a backstop, retention (D9) deletes events and scope rows whose
`user_id` has no `users` row.

**Bounded wait.** The helper runs in a savepoint with `SET LOCAL
lock_timeout = CHANGE_FEED_LOCK_TIMEOUT_MS` (2,000 ms), except inside
`move_note`'s metadata transaction (D3), where failure is all or nothing by
design. What happens on timeout or error depends on the writer (D5): a tool
logs and returns, and a pass records a durable gap row. It never raises out
of a pass or a file-only tool.

**Reading.** `get_changes` runs in one `REPEATABLE READ, READ ONLY`
transaction (the `keyword_search` pattern). It computes the page and the
scope's `last_seq` and `pruned_through_seq` from the same snapshot. If they
came from two snapshots, a commit between them would let `next_since` jump
past an event the page never contained.

**`next_since`** is `ev:<seq>`, exclusive (`seq > N`). On a full page it is
the last returned seq and `more: true`. On a short page it is the larger of
the scope's visible `last_seq` and the input cursor, because everything up
to it has been examined under the filters. Seqs are unique within a scope, so
there is no tie rule beyond that. A cursor greater than the scope's
`last_seq` (from another scope, another deployment, or a forged one) is
refused with `invalid_argument` like any malformed cursor. A restore keeps
`note_events` and `note_event_scopes` together (both durable), so a
legitimate cursor never exceeds `last_seq`.

**A datetime `since`** selects `occurred_at >= since` and orders by seq.
`occurred_at` is not monotone in seq, since a long pass's `now()` is its
start. So a datetime is a way to start a feed, not a way to continue one. The
docstring says to store `next_since`.

Alternatives considered: (a) a `pg_current_xact_id()` column plus a
`pg_snapshot_xmin` horizon (the outbox pattern). It is correct, but the
horizon is cluster-wide on a shared Postgres instance, so an idle
transaction in any other database stalls every tenant's feed. (b) A settle
delay. This is a heuristic with no bound under a long pass transaction. (c) A
global identity as the cursor. It is simpler, but it leaks cross-tenant
volume (Codex round 1 BLOCKER).

## D5: Index-pass events: what the pass emits, and when it emits nothing

Collected from the pass's own classification, in memory, then appended as the
attempt's **final statement before `session.commit()`**:

- `created`: rows inserted (path not in `existing`, not a move destination).
- `modified`: upserts whose hash differs from `existing[path]`. The marker
  and re-derive same-hash re-upserts are excluded, exactly the rows whose
  `content_changed_at` moved.
- `moved`: the committed id-preserving moves (`moves` minus
  `poisoned_moves`).
- `deleted`: **every row the pass removes from the index, for any
  reason**, each with a `reason`. It is `removed` for an ordinary prune (the
  file is gone), `not_indexable` for a row at a present-but-not-indexable
  path (D7 of #308: invalid UTF-8, a path over the cap), and `quarantined`
  for a quarantine removal. The line renders the reason, so the feed always
  says the note left the index even when the file still exists, and an
  agent never reads a plain "deleted" for a file that is still there.
- `created reason=not_indexable`: a present-but-not-indexable path that has
  no row and whose latest event is not already a `reason=not_indexable`
  event. The pass reports a note that appeared but cannot be indexed once,
  not on every tick. A later change that keeps it unindexable records
  nothing more (L7).

Nothing is emitted for protected (walk-incomplete) rows or C5-deferred rows,
because the pass does not change them. Their events come from the pass that
does.

**Atomicity.** The events are in the pass transaction, so a `PoisonNote`
restart, an `IndexIndeterminate`, a cancellation or any failure before commit
writes none of them. The re-run computes its own. This is also how the
change composes with `list-tasks`: any derivation stage, that one included,
runs before the append, and a poison note it finds restarts the attempt
before any event exists. Collection reads only `to_upsert`, `existing`,
`moves` and `deleted_paths`, which no derivation mutates. The indexer
comment at the append says that any later stage goes above it.

**A failed pass append leaves a durable gap.** The pass's row changes
commit, so the next pass will not rediscover what it just recorded; an event
lost here would be lost for good. On a lock timeout or error, the helper
rolls back its savepoint and the pass inserts one `note_event_gaps` row
(`user_id`, `gap_count` = the events not recorded) in its own transaction. A
plain INSERT of a new row waits on nothing, so it commits with the pass. The
scope's next successful append, by a tool or a pass, consumes the gap rows
into one `gap` event (`gap_reason=write_failed`) ahead of its own. The gap
survives restarts and restores. If even the gap-row INSERT fails, the
attempt fails like any other database error, because a database that cannot
take one INSERT cannot take the pass's commit either.

**Baseline.** A pass over a scope with zero rows at its start (first index,
first pass after a restore of a schema-only `notes_metadata`, first pass
after a provenance discard) is not a stream of changes. It emits no
per-note events and one `reset` line. Without this, a fresh install would
write every note as `created` and overflow the cap on the first tick.

**Provenance discard** (`_reconcile_provenance` deletes the scope's rows)
also deletes the scope's `note_events` in the same transaction. Those paths
named another root, and keeping them would serve one root's history as
another's. It raises `note_event_scopes.pruned_through_seq` to `last_seq`
(and `pruned_at`) and appends one `reset`. An old cursor therefore reads
`history_truncated`.

**Overflow.** At most `CHANGE_FEED_MAX_EVENTS_PER_PASS` (2,000) itemised
events per scope per pass, in a fixed order (moves, deletes, creates,
modifies; path order within each). The remainder becomes one `gap` row with
`gap_reason=pass_overflow`, `gap_count=N`. An admin-confirmed empty-root
prune or a vault-wide reformat lands here, and that is the intended
behaviour: the agent re-syncs through `get_recent(order_by="content_changed",
since=…)`.

## D6: `get_changes`

```
get_changes(since: str, folder: str | None = None,
            kinds: list[str] | None = None,       # subset of created, modified, moved, deleted
            by: str = "any",                      # any | me | agents | human
            limit: int = 50)                      # 1..500 via _clamp_limit
```

- `_tracked("get_changes", ["since", "folder", "kinds", "by", "limit"])`,
  `title="Change feed"`, `_READ_ONLY` annotations, docstring ≤ 4,000
  characters. It is a read tool: no write bucket, no `_require_write`.
- **Owner scope is the total mapping** (`_owner_predicate_for(NoteEvent,
  uid)`): `user_id IS NULL` for an ownerless caller, `= uid` otherwise. An
  event is only ever written into the scope whose root the path is relative
  to. Tools write the caller's `current_user_id`, which is the user whose
  root `_vault_root` resolved and `_confirmed_publication` confirmed. The
  pass writes its own scope. Vault roots of active users never overlap (#262
  L1/L2 aside), so a caller sees only events about notes under their own
  root.
- `folder`: LIKE-escaped prefix on `path`, **or** on `from_path` (a move out
  of the folder is a change to the folder).
- `by`:
  - `agents`: `source='mcp'`.
  - `human`: `source='index'`. The label is `human/sync` because the index
    cannot tell a person from a sync client.
  - `me`: `source='mcp' AND principal_kind = :k AND principal_ref = :r` for
    the caller's `current_principal`. An OAuth principal is the **grant**, so
    a refreshed token is still "me", and a second key of the same account is
    not. A caller with no principal (the exempt case) is refused with
    `invalid_argument`.
- `kinds` / `folder` / `by` never filter out `gap` or `reset` lines. They
  describe the completeness of the view, not a note.
- **`history_truncated: true`** when either of these holds:
  - for an `ev:N` cursor, `N < pruned_through_seq`;
  - for a date or datetime `since`, the scope's `pruned_through_seq > 0` and
    `since` is at or before `pruned_at`, meaning events that old may have
    existed and been removed. This is conservative: it may say "truncated"
    when the removed events were all newer than `since`'s neighbours, and it
    never stays silent when they were not.
- Response cap: the page stops at the last whole line under
  `MAX_READ_RESPONSE_CHARS`. `next_since` is then the last rendered seq,
  with `more: true`.

## D7: Rendering, and what an actor label may reveal

```
Changes since ev:1200 — 3 events — more: false — next_since: ev:1203
- ev:1201 2026-10-28T14:03:11Z modified "Projects/Foo.md" by api_key "laptop" via edit_note (you)
- ev:1202 2026-10-28T14:05:00Z moved "A.md" -> "Archive/A.md" by human/sync
- ev:1203 2026-10-28T14:06:00Z gap pass_overflow count=6123 — use get_recent(order_by="content_changed", since=…) to resync
next_since: ev:1203
```

- One line per event. Every note- or user-controlled string (`path`,
  `from_path`, `actor_label`) is rendered as a JSON string literal with C0/C1
  controls, U+2028 and U+2029 escaped. A filename may legally contain a
  newline, so `_one_line`'s whitespace folding would change a path's
  identity. Escaping keeps the path recoverable and the line single.
  Everything else on the line is server-authored.
- The header carries `history_truncated: true` when it applies (D6).
  `next_since` appears in the header and as the final line.
- **Disclosure.** A line may show `actor_kind`, `actor_label` and the
  server-authored `tool`, and `(you)` when the principal equals the caller's.
  It never shows `principal_ref`, a key id or prefix, a `client_id`, a grant
  id, a user id or username, the surrogate `id`, or any tool argument. Within
  one scope every attributed event was written by a principal of that scope's
  own account, so a label is shown only to its own tenant. Seqs are
  per-scope, so no number on the page depends on another tenant's activity.
  Labels are denormalised: a deleted key's events keep its name, as
  `usage_logs` does.
- Times are `YYYY-MM-DDTHH:MM:SSZ`, UTC.

## D8: `since` / `until` / `order_by` / `cursor` on `get_recent` and `list_notes`

One parser, `change_feed.parse_utc_bound(value, role)`:

- `YYYY-MM-DD`: a whole UTC day. As `since` it is `>= D 00:00Z`. As `until`
  it is `< (D+1) 00:00Z`, so `until=2026-10-25` includes the 25th.
- A datetime **with** `Z` or `±HH:MM` is normalised to UTC. `since` is
  inclusive and `until` exclusive.
- A datetime **without** an offset is refused (`invalid_argument`). The
  server could take the server's zone or UTC, and an agent reporting to a
  person in another zone would be silently off by hours.
- `since >= until` is refused. A `get_changes` cursor (`ev:N`) passed to these
  tools is refused with a pointer to `get_changes`.

`order_by`: `"mtime"` (default) orders and filters on `modified_at`.
`"content_changed"` orders and filters on `content_changed_at`. Both are DESC
with the `file_path ASC` tie-break. A bound excludes rows whose column is
NULL. `get_recent` labels the time it shows (`modified` or `changed`). Both
docstrings say that "what changed" is `order_by="content_changed"` with
`since` (owner decision 1).

**Keyset paging, not timestamps.** A timestamp continuation cannot page
`(time DESC, file_path ASC)` without losing ties. With 501 rows at one
instant and the 500-row maximum page, an exclusive `until` skips the 501st
and no page can reach it. So both tools take an opaque `cursor`, the
keyset `(time, file_path)` of the last row of the previous page. **The envelope and owner discriminator are list-tasks' own** (`_b64url`,
`_task_owner_discriminator`), so there is one codec, not two. The cursor is versioned
base64url JSON `{v, o: order_by, t: time with microseconds, p: file_path, f:
digest of (folder, tags, frontmatter, since, until), w: owner
discriminator}`, where `w` is the first 16 hex of SHA-256 over
`owner:<user_id>` (`owner:null` for the NULL scope, as list-tasks computes it). A cursor whose `o`, `f` or
`w` does not match the call is refused (`invalid_argument`). One user's
cursor is therefore refused for another user even under identical filters.
No owner id is rendered: `w` is a one-way digest, and the cursor stays
opaque. The next page's
predicate is `col < t OR (col = t AND file_path > p)`. A row whose column is
NULL is never reached by a cursor, which is consistent with "a bound
excludes NULL".

**The footer and byte-identity.** The tools fetch `limit + 1` and print
`more: cursor=<…>` **only when the call used any of `since`, `until`,
`order_by` or `cursor`**. A call that supplies none of them issues today's
statement, unbounded and with `LIMIT limit`, and returns today's bytes with
no footer. That is the pre-1.0 compatibility contract, so an existing caller
sees nothing new. To page an `mtime` listing, a caller passes
`order_by="mtime"` explicitly.

## D9: Retention

`change_feed.prune(now)` runs in the periodic tick's `finally`, beside
`cleanup_expired_tokens`, in its own transactions, never in a pass. Per scope
and in batches of 5,000 rows it deletes events older than
`CHANGE_FEED_RETENTION_DAYS` (90; `null` disables the age limit, `0` is
refused) and events beyond the newest `CHANGE_FEED_MAX_EVENTS_PER_SCOPE`
(100,000; minimum 1,000). The row cap is never disabled, so a scope stays
bounded even with the age limit off: unbounded history in a shared database
is how #308 started. The prune **never deletes the scope's newest event**.
Each batch takes the scope's feed lock and raises
`note_event_scopes.pruned_through_seq` and `pruned_at` to the newest removed
event in the same transaction as the delete. The backstop also deletes
events, scope rows and gap rows whose `user_id` has no `users` row. The
watermark lives on `note_event_scopes`, not in `indexer_state`, because
`indexer_state` is dumped schema-only (#348) and the watermark must survive a
restore with the events it describes. A failure logs and is retried next
tick.

## D10: Settings

| Setting | Default | Bounds |
|---|---|---|
| `CHANGE_FEED_RETENTION_DAYS` | 90 | 1–3650, or `null` (no age limit); `0` refused |
| `CHANGE_FEED_MAX_EVENTS_PER_SCOPE` | 100,000 | ≥ 1,000 |
| `CHANGE_FEED_MAX_EVENTS_PER_PASS` | 2,000 | 100–50,000 |
| `CHANGE_FEED_LOCK_TIMEOUT_MS` | 2,000 | 100–30,000 |

All four go in README and `.env.example` (public-repo rule).

## Risks / Trade-offs

- [A pass and a tool contend on the feed lock] → The pass holds it from its
  final INSERT to its commit. A tool holds it for one INSERT and a commit.
  The wait is bounded by `lock_timeout`, and a timeout is a gap (pass) or a
  rediscovered `human/sync` event (tool), never a failure.
- [A pass observes a tool's publication in the milliseconds before the tool's
  event commits] → The pass records a `human/sync` event and the tool's
  attributed event follows. The result is a duplicate, never a loss (L4).
- [`delete_user` now waits on a pass's feed lock] → It already waits on the
  pass's `notes_metadata` row locks. The feed lock is held only from the
  pass's final INSERT to its commit.
- [The pass's suppression read and event INSERT add work per changed note] →
  One indexed `DISTINCT ON` over the changed paths and one INSERT per pass,
  both capped at 2,000 rows.
- [Shared Postgres: `note_events` grows with every write] → Retention plus a
  per-scope cap. About 200 bytes a row, so 100,000 rows is about 20 MB per
  scope at the cap.
- [`list-tasks` (032) lands first and changes the same pass and the same
  metadata requirements] → 033 revises 032. The `mcp-request-routing`
  MODIFIED blocks are rebased onto list-tasks' archived text before
  implementing (task 0.2).

## Migration Plan

1. Migration 033 (reconciliation shape of 026/030): pin `search_path`, set
   `lock_timeout` / `statement_timeout`.
   - Add `content_changed_at` nullable, `UPDATE … SET content_changed_at =
     COALESCE(modified_at, indexed_at)`, then `SET NOT NULL`, `SET DEFAULT
     now()` and the marker comment.
   - Create `note_events`, `note_event_scopes` and `note_event_gaps`, with
     their CHECKs, unique constraints, indexes and marker comments, and no
     foreign keys.
   - A reconciling re-run verifies the shapes and refuses an unmarked
     look-alike by name. Downgrade drops only marked objects.
2. `make deploy` already stops the app before a pending migration. On k3s the
   init container runs it. The backfill is one UPDATE over `notes_metadata`
   (about 2,577 rows on the reference deployment, low thousands per tenant).
3. The first pass after deploy finds rows, so it is not a baseline. Events
   start with it. Nothing before 033 has events (L1).
4. Rollback: the previous build neither reads nor writes any of these
   objects. `alembic downgrade 032` drops them. Feed history is lost, and
   nothing else is.

## Owner decisions (2026-10-29)

1. **`order_by` defaults to `"mtime"`.** Existing calls stay byte-identical,
   which is the pre-1.0 contract rule. The `get_recent` / `list_notes`
   docstrings and the guide line say that "what changed" is
   `order_by="content_changed"` with `since`.
2. **`SERVER_INSTRUCTIONS` is unchanged.** The guide line and the tool
   docstrings carry `get_changes`, and the instructions' 2,000-character
   budget stays as it is.
3. **The neutral guide wording is accepted.** It matches the vault-guide
   spec's framing rule and the no-ranked-tools rule.
4. **A transfer upload is attributed to the principal that minted the
   link.** This reuses the rule the write-bucket charge already applies at
   redemption (CLAUDE.md, rate limits), so one credential answers for the
   upload in both places. It is not a new rule.
5. **Retention defaults stand as two settings:** `CHANGE_FEED_RETENTION_DAYS`
   (90; `null` disables the age limit, as `OAUTH_CLIENT_UNUSED_EXPIRY_DAYS`
   does; `0` is refused) and `CHANGE_FEED_MAX_EVENTS_PER_SCOPE` (100,000).
   The row cap always applies, so the table stays bounded. Every new setting
   goes in README and `.env.example` in the same PR (public-repo rule).

## Codex spec review, round 1 (2026-10-29): all seven findings folded in

1. BLOCKER, global ids leak cross-tenant volume → per-scope `seq` cursor (D4).
2. BLOCKER, FK to `users` deadlocks with permanent deletion → no foreign
   keys, plus explicit deletion with the feed lock last (D2, D4).
3. BLOCKER, a move's event could vanish → in `move_note`'s own transaction.
   File-only tools are rediscovered by the pass. A pass failure leaves a
   durable gap row (D3, D5).
4. MAJOR, timestamp continuation loses ties → keyset `cursor` over
   `(time, file_path)` (D8).
5. MAJOR, datetime `since` after retention → conservative
   `history_truncated` using `pruned_at` (D6).
6. MAJOR, event hash vs indexer hash → one `canonical_note_hash` shared
   with the indexer (D3).
7. MAJOR, byte-identical vs footer → the footer appears only when a new
   parameter is used (D8).

## Codex spec review, round 2 (2026-10-29): four findings folded in

1. BLOCKER: an invalid-UTF-8 overwrite after a failed append was never
   rediscovered. Every index removal is now a `deleted` with a `reason`, and
   a first-seen unindexable path is `created reason=not_indexable` (D3, D5).
2. BLOCKER: an append after a vault reassignment could reintroduce the old
   root's path. Tool appends are bound to the root they ran under and
   re-checked under the feed lock (D4).
3. MAJOR: the move-suppression cycle. A move is suppressed only when the
   MCP move is the latest event at both endpoints (D3).
4. MAJOR: the note-filters cursor was not owner-bound. It now carries the
   owner discriminator `w`, with list-tasks' codec (D8).

## Implementation notes (2026-10-29)

Readings the implementation had to choose, recorded so the verifier and the
adversarial pass judge them, not discover them:

1. **Indexes.** `(user_id, path, seq)` and `(user_id, from_path, seq)` are
   plain ascending b-trees (scanned backwards for `ORDER BY seq DESC`); the
   `from_path` one is not partial. Same plans, and nothing Alembic compares
   oddly. `note_events.id` is `bigserial` rather than `GENERATED ALWAYS AS
   IDENTITY` (equivalent; never rendered or accepted). `note_event_scopes` has
   a surrogate `id` primary key beside its `user_id` unique.
2. **The principal CHECK** is `(principal_kind IS NULL) = (principal_ref IS
   NULL)` plus "an `index` row has no actor, principal or tool". An `mcp` row
   may lack a principal: `current_principal` is `None` for the exempt
   in-process caller, and refusing to record that call would lose an event.
   It renders `by agent via <tool>`.
3. **Baseline.** The `reset` is written only when the empty-scope pass writes
   rows, so an empty vault's every tick records nothing; a baseline `reset`
   directly after a discard's own `reset` is dropped under the lock.
4. **Not-indexable creations** are emitted only for a *content* reason
   (bytes not UTF-8). A path not indexable by its *name* cannot be stored in
   a `varchar(1024)` UTF-8 row either. A quarantined path with no row
   produces no event (the "Poison restart" scenario's "none for the
   quarantined note"); one with a row is `deleted reason=quarantined`.
5. **A tool deletion carries `reason=removed`**, so the pass's own prune
   (`removed`) is suppressed by the "same reason" rule.
6. **The note cursor codec lives in `src/mcp_server/tools.py`** beside the
   task codec, sharing its `_b64url` envelope and `_task_owner_discriminator`
   (which hashes `owner:null` for the NULL scope, so the delta now says
   `owner:null`); its payload keys are `{v, o, t, p, f, w}`. The bounds
   parser, rendering and appends are in `src/services/change_feed.py`.
7. **`get_changes` is a `scan`-class tool** (`CLASS_MAPPING_VERSION` 4): a
   folder or `by` filter can walk up to the per-scope cap.
8. **A root mismatch inside `move_note`'s transaction** writes no event and
   still commits the metadata update (the move happened; the reassignment
   check is about the feed only).
9. **`gap` lines are `source='index'`** whoever's append consumed the gap
   rows: they are the server's statement about completeness, never a caller's.
10. **`delete_file` records nothing**: it refuses every `.md` path.
11. **The NUL transfer-upload scenario** is pinned through the route's own
    pieces (the streaming `CanonicalNoteHasher` and `record_tool_events` with
    the minting attribution) rather than an HTTP redemption.
12. **A no-op `edit_note`** (a rewrite to identical bytes) records no event.
13. **`CURRENT_EXTRACTION_VERSION` stays 4**: 033's backfill gives every row
    a `content_changed_at`, so no re-derive is needed.
14. **Overflow is counted after suppression**, and a consumed `write_failed`
    gap line is outside `CHANGE_FEED_MAX_EVENTS_PER_PASS`.
15. **An MCP row with no actor** renders `by agent via <tool>`.
16. **`created` vs `modified` for `write_file` and the transfer upload** is
    chosen from a pre-publish `lstat` (write_file) or the token's
    `expected_fingerprint` (upload), so an overwrite minted without a
    fingerprint can be labelled `created`. Both are content events, so
    suppression and the reader's "this note changed" are unaffected.
17. **A not-indexable `created` is suppressed against any source's** latest
    `not_indexable` event, so it is reported once whoever reported it first.
18. **`CHANGE_FEED_LOCK_TIMEOUT_MS`** (2,000; 100–30,000) bounds the file-only
    tool appends as well as the pass's; the change-feed delta names it.
19. **A discard also deletes the scope's gap rows**, and a caught-up cursor
    (`ev:<last_seq at the discard>`) reads the `reset` without
    `history_truncated`; only a cursor below that seq reads it (verifier).
20. **NULL ordering columns** are excluded whenever any of the four listing
    parameters is given, `order_by` alone included, so a first page and its
    continuations agree (verifier).

## Codex implementation review, round 1 (2026-10-30)

Five findings; three fixed, two declined after triage.

1. **Fixed** — age retention deleted by `max(seq)` of expired events, and
   `occurred_at` is not monotone in seq, so an unexpired event with a lower
   seq could go. The age cut is now the prefix ending just before the oldest
   unexpired event; `pruned_through_seq` stays a prefix watermark.
2. **Fixed** — `move_note` rewrote links after its metadata and event
   transaction had failed; a rewrite of the moved note's own links changed
   its hash, so the pass recorded `deleted` + `created` and the move was
   lost. The rewrites now stop before any publication on that failure.
3. **Fixed** — a provenance discard left the previous root's
   `note_event_gaps` rows, which the new root's next append would consume
   into a `write_failed` gap line. The discard now deletes them under the
   feed lock.
4. **Declined (L11)** — the cursor is not tamper-evident. By design the
   cursor *is* the scope's plain seq: only a cursor above `last_seq` is
   refused, and a lower one from elsewhere is a valid position in the
   caller's own feed that returns the caller's own events after it. No
   tenant's data crosses.
5. **Declined (L12)** — the first event line may exceed a
   1,000-character `MAX_READ_RESPONSE_CHARS` floor. The first event is always
   admitted so the cursor advances; the overrun is bounded by the path cap
   and its escaping.

## Codex implementation review, round 2 (2026-10-30)

Run on eeb607a. Verdict FAIL, on one new finding, which the owner's triage
declined.

1. **VERIFIED**: round-1 finding 1. Age retention now deletes only the
   expired prefix, ending just before the oldest unexpired event.
2. **VERIFIED**: round-1 finding 2. `move_note`'s link rewrites stop when
   the metadata and event transaction fails.
3. **VERIFIED**: round-1 finding 3. A provenance discard drops the
   previous root's `note_event_gaps` rows.
4. **Declined (L14), MAJOR as rated by the reviewer.** A non-UTF-8
   markdown publication in a scope with no indexed notes, whose tool-side
   append also failed, is not rediscovered: `_collect_feed_events` returns
   before the not-indexable candidates when `locked` and `to_upsert` are
   both empty. Triage: the input needs a fresh scope whose first note is
   unreadable plus a simultaneous append failure. The cost is one feed line
   for a file no agent can read, and the file itself is untouched.

## Accepted limitations (L1–L14)

Copied here from tasks.md at archive (task 7.9).

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
