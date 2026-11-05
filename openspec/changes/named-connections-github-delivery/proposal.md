# Named connections and verified continuous delivery

Require a human-chosen connection alias at every OAuth authorization, preserve
it through refresh, and use it as Git author for synchronous and deferred
writes. Existing API keys keep their names and access; invalid aliases refuse
writes until corrected. Migration044 revokes unnamed OAuth credentials and
codes, requiring consent again without invalidating API keys.

GitHub becomes the common remote for supported desktop and server sync engines.
Both use inode-preserving reconciliation and keep conflicts in private recovery
storage. A single desktop timer replaces plugin automatic Git operations.
Working-clone backups include uncommitted data and private recovery storage.

One CI workflow partitions tests, builds and qualifies one candidate, publishes
that exact image after all main-commit gates pass, and emits an attested release
manifest. A protected GitOps consumer validates the source, provenance and
immutable digest before selecting a production release. A fixed Komodo Action
serializes deployment, restores a full backup as proof, stops old writers,
migrates forward, checks schema drift and verifies application readiness.
Production SSH credentials are never provided to GitHub Actions.
