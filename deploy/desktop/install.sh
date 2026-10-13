#!/usr/bin/env bash
# Build a relocatable, dependency-free desktop sync bundle. No timer activation.
set -euo pipefail
[ "$#" -eq 1 ] || { echo "Usage: install.sh NEW_BUNDLE_DIRECTORY" >&2; exit 2; }
[ ! -e "$1" ] || { echo "Destination already exists; choose a new bundle directory" >&2; exit 2; }
SOURCE="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$1/deploy/desktop" "$1/src/services"
cp "$SOURCE/deploy/desktop/obsidian-vault-reconcile.sh" "$1/deploy/desktop/"
cp "$SOURCE/deploy/vault-reconcile.py" "$1/deploy/"
cp "$SOURCE/src/services/vault_reconcile.py" "$SOURCE/src/services/vault_exchange.py" "$1/src/services/"
touch "$1/src/__init__.py" "$1/src/services/__init__.py"
chmod +x "$1/deploy/vault-reconcile.py" "$1/deploy/desktop/obsidian-vault-reconcile.sh"
echo "Bundle ready. Set VAULT_PATH before invoking its desktop script. Timer activation is separate."
