#!/usr/bin/env bash
# Desktop and server use the same inode-preserving engine.
# Disable independent Git plugin pulls: ordinary editor saves remain supported.
set -euo pipefail
: "${VAULT_PATH:?Set VAULT_PATH to the intended working clone}"
LOG="${LOG_PATH:-$HOME/Library/Logs/obsidian-vault-reconcile.log}"
mkdir -p "$(dirname "$LOG")"
ENGINE="$(dirname "$0")/../vault-reconcile.py"
MARKER="$VAULT_PATH/.git/obsidian-desktop-recovery-notified"
# How long a blocked sync may stay quiet before it says so again. One alert per
# distinct recovery state was not enough: a desktop vault stopped syncing on
# 2026-11-24 at 03:21, said so once — at twenty past three in the morning — and
# then nothing for 43 hours and 510 refused sweeps, because the state never
# changed and the marker suppressed every later alert. 0 restores that
# behaviour, one alert per state and never again.
RENOTIFY_MINUTES="${VAULT_RECONCILE_RENOTIFY_MINUTES:-60}"
[[ "$RENOTIFY_MINUTES" =~ ^[0-9]+$ ]] || RENOTIFY_MINUTES=60
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
        # The marker's own mtime is when this last said anything, so a changed
        # recovery state still alerts at once and an unchanged one alerts again
        # once the window is up. `stat` is spelled differently on the two
        # platforms the test suite runs on.
        repeat=0
        if [[ "$RENOTIFY_MINUTES" -gt 0 && -f "$MARKER" ]]; then
            said=$(stat -f %m "$MARKER" 2>/dev/null || stat -c %Y "$MARKER" 2>/dev/null || echo 0)
            [[ $(( ( $(date +%s) - said ) / 60 )) -lt "$RENOTIFY_MINUTES" ]] || repeat=1
        fi
        # Both texts are fixed literals. Neither carries a note name, a path or
        # any saved content: those stay in the private status and log output.
        text="Saved note versions need review. Use vault sync status and recovery tools; details are in your private sync log."
        [[ "$fingerprint" == "$prior" ]] && text="This vault still has not synchronised. $text" || true
        if [[ -n "$fingerprint" ]] && [[ "$fingerprint" != "$prior" || "$repeat" == 1 ]]; then
            if osascript -e "display notification \"$text\" with title \"Obsidian sync needs review\"" >>"$LOG" 2>&1; then
                # A digest-only private marker suppresses unchanged recovery alerts.
                (umask 077; temporary=$(mktemp "$MARKER.XXXXXX"); printf '%s\n' "$fingerprint" >"$temporary"; mv -f "$temporary" "$MARKER") || true
            fi
        fi
    fi
fi
exit "$result"
