# Incident disposition and delivery

## Keep the states separate

For every candidate fix, establish what is known at each stage:

1. The matching code change and merged PR.
2. Each consumer's shared-library/submodule pin.
3. The released build and deployed commit in each relevant environment/platform.
4. Observed behavior on that build, with traffic and verification limits.

A shared fix can be merged while one or more apps still ship the defect. An issue can be correctly closed under its marker policy while production delivery remains pending. Record both facts. Do not duplicate a merged fix to repair an adoption gap.

For example, a merged PDF fix in `packages` can make a GO incident eligible for `telemetry-fixed` under GO's policy even while GO's packages pin lacks it. Put package adoption/release in the delivery follow-ups and report runtime verification as pending. A later app release without the updated pin may legitimately cause the incident to reopen.

## Read the destination policy

Use current destination `AGENTS.md`/`CLAUDE.md`, telemetry source configuration and explicit user decisions. Earlier assistant advice and generic runner runbooks may conflict with newer destination rules. Explain a material unresolved conflict instead of inventing a fleet-wide closure threshold.

The Pocket Manage GO, Crew/Installers and Partner repositories establish the merged-fix marker rule. The owner also established a seven-day quiet criterion during the three-app review:

| Marker / disposition | Meaning |
| --- | --- |
| `telemetry-fixed` | Apply when the matching fix PR has merged. Release/deployment is not a prerequisite. |
| `wontfix` | An intentional decision not to act. It permanently suppresses reopening regardless of version. |
| Quiet for at least seven days | Owner-established closure criterion for these app incidents, after checking actual sightings and coverage. It does not establish a causal code fix. |

These criteria establish eligibility, not permission to mutate a tracker during a report-only task. When asked to clean up/close incidents, honor the authorized criteria and describe the basis of each disposition. The user may explicitly direct a different outcome. Record such a disposition as an owner decision rather than claiming that a diagnosis or runtime fix was proven.

For other services, read their current policy. Do not extend the mobile quiet rule or require production deployment everywhere by assumption.

## Check recurrence correctly

Before recommending quiet-period closure, query the actual fingerprint's sightings through the cutoff, including below-threshold evidence absent from the issue body. Establish that the source had adequate fresh traffic and that the query succeeded. Missing data, stale ingestion, retention gaps and merely old issue-body timestamps cannot establish seven quiet days. A focused verification may require a wider window than the report.

The runner owns incident fingerprints and Fix Floor calculation. For a closed incident on a source declaring `versionedReleases`, `telemetry-fixed` freezes a telemetry-derived version floor. At/below-floor recurrence stays closed; newer-build recurrence can reopen it. Do not substitute the latest store version or manually invent a floor.

Check actual version attribution. Missing/unparseable builds cannot support safe version suppression. Host-emitted exceptions and inbound request records can lack the worker's application version even when the source declares versioned releases. Report that limitation; permanent suppression is an intentional owner choice, not an automatic workaround for missing versions.

In the current runner, inbound request incidents from `AppRequests` are unversioned and reopen on their next recurrence despite `telemetry-fixed`. The source's `versionedReleases=true` does not change this. For these incidents, report the HTTP recurrence directly rather than leaving floor comparison as an unresolved eligibility question.

Within one repository, the same incident may contain staging and production evidence. Keep environments visible. Across repositories, related failures form a workstream while retaining their own identities. `telemetry-fixed` is a marker, not a canonical triage state. Keep the existing `triage` skill and repository label mapping responsible for category/state transitions and briefs.

## Act only on the requested work

For an authorized tracker update, refresh issue state, exact matching fix and marker semantics immediately before writing. Follow current model/harness attribution instructions rather than an older generic AI disclaimer. Closing because of silence, a merged fix or an owner decision must say which basis applies.

For an authorized remediation, choose the actual repair owner and inspect current work to avoid parallel duplicate fixes. Importance, cross-repository scope or sensitive code does not alone make work human-only; name the actual missing access, migration, judgment or verification requirement. Preserve previously declined checks unless new evidence changes their relevance.

Report implementation tests, build results, deployment and runtime verification separately. A green build is not a passing suite, and neither proves production recovery. A report refresh or invocation of this skill does not create a schedule, start a publishing triage job, merge a PR or deploy a release. A manual triage-job start can publish real issues; use the service's current runbook for explicitly requested operational runs.
