---
name: telemetry-review
description: Use when reviewing or ranking mobile-app and web-service telemetry, investigating telemetry incidents, refreshing a telemetry work report, or verifying whether an existing fix addresses observed failures.
---

# Telemetry review

Turn current telemetry and repository evidence into a prioritized work list. Keep issue disposition, fix delivery and runtime verification as separate facts.

## Establish scope

Recover the request and prior decisions before collecting data. Reuse an existing report when refreshing it. For an unspecified window, state a default of the last seven days ending at one fixed UTC cutoff. Preserve requested applications, environments, exclusions and output format.

For fleet reviews, discover the current route registry and explicitly requested services. Account for every selected application/environment; distinguish configured routes from the deployed job's coverage. Read [collect-evidence.md](references/collect-evidence.md) before querying or interpreting telemetry. Read [incident-lifecycle.md](references/incident-lifecycle.md) before assessing existing fixes, markers or closure candidates.

A report request authorizes investigation and reporting. Apply tracker changes or remediate code when requested. Existing authorization persists within its scope; past permission on an unrelated incident adds no authority. Leave extraction, fingerprints, publication and recurrence suppression in the existing telemetry service.

## Gather and reconcile

1. Record source freshness, traffic, platform and build coverage before evaluating failures. Missing or sparse data is a coverage finding. Query failures and partial results are unresolved evidence.
2. Refresh issues, full comments, linked/open PRs, matching commits, consumer pins and deployed versions. Pin the configured assessment branch's commit; distinguish that repair context from the failing deployment. Search local source with `rg`; old assessments are hypotheses.
3. Group related symptoms by plausible mechanism and user workflow. Correlate wrappers before counting independent failures. Keep repository-scoped incident identities and environment-specific evidence visible within cross-repository workstreams.
4. For each finding, separate observation, confirmed mechanism, hypothesis and missing evidence. Name the likely repair owner, including shared packages or infrastructure when appropriate. End with a concrete next action and the evidence needed to verify it.

## Produce the current work list

Use HTML for a fleet review unless another format was requested. A focused answer can stay in chat. Keep one current prioritized list with stable workstream IDs; display positions may change with sorting. A selected row number resolves against the report's stated default order, never directly to a GitHub issue number.

The report contains:

- A scope and coverage summary with exact UTC bounds, source freshness and limitations.
- Active work grouped by similarity. Each item names affected apps/environments, last observed occurrence, current-window counts with units, impact, confidence, linked incidents/fixes, repair owner, merge effort/readiness and next action.
- A separate delivery/verification follow-up section for closed or merged work whose adoption, release or runtime outcome remains pending.

Rank active work by current impact and recent observed changes, with urgent data-integrity or outage risks first. Show merge ease separately; a green check does not resolve conflicts. Use telemetry occurrence time rather than comment update time. Keep old low-volume incidents in a separate backlog.

On refresh, recheck GitHub state, remove closed incidents and completed merged work from the active list, and update all counts, groups and charts consistently. Retain any independently actionable rollout task in the follow-up section. Keep historical snapshots outside the user-facing current list.

Save reports and diagnostic exports outside Git unless requested otherwise. Use `share-artifacts` for downloadable HTML and verify its final URL. When available, preview and render a concise inline view. For authorized issue work, use `triage` with the linked lifecycle policy and current account-attribution instructions. Use `emws-api` for supported API probes.

## Start selected work

When the user directs work into new threads, read [orchestration.md](references/orchestration.md) and launch the selected work with its correct project, workspace, model settings and handoff. Use child tasks for in-session delegation; creating separate conversations requires the user's direction. Keep the report linked to each workstream's actual thread and outcome.
