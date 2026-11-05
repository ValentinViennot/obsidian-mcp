# Decisions and failure boundaries

- An alias is declarative attribution, not an authenticated person's identity.
  NFC, strip, 1–128 characters, no controls/format characters or Git delimiters.
  Consent is the only OAuth alias editing boundary; API keys use their name.
- Opaque grant/key IDs derive stable technical emails. Request ContextVars are
  reset after every middleware scope. MCP schemas accept no author argument.
- Transfer capabilities snapshot author identity at mint. Old unnamed upload
  capabilities refuse before reading payload bytes. Git failure does not undo
  published bytes; later sweeps identify themselves as the service.
- Old OAuth families and exchangeable codes are revoked at044, and runtime
  predicates also reject unnamed credentials issued by an old process. Stop
  old writers before migration; restarting old code is not a data downgrade.
- GitHub is the shared main branch. No force push, rebase, checkout or reset
  operates on live notes. Desktop 300s and server120s timers remain. Bare relay
  stays offline as a transition archive; recovery data never becomes a public
  PR artifact. Disable the independent Obsidian Git auto timers.
- CI partitions are disjoint. Aggregate `pr-gate` includes every mandatory
  test/security/Compose/contract gate. Candidate image is scanned and booted
  using synthetic fixtures, exported once, then loaded for immutable publish.
  Documentation-only changes do not build or publish an image.
- GitOps validates repository, main ref, commit ancestry, workflow signer,
  run identity and image digest. A protected fixed Action receives only typed
  hashes; no arbitrary path, shell command, environment or credential input.
- Deployment requires a full private database dump restored into isolated
  PostgreSQL with every table count compared, plus complete vault filesystem
  archives. Shared sync locks and stopped application prevent supported writes.
  Failed migration leaves application stopped and requires forward recovery.
  Healthy Docker status alone is insufficient: indexer readiness is required.

Independent editors do not participate in the server lock; operators must close
such editors during whole-vault snapshots/cutover. Normal server operation has
no independent editor process. Retained recovery versions are never pruned by
release automation. Public QA uses only synthetic accounts, clients and notes.
