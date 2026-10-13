# Operational runbooks

Deploy, upgrade, reindex, rotate keys, back up, restore, roll back. Written for
the person who has to do this at an awkward hour, so every step is a command
that can be pasted, and every step that can destroy something says so first.

Related: [`continuous-deployment.md`](continuous-deployment.md) covers the
pipeline that does most of this automatically on a merge to `main`, and the
secrets it needs. [`rebase-on-upstream.md`](rebase-on-upstream.md) covers taking
changes from the upstream project, which is a different problem and the harder
one.

Throughout: `odoo` is the SSH alias for the production host, the stack is
`obsidian-mcp`, and the vault lives at `/srv/obsidian`.

---

## Layout

```
/srv/obsidian/repos/<user>.git    bare repo, one per user — the push target
/srv/obsidian/vaults/<user>/      working clone; the PARENT is mounted as /vaults
```

Per-user vaults must be **siblings** under `/srv/obsidian/vaults`, never nested.
Upstream's tenancy guard cannot see a vault grafted inside another one, and one
user would then read and overwrite another's notes.

The container mounts **the working clone, never the bare repo**. The server's
tree carries its own uncommitted agent writes; a `post-receive` checkout into a
dirty tree would corrupt them. Desktop pushes land in the bare repo and are
pulled into the working tree by the reconcile timer instead.

---

## Deploy

First deploy only. Subsequent releases are [Upgrade](#upgrade).

1. **Secrets must exist in Infisical first** under
   `usl-infra` / `prod` / `/stacks/prod-odoo-nbg1-2/obsidian-mcp`. See the
   gitops runbook for the exact key list. A missing secret fails the container
   at boot by design (`${VAR:?}`), which is preferable to booting insecurely.
2. Merge the gitops MR. That syncs the stack *definition* only — it deploys
   nothing.
3. In the Komodo UI, run **DeployStack** on `obsidian-mcp`. This is the step
   that actually starts containers, and it is manual for every new stack in this
   fleet.
4. Create the Cloudflare Zero Trust ingress rule:
   `obsidian-mcp.unstaticlabs.com` → `http://obsidian-mcp:8000`. The tunnel is
   token-managed, so this is done in the dashboard and cannot be scripted from
   the repo.
5. Apply migrations and confirm health:

   ```bash
   ssh odoo 'docker exec obsidian-mcp alembic upgrade head'
   curl -sf https://obsidian-mcp.unstaticlabs.com/health && echo OK
   ```

6. **Bootstrap the first admin — before the ingress rule exists.**

   This step has an ordering trap. Under `AUTH_MODE=pocketid` the registration
   route returns 404, deliberately, so there is no way to create the first
   account through the UI. And federated login **never auto-grants admin** — the
   provider says who you are, not what you may do — so a first login with no
   pre-existing row produces a non-admin account with no vault.

   The tempting fix is to boot once under `AUTH_MODE=local`, register, then
   switch. Do not do that with the ingress rule already live: it puts an open
   registration page on the public internet for as long as it takes you to
   notice.

   Instead pre-seed the row and let the first federated login adopt it.
   Adoption matches on `username` and only proceeds when the row carries no
   `oidc_subject`, so this is exactly the supported path:

   ```bash
   ssh odoo 'docker exec obsidian-mcp-db psql -U obsidian -d obsidian_mcp -c "
     INSERT INTO users (username, password_hash, is_admin, is_active, vault_path)
     VALUES ('"'"'<email-local-part>'"'"', '"'"'!'"'"', true, true, '"'"'/obsidian'"'"')
     ON CONFLICT (username) DO NOTHING;"'
   ```

   `username` must equal the **local part of the email** PocketID asserts
   (`someone@example.com` → `someone`); that is what the adoption logic derives.
   The `'!'` password hash is deliberately unusable — there is no local password
   for this account and none should be invented.

   Verify after your first login that the account came out as admin:

   ```bash
   ssh odoo 'docker exec obsidian-mcp-db psql -U obsidian -d obsidian_mcp -c \
     "select username, is_admin, oidc_subject is not null as linked from users;"'
   ```

7. Confirm the auth boundary actually holds — this is the check people skip:

   ```bash
   curl -so /dev/null -w '%{http_code}\n' https://obsidian-mcp.unstaticlabs.com/mcp     # expect 401
   curl -so /dev/null -w '%{http_code}\n' https://obsidian-mcp.unstaticlabs.com/admin   # expect 302 → PocketID
   ```

---

## Upgrade

**A merge to `main` does all of this by itself** — see
[`continuous-deployment.md`](continuous-deployment.md). `deploy.yml` takes the
same backup, runs the same migration, waits for the same health check, and
rolls the image back if it does not come up. What follows is the manual
equivalent, for when the pipeline is what is broken, or when you are deploying
something that never went through `main`.

```bash
# 1. Know what you are shipping.
git log --oneline <deployed-sha>..main

# 2. Back up the index BEFORE migrating. A failed migration with no backup is
#    the one genuinely bad outcome in this system.
ssh odoo 'cd /srv/obsidian && docker compose --profile backup run --rm backup-prepare && \
                              docker compose --profile backup run --rm backup'

# 3. Deploy the new image (Komodo UI: DeployStack), then migrate.
ssh odoo 'docker exec obsidian-mcp alembic upgrade head'

# 4. Verify.
curl -sf https://obsidian-mcp.unstaticlabs.com/health
```

The app runs with **`--workers 1`** and must continue to. Its rate limiter and
auth-failure budget are in-process; a second worker silently multiplies every
limit. This is an upstream contract, not a tuning default.

---

## Rollback

Two independent things can need rolling back, and confusing them makes an
incident worse.

### Rolling back the code

The stack pins an image digest. Redeploy the previous one:

```bash
git -C ~/Code/gitops log --oneline -- komodo/stacks/prod-odoo-nbg1-2/obsidian-mcp/compose.yaml
git -C ~/Code/gitops revert <commit>     # restore the previous pinned digest
# push, merge, then DeployStack in Komodo
```

**If the release included a migration, the code rollback is not enough on its
own** — old code against a new schema may fail in ways worse than the bug you
are rolling back from. Check whether the migration was additive:

```bash
ssh odoo 'docker exec obsidian-mcp alembic history -r-3:current'
```

Additive changes (new nullable columns, new indexes — for example the
`valid_from`/`valid_to` columns, deliberately nullable for exactly this reason)
are safe to leave in place while running older code. A destructive change is
not; downgrade it explicitly:

```bash
ssh odoo 'docker exec obsidian-mcp alembic downgrade -1'
```

### Rolling back the index

The index is derived data — it can always be rebuilt from the vault, which is
the real source of truth. Prefer [Reindex](#reindex) over restoring a backup
unless embeddings would take unacceptably long to regenerate. See
[Restore](#restore-from-backup) for restoring a specific point in time.

### Rolling back the vault

The vault is git. Nothing is lost, and a bad agent write is reverted like any
other commit:

```bash
ssh odoo 'cd /srv/obsidian/vaults/<user> && git log --oneline -20'
ssh odoo 'cd /srv/obsidian/vaults/<user> && git revert --no-edit <sha> && git push origin main'
```

Use `git revert`, not `reset --hard`: the desktop clone has the old history and
a force-push would leave the two permanently diverged.

---

## Reindex

Cheap operations first; only the last one is expensive.

```bash
# Keyword search only — rebuilds tsvectors. Seconds. Safe any time.
ssh odoo 'cd /srv/obsidian && make rebuild-tsvectors'

# Re-scan the vault and re-index changed notes. Minutes.
ssh odoo 'cd /srv/obsidian && make reindex'

# Discard every embedding and recompute. EXPENSIVE — see below.
ssh odoo 'cd /srv/obsidian && make reset-embeddings && make reindex'
```

**When a full re-embed is unavoidable**: changing `EMBEDDING_MODEL` or
`EMBEDDING_DIMENSIONS`. The app fingerprints its embedding config at startup and
refuses to run against vectors from a different configuration, because mixing
vector spaces produces silently wrong search results rather than an error.

That guard tracks *configuration, not weights*. Re-pulling `bge-m3` under the
same tag, or repointing `OLLAMA_URL` at a host serving different weights under
that name, changes the vector space with nothing to catch it. If the Ollama
model is ever re-pulled, reset embeddings even though no config changed.

Cost, measured against the production Ollama gateway: ~14,000 chunks for this
vault, ~0.017 s/chunk batched — roughly **four minutes** of load on an endpoint
shared with other services. Prefer off-hours; it is not long enough to need a
maintenance window.

---

## Rotate keys

### PocketID client secret

```bash
# Mint a new secret (v2.14+ path is plural /secrets)
ssh odoo 'docker exec pocket-id /app/pocket-id one-time-access-token <admin-email>'
# redeem the printed token, then:
#   POST /api/oidc/clients/<client-id>/secrets
```

Put the new value in Infisical, then **redeploy** — Komodo interpolates secrets
into the container environment at deploy time only, so a rotated secret changes
nothing until the stack is redeployed. Old secrets remain valid until deleted,
so rotate, deploy, verify a login, and only then delete the old one via
`DELETE /api/oidc/clients/<id>/secrets/<secretId>`.

### `SECRET_KEY`

Rotating it invalidates every session and every issued OAuth token. Users
re-authenticate through PocketID; MCP clients (the Claude app) must reconnect
and re-consent. Do it deliberately, not casually.

### Database password / restic password

Change in Infisical, redeploy. For restic, **the old password is required to
read old snapshots** — never rotate it without either retaining the old value
somewhere recoverable or accepting that existing backups become unreadable.
Cloudflare never shows a token value twice; the same discipline applies here.

---

## Backup

Backups are profile-gated compose services, not a cron inside the container.
They run nightly at 06:00 via a Komodo procedure.

```bash
ssh odoo 'cd /srv/obsidian && docker compose --profile backup run --rm backup-prepare'
ssh odoo 'cd /srv/obsidian && docker compose --profile backup run --rm backup'
ssh odoo 'cd /srv/obsidian && docker compose --profile backup run --rm backup -- snapshots'
```

`backup-prepare` refuses to proceed if the database looks implausibly empty.
That gate exists because a backup job elsewhere in this fleet once "succeeded"
against an empty database and quietly replaced good snapshots with useless ones.

The vault itself is **not** in these backups and does not need to be: it is a
git repository with three copies (bare, server clone, desktop clone). Postgres
holds only derived data.

---

## Restore from backup

**Test this before you need it.** A backup that has never been restored is a
hypothesis.

```bash
# 1. See what exists.
ssh odoo 'cd /srv/obsidian && docker compose --profile backup run --rm backup -- snapshots'

# 2. Restore into a scratch volume and validate — does NOT touch production.
ssh odoo 'cd /srv/obsidian && docker compose --profile backup run --rm backup-restore'
ssh odoo 'cd /srv/obsidian && docker compose --profile backup run --rm backup-verify'
```

`backup-verify` checks row counts and runs a real query against the restored
copy. Only if it passes should you promote the restore over production:

```bash
ssh odoo 'cd /srv/obsidian && docker compose stop obsidian-mcp'   # stop writes first
ssh odoo 'cd /srv/obsidian && docker compose --profile backup run --rm backup-restore --promote'
ssh odoo 'cd /srv/obsidian && docker compose start obsidian-mcp'
curl -sf https://obsidian-mcp.unstaticlabs.com/health
```

If a restore is not available or is stale, **rebuild instead**: the vault is the
source of truth and a full reindex costs about four minutes. Restoring is an
optimisation, not a dependency.

---

## The vault sync loop

The server timer and desktop script automatically merge local and incoming
history without checking out files in the live vault. Ordinary Obsidian saves
may continue: displaced file inodes are retained so saves through already-open
descriptors can be recovered. See [the preservation contract](../architecture/vault-sync.md).

Use the branch's configured upstream, including a private hosted SSH/HTTPS
repository and any branch. Neither reconciler assumes `origin/main`.
Set `VAULT_DIR` on the server and `VAULT_PATH` on the desktop. Use Git credential
helpers/SSH agents; never put tokens in remote URLs or tracked environment files.

For a clean environment, clone into a **new** directory as the service owner:

```bash
VAULT_PATH=/path/to/new-vault VAULT_GIT_REMOTE=git@example.invalid:owner/vault.git \
  VAULT_GIT_BRANCH=main python3 deploy/clone-vault.py
```

The helper refuses an existing destination, even an empty directory. It never
replaces a vault, creates a hosted repository or migrates a live one. Those are
separate authorized operator actions. The sync engine requires a regular clone
with a `.git` directory; submodules and linked working clones are unsupported.

For desktop installation, create a **new** relocatable bundle containing the
entry point and its standard-library modules:

```bash
bash deploy/desktop/install.sh /path/to/new-sync-bundle
VAULT_PATH=/path/to/vault LOG_PATH=/path/to/sync.log \
  bash /path/to/new-sync-bundle/deploy/desktop/obsidian-vault-reconcile.sh
```

Python 3.12+ and Git 2.36+ are required. Timer activation is separate. Disable
Obsidian Git plugin automatic pull/push for this clone; independent Git writers
must use `.git/obsidian-sync.lock` or run while the timer and MCP writers are
paused. Kernel locks release on process exit: never unlink one to unlock it.
A busy sweep exits75, leaves the success marker unchanged, and retries on the
next tick. MCP publication briefly waits for an active sweep before asking for
retry; recovered versions pause commits rather than preventing note edits.

Inspect the intended vault's status and saved versions:

```bash
python3 deploy/vault-reconcile.py --vault /path/to/vault status
python3 deploy/vault-reconcile.py --vault /path/to/vault recovery-list
```

Status includes transaction/date, incoming commit, candidate worktree, original
note path, retained location and latest SHA-256. `local_edit_recovered` means a
saved editor version differs from the prior committed content. An incomplete
transaction requires recovery even when the retained bytes are unchanged.
Export the latest version outside the vault before choosing what to keep:

```bash
python3 deploy/vault-reconcile.py --vault /path/to/vault \
  recovery-export TRANSACTION 'daily/Today.md' /path/to/new-recovered-note.md
```

Export never overwrites an existing destination. Retained versions can change
when an editor's already-open descriptor saves later. Reinspect the latest hash
before resolution. Close editors and pause independent Git writers first:

```bash
python3 deploy/vault-reconcile.py --vault /path/to/vault \
  recovery-resolve TRANSACTION 'daily/Today.md' --keep recovered \
  --sha256 LATEST_HASH --editors-quiesced
# Or choose --keep live after comparing/exporting the retained version.
```

Both choices retain saved inodes. Choosing `recovered` keeps the displaced live
version too and leaves the chosen local edit ready for the next normal commit.
A subsequent late save changing an acknowledged hash reopens review. These
commands never delete recovery storage or force-push history.

After an interrupted sync, keep editors closed and continue its transaction:

```bash
python3 deploy/vault-reconcile.py --vault /path/to/vault \
  recovery-continue TRANSACTION --editors-quiesced
```

Recovery classifies completed/native exchanges by inode identity. It does not
replay an ambiguous exchange. If names changed unexpectedly, export retained
versions and compare them with the pinned incoming/local commits; leave the
transaction pending for operator review rather than using `reset --hard`.
Git `index.lock` needs a separate check that no Git process remains before an
operator removes it; automation never removes it based only on age here.

For a same-note merge conflict, status shows a **private candidate worktree**.
Resolve notes there, not in the live vault. All non-conflicting incoming notes
are already present in that candidate. The usual Git conflict workflow applies:

```bash
git -C /path/from/status/merge status
# Edit the candidate note to combine both versions, then:
git -C /path/from/status/merge add -- daily/Today.md
git -C /path/from/status/merge commit -m 'vault: resolve reviewed note conflict'
python3 deploy/vault-reconcile.py --vault /path/to/vault \
  recovery-continue TRANSACTION --editors-quiesced
```

The engine verifies that the candidate retains both original histories before
publishing it. Live local bytes are displaced into recovery, not discarded.
Review any recovered edit and then let the normal timer push. Detached or
unmerged live Git states pause commits/sync; supported note writes still stand.

Run sweeps as the mounted vault's service UID/GID. Replacement preserves POSIX
owner/group and permissions; shared additions inherit parent write access.
The systemd template retains its set-ID restriction: the kernel inherits
shared directory groups during creation, without explicit set-ID chmod.
Configure ownership before enabling the timer. Extended ACL metadata is outside this
contract. Private `.git/obsidian-recovery` storage and recovery refs are never
indexed or sent in ordinary branch pushes. Include them in filesystem backups;
there is intentionally no automatic cleanup while editor descriptors may live.

To inspect retained pre-sync staging:

```bash
git -C /path/to/vault for-each-ref refs/obsidian-recovery/ --format='%(refname)'
git -C /path/to/vault show 'ACTUAL_STAGED_REF:daily/Today.md'
# Or use the staged_snapshot commit SHA shown by status when present.
```

Staged snapshot refs use their own UUID, which can differ from the sync
transaction. Copy the actual ref from the listing; do not construct one from
the sync transaction ID.

This preserves disk-published edits, not unsaved buffers or disk failure. Keep
editor/device backups too. No production timer, hosted repository, permissions
or vault migration is changed by installing this source update.

---

## Health checks worth running after any change

```bash
curl -sf https://obsidian-mcp.unstaticlabs.com/health
curl -so /dev/null -w 'mcp=%{http_code}\n'   https://obsidian-mcp.unstaticlabs.com/mcp     # 401
curl -so /dev/null -w 'admin=%{http_code}\n' https://obsidian-mcp.unstaticlabs.com/admin   # 302
ssh odoo 'cd /srv/obsidian/vaults/<user> && git status --porcelain | head'   # should be empty or agent writes only
ssh odoo 'docker exec obsidian-mcp-db psql -U obsidian -c "select count(*) from note_embeddings where valid_to is null"'
```

The last one is the one that catches a silently broken indexer: if the count of
current embeddings drifts far from the note count, indexing has stopped and
search is quietly answering from stale data.
