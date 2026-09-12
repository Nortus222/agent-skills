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

`prepare` bumps the submodule on `dev`, fast-forwards the existing `staging` branch
to that commit, then promotes `staging` to `release`. The staging push starts
CodeMagic builds publishing to the TestFlight internal dev group and Play internal
track for each app. Land the staging CI changes and create protected staging
branches with their webhooks before running a real `prepare`. A dry run only plans
these steps; it does not verify those prerequisites or start builds.

Add `--staging-only` to stop once staging carries the prepared commit, so a
TestFlight build proves the change before any of it reaches `release`:

```bash
python scripts/mobile_release.py prepare --batch <batch-id> --staging-only --json
```

That rests at `staging-complete` and reports the staging builds. Resume the same
batch with a plain `prepare` to promote it; the submodule bump is not repeated.
Prefer `--staging-only` whenever the batch carries a change no store build has
exercised yet.

Promotion and release merges carry administrator privileges, because the `release`
branch requires an approving review that the release account cannot give its own pull
request. The matched head commit stays the only approval boundary.

Stop when the state is `awaiting-approval`. Print one summary with these fields for
every selected app: app, proposed version, Release Please PR number and URL, checks,
head SHA, staging head SHA, submodule SHA, and result or skip reason. Also print the batch ID, state,
branch flow `dev → staging → release`, warnings, preserved worktree paths, and next command.

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

Release ends once a CodeMagic build has started. Report each detected check name,
state, and URL, and name any configured check that had not registered yet. A failed
run still counts as detected. Do not wait for the builds to finish.

## Quick reference

| State or event | Response |
| --- | --- |
| `release is ahead of dev` warning | Report the drift and offer a back-merge pull request from `release` into `dev`. The human decides; never merge into `dev` unasked. |
| `staging-complete` | Report each staging check and any diagnosis, then pause for the human to judge the TestFlight build. |
| Staging build failed | Name the failing step and its log tail from `staging_diagnosis`. Fix the cause and stage again; never promote a red staging build. |
| Staging check absent | Read `staging_diagnosis.missing`. A build cancelled before it starts registers no check run, so an absent check is not evidence of a broken trigger. |
| `awaiting-approval` | Show the complete batch summary and pause. |
| Approval snapshot changed | Refuse the changed Release Please head, show the refreshed summary, and request new confirmation. |
| App skipped | Keep it in the summary with its recorded reason. Never merge it. |
| `no releasable changes` | Show `unreleased_commits` and get the skip approved before `release`. |
| `partial-release` | Name merged and remaining apps, then resume the same batch with `release` only after reporting the failure. |
| No build observed | Report `released-builds-unverified`, tag and commit URLs, and the unobserved checks. A queued build registers no check run, so this is not evidence that nothing is building. Resume observation with `status`. |
| Diverged `staging` | Report the saved failure and resume command. A human must investigate direct staging pushes before retrying; never merge or force-push staging. |
| Missing `staging` | Report the missing prerequisite and saved resume command. Land staging CI and configure the branch, protection rules, and webhooks before retrying. |
| Preserved worktree | Report its path and leave it untouched. |

## CodeMagic diagnostics

A failed or absent build is diagnosed through the CodeMagic API: the failing step's
name and the tail of its log, and the builds on the branch when no check run appeared.
The token comes from `CODEMAGIC_API_TOKEN`, else the login keychain entry of the same
name. Without a token the skill still reports every check name and state, and records
that the diagnosis is unavailable.

## Recovery

Use `python scripts/mobile_release.py status --batch <batch-id> --json` for read-only
recovery and observation. On any command failure, report completed actions, the saved
state, warnings, and the printed safe resume command.

## Common mistakes

- Treating one confirmation as approval for a changed PR head or a new batch.
- Omitting skipped apps, preserved worktrees, or checks that already failed.
- Running `prepare` or `release` while merely explaining the process.
- Claiming release success before the tag, GitHub release, and every configured
  CodeMagic check link are recorded.
