# Pinned staging deployments

Use this workflow for apps whose reviewed source and packages pointer must remain
unchanged during promotion. The helper publishes staging only. Read the selected
app's repository instructions and `runbook` before preparing a batch.
For GO Measure that is `docs/codemagic-publishing.md` in its repository.

## Prepare the exact source

Inspect pending dev changes, staging-only history, release notes, and recent
publishing activity. GO Measure staging publishes Play internal testing, external
TestFlight, and Firebase staging. Check signing readiness, store build allocation,
and upload serialization in its runbook. Keep packages, signing configuration,
and destinations unchanged.

Run from the shared skill directory:

```bash
python3 scripts/mobile_release.py preflight --app go-measure --repository <task-worktree> --json
python3 scripts/mobile_release.py prepare --batch <batch-id> --json
```

The default source is merged `origin/dev`. When release notes need updating,
commit them on a feature branch from that source and open the repository's feature
PR. `preflight --source <full-sha>` permits a notes-only commit above the
snapshotted dev source. Review the notes content before publishing.

Preflight records dev, staging, production release, source, packages, and notes.
Prepare preserves staging history and checks that the candidate tree equals the
selected source. It stops at `awaiting-approval` without pushing. Staging-only
content or conflicts require investigation; retain the reported worktree.

Show the batch ID, source SHA, previous staging SHA, candidate SHA, packages SHA,
pending scope, notes PR if any, destinations, warnings, and outstanding checks.
Reuse an explicit staging deployment approval for the same source and
destinations. If that scope lacks authorization, ask for approval of the concrete
batch and wait before running the next command.

```bash
python3 scripts/mobile_release.py release --batch <batch-id> --json
```

Here `release` pushes the prepared candidate to staging. It never merges a
production PR or creates a version tag. Changed dev or staging snapshots block
the push and require a fresh review and batch. No force push is used.

## Observe and recover

Report the pushed SHA, every configured staging check's state and URL, and missing
checks. A failed check counts as registered, not as successful publishing. If no
build registers in the bounded observation period, report `pushed; builds
unverified`. Queued builds may register no GitHub check yet.

```bash
python3 scripts/mobile_release.py status --batch <batch-id> --json
```

Status observes the saved deployment SHA even after staging advances or the task
worktree disappears. It leaves saved state and remote refs unchanged. Report a
newer staging SHA separately. An uncertain push retains its attempted candidate
receipt; inspect it without starting another deployment to make checks appear.

If the request includes deployed-app verification, continue observing the
requested workflows and distinguish build, publishing, and store processing.
For GO Measure web verification, follow its preview runbook and compare live
`release.json` with the expected source and backend. TestFlight upload alone does
not establish beta review approval or tester access.

## Prepare production for the owner

The helper's staging command does not automate production promotion. Follow the
app's runbook. For GO Measure the owner merges staging into release, then merges
the Release Please version PR to create the stable tag.

Prepare those PRs only when requested. Before handoff, show source, version,
packages SHA, PR URLs and heads, checks, production destinations, and outstanding
readiness. Leave merges and tags to the owner. After the owner merges, observe
the tag, GitHub release, and configured production checks at that exact commit.

GO Measure's marketing website has its own release-branch push workflow. Include
it only when the requested deployment scope includes the website.
