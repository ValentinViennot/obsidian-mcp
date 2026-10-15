# Obsidian MCP Server

Self-hosted MCP server exposing an Obsidian vault (~2,577 markdown files) via semantic search, full-text search, and agentic exploration.

## Stack
- Python 3.12 / FastAPI / uvicorn
- SQLAlchemy async + asyncpg + pgvector
- MCP Python SDK (Streamable HTTP)
- PostgreSQL 16 with pgvector (shared instance — see `.env`)
- Embedding provider abstraction (`src/services/embeddings.py`):
  - `OllamaProvider` — bge-m3 by default, set `OLLAMA_URL` in `.env`
  - `OpenAIProvider` — `text-embedding-3-{small,large}` over httpx, supports
    Azure OpenAI / OpenAI-compatible base URLs
- Jinja2 control panel; Chart.js vendored under
  `src/control_panel/static/vendor/`, no CDN, hand-written CSS, **nonce-based
  CSP** (`PANEL_CSP`) — see [control panel](docs/architecture/control-panel.md).

## Project Layout
- `src/main.py` — FastAPI app, lifespan, MCP mount
- `src/config.py` — pydantic-settings
- `src/database.py` — async SQLAlchemy engine/session
- `src/models/db.py` — ORM models (api_keys, usage_logs, notes_metadata, note_embeddings, note_links, indexer_runs, transfer_tokens, oauth_clients/codes/tokens)
- `src/mcp_server/` — MCP server, tools, auth middleware
- `src/services/` — vault ops, search, embeddings, indexer, history indexer (temporal embeddings), transfer, anchored FS
- `src/transfer/` — public `/transfer/*` capability-redemption routes
- `src/api/` — control panel REST endpoints
- `src/control_panel/` — Jinja2 templates + static assets
- `alembic/` — database migrations

## Infrastructure
- Container: `obsidian-mcp`, listens on `:8000`, runs as uid 1000 (non-root)
- Compose: one base stack (`docker-compose.yml` — Postgres + app, loopback
  only) plus two front-door **overrides** that add nothing else,
  `docker-compose.caddy.yml` (bundled TLS) and `docker-compose.traefik.yml`
  (labels for an existing Traefik). Never a fourth full stack.
- Traefik routes (the override): hostname driven by `MCP_HOSTNAME` in `.env`
  - Panel routes: human auth via `TRAEFIK_PANEL_MIDDLEWARE`
    (default `chain-oauth@file`)
  - MCP routes (`/mcp/*`): API key auth at app level
  - Transfer routes (`/transfer/*`): same router as `/mcp` (no OAuth chain);
    capability-token auth at app level
- Registry: `localhost:5000` (or change in `Makefile`); CI also publishes to
  GHCR (`.github/workflows/publish-image.yml`)
- Deploy: `make deploy` (build → scan → push → backup → migrate → recreate)
- **CI/CD: a merge to `main` publishes an image. It does NOT deploy.**
  `publish-image.yml` pushes to GHCR and prints the digest; that is where the
  automation stops. **`deploy.yml`'s `workflow_run` trigger is commented out
  on purpose** — a GitHub runner is not on the tailnet, so arming it would put
  an unlocked, passphrase-less deploy key on a publicly reachable port for a
  workload whose releases are infrequent and never urgent. The machinery is
  real and re-armable (four lines, or `tailscale/github-action`), and
  `workflow_dispatch` still redeploys a known-good SHA by hand.

  **Production is rolled by Komodo**, the fleet's convention: repin
  `x-obsidian-mcp-image` in the gitops stack to the new digest, merge, then
  DeployStack. Resolve the digest with `docker buildx imagetools inspect` and
  **check the image's `org.opencontainers.image.revision` label really is the
  commit you think it is** before writing it down. Run the `obsidian-mcp-backup`
  procedure first when the release carries a migration.

  This bullet used to describe the automatic path as if it were live, and it
  cost a session's worth of wrong conclusions — a merge was reported as a
  deploy, twice, while production sat untouched. `pr.yml` is the pre-merge
  gate (`pr-gate` is the one required check) and `scripts/vps-deploy.sh` is
  the host half that the disarmed workflow would call. See
  [continuous deployment](docs/operations/continuous-deployment.md), including
  the three things a rollback does *not* fix.

## Public repo — host paths live outside the tree
This repo is published on GitHub. Anything host-specific (paths, secrets,
hostnames) must stay out of tracked files. The mechanism:
- `Makefile.local` (gitignored) overrides `DEPLOY_DIR` and `DATA_DIR`. On
  the production host both point at `/storage/docker/data/obsidian-mcp/`,
  which holds the real `docker-compose.yml`, `.env`, and `backups/`.
- The compose project that owns the running container is rooted at
  `$(DEPLOY_DIR)`, not the repo. Always invoke `make` from the repo so
  `Makefile.local` loads — `cd /storage/docker/data/obsidian-mcp && docker
  compose ...` works but skips the build/push pipeline.
- **The repo's `docker-compose.yml` is the published, generic stack; the
  deploy-dir copy is the host's and they are deliberately NOT identical.**
  Anything host-specific — a second user's vault mount, a certificate
  resolver, a registry image — belongs in the deploy dir or in a gitignored
  override, never in the tracked file. That file used to carry the
  maintainer's Traefik labels and a named user's vault path; the labels now
  live generically in `docker-compose.traefik.yml`, driven by
  `TRAEFIK_*` variables.

## Commands
- `make init` — first-time setup
- `make deploy` — compose-host build, backup, migration, and deploy (production
  is merge to `main`, see Infrastructure)
- `make db-init` — create database + pgvector extension
- `make db-migrate` — run alembic migrations
- `make db-check` — `alembic check`: schema vs. ORM models (must be clean)
- `make test-schema` — schema gate: migrations vs. models on a throwaway
  pgvector container (run before any deploy that carries a migration)
- `make audit` — pip-audit dependency scan
- `make reindex` / `make reset-embeddings` / `make rebuild-tsvectors` — index
  maintenance (see [indexing and embeddings](docs/architecture/indexing-and-embeddings.md))
- `make backfill-history-dry` / `make backfill-history` — embed the vault's
  *superseded* note versions from its git history (migration 025). Ordinary
  search is unchanged: every current-state reader filters on `valid_to IS NULL`.
  Run the dry form first — a vault's history is much larger than its present and
  every version is a call to a shared embedding endpoint.
- `make db-backup` / `make db-restore` — database dump and restore
- `make logs` — tail container logs
- `make status` — check health

`make help` lists the rest. Deployment detail lives in `DEPLOYMENT.md`, the
user-facing documentation in `README.md`.

## Engineering workflow

The standard flow — supervisor main thread, **Opus** subagents, OpenSpec, the
two Codex audit gates, `openspec-verifier`, and the `user-representative`
browser pass — is defined once in `~/.claude/rules/engineering-workflow.md` and
applies here. Read it; don't restate it. This section records only what is
specific to this MCP server.

**Review budget (from the workflow file):** triage adversarial findings on plausible input and real impact, two full Codex rounds by default and verification-only after that, accepted limitations recorded in the spec rather than polished away, and say which findings were declined. Rigor on what matters; ship in a day what can ship in a day.

**Local gates**

| Gate | Command |
| --- | --- |
| Dependency audit | `make audit` (pip-audit) |
| Migrations | `make db-migrate` (alembic) |
| Schema drift | `make db-check` (`docker exec obsidian-mcp alembic check`) |
| Schema gate (any change carrying a migration) | `make test-schema` |
| Integration suite (real Postgres) | `make test-integration` |
| Deploy | merge to `main` → CI → `image` workflow → Flux (homelab k3s); `make deploy` for a compose host |

**`tests/integration/` skips itself without a database.** Those modules are
guarded on `PGVECTOR_TEST_ADMIN_URL`, so a local `pytest tests` reports green
having executed none of them. CI's `tests` job sets it against a
`pgvector/pgvector:pg16` service, so they do run on every push — but any local
claim that "the suite passes" is a claim about the offline subset unless
`make test-integration` ran, which stands up the throwaway container and sets
the URL. It shares `SCHEMA_TEST_CONTAINER` / `SCHEMA_TEST_PORT` with
`test-schema`, so do not run the two concurrently. **It is Linux-only and
refuses elsewhere**: unlike every other local test path it runs the host's own
`pytest`, because these modules need a docker CLI as well as a Linux kernel
and the test image has no docker client. On a Mac, CI's `tests` job is the
gate — do not report "integration passes" from a machine that cannot run it.

**`alembic check` must be clean** — "No new upgrade operations detected." Run
it after any migration and after any deploy that ran one (`make db-check`, or
`docker exec obsidian-mcp alembic check` directly). A dirty check means the
database and the models disagree, which makes the next autogenerate emit noise
and, worse, hides a real missing constraint in it — see
[schema and migrations](docs/architecture/schema-and-migrations.md).

**Codex framing for this product.** The consumer here is an **agent**, not a
person, and the vault is Max's single source of truth for every project. The
expensive failures are **destructive writes** (an edit or append that clobbers
a note — this has actually happened) and **silently wrong search results**,
which an agent will act on without a human ever seeing the query. Treat any
change to the write tools, section addressing, or the chunking/embedding path
as a mandatory adversarial-pass trigger.

**No `user-representative` pass** for MCP-side changes — the consumer is an
agent. Substitute an end-to-end exercise of the affected MCP tools against the
live server, and say in the report which tools were actually called. Changes to
panel, auth or consent templates (or `panel.js`) do get a browser pass: the
panel sits behind SSO, so Max walks the affected controls by hand with devtools
open and zero CSP violations is part of the bar (see the `panel-csp` change,
tasks §6).

## Key decisions

The load-bearing rationale — the "this was a bug once, do not undo it"
material — lives in [`docs/architecture/`](docs/architecture/), indexed at the
bottom of this file. **Read the relevant note before changing that area, and
update it in the same change.** What stays here is the short list:

- API keys use `omcp_` prefix, stored as SHA-256 hashes
- Panel/OAuth passwords: direct `bcrypt` (`$2b$`, cost 12); both hash and verify truncate the UTF-8 encoding at 72 bytes and reject NUL bytes — passlib's historical semantics — so existing hashes stay valid; don't "fix" the truncation.
- **`AUTH_MODE` governs the *panel* login and nothing else.** `local`
  (default) is the built-in username/password form; `pocketid` federates the
  human login to an OIDC provider (`src/auth/oidc.py`, migration 025's
  `users.oidc_subject`). **`src/oauth/routes.py` — this server's own OAuth 2.0
  authorization server for MCP clients — is untouched in both modes**, because
  PocketID publishes no `registration_endpoint` and MCP clients require DCR.
  The one seam is `authorize_get`'s existing `/admin/auth/login?next=…`
  redirect, whose contract must not change. Under `pocketid` the password form
  and `/admin/register` **404** (not hidden — a working form would be a bypass
  of the IdP), accounts link by the provider's `sub` and never by email, and
  nothing is ever auto-promoted to admin. See
  [control panel](docs/architecture/control-panel.md).
- Vault mounted read-write at /obsidian in container
- Read responses are capped in characters (`MAX_READ_RESPONSE_CHARS`, default
  40,000) independently of the byte caps on disk I/O — see "Three kinds of size cap"
  in [vault tools](docs/architecture/vault-tools.md). Tool output is model
  input; the server must bound it.
- Two active users' vault roots may not overlap. `src/services/vault_overlap.py`
  compares each pair by inode identity `(st_dev, st_ino)` and by a
  **component-wise** canonical-containment test, at assignment time and before
  every pass (five entry points, `detect_and_publish`); an affected pair is
  refused by every MCP tool, pass stage and transfer redemption, and nothing is
  deleted. **Until a snapshot is published, `_vault_root` refuses every
  multi-user caller** — the lifespan publishes synchronously before serving.
  A bind-mount *graft* is undetected **by decision** (L1/L2, owner-accepted
  2026-09-21, #262: never mount one tenant's directory inside another's root)
  and the consequence is cross-tenant read/overwrite/delete — see
  [vault roots and tenancy](docs/architecture/vault-roots-and-tenancy.md).
- **`/mcp` rate control is in-process, and `--workers 1` is part of the
  contract.** Two per-principal token buckets in `_tracked` (general 120/min
  burst 30; write 60/min burst 15 on the eight vault-mutating tools *and*
  `PUT /transfer/upload`, charged to the principal that **minted** the
  capability) plus a per-address budget on failed `/mcp` authentication. The
  principal is `("api_key", id)` or `("oauth", grant_id)` — the **grant**, so a
  refresh does not mint a fresh allowance. State is in the worker's memory and
  deliberately not persisted; a second uvicorn worker multiplies every rate by
  the worker count. See [rate limits](docs/architecture/rate-limits.md).
- **The buckets are the first gates; the daily quota stays the last pre-body
  gate.** Nothing durable is consumed by a call that does not run — a
  rate-refused call spends no `quota_counters` slot. Every refusal raised
  inside `_tracked` ends with one parseable line,
  `MCP-REFUSAL {"code":…,"retry_after_seconds":…}`, carried identically into a
  structured tool's error field; the transport 429s (failed-auth budget,
  transfer redemption) are deliberately outside that contract. `rate_limited` and enforced `slot_timeout`
  rows are coalesced — a row stands for `1 + suppressed` refusals.
- **New API keys get `DEFAULT_DAILY_REQUEST_LIMIT` (5,000); existing keys are
  grandfathered** — applied in application code, never as a column default.
  A blank panel field or an omitted JSON field gets the default; **only an
  administrator creates or restores an unlimited key**, by the panel's
  admin-only Unlimited box or an explicit JSON `null` (a non-admin's is a 403 /
  flash plus `panel_forbidden` `unlimited_requires_admin`); a blank edit is an
  error; and with the default null a create must name its limit (#323). OAuth
  grants (#327), pre-existing NULL-limit keys and admin-made unlimited keys
  therefore have **velocity bounds only**, owner-accepted.
- **Key creation is one budget across both routes, plus a stock cap** (#323).
  `POST /api/keys` and `POST /admin/keys/create` charge one in-process
  `try_charge_key_creation` with two counters that must both admit — exact
  account (10/h) and trusted address (20/h) — charged last, after validation,
  the unlimited rule and the cap, so a refused request spends nothing.
  slowapi cannot share a bucket across two routes, which is why it is not
  slowapi. A non-admin holds at most `KEY_MAX_ACTIVE_PER_ACCOUNT` (25) active
  keys, counted under `FOR UPDATE` on the `users` row; admins exempt, nothing
  revoked. Refusal event `key_creation_throttled`, subject = the exact
  account identity, never the address. The rules live in
  `src/services/api_keys.py` — see [rate limits](docs/architecture/rate-limits.md).
- **Concurrency admission has four modes — `off | shadow | queue | enforce` —
  and defaults to `shadow`** (#261, #188): observe, never wait or refuse.
  `queue` waits like `enforce` but admits with an `overrun` mark where enforce
  would refuse. Promotion is shadow → queue → enforce by a one-line `.env`
  edit, each step gated by `make concurrency-report TARGET=…` PASS over
  covered, single-epoch evidence (migration 028's counters and run
  watermark); never automatic. Transport admission (request envelope, auth
  permit) waits on one bounded deadline and is disconnect-aware — every body
  message it consumes is replayed intact, under a process-wide replay budget.
  Tool admission follows argument checks and precedes quota; leases remain
  held through telemetry (early release lost audit rows). A concurrency
  refusal consumes no quota, but the rate tokens already spent stay spent.
  The pool budget leaves four shared connections of headroom, not a
  reservation. See [rate limits](docs/architecture/rate-limits.md) before
  changing settings.
- **The `/mcp` body-memory budget is always on** (#322): no shadow mode, no
  off switch, every concurrency mode. Each authenticated POST (GET/DELETE
  bypass) reserves its declared `Content-Length` — the full 61 MiB
  per-request limit when unknown — after auth and before the app reads a
  byte, against the process's own cgroup limit × 0.5 ÷ 8 (128 MiB on 2 GiB:
  a 16 MiB small lane for ≤ 1 MiB requests, a strict-FIFO large lane). It
  waits up to 15 s, then answers a transport 429 `code: body_memory`
  outside `MCP-REFUSAL`. Overrides only go the safe way (multiplier ≥ 8,
  fraction ≤ 0.5, budget ≤ the limit's safe allocation), and the **lifespan**
  — never `Settings`, which the alembic init container imports — refuses to
  start when one maximum write cannot fit (≈ 1.1 GiB minimum). See
  [rate limits](docs/architecture/rate-limits.md).
- **`TRUSTED_PROXY_IPS` is the single control of forwarded-header trust**
  (#189). It is validated at boot, stored **canonicalised** (`192.168.0.10/24`
  → `192.168.0.0/24`, because uvicorn's middleware silently matches nothing on
  a host-bit CIDR), logged once at startup, and it drives the app's
  `ProxyHeadersMiddleware`. uvicorn's own layer is switched **off**
  (`--no-proxy-headers` in the Dockerfile and every compose `command:`
  override) — it rewrites the client address *before* the app's middleware
  runs, so a narrowed setting could not undo it. Do not re-enable it or set
  `FORWARDED_ALLOW_IPS`.
- **The panel login has a per-account failed-login budget** (10 / 15 min,
  `PANEL_LOGIN_FAILURE_*`), additive to the 5/min address limit. It is an
  exact in-process map keyed by `users.id`, consulted *before*
  `verify_password`, counts only failures against an existing row (an unknown
  username gets no account budget), and refuses with a response that is
  content-identical to an ordinary failed login — not a 429, not a lockout;
  the window expires on its own. The new timing signal (attempt 11 skips
  bcrypt) is an accepted limitation.
- **Unused dynamically registered OAuth clients expire** (#194).
  `oauth_clients.last_used_at` is stamped by consent approval, code exchange
  and refresh; migration 025 stamped every pre-existing row, so **NULL means
  "registered after 025 and never used"** and nothing registered before 025 is
  ever swept. The periodic cleanup deletes NULL-marked, unclaimed clients older
  than `OAUTH_CLIENT_UNUSED_EXPIRY_DAYS` (30; `null` disables, `0` is refused)
  with no child rows, via `FOR UPDATE SKIP LOCKED` and a re-check *inside* the
  lock — a single `DELETE … WHERE NOT EXISTS` cascades away a just-issued code
  under READ COMMITTED. See [oauth and grants](docs/architecture/oauth-and-grants.md).
- **OAuth grants expire absolutely, and a replayed code revokes its family**
  (#325, #326, migration 029). `oauth_tokens.grant_issued_at` is set at the
  code exchange and inherited verbatim by every rotation, like `grant_id`;
  the deadline is that plus `OAUTH_GRANT_ABSOLUTE_LIFETIME_DAYS` (90, 1–365,
  **not disable-able**), derived from the *current* setting and enforced at
  both mint sites (clamped), the refresh (a live token past it: `invalid_grant`,
  nothing revoked; a rotated-away one is still #182 reuse), the middleware, the
  panel and the transfer credential predicate. Every `OAuthToken(` in `src/`
  passes `grant_issued_at=` (AST guard, Core inserts too; the server default
  is only for a rolling deploy). A spent code keeps its `grant_id` lineage and
  is retained 7 days past expiry; its replay revokes the family **only after
  full revalidation** (client, redirect URI, PKCE), with a response identical
  to an unknown code — and so does **every failed revalidation of a spent
  code** (reason only in the log). A live code keeps its specific refusals
  and the pre-#325 check order (expiry first). See [oauth and grants](docs/architecture/oauth-and-grants.md).
- **Machine-facing paths refuse plaintext HTTP** (#196): a priority-200
  Traefik router on the `http` entrypoint, in `docker-compose.yml`'s labels,
  answers 403 via `ipAllowList` on `192.0.2.0/32` for `/mcp`, `/transfer`,
  `/health`, `/token`, `/register`, `/revoke`, `/.well-known` (except the ACME
  challenge) and the Bearer-qualified root. Browser paths keep redirecting.
  The documentation range is the mechanism, not a placeholder — do not "fix" it.
- **The panel has a nonce-based CSP** (#195). No inline `on*=` handlers and no
  `javascript:` URLs — a control is a `data-*` attribute plus a delegated
  listener in `panel.js`; every `<script>`/`<style>` carries
  `nonce="{{ csp_nonce }}"`; confirm buttons are `type="button"` and fail
  closed. No inline `style=` anywhere (#289): `style-src-attr 'none'`, dynamic
  presentation through `el.style.*` or classes, SVG colours via classes, no
  `!important`; `_utilities.html` is generated. The consent page's
  `form-action` is `'self' https:` by owner decision (multi-hop OAuth
  callbacks). Scope is a marker set by the context
  processor on the four panel/auth/consent template instances — not a path, so
  transfer pages and `/docs` are untouched. `PANEL_CSP=report-only|off` is the
  rollback (recreate, no rebuild). htmx is deliberately absent: its `hx-*`
  attributes are a nonce-bypass gadget. See [control panel](docs/architecture/control-panel.md).
- **Tool-body outcomes are typed internally.** Only the terminal `BodyOutcome`
  (or `ReadNoteResult` private outcome) drives usage classification; never
  infer failure from prose or a note's forged `MCP-REFUSAL` text. Partial writes
  retain publication uncertainty and cannot claim `nothing_written`. Body
  refusals remain executed work; shadow pressure cannot replace their actual
  outcome. See [usage attribution](docs/architecture/usage-attribution.md).
- **Internal hops have one transport policy each** (#184, #185). Database TLS
  is `DATABASE_SSL_MODE` only (default `prefer`; a TLS key in `DATABASE_URL`
  or any `PGSSL*` variable is refused); strict modes are an explicit
  `SSLContext` in both engines, refuse every Unix-socket route before
  connecting, and the lifespan exits on an unencrypted session. The active
  embedding URL must be `https` or loopback unless `EMBEDDING_ALLOW_PLAINTEXT`,
  and every embedding HTTP client comes from `embedding_http_client`
  (`trust_env=False`, no redirects). See
  [schema and migrations](docs/architecture/schema-and-migrations.md) and
  [indexing and embeddings](docs/architecture/indexing-and-embeddings.md).
- **The app is never the PostgreSQL superuser** (#324). The lifespan reads
  `rolsuper` for `current_user` after the transport assertion and exits
  unless `DATABASE_ALLOW_SUPERUSER=true` — every deployment, by owner
  decision; k3s (`obsidian_mcp`, non-superuser) passes. The compose bundles
  bootstrap as `postgres` (password in gitignored `postgres.env`, postgres
  service only; the app service blanks `POSTGRES_PASSWORD`) and create
  `obsidian_mcp` NOSUPERUSER owning its database, validated before `initdb`
  by `docker/postgres-entrypoint.sh`. Migrations need only database
  ownership + `TEMP`; `test_nonsuperuser_migrations_pg.py` gates it, so a
  new extension goes in the init scripts, never a migration. Pre-#324
  compose volumes convert by hand with `docker/upgrade-split-db-roles.sql`
  (nothing automatic). See
  [schema and migrations](docs/architecture/schema-and-migrations.md).
- **Bookkeeping writes commit asynchronously; credential writes never do**
  (#279). `SET LOCAL synchronous_commit = off` is issued in exactly three
  places — the `api_keys.last_used_at` UPDATE (itself skipped when the stamp
  is < 60 s old), `_insert_usage`, and quota admission — so a Postgres crash
  can lose up to ~600 ms of usage rows or quota increments (L1/L2, the latter
  undercounting in the caller's favour). Token rotation/revocation, code
  exchange and transfer-token writes stay synchronous; an AST test pins the
  allow-list. The async commits were sized for PGDATA on a spinning disk
  (~40–100 ms per fsync'd commit); since 2026-09-23 the reference deployment's
  PGDATA is on NVMe (~2.5 ms), so they now matter far less, but they stay.
- **The indexer trusts `(size, mtime_ns, ctime_ns, inode)` and runs off the
  loop** (#278, #282). The scan runs in `asyncio.to_thread`, ahead of the
  generation lock (C1–C8 in `indexing-and-embeddings.md`); an unchanged stat
  skips the read. Stats inside the 2 s racy window of read start are not
  recorded. A full-hash pass runs at process start, every
  `INDEX_FULL_HASH_INTERVAL_HOURS` (24) and on panel Reindex, and the scope
  stays due until one commits with no read failure — so an edit that keeps all
  four stat fields is found within that interval (perf-L3).
  `INDEX_STAT_SHORTCUT=false` for network/FUSE/FAT vault mounts.
- **Indexer failure is counted and reaches `/health`; HTTP stays 200** (#308).
  `src/services/indexer_health.py` holds per-scope consecutive index/embed
  failures and incomplete re-derives, a tick-level enumeration counter and
  "task not running", updated from **every** entrypoint (startup, periodic in
  both modes, panel Reindex). Any counter at `INDEXER_DEGRADED_AFTER_FAILURES`
  (3), a dead task or a quarantined note makes `/health` `status: "degraded"`
  — counts only, no path/error/user id — and logs one CRITICAL, re-armed on
  reset. Never turn it into a non-200: `/health` is the liveness probe, and a
  restart cannot fix vault content. In single-user mode an index failure no
  longer skips the embed stage, and `cleanup_expired_tokens` runs in the
  tick's `finally`. A note whose own write fails with SQLSTATE class 22 or
  54000 (move, upsert row, keyword vector at the floor, links) is
  **quarantined**: the attempt rolls back, the pass re-runs without it (≤
  `INDEXER_QUARANTINE_RETRIES_PER_TICK`, 5), and while its hash is unchanged
  it is absent from the index — row deleted, never served stale — without
  being a read failure or failing the pass. In-process; the full keyword
  rebuild is excluded and stays atomic.
- **Re-derive progress is per row** (#311, migration 030).
  `notes_metadata.derived_under` is a digest of the root facts a re-deriving
  pass observed, bound to the row's path, hash and extraction version; only
  the re-derive tail writes it non-NULL, for rows it fully derived, and any
  writer that mutates a row's extracted link state without changing those
  fields (`move_note`, the move branch, the link backfill) clears it. A
  repeated re-derive rewrites only rows not derived under the current root;
  the stamp records only when an in-transaction re-read finds every surviving
  row current, after re-resolving every link target. A skip (including an
  unlisted directory) withholds only over a row that is not current.
- **The vector index is a `halfvec` expression index** (#283, migration 027),
  defined once in `src/services/vector_index.py` and excluded from `alembic
  check` by `include_object`; queries cast to match it and re-rank by
  full-precision distance. Recall measured 1.00 → 1.00; ~146 MB → 45 MB.
  Embedding reuses a stored vector only on exact chunk text and a matching
  model fingerprint, re-verified under the generation lock (#281).
- **A pass does not prune what it could not see** (#309). Rows at or beneath
  a directory the walk could not list (or type) are neither pruned nor
  move-paired, and the run is recorded `walk incomplete` (failed); under a
  re-derive it withholds the stamp only over a row not yet re-derived (#311). An
  unlistable root, or an empty root over an existing index, raises
  `IndexIndeterminate` with nothing deleted — unless an admin confirmed the
  emptying in the panel: a single-use, 15-minute, in-process permission
  (`src/services/empty_prune.py`) taken by the scope's next pass either way.
  The provenance discard is the stated exception. A wrong but non-empty
  mount is not detected (L1) — see
  [indexing and embeddings](docs/architecture/indexing-and-embeddings.md).
- **The vault may be a git working clone, and every MCP write then makes its
  own commit.** `GIT_VAULT_ENABLED` (default **false** — a non-git vault is
  fully supported and stays the default) plus `GIT_COMMIT_ON_WRITE`.
  `src/services/git_vault.py` carries the whole rationale; the four rules that
  must not be silently reversed are: **a git failure never fails or rolls back
  a write** (the bytes are published before any commit is attempted, so there
  is nothing to roll back — the failure is logged at WARNING and swallowed);
  **global and repo git config are never touched** (the same repository is
  cloned on the owner's desktop where commits must stay attributable to the
  human, so identity travels in `GIT_AUTHOR_*`/`GIT_COMMITTER_*` per
  invocation); **the index is never left half-staged** (a failed commit
  restores its initial index tree, preserving pre-existing staged versions); and **supported publications and sweeps share a kernel lock** (`vault_sync.py`),
  with a process-local `asyncio.Lock` additionally serializing git commits — which is sound for the same
  `--workers 1` reason the rate limiter is, and breaks the same way with a
  second worker. The hook is at the eight write-class `_impl` call sites, which
  know the path, feeding a per-call ContextVar that `_tracked` — which already
  resolves the tool and the principal — flushes into one commit; **not** in
  `vault._atomic_write_at`, which knows neither and is also the write path for
  internal machinery that must produce no commits. `deploy/` holds the
  reconcile sweep (out-of-band edits, isolated-worktree merge, inode-preserving publication, push), its
  systemd units, the bare-repo `post-receive` (a flag file, deliberately not a
  `checkout -f` — see the comment there), and the vault `.gitignore` template.
- **`notes_metadata.modified_at` is a git commit time, not `st_mtime`, when
  `GIT_VAULT_ENABLED`.** A checkout stamps every file with the moment of the
  checkout, so on a server whose vault is a clone the mtime column carried one
  identical value for the whole vault and `get_recent` / `list_notes` sorted
  on noise while reporting nothing wrong. `git_history.last_commit_times`
  answers it in one subprocess per pass; `st_mtime` stays the fallback for a
  note git has not seen yet, and every git failure degrades to it rather than
  failing the pass. See
  [indexing and embeddings](docs/architecture/indexing-and-embeddings.md).
- Wikilink graph extracted from note bodies into `note_links`; resolved at
  index time in Obsidian's own order — exact path, same folder, vault-wide
  stem, then the same three case-folded, then frontmatter `aliases:` — with an
  ambiguous basename going to the note nearest the vault root, never the
  alphabetically first one. `%%comments%%` are masked for link and tag
  extraction and for the embedding text, and **not** for `mask_code`, section
  addressing, `move_note` or `content_tsvector`. `move_note` opts out of alias
  and case-folded resolution (`_MOVE_RESOLUTION`) so widening the graph never
  widens what a move overwrites. See
  [Obsidian compatibility](docs/architecture/obsidian-compatibility.md).
- **The container contract is asserted, not documented.**
  `tests/test_container_hardening.py` holds it: the image is multi-stage and
  runs as uid 1000; `--workers 1` lives in the image CMD and **no compose
  stack may override `command:`** (migrations go through `RUN_MIGRATIONS` in
  `docker/entrypoint.sh` instead — restating the uvicorn line to prepend
  `alembic upgrade head` is how the flag got dropped before); every service
  declares `mem_limit` / `mem_reservation` / `memswap_limit` / `cpus` /
  `pids_limit`; Postgres publishes nothing and the app publishes loopback
  only; a proxy override must use `ports: !reset null`, because compose
  merges `ports` by concatenation and `ports: []` removes nothing. The
  healthcheck must address `localhost`, not `127.0.0.1` — a literal-IP Host
  header is a 400 from `TrustedHostMiddleware`, i.e. a healthy container
  marked unhealthy forever. `docker compose config` itself runs in CI's
  `compose-config` job, since the test container has no docker CLI.
- `MCP_SANDBOX_MODE=true` is a registry-eval-only switch: lifespan skips `_check_embedding_dim` and the indexer, and `APIKeyMiddleware` bypasses auth on `/mcp/*`. Lets Glama's sandbox build the image and validate MCP introspection without external deps. Never enable in production — tools register but cannot run.

## MCP tools

28 tools, all registered in `src/mcp_server/server.py` and all wrapped by
`_tracked`, which resolves the caller's vault root *before* the tool body runs
— that decorator is the whole enforcement of "this user has no vault"
([vault roots and tenancy](docs/architecture/vault-roots-and-tenancy.md)).

**Search and read** — `keyword_search`, `semantic_search`, `read_note`,
`list_notes`, `get_tags`, `get_recent`, `get_vault_guide`.

**Graph**
- `get_backlinks(path, limit)` — notes that link TO `path` (resolved links only).
- `get_links(path)` — outgoing links from `path`, both resolved and dangling.
- `get_neighborhood(path, depth=1, limit=50)` — undirected BFS over the resolved-link graph; capped at `depth ≤ 5` and `limit ≤ 200`.
- `find_related(path, limit=10)` — semantic neighbors via averaged chunk embeddings; pgvector cosine distance, deduped per note.
- `find_orphans(folder, limit)` — notes with no incoming or outgoing resolved links; vault-hygiene tool.

**Git history** — three read-only tools over the vault's own git repository,
for the question the index cannot answer (`src/services/git_history.py` holds
every subprocess; the MCP layer is thin).
- `note_history(path, limit=50)` — `git log --follow` over one note, with the
  **birth commit** reported separately so a short `limit` never costs the
  creation date.
- `note_blame(path, section?, start_line?, end_line?)` — `git blame -w -M -C
  --line-porcelain`, honouring a `.git-blame-ignore-revs` at the vault root.
  `section=` resolves a heading to a line range through the same
  `_section_body_span` the read and write sides use.
- `find_when_written(text, limit=20, path?, regex=False)` — `git log -S`, the
  pickaxe: the commit that *introduced* a string, i.e. "on {datetime} you
  wrote {text}".

This is the only subprocess surface in the codebase. Explicit argv, never
`shell=True`; every `GIT_*` variable dropped and the config layers pinned; a
wall-clock deadline and a stdout byte cap on every invocation, with the process
*group* killed on either; paths through `validate_visible_path` and nothing
re-implementing containment. Bounds live in `src/config.py`
(`GIT_HISTORY_*`, `MAX_HISTORY_COMMITS`, `MAX_BLAME_*`,
`MAX_PICKAXE_TEXT_CHARS`).

Link extraction lives in `src/services/links.py`. The extractor strips fenced/inline code **and `%%comments%%`** before matching `[[wikilink]]`, `![[embed]]`, and `[md](path.md)` forms; a wikilink's `#anchor` and `|alias` are parsed onto `ExtractedLink` rather than discarded. Targets are resolved at index time and stored in `note_links`. On startup, if `note_links` is empty the indexer runs a one-shot backfill across all notes (logged with progress and surfaced on the dashboard). `get_links` reports a non-markdown target (`![[diagram.png]]`, `[[Board.canvas]]`) as an **attachment** rather than a dangling link — it can never resolve, because only `.md` is indexed.

**Write** — `create_note`, `edit_note`, `move_note`, `delete_note`,
`set_frontmatter`. Markdown only, all publishing through
`vault._atomic_write_at` against a descriptor opened at validation. The
destructive-write surface: read
[vault tools](docs/architecture/vault-tools.md) first.

**Raw files (non-markdown)** — `read_file`, `write_file`, `list_files`,
`delete_file`. Pure byte transport, same traversal and dot-dir guards, no
indexing.

**Transfer** — `request_upload`, `check_upload`, `request_download`,
`import_from_url`, plus the public `/transfer/*` routes. A capability pins
everything it may do at mint time; see
[file transfer](docs/architecture/file-transfer.md).

## Architecture notes

One file per area, each holding the decisions that must not be silently
reversed. They are the extracted "why" of this codebase — verbatim, not
summaries.

| Note | Read it before you touch |
| --- | --- |
| [vault-sync.md](docs/architecture/vault-sync.md) | Git-backed vault publication, editor autosaves, retained inodes, interrupted recovery and automatic commit gates |
| [schema-and-migrations.md](docs/architecture/schema-and-migrations.md) | any alembic migration; `alembic check` is the cheap gate, not the whole gate |
| [oauth-and-grants.md](docs/architecture/oauth-and-grants.md) | `src/oauth/`, the consent page, anything minting/rotating/revoking a token |
| [vault-roots-and-tenancy.md](docs/architecture/vault-roots-and-tenancy.md) | `APIKeyMiddleware`, `_vault_root`, owner predicates, publication confirmation, the vault-root overlap guard (`src/services/vault_overlap.py`, the snapshot, the five pass entry points) |
| [vault-tools.md](docs/architecture/vault-tools.md) | any note or file tool: frontmatter, symlinks, anchored writes, section addressing, size caps |
| [file-transfer.md](docs/architecture/file-transfer.md) | `src/transfer/`, `src/services/vault_fs.py`, the publish gate, SSRF policy, the write-bucket charge at redemption |
| [obsidian-compatibility.md](docs/architecture/obsidian-compatibility.md) | `src/services/links.py`, `extract_tags`, `clean_for_embedding`, any graph tool: which Obsidian syntax is read, which is deliberately not, and why `move_note` resolves more narrowly than the indexer |
| [search.md](docs/architecture/search.md) | `semantic_search` / `keyword_search` / `find_related` and every `SET LOCAL` they issue |
| [indexing-and-embeddings.md](docs/architecture/indexing-and-embeddings.md) | the indexer loop, the embed pass, tsvector writers, provider abstraction |
| [security-event-logging.md](docs/architecture/security-event-logging.md) | `src/logging_setup.py`, `src/services/security_events.py`, and any call site that logs a refusal: the field allow-list, the event catalogue, the suppressor |
| [rate-limits.md](docs/architecture/rate-limits.md) | `src/services/rate_limits.py`, `src/services/concurrency.py`, `src/services/pool_budget.py`, `src/services/concurrency_counters.py`, `src/services/concurrency_readiness.py`, `src/services/refusals.py`, the pool subclass in `src/database.py`, the failed-auth budget in `APIKeyMiddleware`, the gate order in `_tracked`, the worker count, the key-creation budget and active-key cap (`src/services/api_keys.py`), and every `MCP_RATE_LIMIT_*` / `MCP_AUTH_FAILURE_*` / `MCP_CONCURRENCY_*` / `KEY_CREATION_*` / `KEY_MAX_ACTIVE_PER_ACCOUNT` / `DEFAULT_DAILY_REQUEST_LIMIT` setting |
| [usage-attribution.md](docs/architecture/usage-attribution.md) | `usage_logs`, `_log_usage`, actor columns |
| [control-panel.md](docs/architecture/control-panel.md) | panel templates, flash messages, admin guards, the Danger zone |

Formal requirements live in `openspec/specs/`; the change history and its
reasoning in `openspec/changes/archive/`.
