# Deploy mobile apps skill design

## Goal

Create a personal skill that coordinates releases for these repositories:

| App | GitHub repository | Development branch | Release branch |
| --- | --- | --- | --- |
| Pocket Manage | `MarketplaceSoftware/pocketmanage` | `dev` | `release` |
| Pocket Manage Installers | `MarketplaceSoftware/pocketmanage_installers` | `dev` | `release` |
| Pocket Manage Partner | `MarketplaceSoftware/pocketmanage_partner` | `dev` | `release` |

Each app uses the `packages` submodule from `MarketplaceSoftware/packages`, with `main` as the source branch. Release Please opens the version PR. Merging that PR creates a `v<version>` tag, which triggers the Android, iOS, and web workflows in CodeMagic.

The first version stops after it verifies that CodeMagic has detected the tag builds. A later version may wait for every build to finish.

## Package

Add `skills/deploy-mobile-apps/` to `Nortus222/agent-skills`. Installing the repository places the skill at `~/.agents/skills/deploy-mobile-apps`.

The skill contains:

- `SKILL.md`, which controls the conversation and the human approval gate.
- `agents/openai.yaml`, which supplies skill-list metadata.
- `references/apps.json`, which records repository names, branches, submodule settings, and expected CodeMagic checks.
- `scripts/mobile_release.py`, which runs the state machine through `git` and the authenticated GitHub CLI.

Repository-level tests cover the script without changing live repositories.

## Invocation

Make the skill model-invoked. Its description should match requests to deploy, release, or push updates to the managed mobile apps. The description names triggers only. The full process remains in `SKILL.md`.

The first release set contains all three apps by default. The script may support a selected subset when the user names specific apps, but it must report that the batch is partial before making changes.

## State and commands

Store batch files under `~/.local/state/deploy-mobile-apps/`. A batch file contains no credentials. It records:

- A unique batch ID and creation time.
- The selected application set.
- Remote branch and submodule SHAs observed at each phase.
- Preparation and Release Please PR numbers, URLs, head SHAs, and check results.
- Proposed versions, tag SHAs, CodeMagic check states, and build URLs.
- Skips, failures, completed actions, and the next legal phase.

Expose phase-oriented commands rather than one unattended command:

- `preflight` inspects every selected app and creates the batch.
- `prepare` updates `dev`, promotes it to `release`, and waits for version PRs.
- `release` accepts a batch ID after user confirmation, revalidates the approval snapshot, and merges version PRs.
- `status` resumes observation and prints the current batch summary without changing remote state.

Every mutating command supports `--dry-run`. Structured JSON output is available for the skill, while the default output stays readable for a person.

## Phase 1: preflight

Complete preflight for every selected app before the first write.

1. Verify `gh` authentication and repository permissions.
2. Locate each local repository by matching its `origin` URL. Use a configurable workspace root instead of an absolute checkout path.
3. Fetch `origin/dev`, `origin/release`, and `packages` `origin/main`.
4. Verify that configured branches, the submodule, Release Please files, and CodeMagic workflow names exist.
5. Record exact remote SHAs.
6. Reject ambiguous repository matches, conflicting deployment worktrees, or a conflicting open `dev` to `release` PR.

Preflight does not require clean integration checkouts because later work runs in isolated worktrees. It must not modify those checkouts.

## Phase 2: prepare development branches

For each app:

1. Create a temporary branch and worktree from the recorded `origin/dev` SHA.
2. Initialize `packages` in that worktree.
3. Check out the fetched `packages` `origin/main` SHA in detached mode.
4. Stage only the `packages` gitlink.
5. If the pointer changed, commit it and push `HEAD:dev` without force. A concurrent update must reject the push and stop the batch.
6. If the pointer was current, record a no-op and continue.

Use a clear commit subject such as `chore: update shared packages`. Remove a temporary worktree only when it is clean. Preserve and report any uncertain worktree.

## Phase 3: promote to release

For each prepared app:

1. Create or reuse the exact `dev` to `release` PR.
2. Wait for required GitHub checks.
3. Merge the PR automatically with a merge commit. Keep `dev`. If repository policy rejects merge commits, stop instead of choosing another strategy.
4. If `dev` and `release` have no diff, mark the app skipped and continue.

A failed check, merge conflict, unexpected PR head, branch-protection failure, or changed remote SHA stops the affected phase. The report must name the app, failed condition, completed actions, and safe resume command.

## Phase 4: discover proposed versions

After preparation PRs merge:

1. Wait for the `release-please` GitHub Actions run triggered by the new `release` commit.
2. Find the open PR with the `autorelease: pending` label and `release` base.
3. Read the proposed version from `.release-please-manifest.json` at the PR head SHA.
4. Record the PR number, URL, version, checks, head SHA, and submodule SHA.
5. If Release Please produces no PR, mark the app `no releasable changes` and continue.

The completion criterion is a batch in `awaiting-approval` state containing either a complete approval snapshot or a skip reason for every app.

## Human approval gate

The skill prints one batch summary with:

| App | Proposed version | Release PR | Checks | Head SHA | Result |
| --- | --- | --- | --- | --- | --- |

It also lists the shared submodule SHA and every skip or warning. The skill then stops and asks for one explicit batch confirmation.

Confirmation authorizes only the Release Please PR numbers, versions, and head SHAs shown in that summary. It does not authorize a changed PR or a new release batch.

## Phase 5: release

After confirmation, run `release` with the saved batch ID.

1. Reload the approval snapshot.
2. Re-fetch every included Release Please PR.
3. Verify that it remains open, targets `release`, carries the expected label, has passing required checks, proposes the recorded version, and has the recorded head SHA.
4. Invalidate approval if any value changed. Print a refreshed summary and return to `awaiting-approval`.
5. Merge each unchanged Release Please PR.
6. Record every successful merge before attempting the next app.

GitHub cannot merge three repositories atomically. If a later merge fails, mark the batch `partial-release`, stop, and report which versions merged and which remain. A retry must verify remote state and continue from the first unfinished app.

## Phase 6: verify tags and CodeMagic startup

For every merged release:

1. Verify that `v<version>` exists and resolves to the expected release commit.
2. Verify that the matching GitHub release exists.
3. Poll the tagged commit's check runs until the expected `Codemagic CI/CD` checks appear:
   - `Build Android AppBundle and Publish`
   - `Build IPA and Publish To AppStore Connect`
   - `Build Web and Publish to Firebase Hosting`
4. Record each check's status, conclusion if already complete, and `details_url`.

Use a bounded timeout. A timeout does not undo a successful release. Mark build startup `unverified` and link the tag and commit so the user can inspect them.

The first version reports initial CodeMagic states and stops. It does not wait for all builds to complete.

## Recovery rules

- Stop the whole batch when preflight fails before any write.
- Record each completed remote mutation before attempting the next one.
- Derive retry behavior from verified GitHub and Git state, not only the local batch file.
- Reuse exact existing PRs. Never open duplicates to bypass an unexpected state.
- Never force-push, delete `dev`, merge a conflicted PR, or resolve unrelated repository changes.
- Treat skipped apps as nonblocking and include them in approval and final reports.
- Preserve dirty or uncertain temporary worktrees and print their paths.

## Tests

Inject command execution, time, and polling so unit tests use fixtures rather than live GitHub repositories. Cover at least:

- Repository discovery and ambiguous remotes.
- Latest and stale submodule pointers.
- Direct `dev` push success and rejection.
- Preparation PR creation, reuse, no-diff skip, and required-check failure.
- Release Please polling and version extraction.
- Batch summaries with releasable and skipped apps.
- Approval revalidation and stale-head rejection.
- Partial multi-repository release recovery.
- Release tag and GitHub release verification.
- CodeMagic check discovery and timeout.
- Idempotent `status` and resume behavior.

Validate `SKILL.md` with the skill repository's validator or the skill-creator validator. Run the script's unit tests and a dry-run against the three configured repositories. The dry-run may fetch and query GitHub but must not push, create PRs, or merge anything.

## Out of scope

- Waiting for Android, iOS, and web builds to finish.
- Retrying or fixing failed CodeMagic builds.
- Store approval, phased rollout, or release-note editing.
- Changing GitHub Actions, Release Please, or CodeMagic configuration in the app repositories.
- Managing mobile apps beyond the initial three until they are added to `apps.json`.
