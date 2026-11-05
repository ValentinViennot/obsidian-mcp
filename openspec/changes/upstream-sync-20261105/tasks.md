# Verification and delivery

- [x] Fetch fork and upstream; preserve published history with a merge.
- [x] Resolve conflicts while retaining PocketID, Git publication, temporal data and read relationships.
- [x] Append unique migrations 037–043 without modifying previously deployed files.
- [x] Run full Linux offline suite: 9,191 passed, 1,419 skipped (DB/optional gates explicitly separate).
- [x] Run real PostgreSQL schema suite: 390 passed initially; three test-reference failures corrected. Targeted rerun: 36 passed, including those failures and the newly unshadowed fork downgrade test.
- [x] Run targeted PostgreSQL integration: 70 passed initially; updated temporal reuse fixture and reran full temporal module, 13 passed.
- [x] Validate all three Compose variants with synthetic configuration.
- [x] Review Gitleaks findings: three new upstream synthetic test credentials, exact commit-scoped baseline only.
- [x] Run pip-audit with resolved transitive dependencies: no known vulnerabilities.
- [x] Independent cross-reviews of auth and search/write integration: no new P1/P2 findings.
- [x] Correct deployment probe auto-migration hazard and preserve full-backup defaults; 112 focused tests pass, native Make expansions verified.
- [x] Inspect synthetic UI captures and embedded metadata; no sensitive content, no page/console errors.
- [x] Read-only production audit; no production migration, reindex, write or deployment.
- [x] Prepare verified merge commit and PR evidence; CI's full Linux/PostgreSQL gates remain separate.

Local validation does not claim the entire PostgreSQL integration suite passed.
That broader Linux-native job, image scan and OpenSpec CLI validation belong to
CI; the shared Claude workflow file and local OpenSpec CLI are unavailable.
