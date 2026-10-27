---
name: telemetry-review
description: Use when reviewing mobile-app or web-service telemetry issues, or closing them after fix PRs merge.
---

# Telemetry review

Produce a current, prioritized HTML report that helps the user choose what to work on.

## Check current status

Use the requested apps, services and period; default to the past week. Check recent sightings and existing fix/PR status. Make a targeted telemetry check where the issue evidence is insufficient.

Finish this pass when every issue is marked current, already addressed or uncertain, with a short reason. Comment updates are not new failures, and missing telemetry does not prove recovery. If the targeted check leaves the status unclear, mark it uncertain and move on. Detailed diagnosis belongs to selected follow-up work.

## Close fixed issues

Confirm that a matching fix PR has merged, then apply `telemetry-fixed` and close the issues it resolves. Release or deployment is not required. An open PR or quiet telemetry alone is insufficient for this closure.

Report the closed issue links and matching fix PR. Exclude closed issues and merged fixes from active priorities; note actionable rollout work separately. Other issue changes and implementation require the user's direction.

## Rank and share

Group similar issues and rank by current impact and recency. Show rough fix/merge effort separately. Give each item a stable ID, available issue links, a short summary, current status, priority and next action. Use a sentence or two per item and mark missing information unknown.

Refresh one current HTML report and share it using `share-artifacts`. Keep reports outside Git unless requested otherwise.

## Start selected work

When the user requests new threads, launch the selected items without asking again. Resolve report selections to stable IDs, use the correct repair project and dedicated worktree, and preserve the requested model/effort. Pass issue links, a short assessment, scope and the merged-fix closure rule. Track thread links and actual outcomes. Keep in-session subagents distinct from requested new conversations.
