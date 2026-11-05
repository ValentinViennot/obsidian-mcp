# Implementation and acceptance

- [x] Add mandatory OAuth alias UI, server validation and migration044.
- [x] Preserve code/token/refresh attribution across DCR and CIMD.
- [x] Retain API keys and block invalid named writes before mutation.
- [x] Isolate Git AUTHOR per request; retain technical COMMITTER.
- [x] Snapshot deferred upload author and preserve publication on Git failure.
- [x] Partition CI, qualify one image, attest immutable release metadata.
- [x] Implement dedicated GitOps intake, scoped promotion and fixed deployment.
- [x] Include working-clone recovery storage in private backups.
- [ ] Finish upstream full CI and merge PR13 without interim deployment.
- [ ] Complete full Linux/PostgreSQL regression gates and independent review.
- [ ] Inspect final synthetic UI captures and metadata before publishing.
- [ ] Install safe sync engines and move both clones to direct GitHub main.
- [ ] Verify offline/concurrent/conflict recovery and unchanged network usage.
- [ ] Configure required branch protection and release credentials.
- [ ] Verify full production backup, split PostgreSQL roles, supervised deploy.
- [ ] Verify health, schema, remote reachability, author/committer attribution.
- [ ] Enable unattended releases after supervised deployment succeeds.
- [ ] Stop task-owned local QA processes and verify cleanup.
