# Continuous deployment

One workflow, [CI](../../.github/workflows/ci.yml), owns the required `pr-gate`
and the release of the exact image it tested. Production deployment belongs to
the protected GitOps pipeline. GitHub holds no production SSH credential.

## Validation and partitioning

Every PR and main push gets the same aggregate check. `scripts/ci_tests.py`
assigns every `test_*.py` file to exactly one partition:

- **offline**: files outside `tests/integration/`, excluding
  `tests/test_fts_integration.py`;
- **schema**: `tests/integration/test_schema_check.py`;
- **postgres**: the remaining integration files and `test_fts_integration.py`.

All three run on Linux with the pinned Python dependencies. Database jobs use
independent disposable pgvector services; integration fixtures create their own
databases. The PostgreSQL job has Docker and psql for tests requiring them.
JUnit evidence must contain every selected module. Required database tests may
not skip; only the explicitly optional real-vault history and RSS modules may
skip. The offline nested-mount namespace test must actually execute. The runtime
candidate is built once; CI does not also build a second test image.

The aggregate requires the three partitions, secret-history scan, dependency
audit, vault-leak checker, OpenSpec, Compose validation, and candidate image
scan/smoke. Dependency audit is blocking. The vault-leak checker still needs the
owner's private denylist for content-specific coverage; without it the job
checks only its available public configuration, not absence of private content.

Docs-only changes (`docs/`, `openspec/`, or root Markdown files) skip runtime
partitions and the candidate image. Security, specs, Compose and `pr-gate` still
resolve. Unknown or empty diffs run every gate; manual runs run every gate.
There is no event-level path filter that can strand the required check.

## One tested candidate, no publication rebuild

The image job builds Linux amd64 with source-repository and exact-commit OCI
labels. Trivy rejects fixable HIGH/CRITICAL vulnerabilities. Then
`scripts/ci_image_smoke.sh` starts the real image entrypoint against a disposable
Postgres database, a synthetic note, and a local fake embedding provider on an
isolated Docker network. It requires successful migrations, a non-root app,
healthy indexing with a current vector, unauthenticated MCP refusal, and clean
`alembic check`. It never enables sandbox mode or reads a real vault. Its trap
removes only its own containers, network and temporary fixture directory.

The tested Docker image is saved with its config image ID and archive checksum
in an immutable Actions artifact. Only a successful main run in
`ValentinViennot/obsidian-mcp` may download and publish it. The publish job loads
those bytes, checks the source/revision labels and image ID, pushes a
commit/run/attempt tag, resolves its registry digest, pulls by digest and checks
the image ID again. There is no second build and no deployment from a mutable
tag. PR jobs have no package-write or deployment-trigger credentials.

## Attested release handoff

`scripts/ci_release.py` writes `obsidian-mcp-release.json`, with:

- `schema_version: 1`;
- source repository, `refs/heads/main`, exact commit;
- image repository and immutable `sha256` digest;
- Actions run ID, attempt and evidence URL;
- successful `offline`, `schema`, `postgres`, `image`, `security`, `openspec`
  and `compose` evidence;
- expected schema head, read from the actual Alembic graph (never hard-coded).

The manifest is published as an OCI artifact in
`ghcr.io/valentinviennot/obsidian-mcp`, with type
`application/vnd.obsidian-mcp.release.v1+json`. Both image and manifest artifact
receive GitHub OIDC build-provenance attestations, stored in the registry. The
consumer verifies the immutable manifest's attestation, repository, source
commit/ref, signer workflow `ValentinViennot/obsidian-mcp/.github/workflows/ci.yml`
and hosted-runner identity before trusting its contents, then independently
checks image source/revision labels. A tag or unsigned JSON is not release
approval.

When repository variable `OBSIDIAN_RELEASE_AUTOMATION_ENABLED=true`, GitHub
triggers the protected GitOps pipeline with:

- `OBSIDIAN_RELEASE_MANIFEST`: immutable OCI artifact reference;
- `USL_SOURCE_REPOSITORY`, `USL_SOURCE_REF`, `USL_SOURCE_COMMIT`;
- `USL_GITHUB_RUN_ID`.

The only trigger secrets are `GITOPS_PIPELINE_TRIGGER_URL` and
`GITOPS_PIPELINE_TRIGGER_TOKEN`. The GitOps side owns backup, schema preflight,
ordered migration/recreation, runtime verification and deployment evidence.
Publishing an image or accepting a trigger does not prove production deployed.
A failed trigger leaves the attested artifact available for controlled retry;
rerunning CI never authorizes deploying an unrelated digest.

For the November 2026 upgrade, stop the old application before the
keyword-data migrations and keep a **full** backup. A current image rollback
cannot undo schema/data changes or bring back overwritten vault bytes. See
[the integration constraints](../architecture/upstream-sync-20261105.md) and
[runbooks](runbooks.md). `scripts/vps-deploy.sh` remains a manual legacy helper,
not a GitHub deployment path. The previous duplicate `pr`, `image`,
`publish-image`, registry publisher and SSH deploy workflows are removed.
