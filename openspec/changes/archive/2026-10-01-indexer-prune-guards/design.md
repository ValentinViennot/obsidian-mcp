## Context

Current state (main @ ae0afca, after #308):

- `discover_markdown_files_at` (`src/services/indexer.py:1777-1845`) catches a `scandir` failure (:1799-1805) and a child `os.open` failure other than ELOOP/ENOTDIR/ENOENT (:1826-1835) and records only a formatted string (`"<prefix or '.'> (directory: <err>)"`). A failure of the root's own listing appears as the entry `". (directory: …)"` and is not otherwise distinguished.
- `ScanResult` (:2079-2099) carries `walk_failures` as strings; `_scan_vault` (:2160-2228) copies them into `skips`, `unverified` and `walk_failures`. No prefix set exists.
- The prune set is `set(existing) − seen`, filtered only by the C5 per-path snapshot check (:3072-3087), executed at :3464-3473. Move pairing builds `deleted_by_hash` from that set (:3097-3110), so a row under an unlisted directory can be UPDATEd to an unrelated new path with the same hash. The quarantine sweep clears entries whose path is not in `seen` (:3646).
- `pinned_root` (:1306-1335) refuses only an unopenable root. There is no zero-files check; the only signal is the `Found N markdown files` log line (:2691) and the DEPLOYMENT.md pitfall (:578-582).
- #308's accounting already does the right thing for a pass that **raises**: every entrypoint records `record_index_outcome(uid, False)` (indexer.py:6866, :6934, :6969; routes.py:3006), the run row's `error` is set and labelled `failed`, and `/health` degrades after `INDEXER_DEGRADED_AFTER_FAILURES` (3).
- #308's D8 already withholds the re-derive stamp on any walk failure (:2688-2689) — the spec's reasoning "anything beneath it may have a row" — but the prune ignores it.
- Multi-user: prunes are scoped by `user_id` (:3468-3471); an unopenable per-user root is already quarantined by the overlap snapshot (`RootUnexaminable`) and refused before any prune. An empty-but-openable root is examinable and is not.

Constraints from `docs/architecture/` and `openspec/specs/index-integrity/spec.md` that this design keeps:
- The walk precedes the generation lock and **every mutation is decided against rows read under the lock** (spec :1616) — the new exclusions and checks are applied to `locked`, not only to the pre-lock snapshot.
- Anchoring "SHALL NOT change what the index contains"; ELOOP/ENOTDIR are deliberately not skips (spec :243) — unchanged. ENOENT (a directory that vanished between listing and open) also stays a non-skip: its rows are genuinely gone.
- D7 "not indexable" deletes stay (those paths were seen). The provenance discard path is untouched.
- C5 deferral ("not pruned, not paired as a move") remains per path; this change adds a per-prefix rule beside it.

## Goals / Non-Goals

**Goals:**
- No row is deleted or move-paired because the walk could not see it.
- An empty or unlistable root never deletes a scope's index without an explicit operator action.
- Both conditions are visible: run record, `/health` degradation, and a log line naming what was protected.

**Non-Goals:**
- Detecting a non-empty but wrong mount (a smaller directory mounted in place of the vault). That needs a share-of-index threshold; not requested. Accepted limitation L1.
- Changing ELOOP/ENOTDIR/ENOENT handling, the provenance discard, or the D7/quarantine deletes.
- A durable (database) record of the empty-prune permission.

## Decisions

### D1. The walk records structured failed prefixes

The walk callback records, for every directory it could not open or list (the same errnos that are recorded as skips today), the vault-relative prefix — `""` for the root itself. It also records the entry's path as a failed prefix when the entry's **type** cannot be determined (`DirEntry.is_dir()` raising, `indexer.py:1814-1818`): such an entry may be a directory, so its exact path and its subtree are protected (codex spec r1 B1). `ScanResult` gains `walk_failed_prefixes: list[str]`; `walk_failures` keeps its human-readable strings. No change to which errnos count.

### D2. Rows under a failed prefix are neither pruned nor move-paired

In `_index_vault_attempt`, before move pairing, a path `p` from `locked` is **protected** if, for some failed prefix `pref ≠ ""`, `p == pref` or `p.startswith(pref + "/")`. Protected paths are removed from the prune set before `deleted_by_hash` is built, so they can be neither deleted nor chosen as a move source. Their quarantine entries are not cleared by the "not seen" sweep. Everything else in the pass commits as today.

- *Why not abort the whole pass on any subdirectory failure:* one unreadable folder would then stop indexing for the whole vault — the #308 failure class. Protecting the subtree costs nothing elsewhere.
- *Why prefix match on `/`:* `sub` must protect `sub/a.md` and `sub/x/b.md` but not `subway.md`.
- The pass logs one WARNING naming the failed directories (bounded list) and the number of rows protected. The run record's `error` gains a line `walk incomplete: N dir(s) not listed: <dir>, …` (≤ 5, then `(+N more)`, backslashreplace-rendered as for D5 of #308), and `run_outcome` labels such a run **failed** (it is: part of the vault was not indexed).
- The scope's new `walk_incomplete` counter (D4) is incremented by a committed pass with any non-root failed prefix and reset by a pass with none.

### D3. An unlistable root, or an empty root over an existing index, is indeterminate

`_index_vault_pinned` raises `IndexIndeterminate` (a new `RuntimeError` subclass), **before** the locked transaction and with nothing written, when either:

- the root's own listing failed (`""` in `walk_failed_prefixes`); or
- the walk discovered **no** `.md` files at all (`seen` is empty) and the scope has at least one row in the pre-lock snapshot, **unless** the scope holds an unexpired empty-prune permission (D5).

The same predicate is evaluated again against `locked` inside `_index_vault_attempt` (a row may have been inserted between snapshot and lock by a concurrent write path); there it raises before any mutation, and the attempt's transaction rolls back with nothing written.

The error message names the condition and the row count, never paths: `vault root is empty but the index holds N notes; nothing was deleted. If the vault was emptied on purpose, confirm it in the panel (Settings → Danger zone, or the user's page).` It surfaces through #308's existing accounting: run row `failed`, `index_consecutive_failures`, CRITICAL at the threshold, `/health` degraded. The embed stage still runs over committed rows in single-user mode (#308 D9), including after a **manual** single-user reindex, whose stages this change isolates the same way (today it rethrows before embedding, `routes.py:3041-3044`; codex spec r1 m1).

- *Why "no files at all" rather than a fraction:* the owner chose an explicit confirmation for the zero-files case and no threshold. A zero-file root over a non-empty index is unambiguous enough to refuse; any threshold would be a heuristic (L1).
- *Why `.md` files and not any entry:* the index holds only notes; a root that contains attachments but no notes still deletes every row.
- A scope with no rows (a new user, an emptied-and-confirmed vault) is never refused.
- **Exception — the provenance discard.** `_reconcile_provenance` (indexer.py ~2377-2434) runs before the walk and deletes a scope's index when its vault **assignment** demonstrably changed (index-integrity: "An assignment that demonstrably changed discards the previous vault's index"). That is an administrator's reassignment, not a mount accident, and it is unchanged: D3's "nothing deleted" covers the ordinary prune of a pass, not a discard decided by provenance. After a discard the scope has no rows, so D3 does not refuse the next empty walk (codex spec r1 M1).

### D4. Accounting

`indexer_health` gains a per-scope `walk_incomplete` counter with the same semantics as `rederive_incomplete`: increment per committed pass with a non-root walk failure, reset by a pass with none, counted toward degradation at `INDEXER_DEGRADED_AFTER_FAILURES`, included in `max_consecutive_failures`, and a CRITICAL once per episode. `/health` adds no new field beyond what #308 defines. `failing_scopes` is redefined, once, as the number of distinct scopes with **any** of their index, incomplete-re-derive or incomplete-walk counters at or over the threshold (deduplicated; embedding failures keep their own `embedding_failing_scopes`) — today it counts only the index counter (`indexer_health.py:241`; codex spec r1 m2). (D3 needs no new counter: it raises.)

### D5. Operator confirmation of an emptied vault

An admin-only panel action **Confirm vault is empty** grants one scope a single-use empty-prune permission valid for 15 minutes, and starts an index pass **for that scope only**.

**Placement.** Single-user: the settings Danger zone. Multi-user: the user's edit page, per user.

**Confirmation token (codex spec r1 M2).** The existing re-embed confirmation (`routes.py:159-160, ~2746-2770`) is signed but neither single-use nor bound to anything, so it is not reused as is. This action gets its own serializer salt. Its token payload binds: the action name, the target scope (`None` or user id), the issuing administrator's user id, the scope's vault assignment string at issue time, a random **process epoch** generated at startup, and a random nonce. Redemption refuses a token whose epoch is not the current process's, so a restart invalidates every outstanding token — the in-process consumed-nonce set cannot be bypassed by restarting (codex spec r2). Redemption requires: a valid signature for this salt; age ≤ 10 minutes (token expiry, separate from the permission's 15-minute TTL); the redeeming administrator equals the issuer; the target scope equals the posted scope; the scope's current assignment equals the bound one; and the nonce not yet consumed — consumption is an atomic insert into an in-process consumed-nonce set (single worker, entries kept until their token's expiry). Any mismatch refuses with nothing granted. The confirmation page states the number of indexed notes that would be deleted.

**Target eligibility (codex spec r1 M4).** The action is refused, with nothing granted, when the target user is inactive, has no vault assigned, is deleted, or is quarantined by the vault-overlap snapshot. Single-user always has its one scope.

**Targeted pass (codex spec r1 M4).** A new background entrypoint runs `index_vault` (and then the embed stage) for exactly the target scope, with #308's accounting and quarantine recovery. Existing callers of `_reindex_background` (panel Reindex, re-embed, reset; `routes.py:2740, 2816, 2982-2984`) keep their all-user behaviour.

**Permission lifecycle (codex spec r1 M3).** The registry holds, per scope, `(expires_at, assignment)`. A pass **takes** the permission atomically at its first evaluation of D3's predicate (pre-lock), whether or not the root is empty, removing it from the registry and carrying it as an invocation-local authorisation `(expires_at, assignment)`. The locked re-evaluation uses that same authorisation, never the registry. Before the prune mutation the pass re-checks `now < expires_at` and that the assignment is unchanged; if either fails it raises `IndexIndeterminate` as if no permission existed. The authorisation survives #308's quarantine restarts within the same invocation, and is never returned to the registry after failure, cancellation or restart. A process restart drops ungranted permissions (re-confirm).

**Audit.** Granting and taking are each logged at WARNING with the scope and (for granting) the administrator's username; the consuming pass logs the number of rows pruned. D2 still applies: a permission never authorises pruning beneath an unlisted prefix, and never overrides an unlistable root (`""`).

### D6. Tests

- Unit: prefix recording for root, nested and sibling-name cases (`sub` vs `subway.md`); D2 exclusion from prune and from `deleted_by_hash`; D3 predicate (root failure; empty with rows; empty without rows; empty with permission; permission consumed; permission expired); `walk_incomplete` accounting and `/health`; panel routes (admin-only, token required, wrong/expired token refused, permission granted, background reindex started) and template rendering.
- Real Postgres (`tests/integration/`): a `chmod 000` subdirectory with indexed rows beneath it — rows and embeddings survive, an unrelated new file with the same hash elsewhere is inserted as new (not a move of the protected row), other notes commit, run record names the directory; restoring the mode re-indexes nothing under it (hashes unchanged); an empty root over 50 rows — nothing deleted, run failed, counter incremented; then grant the permission and run — rows pruned once, permission consumed; re-insert rows (a fresh populated pass), empty the root again, and assert the next pass raises without a new permission (codex spec r1 m3); the locked re-evaluation uses the taken authorisation; an authorisation whose expiry passes during the lock wait refuses; a populated pass consumes the permission without pruning; tokens: replayed, cross-scope, cross-admin, stale-assignment and expired tokens refused; targeted pass touches no other tenant; ineligible targets refused; manual single-user reindex embeds after an indeterminate index stage; an entry whose type lookup raises protects its subtree; root `scandir` failure raises; multi-user: one tenant's empty root does not affect another's.

## Risks / Trade-offs

- [A folder that stays unreadable keeps stale rows for its notes indefinitely] → by design: the notes are still there, the rows are the last known truth, and the condition is reported degraded and named in the run record. A deliberately deleted *and* unreadable folder is not distinguishable; the operator fixes permissions.
- [A real emptying of the vault needs an extra click] → rare, explicit, and the error message says exactly where the click is.
- [L1: a wrong but non-empty mount still prunes the difference] → not detected; documented in DEPLOYMENT.md alongside the existing pitfall.
- [L2: the permission is in-process] → a restart between confirm and pass drops it; re-confirm.

## Migration Plan

No schema change. Deploy via the k3s image bump. Rollback: previous image.

## Spec review round 1 (Codex) — disposition

All eight findings accepted (round 2 verified seven and found the token replay-after-restart gap, closed by the process-epoch binding in D5): B1 (type-lookup failures protect their subtree), M1 (provenance discard stated as an exception), M2 (dedicated, bound, single-use token), M3 (invocation-local authorisation with expiry re-check), M4 (targeted pass and eligibility), m1 (manual single-user stage isolation), m2 (`failing_scopes` defined once), m3 (test sequence corrected).

## Implementation review round 1 — disposition

- **Codex C2/C3 (expiry checked before an await) — fixed (F1).** `_recheck_empty_prune` checked expiry before awaiting the multi-user assignment read and only the assignment after it, so a permission expiring during that read still authorised the DELETE. The decisive check is now `auth.valid_for(current_assignment)` — expiry and assignment together — as the last statement after every await, and the prune statement follows with no await in between. Regression: `test_a_permission_that_expires_during_the_assignment_read_authorises_nothing` (real Postgres, clock advanced inside the read; fails on the previous order).
- **Verifier items — fixed (F2–F4).** F2: a quarantined note has no row (it is pruned when quarantined), so the D2 sweep exemption keyed on the protected *rows* never applied to it; the exemption is now decided by prefix over the quarantine entries themselves, with a real-Postgres test that the entry survives an unlisted directory and is cleared once the directory is readable and the note gone. F3: the unlisted-directory list (WARNING and run record) ends `(+N more)` instead of a bare ellipsis; the index-integrity delta says "a count of the remainder" for the run record too. F4: `control-panel.md` no longer calls `_reindex_background` untouched — its callers and all-user fan-out are unchanged, its single-user branch isolates the index and embed stages (task 3.2).
- **Verifier notes — accepted by design.** The single-user confirmation token binds a sentinel administrator (single-user mode has one administrator). A permission whose pass is refused before it evaluates the D3 predicate (overlap quarantine, unopenable root) is not taken and stays pending until its 15-minute expiry.

## Open Questions

- None for the owner: the confirmation-button default was agreed (2026-10-01). Codex review to check the D3 predicate's race with concurrent `create_note` (a note created into an empty vault between walk and lock) and the permission's consumption semantics.
