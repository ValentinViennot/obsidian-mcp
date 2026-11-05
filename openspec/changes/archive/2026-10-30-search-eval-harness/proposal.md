Refs #356

## Why

Retrieval quality is not measured anywhere today. `tests/integration/test_search_recall.py`
proves that the HNSW index does not lose candidates against an exact scan
on a synthetic corpus. It cannot tell whether the notes returned are the
ones an agent needed. Three queued changes are blocked on that measurement
because each is a ranking decision and nobody can say whether it helps:
#354 (hybrid search with RRF), #355 (heading-aware chunking) and #367 (an
embedding-model trial). #344 (title weighting, OR fill) shipped on a single
reproduced miss for the same reason.

The evidence is already logged. Since #161 every `keyword_search` and
`semantic_search` row in `usage_logs` records the query (capped at 200
characters), its filters and its first ten `result_paths`. Every
`read_note` row records the path read and is marked when the read failed.
When an agent searches and, a minute later, reads one of the paths it was
just shown, that is an implicit relevance judgment from the consumer this
product serves. Mining those pairs gives a query→relevant-note set taken
from the real vault, and any candidate retrieval configuration can then be
scored against the same set.

## What Changes

- **A read-only export of the evidence.** `make search-eval-export` runs one
  fixed SQL script through the existing `docker/db-exec.sh` channel, the same
  path `make db-backup` uses for the docker and CNPG backends. The script
  opens `BEGIN TRANSACTION READ ONLY` and contains nothing but `SELECT`.
  It writes the search and `read_note` rows of a bounded window as JSON lines
  into a git-ignored, owner-only directory (`.search-eval/`). It never writes
  to the database.
- **An offline miner.** `python -m scripts.search_eval mine` pairs each
  successful `read_note` with the latest earlier search by the same principal,
  inside a 120-second window, whose logged `result_paths` contain the read
  path. The principal is the credential principal the rate limiter uses: the
  API key, or the OAuth grant resolved from the token by the export. Rows
  whose credential cannot be resolved are excluded and counted, never merged
  under a client id. It writes the judgments as qrels, one query per
  distinct `(user_id, query text, filters)`, and classifies each query as
  `identifier` or `prose`. Operator-written judgments can be merged in, and
  a query's sources form a set, so a query both mined and written by hand
  counts in both slices. The miner is pure Python over the export file, so
  its join logic is unit-tested without a database.
- **A pre-candidate corpus snapshot.** `make search-eval-snapshot` runs on
  the restored copy before any candidate is applied. It records, with a
  hash, each indexed path and which arms can reach it: `keyword` (a
  non-NULL `content_tsvector`) and `semantic` (at least one
  `note_embeddings` row). A judged path missing from the snapshot is vault drift and is
  dropped. A judged path that was searchable by an arm in the snapshot, but
  is not searchable by that arm in the replay corpus, is **candidate corpus
  loss**. It stays a miss and marks the
  run invalid as evidence (exit 2), so a candidate cannot raise its score
  by losing notes.
- **Pluggable arms.** An *arm* is a Python callable: it receives a query
  (text, owner, filters) and a depth and returns ranked vault paths. Built-in
  arms are `keyword` (the current `full_text_search`) and `semantic` (the
  current `semantic_search`). A follow-up change adds a candidate by naming
  `module:factory`, or by handing over a run file (`runfile:<path>`) that
  the harness's own `run` step wrote against a different database, such as
  a re-embedded copy for #367. Every arm is scored against the same qrels.
  An arm that raises, or returns something other than a list of paths,
  aborts the whole run. Each run is a generation. Before any arm runs, a
  `runs/manifest.json` is written marked `running`. It is then rewritten
  atomically to `complete`, with file hashes, or to `failed`. `score` reads
  only that manifest and never falls back to an earlier generation. Replay runs against a disposable restored `BACKUP_FULL=1`
  copy, named by `SEARCH_EVAL_DATABASE_URL`, and that copy is the only
  replay target the docs and the Makefile describe. The built-in arms write
  nothing. An imported arm runs with whatever that URL permits, so the copy
  is what protects the data. The connection's
  `default_transaction_read_only=on`, asserted before the first query, is a
  backstop only.
- **Scoring.** `python -m scripts.search_eval score` scores every arm with
  hand-written metrics in `scripts/search_eval/metrics.py`: recall@5,
  recall@10, MRR and nDCG@10, overall and per query class. It also reports
  query counts and `novel@10`, the share of an arm's top ten that was never
  in the logged result pool for that query and so could not have been
  judged. With `--baseline <arm>` it adds the per-query deltas and a seeded
  bootstrap 95 % confidence interval on the mean delta. That interval is
  reported and never gates. The output is a table plus a `report.json`.
  The report carries `provenance` and `attachable`. Only a report built
  from a single-user export (`USER_ID=` set, or a one-user deployment) may
  be attached to a PR, because on the multi-user reference deployment even
  aggregate counts reveal another tenant's activity.
  Exit codes follow `concurrency-report`: 0 scored, 2 insufficient data or
  corpus loss, 64 usage error or incomplete runs, 1 arm failure. No metric threshold applies: the gate is a report the
  reviewer judges.
- **`make search-eval`.** With no arguments it runs mine, run and score on
  the committed synthetic fixture (`tests/fixtures/search_eval/`), using two
  fixture arms over a fixture corpus. It needs no database, no embedding
  provider and no real data. Pointed at `.search-eval/` with
  `SEARCH_EVAL_DATABASE_URL`, it scores the real set. A pytest module runs
  the same fixture pipeline and pins a stable projection of the report,
  which excludes `commit_sha`. A separate assertion checks the sha against
  `git rev-parse HEAD`. Everything is standard library, so the module has
  nothing to skip on and runs in CI's existing `tests` job.
- **No new dependency.** The metrics and the bootstrap are small pure-Python
  functions, unit-tested against hand-computed values. `requirements*.txt`,
  the image and the CI install steps are unchanged.
- **The gate becomes standing process.** CLAUDE.md's local-gates table gains
  a `make search-eval` row: any change to the search path (ranking,
  chunking, embedding model or provider, query parsing, fusion, a reranker)
  attaches the real-data report to its PR. The report must be attachable,
  with baseline and candidate arms scored on the same qrels. `docs/architecture/search.md` gains a section on
  the harness. DEPLOYMENT.md's day-2 operations section gets a subsection on
  running it.
- **No change to the search tools, the telemetry they record, the panel or
  the schema.** The harness reads only what #161 already writes.

## Capabilities

### New Capabilities

- `search-evaluation`: covers mining implicit relevance judgments from
  `usage_logs`, the read-only guarantees of export and replay, the arm
  interface, scoring and its report, where real evaluation data may live, the
  synthetic CI fixture, and the rule that search-path changes carry a
  `make search-eval` report.

### Modified Capabilities

None. `panel-search-analytics`' "Search result telemetry" requirement is the
contract the miner consumes. It is read, not changed, and the harness adds
no telemetry key.

## Impact

- New: `scripts/search_eval/` (`__main__.py`, `mining.py`, `classify.py`,
  `arms.py`, `metrics.py`, `scoring.py`) and `scripts/search_eval_export.sql`.
- New: `tests/fixtures/search_eval/` (synthetic export, corpus, manual
  judgments, expected report), `tests/test_search_eval.py` (offline:
  mining, classification, metrics against hand-computed values, bootstrap,
  arm loading, fixture pipeline, golden metrics) and
  `tests/integration/test_search_eval_pg.py` (real Postgres). The
  integration module runs `make search-eval-export` itself, through real
  `psql` and a `DB_EXEC` shim, and checks the output file and the failure
  cleanup. It also covers the snapshot, the built-in arms writing nothing,
  and the read-only backstop.
- New: `tests/integration/fixtures/local-db-exec.sh`, the `DB_EXEC` test
  shim.
- Modified: `Makefile` (`search-eval`, `search-eval-export`,
  `search-eval-snapshot`, help text), `.gitignore` (`.search-eval/`) and
  `.github/workflows/ci.yml` (a `psql` presence step in the tests job,
  installing `postgresql-client` only if it is missing). No dependency file
  changes.
- Docs in the same PR: `docs/architecture/search.md`, `DEPLOYMENT.md`
  (day-2 operations), `README.md` (one line under "Search and discovery"),
  `CLAUDE.md` (local-gates row and the `make search-eval` command).
- Production: none. The image is unchanged apart from the new pure-Python
  files under `scripts/`, which it already copies. The export is one
  read-only transaction on the primary, bounded by its window.
- Data handling: the export and the qrels contain query text and note paths
  for every user in the window. That is the same data `/admin/search-analytics`
  shows an admin, but here it lands on disk. Hence the git-ignored `0700`
  directory, the `0600` files and the optional `USER_ID=` filter.
