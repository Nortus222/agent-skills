# Adding an app

Add the app to `apps.json`. The inventory is the source of repository identities,
branch names, workflow policy, and check names. Inspect the repository's current
instructions and CI before selecting a policy.

- `managed-release` updates the shared packages pointer and automates staging and
  production promotion with guarded administrator merges. Use it only for
  repositories that authorize that behavior.
- `pinned-staging` keeps the selected source's packages pointer, prepares a
  candidate with an identical tree, and pushes staging only. Production merges
  remain with the owner. It currently supports one app per batch.

App fields override `defaults`. Each app may specify `staging_checks` and
`codemagic_checks`; otherwise it uses the inventory-wide lists. Match workflow
display names exactly, including their staging or production channel. Use
`runbook` for a repository-relative path to its deployment contract.
`release_notes_file` identifies the notes file required by a pinned-staging app
and the only file an optional source commit may change above merged dev.

`default_apps` selects the apps deployed when `--app` is omitted. Keep new apps
explicit until their inclusion in the default batch is requested. Different
workflow policies require separate batches. The existing workspace-root setting
discovers sibling repositories by their origin URL. Use `--repository <path>`
for an explicit single-app pinned-staging worktree; its origin must match.

Add local Git regression fixtures for a new policy's source, promotion, and
recovery invariants. Run from the repository root:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

Then exercise a read-only `preflight --dry-run` with the selected app and inspect
the resulting source and check names. This verifies local configuration and
repository discovery; it does not verify signing, webhooks, or store delivery.

Validate metadata with the installed skill-creator validator when available:

```bash
python3 <skill-creator-directory>/scripts/quick_validate.py skills/deploy-mobile-apps
```

Check linked reference paths as well. Metadata validation does not replace the
release-script regressions or readiness checks in the app's runbook.
