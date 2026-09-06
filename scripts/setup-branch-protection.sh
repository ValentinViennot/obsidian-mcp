#!/usr/bin/env bash
# Apply branch protection to `main`, idempotently.
#
# `main` is what deploys: a push to it publishes an image and
# .github/workflows/deploy.yml rolls production onto it. So the rule that
# matters is that nothing reaches `main` except through a pull request whose
# checks went green.
#
# WHAT IT SETS, AND THE TWO DELIBERATE OMISSIONS
#
#   require a pull request       yes
#   required approving reviews   ZERO — see below
#   dismiss stale approvals      yes
#   required status check        pr-gate (the aggregate job in pr.yml)
#   strict (branch up to date)   yes
#   force pushes                 no
#   deletions                    no
#   enforce for admins           NO by default — see below
#
# **Zero approvals, deliberately.** This is a single-maintainer repository.
# Requiring an approving review from somebody else would mean no change could
# ever merge; requiring one from the author is not a thing GitHub offers. The
# PR itself is still mandatory, which is what buys the diff, the checks and the
# history. `dismiss_stale_reviews` is still set so the rule does the right
# thing the day a second maintainer appears.
#
# **`enforce_admins` off by default.** With it on, the sole admin cannot push a
# fix when the check infrastructure itself is what is broken — GitHub Actions
# being down, say — and there is nobody to ask for an override. The protection
# is a guardrail against mistakes, not a control against the person who holds
# the keys anyway. Set ENFORCE_ADMINS=true to turn it on.
#
# TOKEN REQUIREMENTS
#
# The branch-protection API needs BOTH:
#   * admin permission on the repository for the authenticated account, and
#   * a token carrying `repo` (classic PAT / `gh auth login`) or, for a
#     fine-grained token, "Administration: Read and write" on this repository.
#
# `gh auth status` shows the scopes; `gh api repos/OWNER/REPO --jq
# .permissions.admin` shows the permission. A 403 here is almost always the
# permission, not the scope.
#
# Usage:
#   scripts/setup-branch-protection.sh                # apply to the origin repo
#   REPO=owner/name scripts/setup-branch-protection.sh
#   BRANCH=main CHECK=pr-gate ENFORCE_ADMINS=true scripts/setup-branch-protection.sh
#   DRY_RUN=1 scripts/setup-branch-protection.sh      # print the change, apply nothing

set -euo pipefail

BRANCH="${BRANCH:-main}"
CHECK="${CHECK:-pr-gate}"
ENFORCE_ADMINS="${ENFORCE_ADMINS:-false}"
DRY_RUN="${DRY_RUN:-0}"

command -v gh >/dev/null || { echo "gh is not installed: https://cli.github.com" >&2; exit 1; }

# Derived from the `origin` remote, NOT from `gh repo view`: this repository is
# a fork, and gh resolves an unset default to the *upstream* — which is
# somebody else's `main` and not the branch that deploys.
if [ -z "${REPO:-}" ]; then
  origin_url="$(git remote get-url origin 2>/dev/null || true)"
  [ -n "$origin_url" ] || { echo "No 'origin' remote; set REPO=owner/name." >&2; exit 1; }
  REPO="$(printf '%s' "$origin_url" \
    | sed -e 's#\.git$##' -e 's#^git@[^:]*:##' -e 's#^ssh://[^/]*/##' -e 's#^https\{0,1\}://[^/]*/##')"
fi
echo "repo:   $REPO"
echo "branch: $BRANCH"
echo

# Fail early and specifically. The API's own 403 says only "Resource not
# accessible by integration", which sends people looking for the wrong thing.
if ! admin="$(gh api "repos/$REPO" --jq '.permissions.admin' 2>/dev/null)"; then
  echo "Cannot read $REPO — is the token valid? (gh auth status)" >&2
  exit 1
fi
if [ "$admin" != "true" ]; then
  echo "The authenticated account does not have ADMIN on $REPO." >&2
  echo "Branch protection cannot be set without it, whatever the token's scopes are." >&2
  echo "Run this as the repository owner, or from an account with the Admin role." >&2
  gh auth status 2>&1 | sed 's/^/  /' >&2
  exit 1
fi

DESIRED="$(cat <<EOF
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["$CHECK"]
  },
  "enforce_admins": $ENFORCE_ADMINS,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 0
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true,
  "block_creations": false,
  "required_linear_history": false,
  "lock_branch": false,
  "allow_fork_syncing": false
}
EOF
)"

# The GET response is much larger than the PUT body and nests differently, so
# it is projected down to the same shape before comparing. Without this the
# script would always report a change and always PUT — which is harmless but
# makes "print what it changed" a lie.
CURRENT_PROJECTED='null'
if current="$(gh api "repos/$REPO/branches/$BRANCH/protection" 2>/dev/null)"; then
  CURRENT_PROJECTED="$(jq -S '{
    required_status_checks: (
      if .required_status_checks then
        {strict: .required_status_checks.strict, contexts: (.required_status_checks.contexts // [])}
      else null end
    ),
    enforce_admins: (.enforce_admins.enabled // false),
    required_pull_request_reviews: (
      if .required_pull_request_reviews then {
        dismiss_stale_reviews: (.required_pull_request_reviews.dismiss_stale_reviews // false),
        require_code_owner_reviews: (.required_pull_request_reviews.require_code_owner_reviews // false),
        required_approving_review_count: (.required_pull_request_reviews.required_approving_review_count // 0)
      } else null end
    ),
    allow_force_pushes: (.allow_force_pushes.enabled // false),
    allow_deletions: (.allow_deletions.enabled // false),
    required_conversation_resolution: (.required_conversation_resolution.enabled // false),
    required_linear_history: (.required_linear_history.enabled // false)
  }' <<<"$current")"
else
  echo "No protection on $BRANCH yet."
fi

DESIRED_PROJECTED="$(jq -S '{
  required_status_checks,
  enforce_admins,
  required_pull_request_reviews,
  allow_force_pushes,
  allow_deletions,
  required_conversation_resolution,
  required_linear_history
}' <<<"$DESIRED")"

if [ "$CURRENT_PROJECTED" = "$DESIRED_PROJECTED" ]; then
  echo "Already correct — nothing to change."
  exit 0
fi

echo "Changes to apply:"
if [ "$CURRENT_PROJECTED" = "null" ]; then
  jq -S . <<<"$DESIRED_PROJECTED" | sed 's/^/  + /'
else
  diff <(jq -S . <<<"$CURRENT_PROJECTED") <(jq -S . <<<"$DESIRED_PROJECTED") | sed 's/^/  /' || true
fi
echo

if [ "$DRY_RUN" = "1" ]; then
  echo "DRY_RUN=1 — not applying."
  exit 0
fi

gh api --method PUT "repos/$REPO/branches/$BRANCH/protection" \
  -H "Accept: application/vnd.github+json" \
  --input - <<<"$DESIRED" >/dev/null

echo "Applied. Current state:"
gh api "repos/$REPO/branches/$BRANCH/protection" --jq '{
  required_pull_request_reviews: .required_pull_request_reviews.required_approving_review_count,
  dismiss_stale_reviews: .required_pull_request_reviews.dismiss_stale_reviews,
  required_checks: .required_status_checks.contexts,
  strict: .required_status_checks.strict,
  enforce_admins: .enforce_admins.enabled,
  allow_force_pushes: .allow_force_pushes.enabled,
  allow_deletions: .allow_deletions.enabled
}' | sed 's/^/  /'
