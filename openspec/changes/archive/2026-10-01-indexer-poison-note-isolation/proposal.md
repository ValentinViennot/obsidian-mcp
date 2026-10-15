## Why

GitHub #308: two notes containing raw NUL bytes (`\x00`) wedged a user's indexer for 8.5 days. PostgreSQL `text` cannot hold 0x00, so the keyword-vector UPDATE raised `CharacterNotInRepertoireError`; the retreat in `write_tsvector_bounded` only addresses *size*, so it re-raised at the floor, the pass's single transaction rolled back, and every tick re-upserted and re-vectorised all ~5,000 notes and threw the work away — 2,091 times, ~650 GB/day of writes, no new note indexed or embedded, while `/health` answered `ok` and the only signal was a CRITICAL log line.

This is the third route to the same failure class — one note takes indexing down for its whole owner (#126 title, #154 non-finite frontmatter, now NUL). The previous two were fixed value-by-value at the boundary that broke. That fixes the route, not the class, and the class stays silent. NUL is increasingly likely as vaults are written by agents (the reporter's notes were agent-written documentation of code using `\0` as a delimiter), and nothing in `create_note` / `edit_note` rejects it.

## What Changes

- **NUL is removed from note text at the indexer's single decode point** (`read_note_at`), before hashing, so every derived column (tsvector, title, tags, links, chunk text) and every re-verifying reader (embed backlog, reconciliation, link backfill, tsvector rebuild) sees the same NUL-free text and the same hash. The file on disk is never modified. A WARNING names the file.
- **NUL is removed from frontmatter-derived strings** — in the shared title and tag derivations (so indexer, `read_note` metadata and `move_note` agree) and from JSONB keys and values at the indexer's JSON boundary — because YAML's `"\0"` escape produces a NUL from NUL-free bytes. A link whose percent-decoded target contains NUL (`[x](a%00b.md)`) is not a link, rather than being silently redirected to `ab.md`.
- **A note that still fails at a per-note database boundary is quarantined and removed from the index, not fatal to the pass.** A data-class error attributable to one note — at its move, its upsert row, its keyword-vector floor, or its links — rolls the attempt back, records `(owner, path, content_hash)` in an in-process quarantine, and re-runs the pass from classification without that note (bounded). Any existing row for the note is deleted in the same transaction, so search never serves superseded content as current and no committed hash sits beside other content's derived data; it is retried when its content changes. Startup, periodic and manual reindex share this recovery. **Behaviour changes:** the incremental floor-failure scenario no longer aborts the pass, and a quarantined note does not make a re-derive incomplete (two MODIFIED requirements).
- **Indexer failure is visible to monitoring.** Consecutive failed index passes and consecutive embedding-provider failures are counted per scope from every entrypoint in both modes (today the multi-user loop never counts), and a dead indexer task is recorded. `/health` gains an `indexer` object and reports top-level `status: "degraded"` when the task is dead, a counter reaches the threshold, or a note is quarantined. HTTP status stays 200 — a liveness restart cannot fix vault content. `/health` carries counts only: no paths, no error text, no user identity.
- **Found by a same-class bug hunt and folded in:** a non-UTF-8 *filename* or an over-long path wedges the pass exactly like #308 (it fails at the first bind, before any NUL logic), so both are decided at the scan as "present but not indexable"; a note rewritten with non-UTF-8 content is removed rather than served stale; a re-derive is no longer held open (a full-scope rewrite every tick) by an unreadable file with no row behind it (MODIFIED requirement); tags over 1,024 bytes are dropped; in single-user mode an index failure no longer stops the embed stage and the token/OAuth-client sweep.
- The two real-PostgreSQL tests that use a NUL note as their "floor failure" fixture move to a synthetic floor failure; new tests prove a NUL note indexes and a poison note is quarantined while the rest commits.

Out of scope (recorded in design as a follow-up decision for the owner): refusing NUL in `create_note` / `edit_note` / `write_file` content.

## Capabilities

### New Capabilities
<!-- none -->

### Modified Capabilities
- `index-integrity`: NUL-free derivation of every indexed value; per-note quarantine for poison notes; the incremental floor-failure scenario changes from "abort the pass" to "quarantine the note, commit the rest".
- `panel-ops-health`: `/health` reports indexer degradation (consecutive failures, quarantined notes) without disclosing paths or tenants.

## Impact

- `src/services/vault.py`: `note_title`, `extract_tags`. `src/services/links.py`: NUL-bearing link parts. `src/control_panel/routes.py`: manual reindex goes through the shared recovery.
- `src/services/indexer.py`: `read_note_at`, `_note_title`, `extract_tags`, `_jsonb_value`, `write_tsvector_bounded` call site in the incremental pass, the batch upsert, `_index_pass_once` / `run_indexer_loop` failure accounting, a new quarantine registry and failure-state accessors.
- `src/main.py`: `_on_indexer_done`, `/health` response body (additive fields; `status` can now be `"degraded"`).
- `src/config.py`: `INDEXER_DEGRADED_AFTER_FAILURES` (default 3), `INDEXER_QUARANTINE_RETRIES_PER_TICK` (default 5).
- Tests: `tests/integration/test_tsvector_bounded_pg.py` fixtures; new unit and integration tests.
- Docs: `docs/architecture/indexing-and-embeddings.md` (the floor-failure bullet and the "retries next tick" statement), `README.md` / `DEPLOYMENT.md` monitoring note for `/health`.
- No migration. No change to write tools or to note bytes on disk.
