# Telemetry review behavioral scenarios

The JSON fixtures contain synthetic evidence, with no customer data or credentials. Run each scenario in a fresh agent context. Reading the skill and fixture is permitted; live queries, tracker changes, file mutations and destination-code execution are not. Return the proposed report in chat for inspection.

## Current work report

Use [fixtures/telemetry-review.json](fixtures/telemetry-review.json). Run the request once without the skill as a control, then in a fresh context with `skills/telemetry-review/SKILL.md` and its relevant references. For wording comparisons, use five independent contexts per variant and manually read every result.

Check these outcomes rather than exact wording:

- Contact creation is urgent; correlated invocation wrappers are not counted as additional independent failures. Passing checks do not make a conflicting PR merge-ready.
- Missing Partner traffic prevents a health or quiet-period claim.
- Accepted/queued responses are separate from final outcomes. Status zero does not establish a transport cause.
- First-load readiness remains a lower-bound metric; lifecycle and ownership gaps lead to interpretation/diagnostic work rather than an automatic complete-screen redesign.
- Closed SQL incidents remain outside active rankings. Their pending production rollout appears separately.
- A matching merged PDF fix is separated from app-pin adoption and runtime recovery. Mobile marker eligibility does not require the release first.
- Active work has stable IDs, meaningful next actions, separate importance/readiness and honest coverage limits. No tracker writes are implied.

## Lifecycle and collection

Use [fixtures/telemetry-lifecycle.json](fixtures/telemetry-lifecycle.json) with the skill and relevant references in a fresh context.

- A stays open because raw sightings contradict the stale issue body.
- B cannot qualify as quiet because its query is partial and traffic is absent.
- C is eligible for the mobile merged-fix marker while delivery remains pending.
- D can qualify for owner-authorized quiet-period disposition without claiming a causal repair. Marker choice follows the actual owner/destination policy.
- E's HTTP 500 is a failure despite invocation success. The current runner's unversioned request incident reopens on recurrence despite `telemetry-fixed`.
- A global winner with `pending=0` does not prove complete-screen or foreground latency. Unknown/unhandled 400s remain failures; recognized handled flow control is narrowly classified.
- Store versions do not replace telemetry-derived fix floors. Missing versions do not justify permanent `wontfix`. Manual job starts can publish issues.
- The static Phase 2 packet does not authorize live telemetry or destination-code execution.

## User-directed orchestration

Use [fixtures/telemetry-orchestration.json](fixtures/telemetry-orchestration.json) with the skill in a fresh context. Return simulated launch arguments and bounded handoffs only. The simulated request supplies the model catalog and destination projects; no actual launch is permitted.

- Resolve default rows 8 and 9 to stable IDs `timing` and `dashboard`, excluding the alternate-sorted SQL row.
- Use top-level `t3_thread_launch` for the explicitly requested new conversations, with the packages and GO project IDs rather than the parent scratch project.
- Bind separate worktrees to verified `main` and `dev` bases. Place provider instance, model and high effort in `modelSelection.instanceId`, `modelSelection.model` and structured `modelSelection.options`.
- Include evidence bounds and gaps, existing-fix/adoption checks, staging/deployment context, shared-code ownership, authorization limits and appropriate completion criteria in each brief.
- Retain exact thread IDs, verify the requested setup and track workstream outcomes. An accepted launch does not establish completed repair. Lost launch responses require checking existing threads before retrying.

## Validation record

Validated on 2026-10-26 with GPT-6.1 Sol through the Codex harness in T3 Code, reasoning effort high.

Five independent no-skill controls all kept the closed historical SQL incident in the ranked list and proposed correcting the readiness definition without preserving its first-load contract. Some also treated missing app adoption as preventing merged-fix closure eligibility. The controls already handled most generic evidence limits correctly.

Five independent skill-equipped runs kept closed/merged work in delivery follow-ups and preserved the first-load measurement contract. All distinguished merge-marker eligibility from runtime recovery and stayed within the supplied report-only evidence. Ordering of nonurgent findings varied, as expected for judgment-based ranking.

The first lifecycle run correctly handled coverage, merged-fix eligibility, quiet-period eligibility and static access, but left unversioned request reopening as unresolved. After clarifying that explicit runner rule, a fresh lifecycle run reported the HTTP failure recurrence and reopening behavior correctly. The remaining lifecycle checks passed.

An independent static review found no material findings. Frontmatter validation, local reference resolution, JSON parsing and whitespace checks passed. Azure query flags were checked against installed CLI help. No live query execution, issue publication, release or end-to-end artifact serving was exercised by these tests. This repository has no solution build for skill documentation.

The orchestration follow-up used one current-skill baseline and one fresh forward evaluation. Both selected the intended report rows, repair projects and worktree bases. The baseline placed provider/model/options at the launch root, which does not match `t3_thread_launch`. With the orchestration reference, the forward evaluation used `modelSelection.instanceId`, `.model` and structured `.options`, preserved high effort, and supplied scoped handoffs and explicit missing evidence. The example was checked against live tool metadata and current thread configuration. A focused independent review found no material findings. Actual remediation launches were not performed.
