# Taking upstream's changes

This fork tracks [`maxkuminov/obsidian-mcp`](https://github.com/maxkuminov/obsidian-mcp),
a single-maintainer project that moves quickly. Whether this fork is still
maintainable in a year is decided almost entirely by whether this procedure
stays cheap. It stays cheap only if our changes remain *small, separable, and
concentrated in files upstream rarely touches*.

## The strategy: merge, never rebase

**Merge upstream into our `main`. Do not rebase our `main` onto upstream.**

Rebasing rewrites our commits, which breaks anyone who has pulled, and — worse
here — destroys the record of *which* change was ours and why. The whole reason
this fork is legible is that `git log --no-merges upstream/main..main` lists
exactly our divergence. A rebase turns that into a fiction where our work
appears to have always been part of upstream.

```bash
git fetch upstream
git log --oneline main..upstream/main        # what's new upstream
git log --oneline --no-merges upstream/main..main   # what's ours
```

Those two commands are the whole situation. Read both before merging.

## Where our changes live

Divergence is deliberately concentrated. Sorted by how likely a conflict is:

| Area | Files | Conflict risk |
|---|---|---|
| New, self-contained modules | `src/services/git_vault.py`, `src/services/git_history.py`, `src/services/history_indexer.py`, `src/auth/oidc.py` | **None** — upstream has no such files |
| New scripts and deploy assets | `scripts/vault_history_import.py`, `scripts/generate_vault_guide.py`, `scripts/test-in-docker.sh`, `deploy/`, `docker/test.Dockerfile` | **None** |
| New tests | `tests/test_git_*.py`, `tests/test_vault_history_import.py`, `tests/test_generate_vault_guide.py`, `tests/test_oidc*.py` | **None** |
| Tool registration | `src/mcp_server/server.py` | **Low** — we append tool registrations; conflicts are additive and trivial |
| Config | `src/config.py`, `.env.example` | **Low** — we add settings; conflicts are additive |
| Write-path hooks | `src/mcp_server/tools.py` | **Medium** — we add a commit call to five `_impl` functions upstream actively edits |
| Human login | `src/auth/routes.py` | **Medium** — upstream owns this file and we changed its behaviour |
| Embedding provider | `src/services/embeddings.py` | **Medium** — we rewrote `OllamaProvider.embed_batch` |
| Schema | `alembic/versions/` | **Medium** — parallel migration heads, see below |

The medium-risk set is five files. That is the maintenance burden, and it is the
number to keep small: **before adding a change to an upstream-owned file, ask
whether it can live in one of ours instead.**

## Procedure

```bash
git fetch upstream --tags
git switch -c chore/merge-upstream-$(date +%Y-%m-%d)
git merge upstream/main
```

Resolve conflicts with these rules:

1. **In upstream-owned files, prefer upstream's version and re-apply our change
   on top.** Our hooks are small and easy to reinstate; upstream's logic often
   carries a security fix whose reasoning is not obvious from the diff. Never
   resolve by keeping "ours" wholesale in a file we do not own.
2. **Read the upstream commit message before resolving.** This project's
   messages routinely explain *why* — several encode incident history (a
   `#127`-style reference is a real defect that was fixed carefully). Silently
   reverting one by taking "ours" reintroduces the bug.
3. **Our commit-on-write hooks must stay at the end of each `_impl`**, after the
   write has actually succeeded. If upstream restructures a function, re-place
   the hook rather than forcing the old diff.

### Migrations

Both sides add Alembic revisions, so a merge produces **two heads**. Do not
merge migration files by hand.

```bash
./scripts/test-in-docker.sh tests/integration/test_schema_check.py   # will fail on multiple heads
alembic heads                      # confirm there are two
alembic merge -m "merge upstream and fork migration heads" <head1> <head2>
make test-schema                   # the gate that must pass
```

A merge revision is the correct resolution; it is empty and exists only to
reunify the graph. Never renumber or delete either side's revision — a
production database has already applied one of them.

## Verify

```bash
./scripts/test-in-docker.sh -q        # expect the full suite green
make test-schema                      # migrations
make test-integration                 # needs Docker; exercises real Postgres
```

Then check our features specifically, because a green suite proves upstream's
tests pass, not that ours still make sense in a changed codebase:

```bash
./scripts/test-in-docker.sh tests/test_git_vault.py tests/test_git_history.py \
    tests/test_oidc_login.py tests/test_temporal_embeddings.py -q
```

Finally, confirm the divergence is still the divergence you expect:

```bash
git log --oneline --no-merges upstream/main..main
git diff --stat upstream/main..main -- src/
```

If that `--stat` has grown substantially without a deliberate decision, the fork
is drifting and the next merge will be worse. Fix it now, not next time.

## When upstream changes something we depend on

Two things we rely on are not contracts, and upstream may change them without
warning:

- **`_tracked` (`src/mcp_server/tools.py`)** — we use the principal it resolves
  to label commits. If its signature or the context it exposes changes, our
  commit messages lose the caller's identity. Symptom: commits attributed to an
  empty principal. Fix in `src/services/git_vault.py`, not by reverting upstream.
- **`_atomic_write_at` and its callers (`src/services/vault.py`)** — we hook the
  five `_impl` call sites, not the primitive. If upstream adds a *sixth* write
  path, it will silently not produce commits. After any merge that touches
  `vault.py` or the write tools, re-check:

  ```bash
  grep -n "write_file_at\|write_bytes_at" src/mcp_server/tools.py
  ```

  Every call site should have a commit hook nearby. This is the single most
  likely way this fork breaks quietly.

## If a merge becomes genuinely painful

The escape hatch is to stop carrying a patch and upstream it instead. The
history tools, the containerised test runner, and the batched Ollama provider
are all plausibly useful to upstream and are written to be contributable as-is.
Anything accepted upstream is one less file to merge forever.
