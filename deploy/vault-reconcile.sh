#!/usr/bin/env bash
# Reconcile in a detached worktree; never checkout/rebase/abort the live vault.
set -euo pipefail
[ -n "${VAULT_DIR:-}" ] || { echo "VAULT_DIR is not set" >&2; exit 2; }
FLAG="${VAULT_RECONCILE_FLAG:-/run/obsidian-vault-reconcile/requested}"
if [ -e "$FLAG" ]; then
    rm -f "$FLAG"
fi
python3 "$(dirname "$0")/vault-reconcile.py" --vault "$VAULT_DIR" sync
MARKER="${VAULT_RECONCILE_MARKER:-/var/lib/obsidian-mcp/last-reconcile-success}"
mkdir -p "$(dirname "$MARKER")" 2>/dev/null && touch "$MARKER" 2>/dev/null || true
