#!/usr/bin/env bash
#
# vault-reconcile.sh — the slow half of the git-backed vault.
#
# The server commits its own writes the moment they land (see
# `src/services/git_vault.py`), which is what makes `git blame` able to say
# "this line was written by an agent, through edit_note, on Tuesday". This
# script is what makes the history *complete* rather than merely readable:
#
#   1. commit anything that changed on disk without going through an MCP tool
#      — an Obsidian client writing directly into the mounted vault, a file
#      copied in over ssh, or an agent write whose own commit failed;
#   2. `git pull --rebase --autostash` from the bare repo, so the desktop's
#      commits arrive;
#   3. `git push`, so the server's commits leave.
#
# Run it from a systemd timer every couple of minutes (see
# `deploy/systemd/`), and immediately after a desktop push via the flag file
# `deploy/hooks/post-receive` touches.
#
# ── Why this is not a `post-receive` checkout ────────────────────────────────
#
# The obvious design is a `post-receive` hook on the bare repo that runs
# `git --work-tree=/srv/vault checkout -f`. It is wrong here, and the reason is
# specific rather than stylistic: **the server's working tree carries its own
# uncommitted writes.** An agent's `edit_note` publishes bytes and then commits
# them, and between those two steps — and for as long as a commit failure
# leaves them uncommitted, which is a state this system explicitly tolerates —
# the tree holds content that exists nowhere else. `checkout -f` discards
# exactly that: it is defined as "make the tree match this commit, destroying
# whatever disagrees". A push from the desktop would silently delete an agent's
# work, and the vault is the user's single source of truth.
#
# `pull --rebase --autostash` is the opposite disposition. It stashes local
# work, replays the incoming commits, restores the local work on top, and on a
# genuine conflict it *stops* — leaving the tree intact and this script to abort
# the rebase and exit loudly. Nothing is destroyed to make the merge succeed.
#
# So the hook does not touch the working tree at all. It touches a flag file,
# and this script — which knows about the uncommitted writes because it commits
# them first — does the integration.
#
# ── Contract ────────────────────────────────────────────────────────────────
#
#   * **Idempotent.** A run with nothing to do commits nothing, pushes nothing
#     and exits 0.
#   * **Safe to run concurrently with itself.** `flock`; a second instance
#     finding the lock held exits 0, because the run that holds it is about to
#     do the same work.
#   * **Never leaves a rebase in progress.** A conflict aborts the rebase,
#     restores the autostash, and exits non-zero — so the tree stays usable by
#     the agent and by the operator, and systemd marks the unit failed instead
#     of the failure being discovered weeks later.
#
# ── Configuration (environment) ─────────────────────────────────────────────
#
#   VAULT_DIR                  the working clone (required)
#   GIT_AGENT_NAME             identity for sweep commits
#   GIT_AGENT_EMAIL            (match src/config.py so history reads as one)
#   VAULT_RECONCILE_LOCK       flock path
#   VAULT_RECONCILE_FLAG       the flag `deploy/hooks/post-receive` touches
#   VAULT_RECONCILE_NET_TIMEOUT  seconds bounding each network git call
#
set -euo pipefail

VAULT_DIR="${VAULT_DIR:-}"
GIT_AGENT_NAME="${GIT_AGENT_NAME:-obsidian-mcp agent}"
GIT_AGENT_EMAIL="${GIT_AGENT_EMAIL:-agent@obsidian-mcp.invalid}"
VAULT_RECONCILE_LOCK="${VAULT_RECONCILE_LOCK:-/run/obsidian-vault-reconcile/lock}"
VAULT_RECONCILE_FLAG="${VAULT_RECONCILE_FLAG:-/run/obsidian-vault-reconcile/requested}"
VAULT_RECONCILE_NET_TIMEOUT="${VAULT_RECONCILE_NET_TIMEOUT:-120}"

# Exit codes, so a caller (and `systemctl status`) can tell the cases apart.
readonly EXIT_MISCONFIGURED=2
readonly EXIT_CONFLICT=3
readonly EXIT_PUSH_FAILED=4
# `flock`'s own code for "the lock was held". Distinct from every code above so
# a busy lock can never be mistaken for a failure of the work itself.
readonly EXIT_LOCK_BUSY=75

log() { printf '%s vault-reconcile: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "ERROR: $*"; exit "${2:-1}"; }

# ── The lock ────────────────────────────────────────────────────────────────
#
# Re-executes this script under `flock`, once. Two sweeps interleaving would
# `git add -A` each other's half-written state and race each other's rebase,
# which is the one way this script could itself create the corruption it exists
# to prevent.
#
# `-n` (fail rather than wait) and not `-w`: these runs are periodic and
# identical, so a second one waiting for the first buys nothing and, on a
# two-minute timer with a slow remote, quietly builds a queue of sweeps that all
# fire at once when the remote recovers.
if [ "${OMCP_RECONCILE_LOCKED:-}" != "1" ]; then
    export OMCP_RECONCILE_LOCKED=1
    lock_dir=$(dirname "$VAULT_RECONCILE_LOCK")
    mkdir -p "$lock_dir" 2>/dev/null || true
    set +e
    flock -n -E "$EXIT_LOCK_BUSY" "$VAULT_RECONCILE_LOCK" "$0" "$@"
    rc=$?
    set -e
    if [ "$rc" -eq "$EXIT_LOCK_BUSY" ]; then
        log "another reconcile holds $VAULT_RECONCILE_LOCK; leaving it to that run"
        exit 0
    fi
    exit "$rc"
fi

# ── Preconditions ───────────────────────────────────────────────────────────

[ -n "$VAULT_DIR" ] || die "VAULT_DIR is not set" "$EXIT_MISCONFIGURED"
[ -d "$VAULT_DIR" ] || die "VAULT_DIR ($VAULT_DIR) is not a directory" "$EXIT_MISCONFIGURED"
cd "$VAULT_DIR"

# git refuses a repository owned by another uid ("dubious ownership") and there
# is no way around that from a config file here: this unit runs with
# ProtectHome=yes, so /root/.gitconfig is invisible to it. Declaring the
# exemption through the environment reaches every git call in this script
# without one of them being forgotten.
#
# This is not hypothetical. The vault working tree is chowned to the container's
# uid so the server can write notes; the moment that happened, this script began
# failing every two minutes with "is not a git working tree" — and kept failing
# silently, because a timer that fails is still an active timer. The desktop
# went on pushing to the bare repo and the server simply stopped catching up.
GIT_CONFIG_COUNT=1
GIT_CONFIG_KEY_0=safe.directory
GIT_CONFIG_VALUE_0="$VAULT_DIR"
export GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0

git rev-parse --git-dir >/dev/null 2>&1 \
    || die "$VAULT_DIR is not a git working tree" "$EXIT_MISCONFIGURED"

# `--show-toplevel` and not merely "there is a .git somewhere above": pointing
# this at a subdirectory of the vault would sweep the whole enclosing
# repository under a message claiming it swept the vault.
toplevel=$(git rev-parse --show-toplevel)
[ "$(cd "$toplevel" && pwd -P)" = "$(pwd -P)" ] \
    || die "VAULT_DIR ($VAULT_DIR) is inside the repository rooted at $toplevel, not its root" \
           "$EXIT_MISCONFIGURED"

# Identity for the sweep commit AND for the rebase's own commits, in the
# environment only. The desktop clones this same repository and its commits must
# stay attributable to the human, so nothing here writes `git config` — not
# `--global`, not the repo's own.
export GIT_AUTHOR_NAME="$GIT_AGENT_NAME"
export GIT_AUTHOR_EMAIL="$GIT_AGENT_EMAIL"
export GIT_COMMITTER_NAME="$GIT_AGENT_NAME"
export GIT_COMMITTER_EMAIL="$GIT_AGENT_EMAIL"
# Non-interactive, whatever the ambient environment says. A credential prompt on
# a machine with no terminal is a git that holds the lock until its timeout.
export GIT_TERMINAL_PROMPT=0
export GIT_ASKPASS=
export SSH_ASKPASS=
export LC_ALL=C
unset EDITOR VISUAL GIT_EDITOR || true
export GIT_EDITOR=true

# ── The flag ────────────────────────────────────────────────────────────────
#
# Cleared FIRST, before any work. A push that arrives while this sweep is
# running sets it again and earns its own run; clearing it at the end would
# swallow that push's request and leave those commits sitting in the bare repo
# until the next tick — the exact latency the flag exists to remove.
if [ -e "$VAULT_RECONCILE_FLAG" ]; then
    rm -f "$VAULT_RECONCILE_FLAG" || log "could not clear $VAULT_RECONCILE_FLAG"
    log "a push requested this sweep"
fi

# ── 1. Commit whatever changed out of band ──────────────────────────────────
#
# `add -A` over the whole tree, which is what makes this the catch-all: it
# honours `.gitignore` (see `deploy/vault.gitignore` — Obsidian's workspace
# state and plugin caches must stay out or the repo grows without bound) and it
# stages deletions as well as edits.
git add -A

if git diff --cached --quiet; then
    log "nothing to commit"
else
    changed=$(git diff --cached --name-only | wc -l | tr -d ' ')
    # The same trailer vocabulary the per-write commits use
    # (`src/services/git_vault.py`), so one `git log --format='%(trailers...)'`
    # covers both kinds. Paths are deliberately not listed: the sweep can carry
    # thousands after a first import, and the diff is the authority anyway.
    git commit --quiet --no-verify --no-gpg-sign -m "$(cat <<EOF
vault(sweep): $changed path(s) changed outside the MCP server

Found on disk by the periodic reconcile sweep rather than written through
an MCP tool: an Obsidian client writing into the mounted vault directly, a
shell, or an agent write whose own commit did not complete.

Tool: reconcile-sweep
Principal: out-of-band
EOF
)" || die "could not commit $changed out-of-band path(s)"
    log "committed $changed out-of-band path(s)"
fi

# ── 2 & 3. Synchronise with the bare repo ───────────────────────────────────

branch=$(git symbolic-ref --quiet --short HEAD || true)
if [ -z "$branch" ]; then
    die "HEAD is detached; refusing to pull or push" "$EXIT_MISCONFIGURED"
fi

remote=$(git config --get "branch.$branch.remote" || true)
if [ -z "$remote" ]; then
    # A vault repo with no remote is a legitimate setup — local history alone
    # already answers "when did this appear, and was it me or an agent?". Say so
    # once and succeed; a missing remote is not a failure to report every two
    # minutes.
    log "branch '$branch' tracks no remote; committed locally, nothing to sync"
    exit 0
fi

# `--autostash` is what lets this run at all on a live server: the tree can hold
# an agent write published a millisecond ago and not yet committed, and a plain
# `pull --rebase` would refuse outright. It is stashed, the incoming commits are
# replayed, and it is restored on top — including when the rebase is aborted
# below, which is the property that keeps the abort path non-destructive.
if ! timeout "$VAULT_RECONCILE_NET_TIMEOUT" git pull --rebase --autostash --quiet "$remote" "$branch"; then
    log "ERROR: pull --rebase failed (conflict, or the remote is unreachable)"
    # Abort only if a rebase is actually in progress — an unreachable remote
    # fails before starting one, and `rebase --abort` there is a confusing
    # second error on top of the real one.
    if [ -d "$(git rev-parse --git-path rebase-merge)" ] \
       || [ -d "$(git rev-parse --git-path rebase-apply)" ]; then
        if git rebase --abort; then
            log "rebase aborted; the working tree is back where it was and is usable"
        else
            die "rebase --abort FAILED — the vault is mid-rebase and needs a human" "$EXIT_CONFLICT"
        fi
    fi
    # Non-zero and loud. The tree is usable and the server keeps serving, but
    # the two clones have diverged in a way no automation should resolve on its
    # own — somebody edited the same note in both places — and a sweep that
    # exited 0 here would hide that until the divergence was weeks deep.
    die "vault did not sync; resolve the divergence by hand in $VAULT_DIR" "$EXIT_CONFLICT"
fi

if ! timeout "$VAULT_RECONCILE_NET_TIMEOUT" git push --quiet "$remote" "$branch"; then
    # Usually transient and self-healing: a desktop push that landed between our
    # pull and our push makes this a non-fast-forward, and the next tick pulls
    # it and succeeds. Still reported as a failure, because the alternative is a
    # server whose commits stop leaving and nobody finds out.
    die "push to $remote/$branch failed; the commits are safe locally and the next sweep will retry" \
        "$EXIT_PUSH_FAILED"
fi

log "synced with $remote/$branch"
