## 0. Pre-flight

- [x] 0.1 Check the base. Work on branch `search-eval-harness` at or after `6bcc9d9`, and confirm that `grep -c "Prebuilt images are public" docs/deployment-kubernetes.md` prints `1`. Confirm that `src/services/timing.py` defines `MAX_RESULT_PATHS = 10` and `src/mcp_server/tools.py` defines `_MAX_PARAM_LEN = 200`, because the miner's truncation and pool rules depend on both. Confirm that `oauth_tokens.grant_id` is `NOT NULL` in `src/models/db.py`, and read the principal definition in `src/auth/session.py`.
- [x] 0.2 Read this change's `proposal.md` and `design.md`, including "Owner decisions" and "Codex spec review, round 1", and `specs/search-evaluation/spec.md`. Then read:
  - `docs/architecture/search.md`, section "Search result telemetry"
  - `docs/architecture/usage-attribution.md`, sections "The read-only consumer" and "What reaches `params` is rendered"
  - `src/services/search_analytics.py`, for `analysable_sql` and the `(user_id, path)` identity rule
  - `docker/db-exec.sh`
- [x] 0.3 Confirm from `_tracked`'s tail in `src/mcp_server/tools.py` (around `logged["error"] = body_outcome.marker`) that a failed `read_note` row carries `params.error`. If any `read_note` failure path writes a row *without* `error`, stop and record it in design.md L10 before relying on the rule.
- [x] 0.4 Touch nothing under `src/` or `alembic/`, and do not change the Dockerfile or any `requirements*.txt`. This change is tooling, tests and docs only, and adds no dependency (E1). The only workflow edit allowed is 1.2.

## 1. Ignores and CI — D3, D9

- [x] 1.1 Add `.search-eval/` to `.gitignore`.
- [x] 1.2 In `.github/workflows/ci.yml`, add a step to the `tests` job before "Run the test suite". It runs `psql --version`, and only if that fails, `sudo apt-get update && sudo apt-get install -y postgresql-client` followed by `psql --version` again. GitHub's `ubuntu-latest` image ships the PostgreSQL client today; the step guards against the image changing. Do not add a new env variable.

## 2. Export — D1, D3

- [x] 2.1 Write `scripts/search_eval_export.sql`. It contains `BEGIN TRANSACTION READ ONLY;`, then one `SELECT json_build_object(...)::text FROM usage_logs ul LEFT JOIN oauth_tokens ot ON ot.id = ul.oauth_token_id WHERE ul.tool IN ('keyword_search','semantic_search','read_note') AND ul.created_at >= now() - make_interval(days => :'days'::int) AND (:'user_id' = '' OR ul.user_id = NULLIF(:'user_id','')::int) ORDER BY ul.created_at, ul.id`, then `COMMIT;`.
  - Use exactly the fields and params subset in design D1, including `grant_id` from `ot.grant_id` and the D10 provenance fields `user_filter` and `deployment_users` (`(SELECT count(*) FROM users)`).
  - `created_at` is epoch microseconds, as a bigint.
  - Add a header comment explaining why the file uses `-A -t` and not COPY.
- [x] 2.2 Add the Makefile target `search-eval-export`.
  - Variables: `SEARCH_EVAL_DIR ?= .search-eval`; `SEARCH_EVAL_DB_NAME ?= obsidian_mcp`; `DAYS ?= 90`, an integer from 1 to 365, else exit 64; `USER_ID ?=`, empty or an integer.
  - `mkdir -p` the directory, then `chmod 700` it.
  - Under `umask 077`, run `$(DB_EXEC) -i psql -U postgres -d $(SEARCH_EVAL_DB_NAME) -X -q -A -t -v ON_ERROR_STOP=1 -v days=… -v user_id=… < scripts/search_eval_export.sql > $(SEARCH_EVAL_DIR)/usage-export.jsonl.tmp`.
  - Rename the `.tmp` file into place only on success. On failure, remove it, leave any existing `usage-export.jsonl` untouched, and exit non-zero.
  - Print the row count.
  - Add the target to `.PHONY` and `help`. `DB_EXEC` must stay overridable from the command line, because the integration gate (6.3) depends on it.

## 3. Mining and classification — D2, D5

- [x] 3.1 Create the `scripts/search_eval/` package with `__init__.py` and `__main__.py`.
  - The CLI is argparse, with subcommands `mine`, `snapshot`, `run` and `score`.
  - Exit codes are 0/1/2/64.
  - Nothing in the package may import `src.*` at module scope. The DB-backed pieces import lazily.
- [x] 3.2 Write `classify(text) -> Literal["identifier","prose"]` in `scripts/search_eval/classify.py`, exactly as D5 defines it.
- [x] 3.3 Write `scripts/search_eval/mining.py`:
  - `load_export(path)`: type-checks every field, counts malformed lines and never raises on them.
  - `principal_of(row)`: returns `("api_key", key_id)` if `key_id` is set, else `("oauth", grant_id)` if `grant_id` is set, else `None`, counted as `unresolved_principal`. It never falls back to `actor_ref`.
  - `usable_search(row)` and `usable_read(row)`: each returns `(ok, reason)`.
  - `is_truncated(s)`: true when the length is 201 and the string ends in `…`.
  - `mine(rows, *, window_seconds=120, manual=None) -> MineResult`, implementing D2:
    - credit only the latest qualifying search;
    - use a per-principal sorted index, never an O(n²) scan;
    - compute the query id and fold searches that share it;
    - compute the pool and `source_tools`;
    - keep every exclusion counter.

    It is pure and does no I/O beyond what the caller passes in.
- [x] 3.4 Write the manual-judgment loader. Input is JSONL of `{user_id, text, folder?, tags?, frontmatter?, relevant: [path…], class?}`.
  - Validate each line.
  - A manual query uses the same qid rule, gets `sources = {"manual"}` and a pool equal to its relevant set, and its explicit class overrides the classifier.
  - When a manual qid collides with a mined one, union the relevant paths and the pools, set `sources = {"mined","manual"}`, and count the query in `n_both_sources`.
- [x] 3.5 Wire the `mine` CLI: `--export FILE --out DIR [--window-seconds N] [--manual FILE] [--user-id N]`.
  - It writes `queries.jsonl`, `qrels.json` (canonical JSON) and `mine-report.json` with mode `0600`, and creates the out directory with mode `0700`.
  - It derives `provenance` per D10 and writes it into `mine-report.json`.
  - `--user-id` filters the rows in memory and counts as a user filter for provenance. It exists for the fixture's single-user assertion, and the operator path uses `USER_ID=` on the export.

## 4. Metrics, snapshot, arms, replay and scoring — D3, D4, D6, D8

- [x] 4.1 Write `scripts/search_eval/metrics.py`, standard library only, with the D8 functions:
  - `dedup`, `recall_at`, `reciprocal_rank`, `mean`;
  - `ndcg_at`, with binary gains and IDCG over min(|relevant|, k);
  - `bootstrap_ci(deltas, *, resamples=1000, seed=0, level=0.95)`: `random.Random(seed)`, resampling with replacement, returning the 2.5th and 97.5th percentiles of the resampled means, and `None` for an empty list.
- [x] 4.2 Write the read-only engine helper.
  - Build it from `SEARCH_EVAL_DATABASE_URL` or `--database-url` only, as `create_async_engine(url, connect_args={"server_settings": {"default_transaction_read_only": "on"}})`.
  - Assert that `SHOW transaction_read_only` returns `on` before any use; exit 1 if it does not.
  - A DB-backed step with no URL exits 64 before connecting.
  - The URL's own parameters govern TLS. Do not import `Settings`.
- [x] 4.3 Write the `snapshot` CLI: `--database-url URL --data DIR`.
  - In a read-only session, run `SELECT m.user_id, m.file_path, m.content_tsvector IS NOT NULL AS keyword, EXISTS (SELECT 1 FROM note_embeddings e WHERE e.note_id = m.id) AS semantic, m.embedded_content_hash IS DISTINCT FROM m.content_hash AS stale_vector FROM notes_metadata m`. `stale_vector` is informational only and never part of membership (D6).
  - Write `corpus-snapshot.json` (`{user_id-or-"null": {path: {metadata, keyword, semantic}}}`) to a `.tmp` file, then rename it, mode `0600`.
  - Share the membership SQL with the `run` step's replay check: one function, one SQL text.
  - Write `snapshot-meta.json` with the file's `sha256` and the URL's host and database name. No password.
  - Refuse to overwrite an existing snapshot without `--force`.
  - Add a Makefile target `search-eval-snapshot` that wraps it, requiring `SEARCH_EVAL_DATA` and `SEARCH_EVAL_DATABASE_URL`. Its help text says to run it on the restored copy **before** applying any candidate.
- [x] 4.4 In `scripts/search_eval/arms.py`, implement the D4 interface:
  - `EvalQuery` with `sources: frozenset[str]`, `ArmContext`, `Arm` and `ArmFactory`.
  - Built-in `keyword` and `semantic` arms. Each lazily imports `src.services.search.full_text_search` or `src.services.embeddings.semantic_search` and calls it with a fresh supplied session per query, passing the owner and filters through.
  - Membership: `keyword` declares `"keyword"`, `semantic` declares `"semantic"`, and fixture arms use the fixture corpus.
  - Resolution of `<module>:<factory>` arms. A DB-backed factory without a `membership` attribute in `{"metadata","keyword","semantic","keyword_and_semantic"}` exits 64. `keyword_and_semantic` is `keyword AND semantic`.
  - Resolution of `runfile:<path>` arms. The file must sit next to its producing generation's `manifest.json`, that manifest must be `complete` with the file's sha256 listed, and its qrels, snapshot and selected-qid hashes must equal the current ones. Otherwise exit 64.
  - `--arm-opt key=value` pass-through.
  - An unknown arm is exit 64.
- [x] 4.5 Write the `run` CLI: `--data DIR --arm NAME… [--generation ID] [--depth 20] [--limit-queries N] [--database-url URL] [--fixture-corpus FILE]`.
  - **Generation.** `__main__` computes the default generation once, as a UTC stamp. Library code takes it as a parameter and never calls `now()`. Refuse an existing generation directory (exit 64).
  - **Selection.** Require `corpus-snapshot.json` in DIR, or `--fixture-corpus` (the snapshot and replay corpus together); exit 64 if missing. Select the qids after the drift drop, sorted, keeping the first N for `--limit-queries`. `selected_qids_hash` is the sha256 of the newline-joined sorted qids.
  - **Running manifest.** Before any factory or arm executes, atomically write `runs/manifest.json` (`.tmp` plus rename) as `{generation, status: "running", selected_qids_hash, selection: {limit_queries}, qrels_sha256, snapshot_sha256, arms, options, depth}`.
  - **Execution.** Run queries sequentially. Any arm or factory exception, or any output that is not a `list` of `str`, aborts with exit 1, with no per-query catch. Deduplicate to the first rank, then truncate to the depth.
  - **Corpus loss.** For each DB-backed arm, evaluate that arm's membership (4.3's SQL) on the replay URL for the judged snapshot paths. `corpus_loss` is the set of paths the snapshot marks searchable under the membership and the replay database does not. Also record `unsearchable_before`.
  - **Staging.** Stage `runs/<generation>/<arm-slug>.json` with `meta = {arm, membership, options, depth, generation, selected_qids_hash, qrels_sha256, snapshot_sha256, corpus_loss}` and `runs = {qid: [path…]}`.
  - **Failure.** Atomically rewrite the manifest with `status: "failed"`, `failed_arm` and `error` (exception type and message, never query text), then exit 1. Leave earlier generation directories in place.
  - **Success.** Only after every arm has answered every selected query, atomically rewrite the manifest with `status: "complete"` and `files: {arm: {path, sha256}}`.
- [x] 4.6 Implement the drift drop in `scoring.py` from the snapshot only. Record `dropped_paths`, `dropped_queries` and `ownerless_queries`. The replay corpus must never shrink the qrels.
- [x] 4.7 Write `score(data_dir, arms, *, baseline=None, min_queries=50, seed=0)` in `scripts/search_eval/scoring.py`.
  - Read run data **only** through `runs/manifest.json`, with no fallback to any other generation. Refuse with exit 64 unless all of these hold:
    - `status == "complete"`;
    - the manifest's `selected_qids_hash` equals the hash of the qids selected now from the current `queries.jsonl` and snapshot under the manifest's `selection` options;
    - the qrels and snapshot hashes match;
    - every referenced file exists with its recorded sha256;
    - the requested arms are covered.

    A `runfile:` arm's missing queries are the one tolerated omission: report them as `missing_queries` and score them as empty. A `failed` or `running` manifest is reported by generation and error.
  - Copy `provenance` from `mine-report.json` and set `attachable = provenance.users == 1` (D10). When `attachable` is false, the table's first line is `NOT ATTACHABLE (multi-user provenance)`. This does not change the exit code.
  - Using `metrics.py`, compute `recall@5, recall@10, mrr, ndcg@10`, overall, per class and per source. Per source, a query in both `sources` counts in both slices. Also compute `n_queries`, `n_both_sources`, `novel@10` and per-arm `corpus_loss` counts.
  - With a baseline, compute the per-query deltas, their mean and `bootstrap_ci`. These are reported only.
  - Render a table.
  - Write `report.json` containing:
    - the metrics, the counts and the arms with their options;
    - the window and the seed;
    - `qrels_sha256`, `snapshot_sha256` and the manifest's `generation`;
    - `provenance` and `attachable`;
    - each arm's `membership`, `corpus_loss` count and `unsearchable_before` count;
    - `commit_sha` from `git rev-parse HEAD` (or `"unknown"`);
    - the verdict: `OK`, `INSUFFICIENT_DATA` or `CORPUS_LOSS`.
  - Define `UNSTABLE_KEYS = {"commit_sha", "generated_at", "generation"}` and `stable_projection(report)`, documented in the module docstring. Tests pass a fixed `--generation`, but the key stays out of the golden file anyway.
  - Assert explicitly that no query text and no path string appears in the serialized report.
  - Exit codes: 2 for `INSUFFICIENT_DATA` (below `min_queries`) or `CORPUS_LOSS` (any arm with loss above 0); otherwise 0. No metric threshold (E2).
- [x] 4.8 Add the fixture arms `fixture-lexical` (term overlap over title and body) and `fixture-title` (title tokens only), both over `ArmContext.corpus`. They are deterministic, break ties by path, and need no session.

## 5. Make target — D7, E4

- [x] 5.1 Add the `search-eval` target.
  - `SEARCH_EVAL_DATA ?=`: empty means the fixture.
  - `ARMS ?=`: `fixture-lexical fixture-title` for the fixture, `keyword semantic` for real data.
  - `BASELINE ?=`: defaults to the first arm.
  - `MIN_QUERIES ?=`: 1 for the fixture, 50 for real data.
  - `SEED ?= 0`.
  - `SEARCH_EVAL_DATABASE_URL` is passed through from the environment.
  - Fixture mode: run `mine`, then `run --fixture-corpus`, then `score`, into `mktemp -d`, and remove the directory afterwards.
  - Real-data mode: require `$(SEARCH_EVAL_DATA)/corpus-snapshot.json` (exit 64, naming `make search-eval-snapshot`, if missing). Run `mine --export $(SEARCH_EVAL_DATA)/usage-export.jsonl --out $(SEARCH_EVAL_DATA)`, then `run`, then `score` in place. The score step's exit code becomes the target's.
  - The help text describes `SEARCH_EVAL_DATABASE_URL` as the restored `BACKUP_FULL=1` copy, and nothing else.
  - Add the target to `.PHONY` and `help`.

## 6. Tests

- [x] 6.1 Create the fixture under `tests/fixtures/search_eval/`:
  - `usage-export.jsonl`, covering every D7 case, including two grants with the same `actor_ref` and one unresolvable principal;
  - `corpus.json`, the snapshot and replay corpus, with per-arm membership flags (all `keyword` and `semantic` true) and one judged path absent;
  - `manual.jsonl`, with one explicit class and one collision with a mined qid;
  - `expected-report.json`, a stable projection.

  Generate `expected-report.json` once with the implementation. Then check every number by hand for at least the `fixture-title` arm, and write that arithmetic in a comment block in the test.
- [x] 6.2 Write `tests/test_search_eval.py`. It runs fully offline, has no skip condition, and needs no `DATABASE_URL`. Cover:
  - **Mining**
    - one test per mining scenario in the spec, including two grants of the same client and the unresolvable principal;
    - the classifier table, including the four spec examples;
    - qid stability, and two users reading the same path;
    - the mined+manual collision: `sources`, the union, membership in both slices, and `n_both_sources`;
    - malformed export lines are counted, not raised.
  - **Export SQL shape**, pinned by statement parsing: exactly the read-only BEGIN, one SELECT (with the `oauth_tokens` join allowed) and COMMIT, and no `;` inside string literals.
  - **Metrics against hand-computed values** on tiny qrels and runs:
    - an empty run;
    - a relevant path missing from the run;
    - duplicate paths in a run;
    - k larger than the run;
    - more relevant paths than k;
    - the spec's edge-case numbers, exactly.
  - **`bootstrap_ci`**: determinism for a fixed seed, `None` on empty input, a zero-width interval for constant deltas, and an interval that contains the mean.
  - **Drift versus corpus loss**, using exactly the spec's input. A path absent from the snapshot is dropped as drift. Two loss cases:
    - The snapshot holds {A.md, B.md}, the replay corpus lacks B.md, and the candidate returns [A.md]. Expect recall@10 = 0.5, `corpus_loss: 1`, verdict `CORPUS_LOSS` and exit 2.
    - Arm-specific loss: B.md keeps `metadata` and `keyword` but loses `semantic` in the replay corpus. The `semantic` arm and a `keyword_and_semantic` test arm report loss, and the `keyword` arm does not. A path not searchable under an arm's membership in the snapshot counts as `unsearchable_before`, not loss. For `keyword_and_semantic`, that is a path lacking either constituent in the snapshot; test C.md, which has a tsvector but no embeddings.
    - An imported DB-backed factory without `membership` exits 64.
  - **Arm resolution**:
    - a built-in arm;
    - `module:factory` through a test-local module;
    - an unknown name exits 64;
    - a `runfile:` with a missing query is reported;
    - a `runfile:` with mismatched hashes, or without `complete`, exits 64.
  - **Atomicity, with Codex's exact input.**
    - Generation G1 completes.
    - Generation G2 (same qrels, snapshot and selection) runs a test arm that raises on query 61 of 70. `run` exits 1, the manifest shows `generation: G2, status: "failed"`, and G1's files are byte-identical but unreferenced.
    - `score` then exits 64 and does not score G1.
    - A manifest left `running` (simulating a killed process) is refused the same way.
    - A non-list output and a non-string element each abort as above.
    - A complete manifest whose `selected_qids_hash` differs from the current selection (a different `--limit-queries`, or a changed `queries.jsonl`) exits 64.
    - A referenced file whose bytes changed after completion exits 64.
    - A reused generation id exits 64.
  - **Output normalization**: duplicates and over-depth lists.
  - `INSUFFICIENT_DATA` exits 2.
  - A baseline comparison where every delta is negative still exits 0.
  - The report holds no query text and no path.
  - `test_fixture_pipeline_matches_expected_report`: run mine, run (with a fixed `--generation`) and score in-process into `tmp_path`.
    - Assert that `stable_projection(report)` equals `expected-report.json` to six decimal places.
    - Assert `provenance.users == "multi"` and `attachable is False` for the two-user fixture, and that the table's first line is the not-attachable notice.
    - Assert separately that `report["commit_sha"]` matches `^[0-9a-f]{40}$` and equals `git rev-parse HEAD` run in the repo root.
  - **Provenance.** The same fixture mined with `--user-id` set to one fixture user gives `provenance.users == 1` and `attachable is True`. An export with `deployment_users = 1` and one distinct `user_id` (or all NULL) is attachable. An empty export is `"multi"`.
- [x] 6.3 Write `tests/integration/fixtures/local-db-exec.sh`. It drops a leading `-i` and `exec`s the rest, with `PGHOST`, `PGPORT` and `PGPASSWORD` taken from the environment.

  Then write `tests/integration/test_search_eval_pg.py`, guarded on `PGVECTOR_TEST_ADMIN_URL` through `tests/integration/_harness.py`. When the URL is set and `psql` is not on `PATH`, the module **fails**; it never skips. It covers:
  - **The export gate.**
    - Seed `usage_logs` rows, plus `oauth_tokens` rows for two grants of the same client, matching a subset of the fixture.
    - Run `make search-eval-export DB_EXEC="bash tests/integration/fixtures/local-db-exec.sh" SEARCH_EVAL_DB_NAME=<harness db> SEARCH_EVAL_DIR=<tmp>` with `subprocess`, setting `PGHOST`, `PGPORT` and `PGPASSWORD` from the admin URL.
    - Assert exit 0, a `0700` directory and a `0600` file, that every line is JSON, that `grant_id` is present, that the miner reproduces the expected judgments, and that the seeded rows are unchanged.
  - **Export failure cleanup.** Pre-create a sentinel `usage-export.jsonl`, then run the target with `SEARCH_EVAL_DB_NAME=does_not_exist`. Assert a non-zero exit, no `.tmp` file, and a byte-identical sentinel.
  - **Unit convenience, labelled.** Executing the SQL through asyncpg may stay as a miner-wiring check. Its docstring must say it is not coverage of the export entrypoint.
  - **The usable filter** in Python agrees with `search_analytics.analysable_sql` on every seeded row.
  - **The snapshot step** on the seeded `notes_metadata` produces the expected per-owner paths and hash.
  - **The read-only backstop.** Open the replay engine (4.2) on that database and assert `transaction_read_only = on`. Assert that an INSERT through a supplied session raises SQLSTATE 25006 and writes nothing.
  - **The built-in arms write nothing.** Run the `keyword` arm over seeded `notes_metadata` rows to prove the service wiring (owner, filters, depth). Assert that `pg_stat_user_tables` `n_tup_ins + n_tup_upd + n_tup_del` is unchanged for every table, after `pg_stat_force_next_flush()` or an equivalent stats flush.
  - **Corpus loss end to end.**
    - Snapshot, then delete one judged row from `notes_metadata`, run `keyword`, and assert that the staged arm file's `corpus_loss` names the row and that `score` exits 2.
    - In a second database, snapshot, then delete only one judged note's `note_embeddings` rows and run `keyword` and `semantic`. Only `semantic` reports loss, and `score` exits 2.
  - **Snapshot membership SQL.** On seeded rows with a NULL `content_tsvector`, and with notes lacking embeddings, the snapshot flags `keyword` and `semantic` exactly as D6 defines them. A stale `embedded_content_hash` does not clear `semantic`.
  - **Provenance from a real export.** Seed two users. An unfiltered `make search-eval-export` yields `"multi"`, and `USER_ID=<one>` yields `1`.
- [x] 6.4 Run `pytest -q tests/test_search_eval.py`, then `make search-eval` (it must exit 0 and print both fixture arms), then `make test-integration`. Never run `make test-integration` concurrently with `make test-schema`.

## 7. Docs, in the same PR

- [x] 7.1 In `docs/architecture/search.md`, add a section "Search evaluation harness (#356)". It covers:
  - what a judgment is: the D2 join and the credential principal, with the rules verbatim;
  - the read-only guarantees and their limit, as in D3: built-in arms write nothing, an imported arm is protected only by the disposable copy, and the session default is a backstop;
  - the snapshot, the per-arm membership table (D6, naming the columns), and the difference between drift and corpus loss, including both worked examples;
  - the arm seam, membership declaration, and generations with the manifest (D4), with the #354, #355 and #367 examples;
  - the metric definitions and the bootstrap (D8);
  - the report fields, the stable projection, and that the gate is a reviewed report with no threshold (E2);
  - provenance and why only an attachable report may be attached (D10);
  - accepted limitations L1–L12 and L6a/L7a, one line each.

  Also point "Search benchmarks (opt-in integration)" at the new section as the relevance counterpart of the recall benchmark.
- [x] 7.2 In `DEPLOYMENT.md`, add a "Day-2 operations" subsection "Measuring search quality". Describe exactly this path:
  1. `make search-eval-export [DAYS=] USER_ID=<your id>`. Say which backends it supports, what lands on disk, and that it is tenant data. On a multi-user deployment, `USER_ID=` is required for a report that will be attached. An unfiltered export is for private use only.
  2. `BACKUP_FULL=1 make db-backup`.
  3. Restore into a scratch database.
  4. `make search-eval-snapshot SEARCH_EVAL_DATA=.search-eval SEARCH_EVAL_DATABASE_URL=<scratch copy>`, **before** applying any candidate.
  5. Apply the candidate to the copy, if it changes indexed data.
  6. `make search-eval SEARCH_EVAL_DATA=.search-eval SEARCH_EVAL_DATABASE_URL=<scratch copy> ARMS=… BASELINE=…`.
  7. Attach `report.json` to the PR **only if** it says `attachable: true`. A `CORPUS_LOSS` verdict means the candidate lost searchable notes for that arm, and the run is not evidence. A failed or running manifest means re-run; `score` will not fall back to an earlier run.
- [x] 7.3 In `README.md`, add one line under "Search and discovery": retrieval changes are measured with `make search-eval` against judgments mined from the operator's own usage log. Link it to the search note.
- [x] 7.4 In `CLAUDE.md`:
  - Add a local-gates row: `| Search eval (any search-path change) | make search-eval-snapshot then make search-eval SEARCH_EVAL_DATA=.search-eval SEARCH_EVAL_DATABASE_URL=<restored BACKUP_FULL=1 copy> — attach report.json only if attachable: true (USER_ID= export on multi-user), incumbent as BASELINE; CORPUS_LOSS invalidates |`.
  - Add `make search-eval`, `make search-eval-export` and `make search-eval-snapshot` to the Commands list.
  - Add `scripts/search_eval/` to the search.md row of the architecture-notes table.
- [x] 7.5 Run `grep -n "search-eval\|SEARCH_EVAL_" README.md DEPLOYMENT.md CLAUDE.md docs/architecture/search.md Makefile .gitignore` and confirm every place above is covered. Do not touch `.env.example`: no server setting is added, and `SEARCH_EVAL_*` are tooling variables documented in DEPLOYMENT.md.

## 8. Gates and follow-up

- [x] 8.1 Codex spec review. Round 1 found seven issues. Round 2 verified six of the round-1 fixes; the seventh, atomicity, was not fixed, and round 2 raised two new MAJORs. All of these are folded into the design ("Codex spec review, round 1" / "round 2"). Before implementation, run round 3 as verification of the round-2 folds only. Do not sweep for new surfaces.

  Done: three rounds; the round-3 wording regression was fixed in 0c09439.
- [x] 8.2 After implementation, run the `openspec-verifier` subagent against the spec and `openspec validate search-eval-harness --strict`, and iterate until there are zero blocking gaps.

  Done: one BLOCKER and one MAJOR, fixed at 97c6805.
- [x] 8.3 Run adversarial Codex rounds on the implementation, focused on:
  - silently wrong judgments: mis-credited reads, cross-principal or cross-owner leakage, truncation;
  - corpus loss being masked as drift;
  - the metric formulas and their edge cases;
  - run atomicity;
  - the report leaking query text or paths.

  Run two rounds by default, triaged per the workflow, and list the declined findings in design.md.

  Done: round 1 FAIL, round 2 FAIL, round 3 PASS; declined findings are in design.md.
- [ ] 8.4 Owner step: the first real run. Follow the DEPLOYMENT.md path with `USER_ID=<owner>`, against a restored `BACKUP_FULL=1` copy, with `ARMS="keyword semantic" BASELINE=keyword`. Post the attachable `report.json` on #356 (it carries no query text and no paths) as the incumbent baseline for #354, #355 and #367. Re-check the defaults against the observed yield, per L7a and E3: `--min-queries` 50, the 90-day export and the 120 s pairing window. A change to any default is its own small PR.
- [x] 8.5 After the gates pass, archive with `openspec archive search-eval-harness -y` as the last commit of the feature branch. The PR body says `Closes #356`.

  Done: archived as `2026-10-30-search-eval-harness`; `openspec validate --all --strict` passes.
