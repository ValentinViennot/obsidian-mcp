# Design: the search-eval harness (#356)

## Context

What the log already carries (verified against `src/` at `6bcc9d9`):

- `usage_logs` columns: `id`, `key_id`, `oauth_token_id`, `user_id`
  (`ON DELETE SET NULL`), `tool`, `params` (JSONB), `duration_ms`,
  `response_size`, `actor_kind` / `actor_label` / `actor_ref` (#77,
  migration 015; `actor_ref` is the key's `omcp_` prefix or the OAuth
  `client_id`), `created_at` (insert time).
- `keyword_search` / `semantic_search` rows carry the named args
  `query, folder, limit, tags, frontmatter`. `_truncate_params` cuts any
  top-level string longer than 200 characters to 200 and appends `…`.
  Nested `frontmatter` values are not cut. The rows also carry the #161
  telemetry: `result_count` (int, full count) and `result_paths` (a
  ranked *prefix* of what was returned: at most 10 paths and 2048 bytes,
  never a cut path; `src/services/timing.py`).
- `read_note` rows carry `path, section, offset, limit` (path is also cut
  at 200 + `…`). A read whose body refused, such as `not_found` or
  `selector_unresolved`, carries `params.error = <marker>` and
  `params.body_outcome` (`_tracked`'s tail, `tools.py` around l.1867).
  Pre-body refusals carry `error` or `over_quota` too.
- `src/services/search_analytics.py` already treats a row as a result set
  worth aggregating only when it has neither a pre-body refusal marker nor
  any `error` key (`analysable_sql`). It keys every grouping on
  `(user_id, path)`, because a path is unique only within an owner.
- `make db-backup` reaches PostgreSQL through `docker/db-exec.sh`
  (`DB_BACKEND=docker|cnpg`, `-i` keeps stdin), as the server's local
  `postgres` user. The default dump stores `notes_metadata`,
  `note_embeddings` and `note_links` as **schema only**, so a replay
  against a restored dump needs `BACKUP_FULL=1`, or else a re-index of the
  vault into the restored database.
- `scripts/` holds operator CLIs run as `python -m scripts.<name>`
  (`concurrency_report`'s exit codes are 0/1/2/64). It has no
  `__init__.py`, so it is a namespace package, and the Dockerfile copies it
  into the image.
- Dependencies: `requirements.txt` (runtime, image, `make audit`, CI
  pip-audit) and `requirements-dev.txt` (`-r requirements.txt` plus pytest,
  respx, pip-audit), which CI's `tests` job installs. There is no pyproject.

The consumer of this harness is the owner and the implementers of #354,
#355 and #367. They need one fixed set of judgments, and a way to put a
candidate retrieval configuration and the incumbent side by side on it.

## Goals / Non-Goals

**Goals:**

- Judgments mined from real agent behaviour, with every exclusion counted
  and reported.
- Any number of arms, built-in, imported or precomputed, scored on the same
  qrels in one report.
- No write to production, by construction. The export is the only step
  that touches it, and it runs in a read-only transaction. Replay runs
  against a disposable restored copy. The built-in arms write nothing, and
  the read-only session default backs that up.
- A candidate cannot improve its score by losing indexed notes (D6).
- No new dependency. A `make search-eval` that passes in CI with no real
  data, through a fixture test that has nothing to skip on.

**Non-Goals:**

- Changing `keyword_search`, `semantic_search`, `find_related`, their
  telemetry, the panel or the schema.
- An automated pass/fail threshold on the metrics (decision E2).
- Graded relevance, click models, or LLM-judged relevance.
- Evaluating `find_related` (its input is a note, not a query; a later
  change can add it).
- Shipping the eval tooling into the production image's dependency set.

## Decisions

### D1. Export is fixed SQL through `db-exec.sh`; mining is offline Python

`make search-eval-export` runs
`$(DB_EXEC) -i psql -U postgres -d $(SEARCH_EVAL_DB_NAME) -X -q -A -t -v ON_ERROR_STOP=1 -v days=$(DAYS) -v user_id=$(USER_ID) < scripts/search_eval_export.sql`
(`SEARCH_EVAL_DB_NAME ?= obsidian_mcp`, the name `db-backup` uses). It
writes stdout to `$(SEARCH_EVAL_DIR)/usage-export.jsonl.tmp` under
`umask 077`, in a `0700` directory, and renames the file into place only
when psql exits 0. On failure the temporary file is removed.

The SQL file is `BEGIN TRANSACTION READ ONLY;`, one `SELECT`, and
`COMMIT;`. The `SELECT` reads `usage_logs ul LEFT JOIN oauth_tokens ot ON
ot.id = ul.oauth_token_id` and emits one `json_build_object(...)::text`
line per row with these fields: `id`, `created_at` (epoch microseconds),
`tool`, `user_id`, `actor_kind`, `actor_ref`, `key_id`, `oauth_token_id`,
`grant_id` (that is, `ot.grant_id`, used for the D2 principal), two
provenance fields for D10, and `params`. The provenance fields are
`user_filter` (the `:'user_id'` variable, or `null` when it is empty) and
`deployment_users` (the scalar subquery `(SELECT count(*) FROM users)`).
`params` is restricted to `query, folder, tags, frontmatter, limit,
result_paths, result_count, path, section, error, over_quota`. It covers
`tool IN ('keyword_search','semantic_search','read_note')` and
`created_at >= now() - make_interval(days => :'days'::int)`, with an optional `user_id` filter.
`DAYS` defaults to 90, and the filter applies only when `USER_ID` is set.
Plain `SELECT` with `-A -t`, not `COPY … TO STDOUT`: COPY's text format
backslash-escapes its output, which corrupts JSON. `json_build_object`
never emits a raw newline, so one row is one line.

Why this route:

- It reuses the one database channel the operator has already configured
  for backups, docker and CNPG alike, without new credentials or a port
  forward.
- The SQL is a static file, so a test can pin that it contains nothing but
  the read-only transaction and a `SELECT` (D3).
- The join, the most error-prone part, becomes a pure function over a list
  of dicts. It can be unit-tested exhaustively offline.

Alternative considered: the miner connects with asyncpg and runs the join
in SQL. That needs a DSN that can reach production from the operator's
host, and it puts the join behind a database in every test. Rejected. The
miner's filtering mirrors `analysable_sql` in Python: a row is usable iff
`params.error` is absent or null and `params.over_quota` is not true. That
is equivalent because `analysable_sql` is "not pre-body refused AND no
error key", and every pre-body marker except `over_quota` *is* an error
key. An integration test pins the equivalence over seeded rows (task 6.3).

### D2. The mining join

Definitions:

- **Principal** is the credential principal the rate limiter uses
  (`src/auth/session.py`): `(user_id, "api_key", key_id)` when `key_id` is
  set, else `(user_id, "oauth", grant_id)` when `oauth_token_id` resolved
  to a token row carrying a `grant_id`. The grant, not the token, is what
  stays the same across a refresh. Any other row is excluded and counted
  as `excluded.unresolved_principal`: a deleted key whose `key_id` the
  panel NULLed, a deleted OAuth token (`ON DELETE SET NULL`), or a row
  with neither id. Such rows are **never** merged under `actor_ref`.
  `actor_ref` is a key prefix or a `client_id`, and one `client_id` covers
  many grants that can each be revoked on their own, so merging under it
  would credit one agent's read to another agent's search.
- **Usable search** = tool is `keyword_search` or `semantic_search`, the
  row passes D1's usable filter, `result_paths` is a JSON array of strings,
  and `query` is a non-empty string that was not truncated. A logged query
  is truncated iff its length is 201 and it ends in `…`. Such a row cannot
  be replayed faithfully, so it is excluded (`excluded.query_truncated`).
  A row without `result_paths` predates #161 (`excluded.no_telemetry`).
- **Usable read** = tool is `read_note`, the row passes the usable filter,
  and `path` is a non-truncated string (same 201/`…` rule).

**Attribution.** For each usable read R with principal P and path p, find
the usable searches S with principal P and
`R.created_at - W < S.created_at < R.created_at` (W =
`--window-seconds`, default 120). Among those whose `result_paths`
contains p (exact string equality), credit the **latest** S. One read
credits at most one search. If none qualifies, the read is counted as
`unattributed_reads`.

Why the latest: an agent that searched twice and read a note both searches
surfaced acted on the second result list. Crediting both would double-count
one judgment. Repeated reads of the same note after the same search
collapse into one judgment.

**Queries and qrels.** A query id is
`sha256(user_id ‖ normalized text ‖ canonical JSON of {folder, tags, frontmatter})[:16]`,
where normalized text means whitespace runs collapsed and the result
stripped, with case preserved. All usable searches with the same id fold
into one query, whichever tool issued them. Its relevant set is the union
of credited paths, all at grade 1. Its **pool** is the union of every
logged `result_paths` for the id. Its `source_tools` are the tools that
contributed at least one *credited* search. A query with no credited path
is not emitted. A query without judgments cannot be scored, but it still
counts in `mine-report.json` (`queries_without_judgment`).

**Sources are a set.** A query's `sources` is a subset of
`{"mined", "manual"}`. A manual judgment whose qid matches a mined query
merges into it: the relevant set becomes the union, the pool becomes the
union of the pool and the manual paths, and `sources` becomes
`{"mined", "manual"}`. That query is counted and scored in **both** the
`mined` and the `manual` slice. The report gives per-source query counts
and, separately, `n_both_sources`, so the overlap is visible and the two
slices' counts are not expected to sum to the total.

Outputs in `--out` (`0600` files):

- `queries.jsonl`: `{qid, user_id, text, folder, tags, frontmatter, class, sources, source_tools, pool, n_searches}`
- `qrels.json`: `{qid: {path: 1}}`
- `mine-report.json`: every count above plus the window and the export's
  row count. A run that excludes 90 % of its rows says so. It also
  carries `provenance` (D10), which `score` copies into the report.

### D3. Read-only by construction; replay runs on a restored copy

- **Export.** The transaction is `READ ONLY`, so PostgreSQL itself refuses
  any write in it. `tests/test_search_eval.py` parses
  `scripts/search_eval_export.sql` into statements and asserts the exact
  shape `BEGIN TRANSACTION READ ONLY` / one `SELECT` / `COMMIT`. A later
  edit that adds a statement fails the test.

  The integration gate runs `make search-eval-export` itself, with real
  `psql` and the real Make recipe: the `-v` quoting, the redirect, the
  `umask`, the `.tmp` rename and the cleanup. Only the transport is swapped.
  `DB_EXEC` is overridden with a test shim,
  `tests/integration/fixtures/local-db-exec.sh`, which drops `-i` and
  `exec`s the command locally with `PGHOST`, `PGPORT` and `PGPASSWORD`
  pointing at the throwaway server. `SEARCH_EVAL_DB_NAME` is set to the
  harness's per-module database. The gate asserts:
  - the output file exists, with directory `0700` and file `0600`;
  - every line is JSON and the miner reproduces the expected judgments;
  - the seeded rows are unchanged;
  - a forced failure (a nonexistent `SEARCH_EVAL_DB_NAME`) leaves neither
    the `.tmp` file nor a replaced output file, and exits non-zero.

  Executing the SQL through asyncpg is allowed as a unit convenience for
  testing the miner. It does not count as coverage of the export
  entrypoint.

  **psql on the runner.** CI's `tests` job runs on `ubuntu-latest`, and
  GitHub's Ubuntu runner image ships the PostgreSQL client. The job does
  not install it today. Because the image's contents are not this repo's
  to pin, the tests job gains a step that runs `psql --version` and, only
  if that fails, runs `sudo apt-get install -y postgresql-client`. The
  integration module **fails, never skips**, when `PGVECTOR_TEST_ADMIN_URL`
  is set but `psql` is not on `PATH`.
- **Where to replay: a restored copy, always.** Every documented real run
  restores a `BACKUP_FULL=1` dump into a scratch database, applies the
  candidate there if it needs one (a migration, a re-chunk, a re-embed),
  and replays against it. A candidate can also produce a run file there and
  have it scored with `runfile:` (D4) against the qrels mined from
  production. The docs and the Makefile target describe no other replay
  target. Production is touched only by the read-only export.
- **What is guaranteed read-only, and what is not.** `run` builds its
  engine from `SEARCH_EVAL_DATABASE_URL` (or `--database-url`), a plain URL
  and never `DATABASE_URL`. The engine sets
  `connect_args={"server_settings": {"default_transaction_read_only": "on"}}`,
  and before any arm runs, `run` asserts `SHOW transaction_read_only = 'on'`
  on a fresh session.
  - **The built-in arms write nothing.** They issue only the services' read
    statements through the supplied sessions. An integration test runs
    `keyword` against a seeded copy and asserts that no table's
    row-modification counters move (`pg_stat_xact_*` inside the session,
    or a before/after `n_tup_ins + n_tup_upd + n_tup_del` read of
    `pg_stat_user_tables` with a stats flush).
  - **An imported `module:factory` arm runs with whatever the replay URL
    permits.** Session defaults are mutable: such an arm can open its own
    engine, or issue `SET TRANSACTION READ WRITE`. The harness does not
    claim to stop that. What protects the data is the replay target, a
    disposable restored copy. The `default_transaction_read_only`
    assertion is a backstop that catches an accidental write through the
    supplied sessions, not a requirement on every arm.
- **The corpus snapshot (D6).** `python -m scripts.search_eval snapshot
  --database-url <copy> --out DIR` runs on the restored copy **before** any
  candidate derivation (migration, re-chunk, re-embed). It runs in the same
  read-only session type. It writes `corpus-snapshot.json` (`0600`) and
  records the file's `sha256` in `snapshot-meta.json`, next to the qrels.
  The file records **per-arm searchable membership**, not only metadata
  presence: `{user_id: {file_path: {"metadata": true, "keyword": bool,
  "semantic": bool}}}`. The memberships are defined in D6. The harness
  cannot verify that a candidate was not applied first. The ordering is a
  documented operator step, and the snapshot hash in every report is how
  two reports show they share a baseline corpus. For the fixture,
  `corpus.json` is the snapshot.

### D4. The arm seam

```python
@dataclass(frozen=True)
class EvalQuery:
    qid: str
    user_id: int | None
    text: str
    folder: str | None
    tags: list[str] | None
    frontmatter: dict | None
    query_class: str          # "identifier" | "prose"
    sources: frozenset[str]   # subset of {"mined", "manual"}; both after a collision

Arm = Callable[[EvalQuery, int], Awaitable[list[str]]]   # ranked paths, best first, ≤ depth

@dataclass(frozen=True)
class ArmContext:
    session_factory: async_sessionmaker | None   # read-only (D3); None in fixture mode
    corpus: Corpus                               # the replay corpus: member(user_id, path, membership), for the corpus-loss check (D6)
    options: dict[str, str]                      # --arm-opt key=value, passed through

ArmFactory = Callable[[ArmContext], Arm]
```

`--arm` names resolve in this order:

1. Built-ins in `scripts/search_eval/arms.py`:
   - `keyword` calls `full_text_search(session, q.text, folder=…, limit=depth, tags=…, frontmatter=…, user_id=q.user_id)` and keeps `path`.
   - `semantic` calls `semantic_search(...)` the same way.
   - `fixture-lexical` and `fixture-title` (D7) are fixture-only and need no session.
2. `runfile:<path>`: an arm file that this harness's own `run` step
   produced in a **complete** generation on another database, for example
   a re-embedded copy. Its format is that of every staged arm file:
   `{"meta": {arm, membership, depth, generation, selected_qids_hash,
   snapshot_sha256, qrels_sha256, corpus_loss: {qid: [path…]}}, "runs":
   {qid: [path, …]}}`. It is refused with exit 64 when any of these hold:
   - its `qrels_sha256` or `snapshot_sha256` differs from the data
     directory's;
   - its `selected_qids_hash` differs from the current selection's;
   - it does not come with the producing generation's complete manifest.

   On success, `run` copies it into the current generation. A selected
   query absent from `runs` is listed in the report as `missing_queries`
   for that arm and scored as an empty result. This is the only tolerated
   omission (see "Atomicity" below).
3. `<module>:<factory>`: imported with `importlib`, so a follow-up change
   ships `scripts/search_eval_arms/hybrid.py:make_arm` (or any module on
   `sys.path`) and runs `make search-eval ARMS="keyword semantic scripts.search_eval_arms.hybrid:make_arm"`.
   A DB-backed factory must declare `factory.membership`, one of
   `"metadata"`, `"keyword"`, `"semantic"` or `"keyword_and_semantic"` (D6).
   Without it, the run exits 64. Built-in `keyword` and `semantic` declare
   their own, and fixture arms use the fixture corpus.

Each arm call gets a fresh session from `session_factory`, one per query.
That is required because `full_text_search` opens its own snapshot
transaction. The harness does not run arms through `_tracked`: telemetry
`record_results` is a no-op outside a tracked call (`timing.py`), so replay
writes nothing to `usage_logs` even in principle. Runs are stored as
ranked lists, so the order the arm returned is the order that is scored,
and no score-sort or tie-break can change it. A duplicate path keeps its
first rank (the dedupe rule, D8), and a list longer than `depth` is
truncated. Both are normal output.

**Atomicity: generations and one manifest.** Every `run` invocation has a
**generation id**. The CLI defaults it to a UTC stamp
(`YYYYMMDDTHHMMSSffffffZ`) computed once in `__main__`. Tests always pass
`--generation` explicitly, and library code never calls `now()`. A
generation id that already exists under `runs/` is refused with exit 64.

The `selected_qids_hash` is the sha256 of the sorted, newline-joined qids
selected from `queries.jsonl` after the drift drop and after
`--limit-queries`. It is the first N qids in sorted qid order, so selection
is deterministic.

The run proceeds in this order:

1. Before any arm or factory executes, write `runs/manifest.json` (via
   `.tmp` and rename) as `{generation, status: "running", started_by:
   "run", selected_qids_hash, qrels_sha256, snapshot_sha256, arms,
   options, depth}`. From this moment, any earlier generation is no longer
   the published one.
2. Stage each arm's file under `runs/<generation>/<arm-slug>.json`.
3. A failure aborts the run with exit 1. A failure is any exception raised
   by an arm or its factory, or any output that is not a `list` of `str`.
   There is no per-query catch, no "treat as empty" path and no retry. On
   failure, rewrite the manifest atomically with `status: "failed"`, the
   `error` (exception type plus the arm name, never query text) and the
   failing arm. Earlier generations' directories stay on disk, but nothing
   references them.
4. Only after **every** arm has answered **every** selected query, rewrite
   the manifest atomically with `status: "complete"` and `files: {arm:
   {path, sha256}}`.

`score` reads **only** the manifest. It never reads a run file the manifest
does not name, and it never falls back to an earlier generation. It refuses
with exit 64 unless all of these hold:
- `status == "complete"`;
- the manifest's `selected_qids_hash` equals the hash of the qids `score`
  selects now, from the current `queries.jsonl`, the current snapshot and
  the manifest's recorded selection options;
- `qrels_sha256` and `snapshot_sha256` match the data directory;
- every referenced file exists with the recorded sha256;
- the requested arms are a subset of the manifest's arms.

Missing entries are tolerated only for a `runfile:` arm's reported
`missing_queries`.

Example: yesterday's generation G1 completed for the same qrels and
snapshot. Today's G2 writes `status: "running"`, its candidate answers 60
queries and raises on query 61, and the manifest becomes `status:
"failed"`. A later `score` exits 64 naming G2's failure, even though G1's
files are intact and their hashes would match.

`--depth` defaults to 20. The largest cutoff scored is 10, and the extra
depth makes recall comparable for arms that overfetch.

Alternative considered: arms take a raw SQL fragment, or a settings
override. Rejected. #354 is a fusion of two arms and #367 a different
database, and only a callable or a run file covers both.

### D5. Query class

`classify(text) -> "identifier" | "prose"` is a pure function in
`classify.py`. A query is `identifier` when it has at most four whitespace
tokens and at least one token
(a) contains one of `_ . / : # \``, or a digit, between word characters
(`snake_case`, `v0.9.3`, `#356`, `src/main.py`), or (b) is camelCase /
PascalCase with an internal capital, or (c) is an all-caps acronym of two
or more letters, or (d) the whole query is a single double-quoted phrase.
Everything else is `prose`. A table-driven test pins the rule. A manual
judgment may set `class` explicitly, and that overrides the heuristic. The
report shows per-class counts, so a misclassified class is visible as an
implausible split.

### D6. Drift, candidate corpus loss, and the scoring report

Two different absences are kept apart, because merging them would hide a
candidate's damage:

- **Vault drift** happens before any candidate. A judged path that is
  absent from the **snapshot** (D3: the pre-candidate set of indexed paths
  on the restored copy, keyed by `user_id` and matched NULL-safe) is a note
  renamed or deleted since it was read. It leaves the qrels and is counted
  in `dropped_paths`. A query left with no relevant path is dropped and
  counted in `dropped_queries`. Without this rule, every renamed note would
  score as a miss that no arm could fix.
- **Candidate corpus loss** happens after a candidate, and it is
  **arm-specific**: a note can keep its `notes_metadata` row and still be
  unreachable by one arm. Searchable membership is defined against the
  columns the current services actually require:

  | membership | definition (per `notes_metadata` row, owner matched NULL-safe) |
  |---|---|
  | `metadata` | the row exists |
  | `keyword` | `notes_metadata.content_tsvector IS NOT NULL` (`full_text_search` matches only on that vector) |
  | `semantic` | `EXISTS (SELECT 1 FROM note_embeddings e WHERE e.note_id = notes_metadata.id)` (`semantic_search` reaches a note only through its chunks) |
  | `keyword_and_semantic` | `keyword AND semantic`: both constituents. A hybrid arm (#354's RRF) draws candidates from both, so losing either one changes its results. |

  `notes_metadata.embedded_content_hash`, the embed pass's certification
  column, is deliberately **not** part of `semantic` membership. Under
  #200, a stale vector is annotated, never filtered, so a note whose
  `embedded_content_hash` differs from `content_hash` is still returned.
  The snapshot nevertheless records the count of judged paths whose vector
  is stale (`embedded_content_hash IS DISTINCT FROM content_hash`), for the
  reviewer's information.

  For each arm, `run` evaluates that arm's membership on the replay URL.
  A judged path that the snapshot marks searchable for the arm's
  membership, and the replay corpus does not, is recorded in
  `meta.corpus_loss`. It **stays relevant** and counts as a miss. The
  report flags `corpus_loss: N` per arm, any `N > 0` marks the verdict
  `CORPUS_LOSS`, and `score` exits 2, because the run is not valid as
  evidence. A judged path that was not searchable under the arm's
  membership even in the snapshot stays relevant (a miss for baseline and
  candidate alike) and is counted as `unsearchable_before`. For
  `keyword_and_semantic`, that means the path lacked **either** constituent
  in the snapshot. Such a path is `unsearchable_before` for the hybrid arm
  and is never its corpus loss.

  Examples. Query Q has relevant paths {A.md, B.md}, and the incumbent
  returns both.
  - The candidate's derivation drops B.md from `notes_metadata`. Q keeps
    {A.md, B.md}, the candidate's recall@10 is 0.5, the report shows
    `corpus_loss: 1`, and the exit code is 2.
  - A candidate re-embedding pass deletes B.md's `note_embeddings` rows and
    then fails, leaving the metadata row intact. The `semantic` arm (and
    any `semantic` or `keyword_and_semantic` candidate) gets `corpus_loss:
    1` and `CORPUS_LOSS`. The `keyword` arm is unaffected, because B.md's
    `content_tsvector` is unchanged.

The drift drop is computed from the snapshot only, never from a replay
corpus, so no candidate can shrink the qrels.

`score` computes `recall@5`, `recall@10`, `mrr` and `ndcg@10` for each
arm, overall, per class (`identifier`, `prose`) and per source (`mined`,
`manual`), using the metrics of D8. It also computes:

- `n_queries`.
- `novel@10`: the mean share of the arm's top ten that is not in the
  query's pool. A manual query's pool is its judged set. A candidate that
  surfaces notes the incumbent never showed scores low on recall *because*
  those notes were never judged, and this number is how a reviewer sees
  that (L1).

With `--baseline <arm>`, each other arm gets, per metric and per slice,
the per-query deltas (candidate minus baseline, over the queries both
scored) and their mean. It also gets a bootstrap 95 % confidence interval
on that mean: 1,000 resamples of the per-query deltas with replacement,
using `random.Random(seed)` with `--seed` defaulting to 0, and the 2.5th
and 97.5th percentiles. The interval is reported and never gates.
`report.json` carries every number, plus:
- the qrels hash, the snapshot hash and the manifest's generation id;
- the arms, their options and their memberships;
- the window and the seed;
- `provenance` and `attachable` (D10);
- `commit_sha`.

Two reports therefore say whether they used the same qrels and the same
baseline corpus.

**The stable projection.** `commit_sha` (and any timestamp the report
grows) changes with every commit, so a committed golden file cannot
contain it. `scoring.stable_projection(report)` is documented in the
module. It returns the report minus the keys in `UNSTABLE_KEYS =
{"commit_sha", "generated_at", "generation"}`. The fixture test compares that
projection with `expected-report.json`, which is itself a projection. It
separately asserts that `commit_sha` matches `^[0-9a-f]{40}$` and equals
`git rev-parse HEAD`, run in the repo.

Exit codes: 0 when scored. 2 when fewer than `--min-queries` (default 50)
judged queries survive the drift check (`INSUFFICIENT_DATA`), or when any
arm has corpus loss (`CORPUS_LOSS`); the metrics are printed either way.
64 on a usage error: an unknown arm, a missing file, a missing snapshot,
a manifest that is not `complete` or does not match the current data, or
a factory without a declared membership. 1 on any other failure, including
an arm that raised during `run`. There is no metric
threshold: the gate is the report, judged by the reviewer (E2).

### D7. The synthetic fixture and CI

`tests/fixtures/search_eval/` holds:

- `usage-export.jsonl`: hand-written rows in the export's exact shape,
  covering every branch in D2. These include a credited read, a read outside
  the window, a read of an unshown path, a failed read (`error: not_found`),
  a refused search (`over_quota`, `rate_limited`), a truncated query, a
  pre-#161 row, two searches where the later one is credited, two principals
  interleaved, **two grants of the same OAuth client** (same `actor_ref`,
  different `grant_id`), a row with an unresolvable principal, two users
  with the same path, and an `identifier` and a `prose` query.
- `corpus.json`: `{user_id: {path: {title, body}}}`. It serves as both the
  snapshot and the fixture replay corpus, and one judged path is absent
  from it to exercise the drift drop. Candidate corpus loss is exercised by
  unit tests that pass a smaller replay corpus, not by the golden pipeline.
- `manual.jsonl`: two manual judgments, one with an explicit class and
  one colliding with a mined qid.
- `expected-report.json`: the stable projection (D6) of the golden report.
  The fixture export holds two users and no `user_filter`, so the golden
  report has `provenance: {users: "multi"}` and `attachable: false`, and
  the test asserts both. A second in-process run over the same export,
  filtered to one user, asserts `provenance: {users: 1}` and
  `attachable: true`.
- `corpus.json` carries the per-arm membership flags too: every fixture
  path is `keyword` and `semantic` searchable, so the golden pipeline has
  zero corpus loss.

`fixture-lexical` ranks by term overlap over title and body.
`fixture-title` ranks by title match only. The two disagree on purpose, so
the delta and bootstrap-interval output is exercised.

`make search-eval` with no `SEARCH_EVAL_DATA` runs
`mine → run → score --min-queries 1` on the fixture into a temporary
`--out`. It never writes into the fixture directory.
`tests/test_search_eval.py` runs the same pipeline in-process. It asserts
that `stable_projection(report)` equals `expected-report.json` to six
decimal places, and checks `commit_sha` separately (D6).

The harness uses only the standard library on the offline path. The
built-in DB arms import `src.services` lazily, inside their factories, so
loading the fixture needs no `DATABASE_URL`. The module therefore has
nothing to skip on and runs in CI's existing `pytest` step. There is no
skip guard and no new CI variable. The only workflow change is D3's
`psql` presence step for the integration export gate.

### D8. Metrics are hand-written; no dependency

`scripts/search_eval/metrics.py` holds pure functions over
`relevant: set[str]` and `run: list[str]` (ranked, best first):

- `recall_at(k)` = |relevant ∩ dedup(run)[:k]| / |relevant|.
- `reciprocal_rank` = 1 / the rank of the first relevant item in
  `dedup(run)`, or 0 when none is relevant. MRR is its mean over queries.
- `ndcg_at(10)` = DCG / IDCG with binary gains, where
  DCG = Σ_{i ≤ k} rel_i / log2(i + 1) over `dedup(run)[:k]` and IDCG is
  the same for min(|relevant|, k) ones.
- `mean(values)`.
- `bootstrap_ci(deltas, *, resamples=1000, seed=0, level=0.95)`. An empty
  list yields `None`.

`dedup` keeps first occurrences, so a duplicate path never earns gain twice.
A query with an empty relevant set never reaches the metrics, because the
drift drop removes it (D6). These edge cases are pinned in unit tests
against hand-computed values on tiny qrels and runs: an empty run (every
metric 0), a relevant path missing from the run, duplicates in a run,
k larger than the run, and more relevant paths than k (where IDCG is capped
at k).

Alternative considered: a third-party IR evaluation library. Rejected,
see decision E1.

### D9. Where the real data lives

`.search-eval/` at the repo root is added to `.gitignore`. `make
search-eval-export` creates it `0700` and writes files `0600` under
`umask 077`. `SEARCH_EVAL_DIR` overrides it, for example to
`$(DATA_DIR)/search-eval` from `Makefile.local` on the production host. The
directory's contents are tenant data: query text, the paths of notes
agents read, and owner ids. They are never committed, never uploaded as a
CI artifact and never pasted into an issue. A PR may attach only an
**attachable** report (D10), which holds metrics, counts and hashes. The
report contains no query text and no path, and corpus loss appears in it
as a count only. `score` is responsible for keeping it that way (task
4.7). The operator who runs the export is an admin, who already sees the
same rows on `/admin/search-analytics`.

### D10. Provenance: only a single-tenant report is attachable

Removing query text, paths and user ids does not make a multi-user report
safe to publish. Aggregates are themselves tenant data. The reference
deployment is multi-user, and the repo is public. Suppose `n_queries`,
`dropped_paths`, `corpus_loss` or a slice's metrics are published from an
unfiltered export. Anyone who knows tenant A's contribution can then
subtract it and learn that tenant B searched, roughly how much, and how
well retrieval served B. B never agreed to that.

So `mine` derives `provenance` from the export and `score` copies it into
`report.json`:

- `provenance.users = 1` when every row carries the same non-null
  `user_filter`, meaning the export ran with `USER_ID=`. It is also `1`
  when `deployment_users ≤ 1` and the export holds at most one distinct
  `user_id`, counting NULL as one. That covers a single-user deployment.
- Otherwise it is `provenance.users = "multi"`. An export with zero rows
  is `"multi"`, because it carries no evidence.
- `attachable = (provenance.users == 1)`.

The documented PR gate (CLAUDE.md, DEPLOYMENT.md, search.md) says that
only a report with `attachable: true` may be attached, and that on a
multi-user deployment the export must be run with `USER_ID=` set to the
owner's own id. An unfiltered multi-user run still works and is useful
for the operator's private judgement, but its report is marked
`attachable: false` and the table prints `NOT ATTACHABLE (multi-user
provenance)` as its first line. The fixture test asserts both values (D7).

## Risks / Trade-offs

- [The miner trusts `params` shapes] → It reads only keys reserved by the
  #161 telemetry contract. Every value is type-checked, so a non-array
  `result_paths` or a non-string `query` is counted and skipped, never a
  crash. That is the same two-belts rule `search_analytics` follows.
- [Export load on the primary] → One indexed range scan over a bounded
  window, in a read-only transaction. The window is capped at `DAYS ≤ 365`.
- [A hand-written metric is subtly wrong] → Every formula is in D8. The
  unit tests compare against values computed by hand, including the edge
  cases. The fixture's golden report is checked by hand for one arm.
- [Semantic replay needs an embedding provider] → The restored copy is
  replayed with the operator's provider settings. Replay is sequential, and
  `--limit-queries` caps it.

## Accepted limitations

- **L1. Incumbent bias.** Judgments exist only for notes the live tools put
  in a call's first ten logged results. A candidate that finds a better
  note nobody was shown gets no credit. `novel@10` and manual judgments are
  the mitigation, not a fix. Read a recall drop alongside `novel@10`.
- **L2. A read is not relevance.** Agents read wrong notes, and skip right
  ones whose preview sufficed. Grade 1 means "the agent chose to read it".
- **L3. Long values are lost.** Queries over 200 characters and read paths
  over 200 characters are truncated in the log and excluded.
- **L4. Exact path match only.** A read spelled differently from the result
  row (no `.md`, a leading `./`) does not pair. Agents copy paths from
  results, so this undercounts and never mis-credits.
- **L5. Vault drift.** Judgments for notes since renamed or deleted are
  dropped (D6). A rename loses its judgments, and they are not followed to
  the new path.
- **L6. Principal granularity.** The principal is the API key or the
  OAuth grant, the same unit the rate limiter uses. Two concurrent sessions
  sharing one key or one grant are one principal, so a read can be
  credited to the other session's search if that search also surfaced the
  path. Rows from deleted credentials are excluded rather than merged, so
  they lose their evidence. The mine report counts them
  (`excluded.unresolved_principal`).
- **L6a. The snapshot ordering is operator discipline.** The harness
  cannot prove that `snapshot` ran before a candidate was applied to the
  copy. A snapshot taken afterwards would hide that candidate's corpus
  loss as drift. The documented procedure, and the snapshot hash in every
  report, are the controls.
- **L7. The window is a heuristic.** 120 s is a default and is reported in
  `mine-report.json`. A slow agent's read is lost, and a coincidental read
  within the window is credited.
- **L7a. The defaults are unvalidated.** `--min-queries` 50, the 90-day
  export window and the 120 s pairing window are guesses. They are to be
  re-checked against the first real export's yield (task 8.4).
- **L8. Replay is not bit-reproducible.** HNSW is approximate, and the
  embedding provider may drift. Compare arms within one report, not across
  reports taken months apart.
- **L10. Old rows.** Rows before #161 have no `result_paths`. Rows from
  before typed body outcomes may show a failed `read_note` without an
  `error` marker, but such a read of a just-shown path is rare.
- **L11. Ownerless rows.** In multi-user mode, a deleted user's rows have
  `user_id` NULL and replay against the ownerless slice. The drift check
  drops most of them, and the report counts them (`ownerless_queries`).
- **L12. Small samples.** A personal vault may yield tens of judged
  queries, not thousands. The bootstrap interval shows how wide the
  uncertainty is, and `INSUFFICIENT_DATA` stops a 12-query verdict from
  looking like a result.

- **L13. `SEARCH_EVAL_DATA` is not checked against `SEARCH_EVAL_DIR`.**
  `SEARCH_EVAL_DATA` may point outside `SEARCH_EVAL_DIR`; the recipe does
  not verify it is git-ignored. Both are the same directory by convention,
  and the 0700/0600 modes still apply.
- **L14. Escaped truncated queries.** A truncated query containing a NUL or
  a lone surrogate is not caught by `is_truncated`, because #310's escaping
  can grow the stored string past 201 characters. Implausible input; not
  handled.
- **L15. A symlinked `SEARCH_EVAL_DIR` is not refused.** The harness
  writes through unpredictable `O_EXCL` temp files, but it does not check
  whether the data directory itself is a symlink. Declined: the directory is
  the operator's own `0700` directory, and a planted symlink there means the
  operator account is already compromised.
- **L16. `score` trusts the field types of a complete manifest.** A
  hand-edited `complete` manifest whose fields have the wrong types (for
  example `selection.limit_queries` as the string `"5"`) fails with exit 1,
  not 64. The manifest is operator-owned state in a `0700` directory that
  only the harness writes.

## Decisions taken during implementation

- `DAYS ?= 90` is a default for the `search-eval-export` target only. A
  global default would change `make concurrency-report`, which passes
  `--days` whenever `DAYS` is set.
- `make` reports a failing recipe as exit 2, so "exits 64" for a Make
  target is the recipe's code (make prints `Error 64`). The distinct codes
  0/1/2/64 come from `python -m scripts.search_eval score` (and the other
  subcommands) directly.
- D5 classifier additions: a token starting with `#` or a backtick followed
  by a word character, and a letter next to a digit (`v2`, `sha256`), also
  count. A digit between two word characters counts, so any token with
  three or more digits makes a short query `identifier`: `2026`,
  `taxes 2026` and `top 100 books` are identifiers. The qid is sensitive to
  tag order (the tags list is hashed as logged).
- Per-query deltas are reported as a sorted list of numbers without qids,
  because a qid is a hash of the owner and the query text, and a short
  query could be confirmed by guessing.
- A truncated `folder` filter is excluded as `folder_truncated`, for the
  same reason a truncated query is: it cannot be replayed faithfully.
- Snapshot entries carry `stale_vector` (information only; the report
  counts `stale_vector_judged`). It never affects membership.
- `run --fixture-corpus` copies the fixture corpus into the data directory
  as `corpus-snapshot.json`, so `score` reads one snapshot path in both
  modes.
- Real-data `make search-eval` passes `$(SEARCH_EVAL_DATA)/manual.jsonl` to
  `mine` when that file exists.
- An imported factory is database-backed unless it sets
  `requires_db = False`; a non-database factory without a membership
  defaults to `metadata`.
- `mining.passes_usable_filter` is the seam the integration test uses to
  compare the Python usable filter with `analysable_sql`.
- Helper modules beyond the proposal's list: `_io` (canonical JSON, hashes,
  owner-only atomic writes), `corpus` (membership), `db` (the read-only
  engine and the membership SQL), `commands` (file-level steps) and
  `runner` (the generation).
- Task 6.3's optional asyncpg execution of the export SQL was skipped: it
  is a convenience, psql's `:'var'` syntax would have to be substituted,
  and the real Make target already runs in the integration gate.
- The report's leak check compares the generation, arm labels and option
  keys and values by exact equality only (they are stamps and operator
  words); every other string also gets the substring test. A failed
  manifest records the exception type and the arm label only (D4); the
  message goes to stderr.

## Migration Plan

No migration, no setting, no dependency and no deploy step. The change
ships as tooling, and rollback is reverting the PR.

The first real use (task 8.4) is the owner's:

1. `make search-eval-export`.
2. `BACKUP_FULL=1 make db-backup`.
3. Restore that dump into a scratch database.
4. `make search-eval-snapshot SEARCH_EVAL_DATA=.search-eval SEARCH_EVAL_DATABASE_URL=<the scratch copy>`,
   before any candidate is applied.
5. `make search-eval SEARCH_EVAL_DATA=.search-eval SEARCH_EVAL_DATABASE_URL=<the scratch copy> ARMS="keyword semantic" BASELINE=keyword`.

That run records the incumbent baseline that #354, #355 and #367 compare
against.

## Owner decisions (2026-10-29)

- **E1. No evaluation library; the metrics are hand-written** (D8). Four
  formulas and a bootstrap do not justify a numba/pandas/scipy dependency
  tree, its CI install and a skip guard. A parametric paired test would
  need scipy, so per-query deltas with a seeded bootstrap 95 % CI replace
  it.
- **E2. The gate is a report the reviewer judges; there is no regression
  threshold.** On small personal-vault samples a fixed threshold would push
  follow-ups to tune noise. The exit codes and `INSUFFICIENT_DATA` stay.
- **E3. The defaults stay** (`--min-queries` 50, a 90-day export, 120 s
  pairing). They are to be re-checked against the first real export (L7a,
  task 8.4) rather than argued in the abstract.
- **E4. Every documented real replay uses a restored `BACKUP_FULL=1`
  copy** (D3). This keeps production limited to one read-only export.
  `SEARCH_EVAL_DATABASE_URL` stays a plain URL, and the read-only assertion
  stays as a backstop.

## Codex spec review, round 1 (FAIL, seven findings, all folded)

1. **BLOCKER: the drift drop hid candidate index loss.** Drift is now
   measured against an immutable pre-candidate snapshot. A path that is in
   the snapshot but missing from the replay corpus is corpus loss: it stays
   relevant, and the run exits 2 with verdict `CORPUS_LOSS` (D3, D6).
2. **MAJOR: the `actor_ref` principal merged independent grants.** The
   principal is now `("api_key", key_id)` or `("oauth", grant_id)`, with
   `grant_id` joined in by the export. Unresolvable rows are excluded and
   counted, never merged (D1, D2, L6).
3. **MAJOR: read-only could not be enforced for imported arms.** The
   guarantee is narrowed. The built-in arms write nothing, and a test
   asserts it. An imported arm runs with whatever the URL permits, its
   protection is the disposable restored copy, and the assertion is only a
   backstop (D3).
4. **MAJOR: arm failure and publication were not atomic.** Any arm
   exception or invalid output aborts with exit 1. Run files are published
   by rename only after the full matrix completes, and `score` refuses an
   incomplete or mismatched run with exit 64 (D4, "Atomicity").
5. **MAJOR: the mined+manual collision was ambiguous.** `sources` is now a
   set. A collided query counts in both slices with the union of relevant
   paths, and `n_both_sources` is reported (D2).
6. **MAJOR: `commit_sha` broke the golden fixture.** The test now compares a
   documented stable projection and checks `commit_sha` separately against
   `git rev-parse HEAD` (D6, D7).
7. **MAJOR: the export gate could be bypassed through asyncpg.** The
   integration gate now runs `make search-eval-export` itself through a
   `DB_EXEC` shim and real `psql`, and checks the output file and the
   failure cleanup. The asyncpg path is a unit convenience only. CI's
   runner image ships `psql`, and a presence step installs
   `postgresql-client` if it is ever missing (D3).

## Codex spec review, round 2 (FAIL; six verified, one not fixed, two new)

1. **NOT_FIXED: a stale run survived a failed attempt.** Runs are now
   tracked by generation. `runs/manifest.json` is written with `status:
   "running"` before any arm executes, and is then rewritten atomically to
   `complete` (with file hashes) or to `failed`. `score` reads only the
   manifest, checks that the manifest's `selected_qids_hash` matches what
   `score` selects now, and never falls back to an earlier generation (D4).
2. **MAJOR: corpus loss is specific to each arm.** The snapshot and the
   replay check now use per-arm searchable membership. `keyword` means
   `content_tsvector IS NOT NULL`. `semantic` means at least one
   `note_embeddings` row. `embedded_content_hash` is deliberately excluded,
   because under #200 stale vectors are annotated and still searchable.
   Imported factories must declare which membership they use (D3, D4, D6).
3. **MAJOR: a multi-user report leaked aggregates.** Reports now carry
   `provenance` and `attachable`. A report is attachable only when the
   export was filtered to one user or the deployment has one user. The PR
   gate allows only attachable reports, and the fixture test asserts both
   values (D10).

The next Codex pass on this change verifies these fixes and does not
sweep for new surfaces.

## Codex spec review, round 3 (verification only)

Round 3 verified the round-2 fixes and swept for nothing new. It confirmed
the generation/manifest atomicity fix and the provenance/attachability fix.
It found one wording regression: the former hybrid membership name (an OR
reading) was defined as OR, while the required scenario treats
loss of only the semantic constituent as corpus loss. It was fixed in
0c09439 by renaming the membership `keyword_and_semantic`, defined as both
constituents, and verified by grep and `openspec validate --strict` without
a fourth round.

## Codex implementation review, round 1 (FAIL, six findings)

1. **Manual judgments of another owner rode a user filter** (real, fixed in
   1a27a6d). Under `USER_ID=` or `--user-id`, a manual line for another
   owner entered the query set while the report stayed attachable. Such
   lines are now excluded and counted (`manual_excluded_other_owner`), and
   provenance is derived from every judgment that enters the report.
2. **`score --arm X` validated only the requested arm's file** (real, fixed
   in 78eee3c). Every file the complete manifest references is now checked
   for presence, hash and metadata before scoring the subset.
3. **Predictable `.tmp` paths opened with `O_TRUNC` would follow a planted
   symlink** (hygiene, fixed in 974b479). `atomic_write` uses
   `tempfile.NamedTemporaryFile` in the destination directory and
   `os.replace`; the export recipe uses `mktemp` with an `EXIT` trap.
4. **The extra guard refusing a symlinked `SEARCH_EVAL_DIR`** was declined
   and recorded as L15: the directory is the operator's own `0700`
   directory, and a planted symlink there means the account is already
   compromised.
5. **An OAuth principal was built from `grant_id` alone** (fixed in
   1a4f629). It now requires `oauth_token_id` and `grant_id` together;
   otherwise the row is `unresolved_principal`.
6. **`load_export` repaired undecodable bytes with U+FFFD** (fixed in
   e320738). Lines are decoded strictly, and an undecodable line counts as
   malformed.

## Codex implementation review, round 2 (FAIL; five round-1 fixes verified)

Round 2 verified the round-1 fixes for the manual-owner filter, the
manifest-wide file check, the OAuth principal, strict decoding and the
Python temp files. The Make recipe's `mktemp` change was judged NOT_FIXED
only for want of a regression test. Findings:

1. **A generation was reserved only after the replay engine opened** (MAJOR,
   fixed in 2ce4c5c). An early database failure left the name free, and two
   concurrent runs passed the existence check. `runs/<generation>/` is now
   reserved with an exclusive `mkdir` before the running manifest is
   published; a taken name is exit 64; the directory and a `_manifest.json`
   copy survive every failure.
2. **A hand-edited manifest with a string `limit_queries` exits 1, not 64**
   (MAJOR per the reviewer). Declined after triage and recorded as L16: the
   manifest is operator-owned state in a `0700` directory that only the
   harness writes, and type-validating every field buys an exit-code
   distinction on an implausible input.
3. **No integration test plants a symlink at the export's temp path**
   (MINOR). Declined: the `mktemp` recipe is verified by reading and by the
   pg assertion that no `*.tmp` file remains, and the symlink threat is the
   one L15 already places outside the threat model (the operator's own
   `0700` directory).
4. **design.md still named the obsolete hybrid membership identifier**
   (MINOR, fixed). The round-3 history now describes it without the
   literal name, so `git grep` finds it nowhere in the repository.

## Codex implementation review, round 3 (verification only; PASS)

Round 3 verified the four round-2 items (the atomic generation reservation,
L16, the declined temp-symlink test and the removed obsolete identifier):
four VERIFIED, no findings.
