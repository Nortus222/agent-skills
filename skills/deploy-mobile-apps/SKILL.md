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
track for each app. When an app commits `pubspec.lock`, the bump runs `flutter pub get`
and commits the regenerated lockfile with the pointer, so `flutter` must be on `PATH`.
Land the staging CI changes and create protected staging
branches with their webhooks before running a real `prepare`. A dry run only plans
these steps; it does not verify those prerequisites or start builds.

Add `--staging-only` to stop once staging carries the prepared commit, so a
TestFlight build proves the change before any of it reaches `release`:

```bash
python scripts/mobile_release.py prepare --batch <batch-id> --staging-only --json
```

That rests at `staging-complete` and reports the staging builds. Resume the same
batch with a plain `prepare` to promote it; the submodule bump is not repeated.
Resumed `prepare` refreshes the checks and diagnosis for each saved staging SHA
before promotion. A failed staging check blocks promotion. `release` refreshes
the same fields for its report without replacing the release result.
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
| codemagic.yaml rejected in preflight | Report the named keys and fix them before pushing. Codemagic requires a non-empty string, int, float or bool; a rejected configuration starts no build and reports no failure. |
| `staging-complete` | Report each staging check and any diagnosis, then pause for the human to judge the TestFlight build. |
| Staging build failed | Name the failing step and its log tail from `staging_diagnosis`. Fix the cause and stage again; never promote a red staging build. |
| Staging check absent | Run `status` to refresh the saved staging SHA's checks and read `staging_diagnosis.missing`. Queued or cancelled builds may register no check run; the diagnosis queries builds for the matched app and staging branch. |
| No CodeMagic application matched | The lookup uses the exact GitHub repository in `/apps`, normalizing case, SSH URLs, and `.git`. Verify that the token can see that repository's app and its repository URL is correct, then rerun `status`. App names and repository prefixes do not match. |
| Git authentication failed | Load the intended SSH key with `ssh-add <key-path>` and retry, or verify the account with `gh auth status --hostname github.com` and explicitly opt into `--git-auth gh`. Promotion and release merges use admin privileges. |
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

`status` refreshes staging checks, missing checks, failed checks, and diagnosis for
each saved staging SHA. It leaves the saved batch and remote state unchanged.

For SSH authentication failures such as `Permission denied (publickey)`, the error
includes recovery advice even when preflight has not saved a batch. Load the
intended SSH key, or check `gh auth status --hostname github.com` and use:

```bash
python scripts/mobile_release.py preflight --git-auth gh --json
python scripts/mobile_release.py prepare --batch <batch-id> --git-auth gh --json
```

Repeat `--git-auth gh` on each command that needs it, including `release` or
`status`. It routes `git@github.com:` remotes over HTTPS and resets the GitHub
credential helper to `gh auth git-credential` through environment-only git config.
Existing environment config is preserved. The flag applies only to the script's
git subprocesses and their children; it changes no saved git configuration.
The gh account may differ from the SSH identity, so verify it before opting in.
Without the flag, git uses the current authentication configuration.

## Common mistakes

- Treating one confirmation as approval for a changed PR head or a new batch.
- Omitting skipped apps, preserved worktrees, or checks that already failed.
- Running `prepare` or `release` while merely explaining the process.
- Claiming release success before the tag, GitHub release, and every configured
  CodeMagic check link are recorded.
