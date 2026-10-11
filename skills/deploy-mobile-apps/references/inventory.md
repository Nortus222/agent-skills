# Adding an app

Add an entry to `apps.json` with a selection key, display name, GitHub repository,
and local directory. Give each app its exact `staging_checks` and
`codemagic_checks` workflow display names. The first list observes staging
publishing; the second observes production publishing. Set `runbook` to a
repository-relative publishing document when available.

All apps use the same workflow: update packages from its configured branch,
publish staging, prepare production promotion, approve the saved Release Please
heads, merge those heads, and observe production builds. App fields override
`defaults` for branch names, submodule settings, and release metadata. Leave store
credentials, destinations, version allocation, and build counters in each
repository's CI and external configuration.

Every preflight names each selected app with `--app`. Repeat the flag to select a
batch; there is no default selection. A single app can use `--repository <path>`
for a specific checkout. Otherwise the workspace setting discovers sibling
repositories by origin URL.

Before enabling an app, verify its existing staging branch can fast-forward to
the prepared dev source, its staging and release workflows exist, and the packages
checkout is usable. Its release branch must contain Release Please configuration
and the production workflows before the shared preflight can pass.
The deployment worktree directory must be ignored by that repository. Diverged
staging requires review and reconciliation before deployment; keep the shared
branch guards in place.

Run regressions from the repository root:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

Exercise an explicit `preflight --app <key> --dry-run`, then inspect its recorded
branches, latest packages pointer, and check names. This does not verify signing,
webhooks, or store delivery. Validate metadata with the installed skill-creator
validator when available and check linked reference paths:

```bash
python3 <skill-creator-directory>/scripts/quick_validate.py skills/deploy-mobile-apps
```
