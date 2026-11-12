# Upstream synchronization, 12 November 2026

This integration merges upstream `aafb9f024431432c767a467157646e019e406c2f`
into fork `ba324fa4993e87ab2f6f230cc9baae1b3ffd4b89`, preserving merge
ancestry. It adds CIMD `private_key_jwt`, an optional TEI provider, richer task
queries, an agenda, safe one-line task writes and per-account task rules.

## Existing database history

The fork already deployed revisions 038–044. New upstream steps are appended:

| Upstream | Fork | Feature |
| --- | --- | --- |
| 038 | 045 | CIMD client assertions and pinned JWKS |
| 039 | 046 | Task ids, write-time refresh and durable task settings |
| 040 | 047 | Per-account checkbox task rules |

The new chain starts at existing fork 044. Ownership comments retain the
upstream marker identities (`038_oauth_cimd_private_key_jwt`, `039_task_tools`,
`040_task_rule`), as in the preceding integrations. These identify each named
object, not the global Alembic revision, and do not collide with older fork
objects. Operator-facing revision headers and diagnostics name actual fork
045–047. An upstream database is not a supported fork starting point. Older reconciliation steps gain upstream successor awareness
without replacing the fork's deployed revision identities.

Named connection aliases, OAuth cutover semantics, Pocket ID and separate
native OIDC identities, temporal vectors and full backups remain in force.
`task_settings` is durable and always backed up. A schema downgrade can delete
saved task preferences; an image rollback does not restore them. Production
uses the existing verified-backup and forward-migration GitOps procedure.

## Fork behavior

Extraction version advances from fork 5 to 6. Existing notes are re-derived
once to populate task ids and task-tool columns. Versions 5 and 6 use the same
embedding cleaner, so this step does not itself invalidate embeddings or call
the provider. Task rows pending that pass are reported explicitly.

TEI remains optional. The Ollama provider, active model and stored embedding
fingerprint are not changed by this integration. No model migration, index
reset or corpus rebuild is part of this work.

The combined MCP contract now has 35 tools, including the three fork Git
history tools. Read-only OAuth grants see 23; task mutations share the existing
write permission and connection-name checks. Classification epoch advances
from fork 6 to 7. Task writes record their confirmed path immediately after
publication, before index refresh and event recording, and the existing
per-call wrapper commits under the named connection author. Dry runs and
refusals create no git commit.

`read_note` relationship context, the optional guide primer, current/historical
search boundaries and git modification timestamps remain available. Generic
note writes retain the existing synchronization lock. Task writes join the same
lock and recovery behavior; automatic synchronization still retains displaced
versions and pauses automatic commits/pushes when recovery review is required.
No recovery data is pruned.

The wire fixtures record the deliberate combined fork surface. Upstream v1/v2
fixtures remain unmodified for their historical contract proof.

## Qualification boundaries

The 450-case schema suite includes stamp-back reconciliation of the existing
fork migration history, including fork OIDC and temporal-vector preservation.
It exercises both predecessor and successor object shapes and refuses foreign
markers rather than adopting them.

The integration is qualified against disposable synthetic PostgreSQL and vault
fixtures, including the schema/model suite, task and note search, mutations,
read-only and tenant boundaries, named attribution and concurrent edit recovery.
The Tasks card is exercised as a non-admin in a synthetic panel, on desktop
and mobile. Its saved values survive reload; invalid timezone input leaves
prior settings intact. The captures illustrate layout, not functional proof.

Production data and backup artifacts are private and never enter this tree.
Any restored-copy qualification and remaining CI requirements are recorded in
the pull request. No deployment or live vault mutation is performed here.


## Observed qualification

- Linux offline repeat: 10,994 passed, 13 explicit prerequisite or documented skips.
- Synthetic PostgreSQL schema suite: 450 passed, including existing-fork stamp-back reconciliation.
- Authentication/task/fork integration: 260 passed initially; the three fork-fixture compatibility failures were corrected, then the affected task/search/readonly modules passed 130 tests.
- Operator-diagnostic and fork-reconciliation follow-up: 80 passed.
- Runtime requirements audit and scoped history secret scan passed. Eleven exact upstream synthetic task-key fingerprints were reviewed; no rule or path exemption was added.
- Daily database snapshot from 2026-11-11 05:00:21 UTC: privately restored on its host, upgraded 044 → 047 twice, clean `alembic check`, all original-column row digests unchanged. Dump SHA-256: `cbb313dbc886ae041ddf62746e7c4d4445ed1a4d66d33c3c4085db9a50aaccb4`.

The restored proof ran candidate source with the incumbent runtime dependency
set plus the newly pinned timezone-data package. It did not run an application
lifespan, indexer, provider, or live vault. Separate vault saved-edit/recovery
behavior was exercised with synthetic native-filesystem tests. The local Linux
test image lacks Make, Node, Docker Compose and unprivileged mount-namespace
prerequisites for some checks; protected CI remains the complete partition gate.
The shared Claude engineering-workflow file and local OpenSpec CLI were absent;
no unavailable review or CLI gate is claimed. Task-only QA containers and
networks were stopped; only private non-secret restore metadata was retained.
