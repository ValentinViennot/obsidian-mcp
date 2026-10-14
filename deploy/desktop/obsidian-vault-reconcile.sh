#!/usr/bin/env bash
# Desktop and server use the same inode-preserving engine.
# Disable independent Git plugin pulls: ordinary editor saves remain supported.
set -euo pipefail
: "${VAULT_PATH:?Set VAULT_PATH to the intended working clone}"
LOG="${LOG_PATH:-$HOME/Library/Logs/obsidian-vault-reconcile.log}"
mkdir -p "$(dirname "$LOG")"
ENGINE="$(dirname "$0")/../vault-reconcile.py"
MARKER="$VAULT_PATH/.git/obsidian-desktop-recovery-notified"
result=0
python3 "$ENGINE" --vault "$VAULT_PATH" --desktop sync >>"$LOG" 2>&1 || result=$?
if [[ "$result" == 0 ]]; then
    rm -f "$MARKER" 2>>"$LOG" || true
elif [[ "$result" == 3 ]] && command -v osascript >/dev/null 2>&1; then
    # Hash private status instead of putting note names or paths into an alert.
    # A failed/busy status read must not consume the notification opportunity.
    status=""
    if status=$(python3 "$ENGINE" --vault "$VAULT_PATH" --desktop status 2>>"$LOG"); then
        fingerprint=$(printf '%s' "$status" | python3 -c 'import hashlib,json,sys; s=json.load(sys.stdin); print(hashlib.sha256(json.dumps(s,sort_keys=True).encode()).hexdigest() if s["review_required"] else "")') || fingerprint=""
        prior=""
        [[ ! -f "$MARKER" ]] || prior=$(cat "$MARKER") || prior=""
        if [[ -n "$fingerprint" && "$fingerprint" != "$prior" ]]; then
            if osascript -e 'display notification "Saved note versions need review. Use vault sync status and recovery tools; details are in your private sync log." with title "Obsidian sync needs review"' >>"$LOG" 2>&1; then
                # A digest-only private marker suppresses unchanged recovery alerts.
                (umask 077; temporary=$(mktemp "$MARKER.XXXXXX"); printf '%s\n' "$fingerprint" >"$temporary"; mv -f "$temporary" "$MARKER") || true
            fi
        fi
    fi
fi
exit "$result"
