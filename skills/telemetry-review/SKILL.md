---
name: telemetry-review
description: Use when checking whether mobile-app or web-service telemetry issues are still current, ranking them, or refreshing a prioritized issue list.
---

# Telemetry review

Give the user a current, prioritized list so they can choose what to work on. Gather enough evidence to judge currency and priority. Detailed diagnosis belongs to selected follow-up work.

## Check currency

Use the requested apps, services and period; default to the past week. Start with current issues, recent sightings and existing fix/PR status. Make a small targeted telemetry check only when needed.

Classify issues as current, already addressed, or uncertain. Comment updates are not new failures, and missing telemetry does not prove recovery. If a quick check cannot settle currency, mark it uncertain and move on. Ranking does not require reproducing every issue or auditing every deployment.

Remove closed issues and matching merged fixes from the active list. Briefly note actionable rollout work separately.

## Rank and report

Group similar issues. Rank by current impact and recency, with rough fix/merge effort shown separately. Give each item a stable ID, available issue links, a short summary, why it appears current, priority and next action. Keep it to a sentence or two per item; mark missing information unknown.

Refresh one current list. Use HTML for fleet reports unless another format was requested, and `share-artifacts` for downloadable reports. Keep reports outside Git unless requested otherwise.

## Follow the user's direction

When asked, start selected items in new threads without asking again. Use the correct repair project and dedicated worktree, preserve the requested model/effort, and pass issue links plus a short assessment and scope. Track thread links and actual outcomes. Keep in-session subagents distinct from new conversations.

A review alone does not authorize fixes, issue changes or new work threads.
