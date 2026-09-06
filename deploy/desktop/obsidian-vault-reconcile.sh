#!/usr/bin/env bash
# Desktop-side vault reconcile: commit local edits, rebase on the server, push.
#
# The obsidian-git plugin already does this — but only while Obsidian is
# running. Close the app for a week and nothing syncs, and the server's own
# agent writes pile up unseen. This runs on a launchd timer so the desktop
# behaves the same whether or not the app is open, and so the plugin is a
# convenience rather than a load-bearing component.
#
# Safe to run concurrently with the plugin: the lock below means whichever gets
# there first wins and the other backs off, and both use rebase, so history
# stays linear either way.
set -uo pipefail

VAULT="${VAULT_PATH:-$HOME/Code/vault}"
LOCK="$VAULT/.git/obsidian-reconcile.lock"
LOG="${LOG_PATH:-$HOME/Library/Logs/obsidian-vault-reconcile.log}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG"; }

[ -d "$VAULT/.git" ] || { log "no git repo at $VAULT"; exit 1; }

# mkdir is atomic on every filesystem we care about; macOS has no flock(1).
if ! mkdir "$LOCK" 2>/dev/null; then
  # A lock older than an hour is a crashed run, not a live one.
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +60 2>/dev/null)" ]; then
    log "removing stale lock"
    rmdir "$LOCK" 2>/dev/null || true
    mkdir "$LOCK" 2>/dev/null || { log "lock still held, skipping"; exit 0; }
  else
    exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT INT TERM

cd "$VAULT" || exit 1

# Commit whatever the human changed while the app was closed. Authored as the
# human: this is their edit, and blame should say so. The server commits under
# its own identity, which is what keeps "did I write this, or did an agent?"
# answerable.
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  if git commit -q -m "vault: desktop sync $(date -u +%Y-%m-%dT%H:%M:%SZ)"; then
    log "committed local changes"
  fi
fi

if ! git pull -q --rebase --autostash origin main; then
  # Leave the tree usable rather than parked mid-rebase, and be loud: a
  # conflict needs a human, and silently retrying every 5 minutes would bury
  # that fact in a log nobody reads.
  git rebase --abort 2>/dev/null || true
  log "ERROR rebase conflict — resolve by hand in $VAULT"
  osascript -e 'display notification "Vault sync needs manual conflict resolution" with title "Obsidian vault"' 2>/dev/null || true
  exit 1
fi

if ! git push -q origin main; then
  log "ERROR push failed (server unreachable?)"
  exit 1
fi

log "ok"
