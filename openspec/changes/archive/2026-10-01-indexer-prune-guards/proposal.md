## Why

GitHub #309: the index pass treats "not seen by the walk" as "deleted from disk", and two ordinary host problems make the walk see less than is there.

1. **A subdirectory the walk cannot list** (a folder created by another uid with a restrictive mode, an EACCES/EIO on `scandir`) is recorded as a skip and nothing beneath it is yielded. The pass then builds its prune set as `existing − seen`, so every row under that folder is deleted (embeddings cascade) — or, worse, paired as the *source of a move* to an unrelated file with the same content hash and rewritten in place. The run is recorded clean; only a WARNING mentions it. When the folder is readable again every note in it is re-inserted and re-embedded (provider cost, hours of partial semantic search).
2. **An empty-but-openable vault root** — a mount that did not mount, a Docker-created empty bind source, a misbound k3s volume — yields zero files, and the ordinary prune deletes the owner's **entire** index and embeddings. The run row reads `scanned=0, error=NULL`.

Both are silent and destructive, and #308's new `/health` accounting does not see them, because the pass *succeeds*. The index-integrity spec already states the principle for the re-derive stamp — "a directory it could not open or list: anything beneath it may have a row" — but the prune does not apply it.

## What Changes

- **A pass SHALL NOT delete or move-pair what it could not see.** The walk records the vault-relative prefix of every directory it could not open or list. Rows at or beneath such a prefix are excluded from the prune and from move-source pairing (and their quarantine entries are not cleared). The pass still commits everything else. The skipped directories are named in the run record and the scope's new `walk_incomplete` counter feeds `/health` degradation.
- **An unlistable root, or an empty root over an existing index, is indeterminate.** If the root's own listing fails, or the walk discovers no files at all while the scope's index holds rows, the pass raises before its locked transaction and deletes nothing. The failure is recorded on the run row and counted by #308's index-failure counter, so `/health` reports `degraded` after `INDEXER_DEGRADED_AFTER_FAILURES` passes. The check is repeated against the rows read under the lock.
- **An operator confirms a genuinely emptied vault from the panel.** A new admin action, "Confirm vault is empty", in the settings Danger zone (single-user) and on the user's edit page (multi-user), grants that scope a single-use, short-lived permission for the next pass to prune an empty root, and triggers that pass. It uses the panel's signed one-time-token confirmation pattern and the CSP rules.
- Docs: the DEPLOYMENT.md "Found 0 markdown files" pitfall and the Kubernetes guide point at the new behaviour and the panel action.

Out of scope (recorded in design): a non-empty but *wrong* mount (a different, smaller directory mounted in place of the vault) is not detected — that would need a share-of-index threshold, which the owner did not request.

## Capabilities

### New Capabilities
<!-- none -->

### Modified Capabilities
- `index-integrity`: walk-failure prefixes protect rows from prune and move pairing; an unlistable or empty root over an existing index aborts the pass; the failure-accounting requirement gains the `walk_incomplete` counter.
- `panel-ops-health`: `/health` degradation includes `walk_incomplete`; the panel gains the "Confirm vault is empty" action.

## Impact

- `src/services/indexer.py`: `discover_markdown_files_at` (structured failed prefixes), `ScanResult`, `_scan_vault`, `_index_vault_pinned` / `_index_vault_attempt` (indeterminate checks, prune and move-pairing exclusion, quarantine-clear exclusion), `IndexPassResult`, run-record text.
- `src/services/indexer_health.py`: `walk_incomplete` counter; empty-prune permission registry (in-process, single-use, TTL).
- `src/control_panel/routes.py` + templates (`settings.html`, `user_edit.html`, a confirm page) + `static/panel.js` if a new `data-*` control is needed.
- Docs: `docs/architecture/indexing-and-embeddings.md`, `docs/architecture/control-panel.md`, `DEPLOYMENT.md`, `docs/deployment-kubernetes.md`, `CLAUDE.md`.
- No migration. Panel templates change → owner browser/CSP pass.
