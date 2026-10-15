# Upstream synchronization, 15 October 2026

The fork integrates upstream `70f284f` with merge ancestry preserved. Existing fork OIDC revision 025 and temporal-embedding revision 026 remain byte-identical. Upstream independently reused those IDs, so its six incoming migrations are appended after the fork’s 026 under unique compatibility identities:

| Upstream revision | Fork revision | Meaning |
| --- | --- | --- |
|025|031|OAuth client last-use marker|
|026|032|Recorded note stat tuple|
|027|033|Note maintenance and halfvec search index|
|028|034|Concurrency counters|
|029|035|OAuth grant lifetime|
|030|036|Re-derivation progress marker|

Upstream SQL and ownership-marker strings keep their provenance; the revision IDs, predecessor chain and test references name the new fork identities. This supports fresh fork installs and existing fork 025/026 databases. A database already migrated through upstream’s independent 025–030 chain is outside this upgrade path.

Current-only vector reads, replacement and provider reuse exclude historical rows. Native historical validity intervals, OIDC subject identity, data ownership and saved-editor recovery remain authoritative. The new halfvec index replaces the old HNSW layout intentionally; note/vector contents and temporal meaning remain. Explicit schema tests exercise both deployed fork starting revisions, repeated upgrade and clean Alembic comparison against real PostgreSQL.

The shared extraction marker advances to 4 because both histories independently used 3. Fork 3 remains a frozen comment-aware embedding cleaner; 4 uses the same cleaner while refreshing the combined graph grammar. An unchanged cleaned body therefore needs graph/tag re-derivation without a blanket provider re-embedding.

Ollama adopts the upstream pooled hardened client and configurable request size (default 16, 1–256). It retains the fork’s per-request deadline `30 + 10*(inputs-1)` seconds, exact cardinality checks and per-input fallback after ordinary batch failure. Timeout and malformed-cardinality responses never trigger that fallback. There is no note-wide deadline. A query uses the upstream one-element array. Tests cover both fixed 32 compatibility fixtures and the configurable 16-input surface.

Deployment retains one generic base stack plus Caddy/Traefik front-door overrides, uid 1000 and one worker. Upstream application/superuser database-role separation is applied to that base; existing volumes require the documented explicit role-conversion procedure and backup. No role conversion is run automatically. The app limit advances to 2 GiB because the new startup request-body budget cannot accommodate the default maximum upload in the old 1 GiB limit. There is no production deployment or vault mutation in this synchronization task.


## Conflict decisions

The merge reviewed each ancestor against both branch tips. These are the 23
conflicted paths and the behavior retained or adopted.

| Path | Resolution |
| --- | --- |
| `CLAUDE.md` | Retain the fork's deployment authorization, Git recovery, single-stack and one-worker instructions; add upstream operating guidance. |
| `DEPLOYMENT.md` | Keep generic base/override deployment commands and saved-editor sync guidance; adopt explicit database-role separation and current upstream limits. |
| `Dockerfile` | Retain uid 1000, hardened runtime, migration entrypoint and one worker; adopt explicit Uvicorn proxy-header refusal so application proxy trust remains authoritative. |
| `Makefile` | Union supported maintenance, backfill, concurrency and vacuum targets; retain the fork's Linux test gate and deployment sequence. |
| `README.md` | Keep fork setup and history guidance alongside upstream feature and limit documentation. |
| `docker-compose.proxy.yml` | Keep deleted: its TLS behavior belongs in the Traefik override. |
| `docker-compose.simple.yml` | Keep deleted: the generic base is the single authoritative application/database definition. |
| `docker-compose.yml` | Keep fork topology and hardening; apply upstream separate superuser/runtime roles, initialization scripts and body-budget-compatible application memory limit. |
| `docker/db-init-vector.sql` | Retain the fork's historical script; the current base explicitly uses the upstream role-aware initializer. |
| `docs/architecture/indexing-and-embeddings.md` | Combine temporal/current-only semantics with upstream scan, reuse and derivation behavior; document extraction marker 4 and bounded configurable batching. |
| `docs/architecture/schema-and-migrations.md` | Retain OIDC and temporal contracts and add upstream schema ownership guidance; compatibility identities are documented here separately. |
| `src/control_panel/routes.py` | Keep current-only vector accounting and adopt enforced panel CSP and upstream panel controls. |
| `src/mcp_server/tools.py` | Union typed outcomes, concurrency admission and transport bounds with fork Git history, write commits, sync guards and current-only related-note search. Release both the sync guard and admission lease even if cleanup raises. |
| `src/models/db.py` | Union OIDC identity, temporal validity and ownership comments with upstream OAuth lifetime, stat tuple, concurrency and derivation fields. Keep temporal validity indexing and adopt halfvec search indexing. |
| `src/services/embeddings.py` | Keep temporal replacement, frozen cleaners, exact cardinality checks and request deadlines; adopt pooled hardened transport, configurable batching and chunk reuse restricted to current rows. |
| `src/services/indexer.py` | Keep Git-authored modification dates and fork aliases; adopt stat shortcuts, bounded scan offload, quarantine/prune guards and derivation progress. Pass the Git-date map explicitly into the extracted index attempt. |
| `src/services/links.py` | Retain fork anchor, display and block-reference support with upstream mask-decided link grammar and code-point offsets. |
| `src/services/vault.py` | Retain Unicode tags, comment handling and indented-code exclusions; adopt upstream bounded extraction and NUL cleaning. |
| `tests/integration/test_asvs_links_truncated_pg.py` | Retain fork graph checks and upstream bounded-link outcomes; assert marker 4. |
| `tests/integration/test_schema_check.py` | Keep both catalog/ownership suites without helper-name shadowing; map incoming migration identities and add real fork 025/026 preservation upgrades. |
| `tests/integration/test_transfer_pg.py` | Union fork transfer/history coverage with upstream native transfer and schema behavior. |
| `tests/test_issue_127_embed_batch_deadline.py` | Retain per-request/no-note-deadline regressions and adapt the one-input fixture to the upstream array transport. |
| `tests/test_provider_input_limit.py` | Keep provider-refusal attribution and fallback safety with explicit configurable batch size. |

Adjacent automatic merges also needed compatibility checks. The three fork Git
history tools join the upstream scan-admission class, bumping its classification
epoch; the complete tool registry and unknown-argument tests cover all 28 tools.
The OIDC retry link uses a CSS class instead of inline styling under the enforced
panel CSP. CI validates the authoritative base and both proxy overrides with
separate synthetic application and bootstrap database credentials.

The native integration harness can explicitly name a Docker host and subnet for
nested-container QA. Its default remains loopback on an ordinary host. This is a
test-environment adaptation, not an application transport or database exception.


Native visual review found that the dashboard's animated counters could remain
at their literal zero placeholders in the review browser while the server's
coverage and settings correctly reported two embedded notes. No script error,
nonce mismatch or database inconsistency was observed, so the browser-specific
cause is not claimed. The page now server-renders the actual counter values and
retains the existing animation. A focused rendered-HTML regression proves a
truthful two-notes, two-embedded, 100% baseline before any script runs.


Lead review also extended the exact-cardinality rule to query embeddings: an
empty or multi-vector reply to one query now raises `ProviderBatchSizeMismatch`
without retrying or selecting an arbitrary vector. The HTTP-seam regressions
failed on both malformed replies before this correction and pass afterwards.
