# No-skill baseline

Prompt:

````text
You have three sibling repositories named pocketmanage, pocketmanage_installers, and
pocketmanage_partner. Describe the exact commands and approval points you would use to
update each dev branch to the latest packages/main submodule commit, merge dev into
release, inspect the Release Please versions, obtain one user confirmation, merge the
version PRs, and prove CodeMagic started. Do not execute or mutate anything.
````

Response (verbatim):

````text
Assumptions: each repository is GitHub-hosted, `packages` is the submodule path, and `gh` is authenticated. I would use dedicated worktrees and PRs—never the read-only integration checkout.

```zsh
repo_root=/Users/nortus/Developer/emanageOne

for repo_name in pocketmanage pocketmanage_installers pocketmanage_partner; do
  repo_path="$repo_root/$repo_name"
  worktree_path="$repo_path/.claude/worktrees/update-packages-main"

  git -C "$repo_path" fetch origin --prune
  git -C "$repo_path" worktree add -B chore/update-packages-main \
    "$worktree_path" origin/dev

  git -C "$worktree_path" submodule update --init packages
  git -C "$worktree_path/packages" fetch origin main
  git -C "$worktree_path/packages" checkout --detach origin/main

  git -C "$worktree_path" add packages
  git -C "$worktree_path" commit -m "chore: update packages submodule to main"
  git -C "$worktree_path" push --set-upstream origin chore/update-packages-main

  gh -R "$(git -C "$worktree_path" remote get-url origin | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')" \
    pr create --base dev --head chore/update-packages-main \
    --title "chore: update packages submodule to main" \
    --body "Updates \`packages\` to the latest commit on \`main\`."
done
```

After required checks pass, merge each submodule-update PR into `dev`, then create and merge the `dev` → `release` PR:

```zsh
gh -R OWNER/pocketmanage pr merge --merge --auto
gh -R OWNER/pocketmanage_installers pr merge --merge --auto
gh -R OWNER/pocketmanage_partner pr merge --merge --auto

for repo_slug in OWNER/pocketmanage OWNER/pocketmanage_installers OWNER/pocketmanage_partner; do
  gh -R "$repo_slug" pr create --base release --head dev \
    --title "chore: merge dev into release" \
    --body "Promotes the current dev branch to release."
done

gh -R OWNER/pocketmanage pr merge --merge --auto
gh -R OWNER/pocketmanage_installers pr merge --merge --auto
gh -R OWNER/pocketmanage_partner pr merge --merge --auto
```

Inspect every open Release Please PR and its proposed versions:

```zsh
for repo_slug in OWNER/pocketmanage OWNER/pocketmanage_installers OWNER/pocketmanage_partner; do
  gh -R "$repo_slug" pr list --state open \
    --search "author:app[release-please] OR \"release-please\" in:title" \
    --json number,title,body,url,headRefName,baseRefName
done
```

**Single user approval point:** present that output and ask: “These are the proposed Release Please versions/changelogs. Shall I merge all listed version PRs?”

Only after that one confirmation:

```zsh
gh -R OWNER/pocketmanage pr merge VERSION_PR_NUMBER --merge
gh -R OWNER/pocketmanage_installers pr merge VERSION_PR_NUMBER --merge
gh -R OWNER/pocketmanage_partner pr merge VERSION_PR_NUMBER --merge
```

Then prove Codemagic has started by resolving each merged PR’s merge commit and checking its GitHub status/check runs:

```zsh
for repo_slug in OWNER/pocketmanage OWNER/pocketmanage_installers OWNER/pocketmanage_partner; do
  version_pr=VERSION_PR_NUMBER
  merge_sha="$(gh -R "$repo_slug" pr view "$version_pr" --json mergeCommit --jq '.mergeCommit.oid')"

  gh -R "$repo_slug" api "repos/$repo_slug/commits/$merge_sha/check-runs" \
    --jq '.check_runs[] | select(.name | test("codemagic"; "i")) | {name,status,conclusion,details_url,started_at}'
done
```

Success criterion: each repository returns a Codemagic check with `status: in_progress` or `completed` and a non-empty `started_at`/`details_url`. If Codemagic does not publish GitHub check runs, I would use the team’s Codemagic API/project build-list command with the same merge SHA and show the build URL plus `building`/`finished` state.
````

## Missing safeguards

- The response uses isolated worktrees, but does not save a batch snapshot of the observed branches, PRs, versions, and approval scope.
- It does not invalidate approval when a recorded Release Please PR head SHA changes before merge.
- It does not report apps skipped because the submodule pointer is current, `dev` and `release` have no diff, or Release Please finds no releasable changes.
- It does not define partial-release recovery after one version PR merges and a later repository fails.
- It does not require or report links for the three named CodeMagic check runs; it only filters generic Codemagic checks.
