## Context

Current state (file:line as of `0bfddc8`):

- Bytes become text in exactly one place, `read_note_at` (`src/services/indexer.py:1527`), UTF-8 strict. Every indexer reader goes through it: the scan via `_read_and_hash` (:1727), and `read_note_beneath` (:1580) for the embed backlog (:4237), reconciliation (:3865), link backfill (:3211) and the tsvector rebuild (:3220). It is indexer-only; no MCP tool reads through it.
- `content_hash` is `sha256` of the decoded text (`_content_hash`, :684). The write-precondition hash is separate and deliberately so (`vault.py:1875`).
- One index pass for one scope is one transaction: generation lock (:2269) → moves → batch upsert (:2616, 100 rows per statement) → tsvectors (:2701, `write_tsvector_bounded`) → deletes → links → provenance stamp → single `commit()` (:2868). "Commit together so a new hash is never paired with stale search data" (:2864).
- `write_tsvector_bounded` (:762) retreats by halving to a 100,000-char floor, each attempt in its own savepoint, and re-raises at the floor. A short note starts below the floor, so a NUL fails on the first attempt and aborts the pass.
- Failure accounting: `consecutive_failures` is a local of `run_indexer_loop` (:6111), incremented only in single-user mode; the multi-user path (`_index_pass_once`, :5990) swallows the error. In single-user mode an index failure also skips `embed_vault` (:6165). `/health` (`src/main.py:776`) returns a static `ok`.
- A pass that aborts leaves the scope due, and `_last_full_hash` (:1905) advances only after a clean committed full-hash pass — so after a restart, every failing tick is a full-hash pass re-reading and re-writing everything. That is the 650 GB/day.
- `tests/integration/test_tsvector_bounded_pg.py:162-214` uses a NUL note as the *fixture* for "the floor fails and the pass aborts with nothing committed".

Constraints carried from `docs/architecture/indexing-and-embeddings.md` that this design must not undo:

- **No skip list that strands a committed `content_hash` beside a stale keyword vector** (doc :260-270; code :777-784).
- **One commit per pass**; never a new hash beside stale derived rows.
- **Never rewrite the note's bytes** to make it indexable; convert at each JSON boundary, not at the parse (#154).
- The full rebuild (`rebuild_tsvectors`) stays atomic and fails loud to the operator.

## Goals / Non-Goals

**Goals:**
- A NUL anywhere in a note — body or YAML-escaped frontmatter — indexes normally.
- A note that fails at a per-note database boundary for *any* reason costs that note, not its owner's whole index, and cannot cause whole-pass rewrite loops.
- A persistently failing indexer, or a quarantined note, is visible on `/health` to an ordinary uptime monitor.

**Non-Goals:**
- Changing the write tools. Refusing NUL in `create_note` / `edit_note` / `write_file` is a separate owner decision (Open Questions).
- Making the full `rebuild_tsvectors` non-atomic. It keeps aborting loudly; with D1 in place, NUL no longer triggers it.
- Isolating failures that are not attributable to one note (database down, lock timeout, provider outage). Those still fail the pass and now count toward degradation.
- A panel UI for the quarantine. It is visible through the run record (D5) the panel already renders; a dedicated view can follow.

## Decisions

### D1. Remove NUL in `read_note_at`, before hashing — and nowhere else in the body path

`read_note_at` returns `text.replace("\x00", "")`, and logs one WARNING per read naming the vault-relative path and the NUL count (never content). Because every reader and re-verifier shares this function, the scan's hash, the embed pass's `StaleCertification` check and the rebuild's certification all compute over the same NUL-free text — so no reader can disagree with another about a hash (a perpetual re-index loop would need *some* reader to strip and another not to). A NUL-free note's text is unchanged, so its hash is unchanged: **no mass re-index on deploy**.

- *Removal vs. replacement with a space / U+FFFD:* removal is what the reporter asked for and what Postgres would do if it could; replacement changes token boundaries in a way that is no more "correct". Removal is chosen, and is the documented behaviour.
- *Strip at each DB boundary instead:* rejected — five-plus boundaries (tsvector, title, tags, links, chunk text), and any one missed is the next #308. The decode point is one line.
- *Hash the raw bytes, strip after:* rejected — the hash must describe what was indexed, and every re-verifier would need the same split.
- The warning fires on each actual read, which after the first pass is at most once per full-hash interval (24 h) per file thanks to the stat shortcut. Acceptable; it is the operator's pointer to the file.

### D2. Remove NUL from derived title and tags in the shared helpers; from JSONB at the indexer boundary; refuse NUL-bearing link targets

YAML `"\0"` produces a NUL from NUL-free bytes, so D1 does not cover frontmatter.

- **Title and tags — shared normalization.** `vault.note_title` and `vault.extract_tags` are the one derivation used by the indexer (`_note_title` wraps the former), `read_note`'s metadata (`vault.py:2079-2080`) and `move_note` (`tools.py:5281,5337`). NUL is removed inside those two shared functions, so every surface shows the same title and tags — the #154 "one title rule" extended, not forked. Raw note content and the parsed frontmatter mapping returned by `read_note` / re-serialised by `set_frontmatter` are **not** touched.
- **Tag length.** `extract_tags` drops any tag (frontmatter or inline) whose UTF-8 encoding exceeds 1,024 bytes, logged at WARNING. `tags` has a GIN index, and a key over ~2.7 KB raises 54000 (`index row size exceeds maximum`), which would otherwise quarantine a whole note for one tag.
- **JSONB.** `_jsonb_value` removes NUL from every string key and value on its existing walk (same boundary #154 uses; first-key-wins collision rule unchanged).
- **Link targets.** Markdown hrefs are percent-decoded (`links.py:754`), so `[x](bad%00target.md)` yields a NUL from NUL-free text. Removing the NUL would *invent* a link to `badtarget.md`, a different file. A link whose decoded target, anchor or alias contains U+0000 is therefore **not a link**: the extractor drops it, exactly as it drops an empty href. Same for wikilink parts (the regex at `links.py:75` admits NUL; after D1 the body has none, but the rule is stated at the extractor so it holds for any input).

### D3. Quarantine: a poison note is removed from the index, never kept stale

**Poison failure.** A database error whose SQLSTATE is class 22 (data exception — 22021, 22P02, 22003, 22001, 2200N…) or 54000 (program limit exceeded), raised by a write attributable to exactly one note during an **incremental** index pass. The attributable write sites are all of the pass's per-note writes:

1. the id-preserving **move** UPDATE (an over-long destination is caught earlier by D7; this site is the backstop for anything else);
2. the **batch upsert**;
3. the **keyword-vector** build failing at its floor;
4. the note's **link** inserts.

Each site runs inside a savepoint (`async with session.begin_nested()`, `try` *outside* the context, as `write_tsvector_bounded` already does), so a failure leaves the outer transaction usable. A batch statement (upsert, links) that fails with a poison SQLSTATE is replayed one row at a time, each row in its own savepoint, to find the offending note; if no single row fails alone, the error is not a poison failure and is re-raised. **Every** row of the batch that fails alone is quarantined in the same restart, not just the first, so a folder of N bad notes costs one restart, not N/5 ticks. The rebuild (`rebuild_tsvectors`) is excluded and stays atomic.

**Restart, not repair-in-place.** Once the offending note is identified the pass raises `PoisonNote(owner, rel_path, content_hash, sqlstate)`; the whole transaction rolls back and the pass is re-run **from classification** (moves included), with the note in the quarantine. Repairing in place would leave move bookkeeping (`moved_new_paths`, the `to_upsert` exclusions at `indexer.py:2594-2612`) describing writes that were undone. At most `INDEXER_QUARANTINE_RETRIES_PER_TICK` (default 5) restarts per scope per invocation; beyond that the pass fails as today and counts toward D4.

**What a quarantined note looks like in the index: absent.** The quarantine is an in-process map keyed by `(owner, rel_path)` holding the failed `content_hash`, SQLSTATE and time. During classification a file whose current hash equals its quarantined hash is treated as **present on disk but not indexable**: it is excluded from `to_upsert` and from move detection (as source or destination), and **any existing row at that path is deleted in the same transaction**, with its embeddings and outgoing links, through the pass's ordinary delete path. So:

- no committed `content_hash` is ever paired with derived data from other content (the hard invariant), and
- **search never serves a quarantined note's superseded content as current.** Keeping the old row would do exactly that — `semantic_search` computes staleness only as `embedded_content_hash != content_hash` (`embeddings.py:1629,1647`) and the old row satisfies it. Absence is the same degradation an unreadable file already has, and `read_note` still reads the file from disk.
- Inbound links to the note lose their resolution (`ON DELETE SET NULL`), as for any deleted-and-recreated note; they re-resolve when the note returns, subject to the pre-existing limitation recorded at `openspec/specs/index-integrity/spec.md:217`. Accepted; this path is reached only by an input D1/D2 do not already neutralise.

**A quarantine is not a read failure and does not make a re-derive incomplete.** The file was read and hashed; its row is absent by decision, so a re-derive that quarantines a note has still established, for every row it leaves, that the row was derived from the current root. The existing requirement "A re-derive that skipped any file is incomplete…" exists because a skipped file may leave a *stale row from the old root*; a quarantined path leaves **no** row, so it is not a skip in that sense (MODIFIED). Nor does it keep the scope due for a full-hash pass. Without both rules a single quarantined note re-arms whole-scope rewrites — #308 by another route.

The entry is cleared when the note indexes successfully, when the file is deleted, or when the scope's index is discarded; it does not survive a process restart (a restart re-learns each poison note at the cost of one rolled-back attempt). One ERROR log per quarantine (path, SQLSTATE, no content).

**One entrypoint.** Recovery and accounting live in `index_vault` itself (or a wrapper that every caller uses), so the **startup** pass (`indexer.py:6059,6086`), the **periodic** pass (`_index_pass_once`) and the panel's **Reindex now** (`control_panel/routes.py:2998,3022`) all get the same bounded restart and update D4's state. Startup backfill ordering and manual `full_hash` semantics are unchanged.

### D4. Failure accounting and `/health`

A module-level registry in `indexer.py` holds, per scope (`None` for single-user, `user_id` otherwise):

- `index_consecutive_failures` — incremented when an index pass for the scope raises (after D3's bounded restarts), reset by a successful pass, from **every** entrypoint in both modes;
- `rederive_incomplete` — incremented by a committed re-derive that withheld its stamp (D8), reset when one records provenance or the scope leaves re-derive;
- `embed_consecutive_failures` — incremented when a completed embed pass for the scope reports typed provider failures (`EmbedPassResult`, `indexer.py:280-293`), reset by an embed pass with none. A quarantine is **not** a failure of either kind;
- `last_success_at`, `last_failure_at`.

Plus two process-wide signals that are not per scope:

- **indexer task not running** — set by `_on_indexer_done` (`main.py:405`) when the background task ends by exception or returns while the app is not shutting down; not set on lifespan cancellation, nor when indexing is disabled (`MCP_SANDBOX_MODE`), where `indexer.status` is `"disabled"`;
- **enumeration failures** — consecutive failures of the loop's own per-tick work that is not attributable to a scope (user enumeration, overlap detection), counted like a scope.

The CRITICAL "manual intervention required" log fires once when a counter first reaches `INDEXER_DEGRADED_AFTER_FAILURES` (default 3, replacing the hard-coded 5) and re-arms on reset.

`/health` adds:

```json
"indexer": {"status": "ok" | "degraded" | "disabled",
            "task_running": true,
            "failing_scopes": 0, "embedding_failing_scopes": 0,
            "max_consecutive_failures": 0,
            "quarantined_notes": 0, "last_success_at": "…" | null}
```

and top-level `status` is `"degraded"` when the task is not running, any index/embedding/enumeration counter is at or over the threshold, or `quarantined_notes > 0`. In-process state only — it never probes. **HTTP stays 200**: the Kubernetes manifests use `/health` for liveness (`deploy/kubernetes/base/deployment.yaml:108-139`) and a restart loop cannot repair vault content or a provider outage; operators alert on the `status` field. **No paths, error text, SQLSTATEs or user ids**: `/health` is unauthenticated and publicly routed.

### D5. Surface the quarantine where the operator already looks

The pass's `indexer_runs` row names quarantined paths in its `error` column — `"quarantined N note(s): <path>, …"` (≤ 5, then `…`), each rendered with `backslashreplace` so a path can never itself be unstorable — so the dashboard strip and health page show it without a template change. D4's success/failure is **not** derived from that column (a quarantining pass is a successful pass).

### D7. Paths and contents the database can never hold are "present but not indexable", decided at the scan (bug hunt)

The bug hunt (below) found three inputs that D1–D3 as first written either missed or handled expensively:

- **A filename that is not valid UTF-8.** `os.scandir` on an fd yields a surrogate-escaped `str` (`caf\udce9.md`); asyncpg cannot encode it and raises a *client-side* `DataError` (SQLSTATE `22000` / or none after SQLAlchemy's wrapping) at the first bind — the move UPDATE or the batch upsert — so today it wedges the pass exactly like #308. The codebase already knows this (`encode_realpath`, #91) but the scan never checks.
- **A path longer than `file_path` allows** (1,024) — 22001 at the same sites.
- **A note rewritten with non-UTF-8 content.** `_scan_vault` adds the path to `seen` before the read, so on `UnicodeDecodeError` the existing row is neither upserted nor pruned and `semantic_search` keeps serving the old content as current — contradicting D3's premise that an unindexable note is absent. Plausible without an adversary: `write_file` with base64 or `import_from_url` of a Latin-1 page onto an existing `.md`.

All three are decided **at the scan**, before any write, and treated like a quarantined note: **present on disk, not indexable, any existing row at that path deleted** (with embeddings and outgoing links) through the ordinary delete path, excluded from `to_upsert` and move pairing, logged at WARNING with the path rendered via `backslashreplace`. The scan checks `vault.is_encodable(rel)` and `len(rel) <= MAX_PATH_CHARS`; the C4 locked re-read applies the same checks. D3 remains the backstop for anything not predictable at the scan.

This is deliberately **not** extended to a file that cannot be *read* (EACCES, EIO, a dangling symlink): for those the existing decision stands — the row may be correct for a file merely unreadable at that moment, so it is kept (index-integrity spec, the re-derive requirement). A decode failure is different because the bytes *were* read and are provably not the bytes the row was derived from.

### D8. A skip withholds the re-derive stamp only if it could hide a foreign row (bug hunt)

Today any skip withholds the provenance stamp, and a re-derive re-runs every tick until one completes — forcing every file into `to_upsert` (`indexer.py:2210,2367`) with a keyword-vector UPDATE and link rebuild each. One unreadable file with no row behind it therefore commits a **full-scope rewrite every tick, indefinitely** — #308's write amplification by a route the spec currently *accepts*, on a rationale ("a vault the pass already reads in full") the stat shortcut (#282) has since made false.

The requirement exists to stop a skipped path from certifying a **foreign row**. A skip can hide one only if a row exists at that path. So the stamp SHALL be withheld only by: a skipped path that has a row in the locked snapshot; a directory walk failure (anything beneath it could have one); or a skip on a path already in `to_upsert` (buffered-body / link skips). A read skip on a path with no row, and every D7 "not indexable" path and D3 quarantine (whose rows are deleted), do not withhold. A scope that stays incomplete for `INDEXER_DEGRADED_AFTER_FAILURES` consecutive passes is counted by D4 and reported degraded, replacing "log the paths on every pass" as the only signal.

### D9. Single-user tick isolation (bug hunt)

In single-user mode an index failure currently jumps past `embed_vault`, cache prewarm and `cleanup_expired_tokens` (`indexer.py:6159-6178`) — the only caller of the token, code and unused-OAuth-client sweeps. D4 already rewrites this loop: the index stage is caught on its own (recorded for D4), and the embed stage still runs over already-committed rows; `cleanup_expired_tokens` moves to the tick's `finally` beside `flush_expired`, in its own `try`. This mirrors the multi-user `_index_pass_once`.

### D6. Tests

- `tests/integration/test_tsvector_bounded_pg.py`: split the two NUL-fixture tests. The **incremental** one uses a genuine server-side floor failure on one note (a real PostgreSQL statement error, e.g. a temporary trigger raising `program_limit_exceeded` for that path — not a Python-raised exception, so transaction recovery is exercised) and asserts quarantine + the rest committed. The **full-rebuild** one (:249-273) keeps asserting whole-rebuild rollback and operator-visible failure, with the same synthetic trigger in place of NUL.
- New real-Postgres tests: #308 repro indexes and is searchable as `helloworld`, and the next unchanged tick writes nothing; NUL note embeds; YAML `"a\0b"` title/tags/key; `[x](bad%00target.md)` produces no link and does not resolve to `badtarget.md`; poison in each attributable site (a genuine server-side failure on a valid-length move, upsert row, tsvector, link row) is quarantined while other notes commit, **after an earlier successful move in the same pass**; an already-indexed-and-embedded note whose edit becomes poison is **removed** from the index (not served stale by `semantic_search`/`keyword_search`); edit clears quarantine; a quarantine during a re-derive stamps provenance and the next tick re-upserts nothing; retry bound exceeded fails the pass; startup and manual-reindex entrypoints recover the same way.
- Bug-hunt tests: a non-UTF-8 filename and a > 1,024-char path are skipped at the scan and the rest commits; an indexed note rewritten as Latin-1 is removed (not served stale); a re-derive with one unreadable row-less file stamps and the next tick re-upserts nothing, while a re-derive with an unreadable file that *has* a row does not stamp; a 3 KB tag is dropped and the note indexes; a batch with three poison rows quarantines all three in one restart; a single-user index failure still runs the embed stage and the token sweep.
- Unit tests: `/health` shapes (ok; degraded by index failures, embedding failures, dead task, enumeration failures, quarantine; disabled; body contains no path or id), per-scope multi-user counting, CRITICAL-once.

## Risks / Trade-offs

- [A quarantined note — new or previously indexed — is absent from search until edited] → by design, rather than served stale; logged at ERROR, counted on `/health`, named in the run record; with D1/D2 no known input reaches this.
- [Inbound links to a quarantined note lose resolution] → same as delete-and-recreate today; documented pre-existing limitation.
- [Restart repeats up to N partial passes' writes when poison notes first appear] → bounded per tick; subsequent ticks exclude the note up front. Far below today's unbounded loop.
- [A non-note-specific class-22 error mis-attributed to a row] → the per-row replay identifies a row only if that row fails alone; if no single row fails, the error is not a poison failure and the pass fails normally.
- [NUL removal merges tokens: `a\0b` is searchable as `ab`] → declared behaviour; the file keeps its bytes.
- [`/health` degraded does not change HTTP code, so a status-code-only monitor misses it] → documented in DEPLOYMENT.md; changing the code would turn liveness probes into a restart loop.

## Migration Plan

No schema change. Deploy normally. On first pass after deploy, NUL-free notes keep their hashes (no re-index); a previously wedged scope completes its pass, which is one full write of the backlog, then returns to incremental. Rollback: redeploy the previous image — behaviour returns to the pre-change abort.

## Bug hunt (2026-09-30) — disposition

An 8-agent read-only hunt (4 bug classes, each finding independently verified and triaged) ran against this spec. Folded in: D7 (non-UTF-8 filename — HIGH, found by two hunters independently; over-long path; non-UTF-8 content served stale — MEDIUM), D8 (re-derive rewrite loop — MEDIUM), D9 (single-user tick isolation — LOW), D2 tag bound (LOW), D3 all-failing-rows-per-restart, D5 path rendering. Filed separately: #309 — an unlistable directory or an empty-but-mounted root prunes the scope's rows and embeddings on a "clean" run (HIGH — a different class, a wrong prune, with its own spec change); #310 — NUL / surrogate / NaN in logged tool arguments loses the `usage_logs` row and the rate-refusal coalescer requeues the unstorable template every tick (LOW). Declined per triage: a FIFO or `/dev/zero` symlink or hung NFS mount blocking the scan (no plausible input on this host; `INDEX_STAT_SHORTCUT=false` guidance already covers network mounts); a permanently unreadable file keeping full-hash passes due (reads, not writes; bounded by D8); NUL in search/filter arguments returning a tool error (correct refusal, no state); OAuth endpoints 500 on NUL `client_id` (no state, unauthenticated caller only harms itself).

## Review round 1 (implementation) — disposition

- Fixed: confirm-retry before quarantine (C8); classifier no longer follows exception context through connection/cleanup failures (C4); panel shows a quarantining pass as "quarantined", not "failed"; single ERROR without driver text for a quarantined keyword-vector failure.
- Declined (accepted limitations): (L1) a quarantine registered during a recovery attempt that then fails for an unrelated, non-poison reason leaves the note's previous row in place until the next successful pass — the same staleness every failed pass has; repeated failure is reported degraded on /health. (L2) link-resolution UPDATEs, stat refresh and deletes are not attributed: they write only ids, integers and timestamps, and no note content can make them fail; only an injected trigger does.
- Corrected rationale and filed as follow-up: a re-derive withheld by an unreadable file that still has a row re-upserts every file each tick until the file is readable again (pre-existing; made rarer by D8 and now reported degraded on /health after INDEXER_DEGRADED_AFTER_FAILURES passes). The earlier "reads, not writes" note applied only to full-hash due-ness outside re-derive. Per-row re-derive progress tracking is the real fix — issue #311.

## Open Questions

- **Owner decision (follow-up, not this change):** should `create_note` / `edit_note` refuse content containing NUL? It would stop agents planting it (the #308 origin) at the cost of a new refusal on the write surface, which is an adversarial-review trigger of its own.
- Codex review: is SQLSTATE class 22 + 54000 the right poison set, or too broad/narrow?
