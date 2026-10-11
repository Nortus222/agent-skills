---
name: deploy-mobile-apps
description: "Use when asked to deploy or release an inventoried app, including Pocket Manage and GO Measure, or inspect its deployment status."
---

# Deploy apps

Read [references/apps.json](references/apps.json) for the app inventory and select
its `workflow` before running commands. Inspect the selected repository's
instructions, publishing runbook, and CI configuration for destinations and
readiness. Inspection, parity checks, and explanations authorize read-only work.
Deployments use the source, channel, and destinations the user authorized.

## Select the workflow

- `managed-release`, currently the three Pocket Manage apps: read
  [references/managed-release.md](references/managed-release.md). Preparation
  updates packages and starts staging builds before production promotion. Release
  merges the approved Release Please PRs.
- `pinned-staging`, currently GO Measure: read
  [references/pinned-staging.md](references/pinned-staging.md). Preparation keeps
  packages pinned and makes a staging candidate without pushing. Release pushes
  that candidate to staging. Production PR merges stay with the owner.

Run commands from this skill directory. The default batch remains the three
Pocket Manage apps. Select GO Measure explicitly with `--app go-measure`.
Mixed workflows require separate batches; a pinned-staging batch selects one app.
Report the partial-batch warning before preparing a subset of the default Pocket
Manage batch.

Use `status --batch <batch-id> --json` for read-only recovery. Report the saved
source, observed checks and URLs, missing checks, preserved worktrees, and next
command. Use `--git-auth gh` on each command only when the intended GitHub
account has been checked and HTTPS credential routing is needed; see the auth
recovery section in [references/managed-release.md](references/managed-release.md).
A registered build establishes startup; report publishing and store
availability separately. Preserve any worktree named in the receipt.

When adding an app or changing its policy, read
[references/inventory.md](references/inventory.md). Run the Python regression
suite from the repository root and validate this skill before installing it.
