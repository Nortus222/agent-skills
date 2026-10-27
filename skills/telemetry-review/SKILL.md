---
name: telemetry-review
description: Use when checking and ranking current mobile-app or web-service telemetry issues, refreshing the issue list, or closing telemetry issues resolved by merged fix PRs.
---

# Telemetry review

Give the user a current, prioritized list so they can choose what to work on. Gather enough evidence to judge currency and priority. Detailed diagnosis belongs to selected follow-up work.

## Check currency

Use the requested apps, services and period; default to the past week. Start with current issues, recent sightings and existing fix/PR status. Make a small targeted telemetry check only when needed.

Classify issues as current, already addressed, or uncertain. Comment updates are not new failures, and missing telemetry does not prove recovery. If a quick check cannot settle currency, mark it uncertain and move on. Ranking does not require reproducing every issue or auditing every deployment.

Remove closed issues and matching merged fixes from the active list. Briefly note actionable rollout work separately.

## Close fixed issues

When a matching fix PR has merged, apply `telemetry-fixed` to the telemetry issues it resolves and close them. Confirm the PR addresses each issue; a merged fix is enough, without waiting for release or deployment. An open PR or quiet telemetry alone does not establish a merged fix.

Include this rule in follow-up thread handoffs. Report the closed issue links and matching fix PR, and remove them from the active list. Keep any pending rollout as a brief separate follow-up.

## Rank and report

Group similar issues. Rank by current impact and recency, with rough fix/merge effort shown separately. Give each item a stable ID, available issue links, a short summary, why it appears current, priority and next action. Keep it to a sentence or two per item; mark missing information unknown.

Refresh one current HTML report and use `share-artifacts` to share that HTML report. Keep reports outside Git unless requested otherwise.

## Follow the user's direction

When asked, start selected items in new threads without asking again. Use the correct repair project and dedicated worktree, preserve the requested model/effort, and pass issue links plus a short assessment and scope. Track thread links and actual outcomes. Keep in-session subagents distinct from new conversations.

Closing issues with verified merged fixes is part of this workflow. Other issue changes, implementation and new work threads require the user's direction.
