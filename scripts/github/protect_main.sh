#!/usr/bin/env bash
# Branch protection for main, as the brief requires (Milestone 1, verified at
# Milestone 2). Needs the GitHub CLI signed in as a repository admin.
#
#   bash scripts/github/protect_main.sh LakshmiPrasanna2404/MediSlot
#
# Required checks are the pull-request job names in .github/workflows/deploy.yaml.
set -euo pipefail

REPO="${1:?usage: protect_main.sh <owner/repo>}"

gh api --method PUT "repos/${REPO}/branches/main/protection" --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["Lint"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": false,
  "required_conversation_resolution": true
}
JSON

echo "Branch protection on ${REPO}@main:"
gh api "repos/${REPO}/branches/main/protection" \
  --jq '{checks: .required_status_checks.contexts, strict: .required_status_checks.strict,
         approvals: .required_pull_request_reviews.required_approving_review_count,
         dismiss_stale: .required_pull_request_reviews.dismiss_stale_reviews,
         admins: .enforce_admins.enabled, force_push: .allow_force_pushes.enabled,
         deletion: .allow_deletions.enabled}'
