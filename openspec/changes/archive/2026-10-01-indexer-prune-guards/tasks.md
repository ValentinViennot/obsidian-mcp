## 1. Walk and prune protection (D1, D2)

- [x] 1.1 `discover_markdown_files_at`: record the vault-relative prefix (`""` for the root) for every directory failure already recorded as a skip, and the entry path when `is_dir()` raises; `ScanResult.walk_failed_prefixes`
- [x] 1.2 `_index_vault_attempt`: compute protected paths from `locked` (`p == pref` or `p.startswith(pref + "/")`, `pref != ""`); remove them from the prune set before `deleted_by_hash`; skip their quarantine-clear
- [x] 1.3 WARNING naming unlisted dirs (bounded) and rows protected; run-record line `walk incomplete: N dir(s) not listed: …` (≤ 5, backslashreplace); `run_outcome` labels it failed; `IndexPassResult` exposes the count

## 2. Indeterminate root (D3, D5 consumption)

- [x] 2.1 `IndexIndeterminate`; raise in `_index_vault_pinned` before the lock on root listing failure, or on empty `seen` with snapshot rows and no unexpired permission
- [x] 2.2 Re-evaluate against `locked` in `_index_vault_attempt` before any mutation
- [x] 2.3 Empty-prune permission registry `(expires_at, assignment)`; atomic take into an invocation-local authorisation at the first predicate evaluation; locked re-check uses it; expiry + assignment re-check right before prune; kept across quarantine restarts; never returned; WARNING on grant/take and rows pruned
- [x] 2.4 Error text per spec (condition + row count + where to confirm; no path)

## 3. Accounting (D4)

- [x] 3.1 `indexer_health`: `walk_incomplete` per scope, wired from `IndexPassResult` at every entrypoint; counts toward degraded, `max_consecutive_failures`, CRITICAL once per episode; `failing_scopes` = distinct scopes with index / re-derive / walk counter at threshold
- [x] 3.2 Manual single-user reindex: isolate index and embed stages (embed runs after an index failure), as #308 D9 did for the loop

## 4. Panel (D5)

- [x] 4.1 Admin-only confirmation page (own salt; token binds action, scope, admin id, assignment, process epoch, nonce; 10-min expiry; atomic nonce consumption) stating the row count + POST action that checks eligibility, grants the permission, and starts a new **scope-targeted** background pass; existing `_reindex_background` callers unchanged; WARNING with admin username and scope
- [x] 4.2 Single-user: settings Danger zone control; multi-user: user edit page control; CSP rules (`data-*`, nonce, `type="button"` + `data-confirm`, no inline style)

## 5. Tests (D6)

- [x] 5.1 Unit tests per design D6 (panel part done: `tests/test_issue_309_panel_confirm_empty.py`, CSP route/template inventories)
- [x] 5.2 Real-Postgres tests per design D6

## 6. Docs

- [x] 6.1 `docs/architecture/indexing-and-embeddings.md` (prefix protection, indeterminate root, permission), `docs/architecture/control-panel.md` (the new action — done), `DEPLOYMENT.md` pitfall + L1, `docs/deployment-kubernetes.md` troubleshooting row, `CLAUDE.md` key-decisions bullet

## 7. Gates

- [x] 7.1 Offline suite and `make test-integration` green
- [x] 7.2 `openspec-verifier`; adversarial Codex (deletion path → mandatory)
- [ ] 7.3 Deploy (k3s image bump PR); live check; owner browser/CSP pass on the new panel controls
