# Upstream synchronization, 5 November 2026

This integration merges upstream `7e8c992` into fork `d04ebc2`. It preserves
merge ancestry and all previously deployed fork migration files. It does not
deploy production or mutate any live vault.

## Migration identities

Upstream independently reused the fork's already-applied 031–036 revision IDs.
Incoming migrations are appended, preserving their ownership marker strings
and upstream diagnostic references:

| Upstream | Fork | Feature |
| --- | --- | --- |
| 031 | 037 | Keyword title/alias weighting and excerpts |
| 032 | 038 | Indexed Markdown tasks |
| 033 | 039 | Content-change timestamps and event feed |
| 034 | 040 | Heading-aware chunk metadata |
| 035 | 041 | Keyword tags |
| 036 | 042 | OAuth client metadata documents |
| 037 | 043 | Native OIDC account links |

The preceding map (upstream 025–030 → fork 031–036) remains in
[the October note](upstream-sync-20261015.md). Migration diagnostics and object
comments deliberately retain their upstream numbers; resolve them through
these maps. The schema suite exercises fresh databases and upgrades from fork
025/026, preserving identities, vector contents and temporal validity intervals.
A database migrated along upstream's independent chain is not a supported
starting point for this fork.

## Authentication compatibility

The existing `AUTH_MODE=pocketid` flow moves to `src/auth/pocketid.py`, preserving
the callback URL and `users.oidc_subject` column/index. Upstream native OIDC lives
in `src/auth/oidc.py` and uses `users.native_oidc_subject`, with its issuer-bound
constraints and administrator-managed links. Native OIDC is opt-in and cannot
be enabled simultaneously with PocketID. No existing identity is linked by
email, copied to another provider or promoted by this integration.

## Search and vault compatibility

The extraction marker advances to 5: the fork had already used upstream's new
marker 4, so retaining it would skip the task backfill on existing notes.
Historical vectors remain separate from current search, replacement, reuse,
rechunk health and search-evaluation corpus statistics. The temporal `as_of`
semantic query keeps its interval filtering through the new shared search arm.
Git modification timestamps remain distinct from the new content-change time.

The initial heading-chunk upgrade makes legacy chunks pending for new embedding
inputs, even when note bytes have not changed. Budget provider capacity for
that work; do not describe it as a metadata-only upgrade. Task-local tag parsing
retains upstream's ASCII grammar; note tags keep the fork's Unicode and
comment-aware grammar. This inconsistency is a documented follow-up, not a
reason to silently change the new tool's semantics during synchronization.

The combined MCP contract has 31 tools, 22 visible to read-only OAuth grants.
The three Git tools publish explicit metadata; `read_note` keeps bounded
relationships and the fork's primer controls. Classification epoch is 6.
Permissions are checked before acquiring the Git publication lock. Successful
writes retain both upstream change-feed emission and the fork's Git records.
Upstream tool fixtures remain available alongside the combined fork fixtures.

## Release constraints

- A merge publishes an image; production requires a separate digest pin and
  Komodo deployment. The upstream MCP Registry workflow is restricted to its
  owning repository; this fork does not claim the upstream registry identity.
- Stop the old writer before migrations that rewrite keyword vectors (037/041).
  The Makefile now carries upstream's stop/migrate/start sequence. Do not use
  the legacy SSH deployment path without adapting its old-writer sequencing.
- Verify the application uses a non-superuser PostgreSQL role. The new startup
  guard rejects historical compose installations using a superuser unless an
  explicit compatibility override is set. Prefer the documented role split.
- Take and validate a full backup before migration. Preserve temporal vectors
  and note identity rows; ordinary reindexing cannot reproduce historical
  vectors. A rollback of the image does not roll back schema or indexed data.
- Verify `alembic check`, authentication, health payload (not just HTTP 200),
  synchronization and indexing after rollout. No rollout was performed as part
  of this integration.
