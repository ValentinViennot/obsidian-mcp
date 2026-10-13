#!/usr/bin/env bash
# Desktop and server use the same inode-preserving engine.
# Disable independent Git plugin pulls: ordinary editor saves remain supported.
set -euo pipefail
: "${VAULT_PATH:?Set VAULT_PATH to the intended working clone}"
LOG="${LOG_PATH:-$HOME/Library/Logs/obsidian-vault-reconcile.log}"
mkdir -p "$(dirname "$LOG")"
python3 "$(dirname "$0")/../vault-reconcile.py" --vault "$VAULT_PATH" --desktop sync >>"$LOG" 2>&1
