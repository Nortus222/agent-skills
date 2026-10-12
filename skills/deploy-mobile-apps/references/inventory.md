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

Before enabling an app, verify its staging branch exists, the incoming dev source
contains Release Please configuration and all configured staging/production
workflows, and the packages checkout is usable. The current release branch must
exist; incoming configuration can reach it through the normal promotion.

The deployment worktree directory must be ignored. Staging can fast-forward or
have extra promotion history with the same content as its shared ancestor with
dev. The publisher preserves that history and checks its candidate against the
prepared dev tree. Separate staging content requires review; keep the shared
source, target, and tree guards in place.

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
