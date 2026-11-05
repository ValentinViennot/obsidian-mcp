# Implementation and acceptance

- [x] Add mandatory OAuth alias UI, server validation and migration044.
- [x] Preserve code/token/refresh attribution across DCR and CIMD.
- [x] Retain API keys and block invalid named writes before mutation.
- [x] Isolate Git AUTHOR per request; retain technical COMMITTER.
- [x] Snapshot deferred upload author and preserve publication on Git failure.
- [x] Partition CI, qualify one image, attest immutable release metadata.
- [x] Implement dedicated GitOps intake, scoped promotion and fixed deployment.
- [x] Include working-clone recovery storage in private backups.
- [x] Finish upstream full CI and merge PR13 without interim deployment.
- [x] Complete full Linux/PostgreSQL regression gates and independent review.
- [x] Inspect final synthetic UI captures and metadata before publishing.
- [x] Install safe sync engines and move both clones to direct GitHub main.
- [x] Verify offline/concurrent/conflict recovery and unchanged network usage.
- [x] Configure required branch protection and release credentials.
- [x] Verify full production backup, split PostgreSQL roles, supervised deploy.
- [x] Verify health, schema, remote reachability, author/committer attribution.
- [x] Enable unattended releases after supervised deployment succeeds.
- [x] Stop task-owned local QA processes and verify cleanup.

Verified on 2026-11-05:

- [Main CI run 42079750553](https://github.com/ValentinViennot/obsidian-mcp/actions/runs/42079750553) passed every disjoint regression and release gate. The tested image and its exact-source manifest were published and their attestations verified before admission.
- The supervised release restored a full pre-migration database backup for verification, preserved complete working clones and recovery storage, upgraded to schema044, and passed `alembic check` and indexer readiness with a non-superuser runtime role.
- Two named API-key connections performed simultaneous real MCP writes with distinct stable authors and the configured technical committer. Synthetic notes were deleted and test credentials revoked. OAuth alias, refresh, forced reconnection and deferred-upload coverage passed in the regression suites; no unnamed legacy OAuth token or code remained usable after migration.
- The installed synchronization engines converged both clean working clones through GitHub after synthetic bidirectional writes and deletes. Direct desktop GitHub SSH has no VPS proxy dependency; existing network services were preserved. The old bare relay remains an offline transition archive.
- The actual daily working-clone backup was restored into private scratch storage; archive hashes, recovery content and Git integrity passed. Scratch storage and task-owned QA processes, containers, networks and volumes were removed.
- Protected unattended delivery is enabled after supervised admission. Replaying the attested release passed intake and returned unchanged without another promotion.

All published UI captures use synthetic fixtures; their pixels and metadata were inspected. Production backup and operational evidence remain private.
