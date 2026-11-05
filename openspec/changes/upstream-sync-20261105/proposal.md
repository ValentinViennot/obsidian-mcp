# Synchronize upstream through 7e8c992

Integrate the 39 upstream commits since the October synchronization while
preserving the fork's deployed migration identities, PocketID login, Git vault
publication/recovery, temporal embeddings and bounded relationship context.
The merge keeps upstream ancestry; it does not rebase published fork history.

The result adds hybrid search, indexed tasks, a change feed, heading-aware
embedding inputs, optional reranking, MCP metadata, CIMD and optional native
OIDC. Production rollout is separate: a full backup, non-superuser application
role, old-writer shutdown and capacity for re-embedding must be verified first.

No production data or credentials belong in this change. The live audit remains
private; version-independent deployment constraints belong in the architecture
note. See `docs/architecture/upstream-sync-20261105.md` for compatibility decisions.
