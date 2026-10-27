# Collect and interpret evidence

## Discover the actual system

Read current `AGENTS.md`/`CLAUDE.md`, the effective registry, telemetry package guidance and deployment configuration. Integration checkouts may be months behind their remote-tracking refs. Fetch when needed, then inspect a pinned ref without switching or editing the integration checkout. The registry's `triageBranch` identifies assessment context; the repository's instructions identify its PR target.

For e-manage|ONE, start with `telemetry-triage/config/application-routes.json`, the registry actually loaded by the deployed job, and `packages/emws_telemetry/CLAUDE.md`. Resolve local checkouts by repository identity:

| Name | Repository | Evidence/ownership |
| --- | --- | --- |
| GO | `MarketplaceSoftware/pocketmanage` | App behavior and its packages pin |
| Crew / Installers | `MarketplaceSoftware/pocketmanage_installers` | App behavior and its own packages pin |
| Partner | `MarketplaceSoftware/pocketmanage_partner` | App behavior and its own packages pin |
| Shared Flutter code | `MarketplaceSoftware/packages` | Telemetry capture and shared app/API behavior |
| EMWS | `MarketplaceSoftware/EMWS` | API behavior and telemetry ingestion |
| Shared services | `MarketplaceSoftware/emws_shared` | Middleware and shared backend behavior |
| Other services | Current registry and requested service repositories | Dedicated components, routes and deployments |

The mobile package uses custom Application Insights events plus native termination evidence from Android `ApplicationExitInfo` and iOS MetricKit. Firebase Hosting or Messaging configuration does not establish Crashlytics reporting. Verify the current implementation before selecting a crash source. Web evidence cannot establish native coverage, and delayed native recovery events need their original occurrence time distinguished from ingestion time.

Discover a capable connected tool first. For Azure CLI fallback, read installed command help, request JSON and preserve the exact query privately. Application Insights queries need both `--start-time` and `--end-time`; the CLI's default offset can silently narrow an otherwise correct KQL interval. Workspace queries need an explicit timespan and component/resource restriction. Inspect returned timestamps, table schema and result completeness before relying on aggregates. Classic and workspace schemas use different table/column names.

An interactive review may gather live telemetry within the user's scope. When working as an automated Phase 2 assessor, keep the configured Evidence Packet, read set and verification capability boundary. Loading this skill does not expand that profile or authorize executing destination code.

## Bound the evidence

Record the selected source/resource, app key, environment, platform, build, query bounds and retrieval time. Check recent app-open/login/screen/API activity or server traffic alongside failures. Inventory busy production routes absent from staging when assessing release readiness. Zero errors on an unexercised path do not verify the path.

Use the same population and interval for a numerator and denominator. Label raw rows, sampling-weighted occurrence estimates, API attempts, distinct operations, sessions and users separately. Keep lifetime incident totals apart from review-window results. Correlated exceptions and wrapper fingerprints can describe the same operation; similarity alone does not prove overlap.

For a refresh, preserve the original snapshot's cutoff or explicitly label a new one. For post-deploy verification, record deployment time, deployed commit and comparable pre/post cohorts. Existing errors, newly observed errors and release-caused regressions are different findings. Report a release recommendation separately from the actionable incident list.

## Interpret the contract

| Observation | Required interpretation |
| --- | --- |
| HTTP 500 with invocation `Success=true` | Count the HTTP failure independently. Function completion is a separate outcome. |
| Blank/zero HTTP status | The request has no recorded response; transport type, correlation or reproduction is needed to identify why. |
| 202 and a queued body | Accepted/pending. Verify eventual outcome separately. |
| Handled business-rule 400 | Check the exact route, error code and client handling. Only recognized flow-control outcomes are expected. |
| Caller cancellation, 401 challenge, IMDS probe or cache fallback | Apply only the verified narrow classification. Keep unexpected failures and user-visible impact observable. |
| A sanitized endpoint containing `{id}` | Compare the real route contract and source interpolation before diagnosing a literal-placeholder bug. |
| A nearby screen event | It does not establish which screen owned a request. Check operation and source ownership. |

Inspect metric definitions before treating timings as UX measurements. Mobile `screen_ready` is the first qualifying tracked load, potentially a global bloc. Its `bloc`, `pending`, cold/warm and lifecycle cohorts matter. A first-load duration is a lower bound; even `pending=0` does not prove all screen content finished. Preserve that contract unless complete-screen readiness is explicitly in scope. Long wall-clock durations may include suspension or clock changes. Monotonic timing alone does not remove suspension. Keep useful failures/outliers while improving interpretation.

Exclude known local/development builds from incident populations using current ingestion/capture rules; an environment string alone is insufficient. Keep production instrumentation limited to useful monitoring events and gate detailed diagnostics to testing when applicable. Preserve request behavior while correcting telemetry classification.

Use aggregate or sanitized evidence in reports. Retain necessary authorized correlation context privately. Payload logging is service-specific: a historical approval for bounded QBO failure payloads is not permission to export bodies from another service. Preserve billing identity dimensions, and use deterministic accounting evidence for billing rather than treating sampled operational telemetry as a ledger.
