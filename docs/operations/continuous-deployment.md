# Continuous deployment

A merge to `main` publishes an image and rolls production onto it. This note is
the setup — the secrets a human has to create, what branch protection applies,
and the parts of the path that are deliberately still manual.

The day-to-day incident procedures are in
[`runbooks.md`](runbooks.md); this file is about the pipeline itself.

---

## The pipeline

```
PR opened ──▶ pr.yml            tests (container) · gitleaks · vault-leak · compose-config
                                            └──▶ pr-gate   ← the required check
              ci.yml            upstream's: full suite with real Postgres, schema
                                gate, trivy, openspec, pip-audit

merge to main ──▶ publish-image.yml   build linux/amd64, push ghcr.io/…:sha-<sha> + :latest
                        │
                        └── workflow_run ──▶ deploy.yml
                                              resolve sha-<sha> to a digest
                                              ssh → scripts/vps-deploy.sh
                                                 back up  →  alembic upgrade head
                                                 →  up -d on the digest
                                                 →  wait for /health
                                                 →  roll back the image on failure
```

Four workflows, and they are separate on purpose:

- **`pr.yml`** is the fork's gate. It runs the suite in `docker/test.Dockerfile`
  — the same image `scripts/test-in-docker.sh` runs locally, so "it passed on
  my machine" and "it passed in CI" cannot mean different things.
- **`ci.yml`** is upstream's and is left alone. It is the only place the
  `tests/integration/` modules run against a real Postgres.
- **`publish-image.yml`** builds and pushes. It carries no deployment secrets.
- **`deploy.yml`** deploys, triggered by `workflow_run` on a *successful*
  publish. Keeping it separate means a deploy failure does not make the build
  look red, the SSH key never enters the job that runs `docker build` on the
  tree, and a redeploy of a known-good commit is one `workflow_dispatch` away
  with no rebuild.

---

## Secrets a human must create

Nothing in this repository generates a key. Set these under
**Settings → Secrets and variables → Actions** on
`ValentinViennot/obsidian-mcp`. `deploy.yml` fails on its second step with a
named list if either required one is missing.

| Name | Required | What it is |
| --- | --- | --- |
| `VPS_SSH_KEY` | yes | Private half of a deploy-only SSH keypair, whole file including the header and trailer lines |
| `VPS_HOST` | yes | Hostname or IP of the VPS **as reachable from a GitHub-hosted runner** |
| `VPS_SSH_KNOWN_HOSTS` | no | The host's public SSH key line. Without it the workflow accepts whatever key answers on the first connection |

And optionally, as **variables** (not secrets — they are not sensitive and
being readable in the log is useful):

| Name | Default | What it is |
| --- | --- | --- |
| `VPS_SSH_USER` | `root` | SSH user |
| `VPS_SSH_PORT` | `22` | SSH port |

### Generating the keypair

On your own machine, **not** on the VPS and **not** in CI:

```bash
ssh-keygen -t ed25519 -C "github-actions-deploy@obsidian-mcp" -N "" \
  -f ~/.ssh/obsidian-mcp-deploy
```

`-N ""` gives it no passphrase, which is required: an unattended workflow
cannot answer a prompt. That is the whole reason this is a **separate,
deploy-only** key and not your personal one.

Install the public half on the VPS, restricted to what a deploy needs:

```bash
ssh odoo 'mkdir -p ~/.ssh && chmod 700 ~/.ssh'
ssh-copy-id -i ~/.ssh/obsidian-mcp-deploy.pub odoo
```

Then put the **private** half into the secret. It must be the entire file:

```bash
gh secret set VPS_SSH_KEY --repo ValentinViennot/obsidian-mcp \
  < ~/.ssh/obsidian-mcp-deploy

gh secret set VPS_HOST --repo ValentinViennot/obsidian-mcp
# paste the hostname, then Ctrl-D
```

And the host key, so the first connection is not trust-on-first-use:

```bash
ssh-keyscan -H <host> 2>/dev/null \
  | gh secret set VPS_SSH_KNOWN_HOSTS --repo ValentinViennot/obsidian-mcp
```

Finally, delete the private key from your machine if it is not going to live
in a password manager — it exists only to be pasted into GitHub.

### `VPS_HOST` must be reachable from a GitHub runner

The `odoo` alias in `~/.ssh/config` points at a **Tailscale** address. A
GitHub-hosted runner is not on the tailnet, so that address will not resolve
there and `VPS_HOST` has to be the host's public name or address instead. The
host's sshd listens on all interfaces and no local firewall blocks 22, so this
works today — but it means the deploy key is a credential on a publicly
reachable port. Two things to keep true, in order of importance:

1. **Password authentication stays off** on the VPS
   (`PasswordAuthentication no`).
2. Consider narrowing the key in `~/.ssh/authorized_keys` with
   `from="<GitHub Actions egress>"` — the list is large and changes, so this is
   a judgement call, not a recommendation.

If you would rather keep SSH off the public internet, the alternative is to
join the runner to the tailnet with `tailscale/github-action` and an OAuth
client, then set `VPS_HOST` to the tailnet name. That is a second credential to
manage; it was left out on the "keep it simple" principle, not because it is
wrong.

---

## What the deploy actually does

`scripts/vps-deploy.sh` runs on the VPS — piped in over stdin, so nothing is
left on the host and the script that runs is always the one from the commit
being deployed. In order:

1. **Discovers the compose project from the running container.** Compose stamps
   the project name, working directory and config-file list onto every
   container it creates, so `docker inspect obsidian-mcp` is enough. No host
   path lives in this public repository, and there is no third secret to keep
   in sync with the GitOps repo.
2. **Records the digest that is running.** That is the rollback target. If the
   running image has no registry digest, the script **refuses to deploy** — a
   deploy with no way back is the failure it exists to prevent.
3. **Pulls the new digest** before touching anything.
4. **Backs up**, using the stack's own `backup` profile (the same
   `backup-prepare` + `backup` pair the runbook and the nightly Komodo
   procedure use). A failure here aborts before any migration, for the reason
   `make deploy` gives: the backup is the only way back from a bad migration.
5. **Runs `alembic upgrade head`** with the new image, before the new image
   serves traffic. Migrations are written to be backward-compatible, so the old
   container keeps serving correctly for the few seconds in between; the
   reverse order would put new code in front of an old schema.
6. **`up -d` on the pinned digest.** No `--force-recreate`: an unchanged digest
   is a no-op plus a health check, which is what makes a re-run safe.
7. **Waits for health.** The container's own healthcheck *is*
   `curl -f localhost:8000/health`, so its status is polled rather than a
   second probe being invented — the port is `expose`d, not published, so the
   host cannot reach it directly anyway. Restarts are counted separately and
   abort early: a crash loop restarts faster than a healthcheck can ever go
   unhealthy.
8. **Rolls the image back** if health does not come up, waits for the old
   digest to be healthy again, and exits non-zero either way.

### Three things a rollback does not fix

Stated here because the failure output says them too, and because getting them
wrong during an incident is expensive.

- **The migration is not undone.** `alembic downgrade` is not safe to run
  unattended. A release whose migration is not backward-compatible will fail
  health, roll the image back, and still be broken.
- **The pin does not survive Komodo.** The digest pin is a compose override
  written to `/var/lib/obsidian-mcp-deploy/pin.yaml`, outside the Komodo clone.
  The next DeployStack drops it and the stack returns to the floating `latest`
  tag — which, after a rollback, is the image that just failed. Fix forward, or
  pin the good digest in the GitOps repo.
- **The first deploy is still manual.** The script rolls an existing stack; it
  does not create one. A brand-new stack is a Komodo DeployStack, which is the
  fleet's rule for every stack.

---

## Branch protection

```bash
scripts/setup-branch-protection.sh            # apply
DRY_RUN=1 scripts/setup-branch-protection.sh  # show what would change
```

It is idempotent: it projects the current protection into the shape of the
request, compares, and does nothing when they already agree.

What it applies to `main`:

| Setting | Value |
| --- | --- |
| Pull request required | yes |
| Required approving reviews | **0** |
| Dismiss stale approvals | yes |
| Required status check | `pr-gate` |
| Branch must be up to date (`strict`) | yes |
| Force pushes | no |
| Deletions | no |
| Enforced for admins | no (`ENFORCE_ADMINS=true` to change) |

**Zero approvals is deliberate.** This is a single-maintainer repository;
requiring an approving review from someone else would mean nothing could ever
merge, and GitHub does not let an author approve their own PR. The pull request
itself is still mandatory, which is what buys the diff, the checks and the
history. `dismiss_stale_reviews` is set anyway so the rule does the right thing
the day a second maintainer appears.

**Admins are not enforced, by default.** With `enforce_admins` on, the sole
admin cannot push a fix when the *check infrastructure* is what is broken, and
there is nobody to ask for an override.

`pr-gate` is the only required check because it is an aggregate: it `needs`
every other job in `pr.yml` and fails if any of them did not succeed. New
checks are added to that `needs` list, not to branch protection.

### Token requirements

The branch-protection API needs **both**:

- **admin** permission on the repository for the authenticated account, and
- a token carrying **`repo`** (classic PAT, or `gh auth login`) — or, for a
  fine-grained token, **Administration: Read and write** on this repository.

```bash
gh auth status                                          # scopes
gh api repos/ValentinViennot/obsidian-mcp --jq .permissions.admin
```

A 403 or a 404 from the protection endpoint is almost always the *permission*,
not the scope: GitHub returns 404 rather than 403 to accounts that are not
admins. The script checks `.permissions.admin` up front and says so plainly
rather than letting the API's own wording send you looking for the wrong thing.

---

## Deploying by hand

From the Actions tab: **Deploy → Run workflow**, optionally with a commit SHA
(defaults to the head of `main`). The commit must already have a published
image; if it does not, the workflow says so at the digest-resolution step
rather than half-deploying.

`skip_backup` exists for the one case where the backup path itself is what is
broken. It is not a shortcut for a slow backup.
