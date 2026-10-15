# Obsidian MCP upstream review, 15 October 2026

Captured from the native panel at this integration's final UI source hashes
(`ui-source-hashes.json`). Disposable PostgreSQL at Alembic 036, two invented
notes, legitimate synthetic administrator, deterministic 64-dimension
FakeProvider; no external provider or private vault. Settings' direct Ollama
reachability check remains unavailable because this fixture has no Ollama daemon.
No persistent API key was created during the visual walkthrough.

The lead independently exercised reindex completion, desktop quota controls,
mobile note reading, drawer navigation and the quota modal at 390 × 844.
A separate headless Chromium run authenticated, checked visible 2/2 totals,
opened both quota dialogs and the mobile drawer, and reported no JavaScript
errors. These still images document appearance; they do not prove behavior.

Both implementer and lead personally inspected the exact published pixels and
PNG metadata. The images contain only synthetic content and no credentials,
private contacts, financial details, local paths or browser address bars. PNG
chunks are IHDR, IDAT and IEND only. Desktop viewport 1440 × 1000; mobile
viewport 390 × 844. Full-page captures can extend below the viewport.

- `desktop-dashboard.png`: two indexed and embedded notes, 100% coverage.
- `desktop-key-quota.png`: readable quota explanation and all dialog actions.
- `mobile-navigation.png`: narrow-screen navigation drawer.
- `mobile-key-quota.png`: quota dialog with all actions visible.


## Qualification

All runs used synthetic files and task-owned PostgreSQL databases. No production
vault, provider, schema, credentials or deployment was changed.

| Check | Result |
| --- | --- |
| Complete offline suite | 7,168 passed, 14 environment/opt-in skips |
| Native PostgreSQL/Docker integration, excluding separately run schema module | 682 passed; 7 opt-in cases exercised separately below |
| Schema/catalog/ownership gate | 275 passed |
| Existing fork 025 and 026 upgrade preservation | 2 passed, including repeated upgrade and clean Alembic comparison |
| Native maximum-body RSS and six concurrent MCP writes | 2 passed; all six writes returned 200 within the stated memory bound |
| Synthetic Git-vault acceptance | 5 passed |
| Actual Linux mount-namespace gate | 41 passed |
| Final rendered dashboard, CSP templates and exceptional cleanup | 282 passed |
| Final provider batching, deadlines, input limits and query cardinality | 76 passed |
| Strict OpenSpec | 36 passed |
| Compose parsing | Base, Caddy and Traefik combinations pass with the exact synthetic CI inputs |
| Runtime dependency audit | No known vulnerabilities found |
| Runtime image CVE scan | No fixable HIGH/CRITICAL findings |

The complete suites preceded the final query-cardinality guard. Its 76-case
focused run covers valid one-vector queries, malformed zero/two-vector replies,
request bounds, multi-batch behavior and fallback/refusal behavior. The tiny
truthful-counter correction is covered by the 282-case rendered-template run.
The native temporal regressions also demonstrate causality: removing only the
current-row reuse/interlock predicates fails both cases; restored source passes
all 13 temporal PostgreSQL tests.

The fork's OIDC 025 and temporal 026 migration files are byte-identical to the
base release. There are 32 unique migration identities and one head, 036. This
upgrade qualifies fresh fork installations and existing fork 025/026 databases;
it does not claim an upgrade from a separately migrated upstream database.
