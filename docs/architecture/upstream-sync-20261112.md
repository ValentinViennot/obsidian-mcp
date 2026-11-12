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

The new chain starts at existing fork 044. Ownership comments and diagnostics
retain upstream numbers, as in the preceding integrations. Resolve those
numbers through this table; an upstream database is not a supported fork
starting point. Older reconciliation steps gain upstream successor awareness
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

The integration is qualified against disposable synthetic PostgreSQL and vault
fixtures, including the schema/model suite, task and note search, mutations,
read-only and tenant boundaries, named attribution and concurrent edit recovery.
The Tasks card is exercised as a non-admin in a synthetic panel, on desktop
and mobile. Its saved values survive reload; invalid timezone input leaves
prior settings intact. The captures illustrate layout, not functional proof.

Production data and backup artifacts are private and never enter this tree.
Any restored-copy qualification and remaining CI requirements are recorded in
the pull request. No deployment or live vault mutation is performed here.
