---
name: deploy-mobile-apps
description: "Use when asked to deploy, release, or push updates to the managed Pocket Manage mobile applications."
---

# Deploy mobile apps

Coordinate the configured repositories as one saved batch. Read
`references/apps.json` for the inventory. Never substitute remembered repository or
check names.

## Command sequence

Run from this skill directory. Use `--app <key>` once per requested app only when the
request names a subset. Report the partial-batch warning before `prepare`.

```bash
python scripts/mobile_release.py preflight --json
python scripts/mobile_release.py prepare --batch <batch-id> --json
```

Promotion and release merges carry administrator privileges, because the `release`
branch requires an approving review that the release account cannot give its own pull
request. The matched head commit stays the only approval boundary.

Stop when the state is `awaiting-approval`. Print one summary with these fields for
every selected app: app, proposed version, Release Please PR number and URL, checks,
head SHA, submodule SHA, and result or skip reason. Also print the batch ID, state,
warnings, preserved worktree paths, and next command.

An app skipped for `no releasable changes` carries `unreleased_commits`: the commits
this batch promoted that produced no version. List them under that app and ask the
user to approve the skip. A commit describing user-visible work is a `feat` or a `fix`
typed as something else, and the fix is a corrected commit, not an approved skip.

Ask for one explicit confirmation of that batch. Confirmation covers only the shown
PR numbers, versions, head SHAs, and approved skips. Do not run `release` in the same response that
asks for confirmation.

After the user confirms, run exactly:

```bash
python scripts/mobile_release.py release --batch <batch-id> --json
```

Release ends after all configured CodeMagic check runs are detected. Report each
check name, state, and URL. A failed run still counts as detected. Do not wait for
the builds to finish.

## Quick reference

| State or event | Response |
| --- | --- |
| `awaiting-approval` | Show the complete batch summary and pause. |
| Approval snapshot changed | Refuse the changed Release Please head, show the refreshed summary, and request new confirmation. |
| App skipped | Keep it in the summary with its recorded reason. Never merge it. |
| `no releasable changes` | Show `unreleased_commits` and get the skip approved before `release`. |
| `partial-release` | Name merged and remaining apps, then resume the same batch with `release` only after reporting the failure. |
| CodeMagic timeout | Report `released-builds-unverified`, tag and commit URLs, and seen or missing checks. Resume observation with `status`. |
| Preserved worktree | Report its path and leave it untouched. |

Use `python scripts/mobile_release.py status --batch <batch-id> --json` for read-only
recovery and observation. On any command failure, report completed actions, the saved
state, warnings, and the printed safe resume command.

## Common mistakes

- Treating one confirmation as approval for a changed PR head or a new batch.
- Omitting skipped apps, preserved worktrees, or checks that already failed.
- Running `prepare` or `release` while merely explaining the process.
- Claiming release success before the tag, GitHub release, and every configured
  CodeMagic check link are recorded.
