#!/usr/bin/env bash
# Queue proof: look up a merge_group CI run for this commit's SHA to verify that
# the merge queue actually tested this tree before it landed.
#
# Called from the `queue-proof` job in ci.yml, build.yml and fast-gate.yml on the
# push-to-main path. When MERGE_QUEUE_ENABLED is 'true', the push path trims the
# matrix to macOS only on the assumption that the merge queue ran everything else.
# This script verifies that assumption: it queries the GitHub API for a merge_group
# event CI run whose head_sha matches github.sha (the landed commit). If a
# successful run exists, it outputs queue_proved=true; otherwise queue_proved=false.
#
# Fail-safe by design: every error path outputs queue_proved=false, which causes
# the full matrix to run. There is no fail-open path. The cost of a false negative
# is one extra full CI run on main; the cost of a false positive would be a
# commit landing with no Linux/Windows verdict.
#
# Provider premise: a merge group's head_sha equals the commit that lands on main
# when the group passes. This holds for GitHub's fast-forward merge strategy used
# by the merge queue. If the premise is wrong, queue_proved is always false and the
# full matrix always runs -- safe but slightly costlier. Verify on the first
# queued PR after enablement.
#
# Environment (set by the workflow step):
#   GH_TOKEN   GITHUB_OUTPUT   REPO   SHA   WORKFLOW
#
# The lookup checks ONLY the ci.yml workflow: if the merge queue ran CI on the
# tree, that is sufficient proof. build.yml and fast-gate.yml share the same
# trigger configuration, so a CI merge_group run implies those ran too.

set -euo pipefail

# When the variable is not set, the push path already runs the full matrix,
# so there is nothing to prove. Output false and exit cleanly.
if [ "${QUEUE_ON:-}" != "true" ]; then
  echo "MERGE_QUEUE_ENABLED is not 'true'; full matrix path."
  echo "queue_proved=false" >> "$GITHUB_OUTPUT"
  exit 0
fi

# Query for merge_group runs of the CI workflow with this commit as head_sha.
# The API returns runs matching the filters; we need at least one successful one.
read_err=""
if ! runs="$(gh api --method GET \
  "repos/$REPO/actions/workflows/${WORKFLOW}/runs" \
  -f "head_sha=$SHA" -f "event=merge_group" \
  -f per_page=10 2>"${TMPDIR:-/tmp}/queue-proof-err.$$")"; then
  read_err="$(head -c 300 "${TMPDIR:-/tmp}/queue-proof-err.$$" 2>/dev/null | tr '\n' ' ')"
  runs=""
fi
rm -f "${TMPDIR:-/tmp}/queue-proof-err.$$"

if [ -n "$read_err" ] || [ -z "$runs" ]; then
  echo "::warning::Queue proof lookup failed: ${read_err:-empty response}. Falling back to full matrix."
  echo "queue_proved=false" >> "$GITHUB_OUTPUT"
  exit 0
fi

# Check whether any returned run concluded successfully. The jq program filters
# for completed/success runs and returns "true" if at least one exists.
proved="$(printf '%s' "$runs" \
  | jq -r '[(.workflow_runs // [])[]
            | select(.status == "completed")
            | select(.conclusion == "success")]
           | if length > 0 then "true" else "false" end' \
      2>/dev/null || echo "false")"

if [ "$proved" = "true" ]; then
  echo "Merge queue CI run found for $SHA. Queue proved."
else
  echo "::warning::No successful merge_group CI run found for $SHA. Falling back to full matrix."
fi

echo "queue_proved=$proved" >> "$GITHUB_OUTPUT"
