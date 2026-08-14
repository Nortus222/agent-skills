# Deploy Mobile Apps Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a tested personal skill that prepares, approves, releases, and verifies CodeMagic startup for Pocket Manage, Pocket Manage Installers, and Pocket Manage Partner.

**Architecture:** A small Python CLI delegates release behavior to an injectable operator library. JSON holds the app inventory and resumable batch state. `SKILL.md` controls the human approval boundary, while the library makes every remote transition explicit, revalidated, and testable with a fake command runner.

**Tech Stack:** Python 3 standard library, `unittest`, `git`, GitHub CLI `gh`, Agent Skills Markdown and YAML.

## Global constraints

- Work only in the `agent-skills` feature worktree. Keep its `main` checkout unchanged.
- Use isolated temporary worktrees for mobile repositories. Never change their integration checkouts.
- Push submodule pointer commits directly to `dev` without force.
- Merge `dev` to `release` preparation PRs automatically with merge commits after checks permit it.
- Require one batch confirmation before merging any Release Please PR.
- Revalidate Release Please PR number, version, label, base, checks, and head SHA after confirmation.
- Continue past apps with no releasable changes and report every skip.
- Stop after CodeMagic check runs appear. Do not wait for Android, iOS, and web completion.
- Store no credentials in configuration or batch files.
- Use only the Python standard library.

---

### Task 1: Baseline scenario and generated skill package

**Files:**
- Create: `tests/skill-scenarios.md`
- Create: `skills/deploy-mobile-apps/SKILL.md`
- Create: `skills/deploy-mobile-apps/agents/openai.yaml`
- Create directory: `skills/deploy-mobile-apps/references/`
- Create directory: `skills/deploy-mobile-apps/scripts/`

**Interfaces:**
- Consumes: `docs/superpowers/specs/2026-08-14-deploy-mobile-apps-skill-design.md`.
- Produces: initialized skill package and a verbatim no-skill baseline for later comparison.

- [ ] **Step 1: Run the RED skill scenario without the new skill**

Dispatch a fresh subagent with no access to the planned skill and this read-only prompt:

```text
You have three sibling repositories named pocketmanage, pocketmanage_installers, and
pocketmanage_partner. Describe the exact commands and approval points you would use to
update each dev branch to the latest packages/main submodule commit, merge dev into
release, inspect the Release Please versions, obtain one user confirmation, merge the
version PRs, and prove CodeMagic started. Do not execute or mutate anything.
```

Capture its response verbatim in `tests/skill-scenarios.md`. List missing behavior factually: isolated worktrees, a saved batch snapshot, stale-SHA invalidation, skip reporting, partial-release recovery, and CodeMagic check-run links. RED requires at least one missing safeguard. If it passes every item, repeat with an interrupted-resume scenario before authoring the skill body.

- [ ] **Step 2: Initialize the skill with the official generator**

```bash
python /Users/nortus/.codex/skills/.system/skill-creator/scripts/init_skill.py \
  deploy-mobile-apps \
  --path skills \
  --resources scripts,references \
  --interface 'display_name=Deploy Mobile Apps' \
  --interface 'short_description=Coordinate guarded mobile app releases' \
  --interface 'default_prompt=Use $deploy-mobile-apps to prepare and release the managed mobile apps.'
```

Expected: the skill directory contains `SKILL.md`, `agents/openai.yaml`, `scripts/`, and `references/`.

- [ ] **Step 3: Keep a valid temporary skill body**

Use only this body until the operator passes its tests:

```markdown
---
name: deploy-mobile-apps
description: "Use when asked to deploy, release, or push updates to the managed Pocket Manage mobile applications."
---

# Deploy mobile apps

Implementation follows after the operator passes its tests.
```

- [ ] **Step 4: Validate and commit the RED baseline**

```bash
python /Users/nortus/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/deploy-mobile-apps
git add tests/skill-scenarios.md skills/deploy-mobile-apps
git commit -m "test: capture mobile release skill baseline"
```

Expected: skill validation passes; the baseline documents at least one missing safeguard.

---

### Task 2: Application inventory and resumable batch state

**Files:**
- Create: `skills/deploy-mobile-apps/references/apps.json`
- Create: `skills/deploy-mobile-apps/scripts/mobile_release_lib.py`
- Create: `tests/test_mobile_release.py`

**Interfaces:**
- Produces: `AppConfig`, `Inventory`, `BatchStore`, `new_batch()`, and `load_inventory(path)`.
- State shape: `schema_version`, `batch_id`, `state`, `created_at`, `selected_apps`, and per-app records under `apps`.

- [ ] **Step 1: Write failing inventory and state tests**

```python
class InventoryTests(unittest.TestCase):
    def test_inventory_defines_the_three_managed_apps(self):
        inventory = load_inventory(SKILL_ROOT / "references/apps.json")
        self.assertEqual(set(inventory.apps), {"pocket-manage", "installers", "partner"})
        for app in inventory.apps.values():
            self.assertEqual(app.dev_branch, "dev")
            self.assertEqual(app.release_branch, "release")
            self.assertEqual(app.submodule_path, "packages")
            self.assertEqual(app.submodule_branch, "main")

    def test_batch_store_round_trips_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BatchStore(Path(directory))
            batch = new_batch(["pocket-manage"], now="2026-08-14T12:00:00Z")
            store.save(batch)
            self.assertEqual(store.load(batch["batch_id"]), batch)
            self.assertNotIn("token", json.dumps(batch).lower())
```

- [ ] **Step 2: Run the tests and verify RED**

```bash
python -m unittest tests.test_mobile_release.InventoryTests -v
```

Expected: import or file-not-found failure.

- [ ] **Step 3: Add the exact inventory**

```json
{
  "workspace_root_env": "EMANAGE_MOBILE_ROOT",
  "default_workspace_root": "~/Developer/emanageOne",
  "codemagic_checks": [
    "Build Android AppBundle and Publish",
    "Build IPA and Publish To AppStore Connect",
    "Build Web and Publish to Firebase Hosting"
  ],
  "apps": {
    "pocket-manage": {"display_name": "Pocket Manage", "repository": "MarketplaceSoftware/pocketmanage", "directory": "pocketmanage"},
    "installers": {"display_name": "Pocket Manage Installers", "repository": "MarketplaceSoftware/pocketmanage_installers", "directory": "pocketmanage_installers"},
    "partner": {"display_name": "Pocket Manage Partner", "repository": "MarketplaceSoftware/pocketmanage_partner", "directory": "pocketmanage_partner"}
  },
  "defaults": {
    "dev_branch": "dev",
    "release_branch": "release",
    "submodule_path": "packages",
    "submodule_branch": "main",
    "release_label": "autorelease: pending",
    "release_manifest": ".release-please-manifest.json"
  }
}
```

- [ ] **Step 4: Implement typed configuration and atomic state writes**

```python
@dataclass(frozen=True)
class AppConfig:
    key: str
    display_name: str
    repository: str
    directory: str
    dev_branch: str
    release_branch: str
    submodule_path: str
    submodule_branch: str
    release_label: str
    release_manifest: str

@dataclass(frozen=True)
class Inventory:
    workspace_root_env: str
    default_workspace_root: str
    codemagic_checks: tuple[str, ...]
    apps: dict[str, AppConfig]

class BatchStore:
    def __init__(self, root: Path) -> None: ...
    def save(self, batch: dict[str, Any]) -> Path: ...
    def load(self, batch_id: str) -> dict[str, Any]: ...

def load_inventory(path: Path) -> Inventory: ...
def new_batch(selected_apps: Sequence[str], now: str | None = None) -> dict[str, Any]: ...
```

Write JSON to a sibling temporary file, then call `os.replace`. Create state directories with mode `0o700` where supported.

- [ ] **Step 5: Verify and commit**

```bash
python -m unittest tests.test_mobile_release.InventoryTests -v
git add skills/deploy-mobile-apps/references/apps.json \
  skills/deploy-mobile-apps/scripts/mobile_release_lib.py tests/test_mobile_release.py
git commit -m "feat: add mobile release inventory and state"
```

Expected: both tests pass.

---

### Task 3: Command boundary and all-app preflight

**Files:**
- Modify: `skills/deploy-mobile-apps/scripts/mobile_release_lib.py`
- Modify: `tests/test_mobile_release.py`

**Interfaces:**
- Produces: `CommandResult`, `Runner`, `SubprocessRunner`, `ReleaseError`, and `ReleaseOperator.preflight(app_keys, dry_run=False)`.
- `preflight` saves a batch only after every selected app passes.

- [ ] **Step 1: Write failing preflight tests with a fake runner**

Create a `FakeRunner` that records `args`, `cwd`, and `mutates`, then returns queued results:

```python
def test_preflight_records_exact_remote_shas_for_every_app(self):
    operator = make_operator(fake_responses=successful_preflight_responses())
    batch = operator.preflight(["pocket-manage", "installers", "partner"])
    self.assertEqual(batch["state"], "preflight-complete")
    self.assertEqual(batch["apps"]["pocket-manage"]["dev_sha"], "d" * 40)
    self.assertEqual(batch["apps"]["pocket-manage"]["packages_sha"], "p" * 40)
    self.assertFalse(any(call.mutates for call in operator.runner.calls))

def test_preflight_failure_saves_no_batch(self):
    operator = make_operator(fake_responses=responses_with_ambiguous_remote())
    with self.assertRaisesRegex(ReleaseError, "ambiguous repository"):
        operator.preflight(["pocket-manage", "installers"])
    self.assertEqual(list(operator.store.root.glob("*.json")), [])
```

- [ ] **Step 2: Run preflight tests and verify RED**

```bash
python -m unittest tests.test_mobile_release.PreflightTests -v
```

Expected: missing runner and operator interfaces.

- [ ] **Step 3: Implement the command boundary**

```python
@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

class Runner(Protocol):
    def run(self, args: Sequence[str], *, cwd: Path | None = None,
            mutates: bool = False) -> CommandResult: ...

class SubprocessRunner:
    def run(self, args, *, cwd=None, mutates=False):
        completed = subprocess.run(
            list(args), cwd=cwd, text=True, capture_output=True, check=False
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)
```

Raise `ReleaseError` with the redacted command, repository, exit code, and stderr. Never include environment values.

- [ ] **Step 4: Implement read-only repository discovery and preflight**

`ReleaseOperator.preflight()` must check `gh` authentication, resolve the workspace root, match normalized `origin` URLs, fetch app refs, query the submodule's `origin/main` SHA without changing its checkout, verify Release Please and CodeMagic files, and inspect open `dev` to `release` PRs. Reject ambiguous repository matches, duplicate PRs, missing configured branches, or missing workflow names.

- [ ] **Step 5: Verify and commit**

```bash
python -m unittest discover -s tests -v
git add skills/deploy-mobile-apps/scripts/mobile_release_lib.py tests/test_mobile_release.py
git commit -m "feat: add guarded mobile release preflight"
```

Expected: all tests pass.

---

### Task 4: Development preparation and automatic promotion PRs

**Files:**
- Modify: `skills/deploy-mobile-apps/scripts/mobile_release_lib.py`
- Modify: `tests/test_mobile_release.py`

**Interfaces:**
- Produces: `ReleaseOperator.prepare(batch_id, dry_run=False)` through the version-discovery boundary.
- Adds per-app fields `worktree`, `submodule_before`, `submodule_after`, `dev_push`, `preparation_pr`, `release_sha`, `status`, and `error`.

- [ ] **Step 1: Write failing preparation tests**

```python
def test_prepare_pushes_only_changed_packages_pointer_to_dev(self):
    operator, batch = prepared_operator(submodule_changed=True)
    result = operator.prepare(batch["batch_id"])
    calls = command_args(operator.runner.calls)
    self.assertIn(["git", "add", "--", "packages"], calls)
    self.assertIn(["git", "push", "origin", "HEAD:dev"], calls)
    self.assertNotIn("--force", flatten_command_args(calls))
    self.assertEqual(result["apps"]["pocket-manage"]["dev_push"], "pushed")

def test_prepare_reports_no_release_changes_as_skip(self):
    operator, batch = prepared_operator(no_dev_release_diff=True)
    result = operator.prepare(batch["batch_id"])
    self.assertEqual(result["apps"]["partner"]["status"], "skipped")
    self.assertEqual(result["apps"]["partner"]["skip_reason"], "no dev to release changes")

def test_prepare_records_rejected_dev_push(self):
    operator, batch = prepared_operator(push_rejected=True)
    with self.assertRaisesRegex(ReleaseError, "dev push rejected"):
        operator.prepare(batch["batch_id"])
    self.assertEqual(operator.store.load(batch["batch_id"])["state"], "prepare-failed")
```

Also test one exact reusable preparation PR, duplicate PR rejection, failed checks, merge conflicts, preserved dirty worktrees, and resume after a completed preparation merge.

- [ ] **Step 2: Run preparation tests and verify RED**

```bash
python -m unittest tests.test_mobile_release.PrepareTests -v
```

Expected: `prepare` is missing.

- [ ] **Step 3: Implement isolated submodule preparation**

Use a unique local branch `deploy-mobile-apps/<batch-id>/<app-key>` and an ignored `.claude/worktrees/` path. Base it on the recorded `origin/dev` SHA, and verify `origin/dev` still matches before push. Route these mutations through calls marked `mutates=True`:

```text
git worktree add -b <temporary-branch> <worktree-path> <dev-sha>
git submodule update --init -- packages
git -C packages checkout --detach <packages-main-sha>
git add -- packages
git commit -m "chore: update shared packages"
git push origin HEAD:dev
```

Skip commit and push when the gitlink is unchanged. Require the staged name list to be empty or exactly `packages`.

- [ ] **Step 4: Implement preparation PR checks and merge**

Use `gh pr list` to reuse one exact open `dev` to `release` PR or `gh pr create` to open it. Observe checks with `gh pr checks`; when there are no checks, require GitHub to report the PR mergeable. Merge with:

```text
gh pr merge <number> --repo <owner/repo> --merge
```

Keep `dev`. Save the batch after each successful remote mutation. Preserve any dirty or uncertain worktree and record its path.

- [ ] **Step 5: Implement dry-run behavior**

Perform all read-only comparisons, record planned mutation commands, set state `dry-run-complete`, and stop before version discovery. Assert that no fake-runner call marked `mutates=True` executed.

- [ ] **Step 6: Verify and commit**

```bash
python -m unittest discover -s tests -v
git add skills/deploy-mobile-apps/scripts/mobile_release_lib.py tests/test_mobile_release.py
git commit -m "feat: prepare and promote mobile app releases"
```

Expected: all tests pass.

---

### Task 5: Release Please discovery and approval snapshots

**Files:**
- Modify: `skills/deploy-mobile-apps/scripts/mobile_release_lib.py`
- Modify: `tests/test_mobile_release.py`

**Interfaces:**
- Produces: `ReleaseOperator.discover_versions(batch)`, `approval_rows(batch)`, and batches in `awaiting-approval` state.
- Approval row fields: app, version, PR number, URL, checks, head SHA, submodule SHA, and result.

- [ ] **Step 1: Write failing discovery and reporting tests**

```python
def test_discover_versions_reads_manifest_at_release_pr_head(self):
    operator, batch = operator_after_preparation()
    result = operator.discover_versions(batch)
    app = result["apps"]["pocket-manage"]
    self.assertEqual(app["version"], "2.7.0")
    self.assertEqual(app["release_pr_head_sha"], "a" * 40)
    self.assertEqual(result["state"], "awaiting-approval")

def test_no_release_pr_is_a_nonblocking_reported_skip(self):
    operator, batch = operator_after_preparation(no_release_pr_for="partner")
    result = operator.discover_versions(batch)
    self.assertEqual(result["apps"]["partner"]["skip_reason"], "no releasable changes")
    self.assertEqual(result["apps"]["installers"]["version"], "1.3.0")
```

- [ ] **Step 2: Run discovery tests and verify RED**

```bash
python -m unittest tests.test_mobile_release.DiscoveryTests -v
```

Expected: discovery interfaces are missing.

- [ ] **Step 3: Implement bounded Release Please polling**

Poll the workflow run for the recorded `release_sha`, then list open PRs with base `release` and label `autorelease: pending`. Require zero or one match. Fetch `.release-please-manifest.json` at the PR head through the GitHub Contents API, base64-decode it, and read key `.`.

Inject a `Clock` protocol with `monotonic()` and `sleep(seconds)`. Put intervals and timeouts in an immutable `PollPolicy` so tests never wait.

- [ ] **Step 4: Implement approval row data**

Return data without terminal codes. Keep full URLs and 40-character SHAs in JSON. Human formatting may show 12-character SHAs while retaining full values in the batch.

Call `discover_versions()` at the end of a successful non-dry-run `prepare()`. The completion criterion for `prepare` is one approval row or explicit skip reason for every selected app.

- [ ] **Step 5: Verify and commit**

```bash
python -m unittest discover -s tests -v
git add skills/deploy-mobile-apps/scripts/mobile_release_lib.py tests/test_mobile_release.py
git commit -m "feat: discover and snapshot proposed app versions"
```

Expected: all tests pass.
### Task 6: Approval revalidation, release merges, and CodeMagic detection

**Files:**
- Modify: `skills/deploy-mobile-apps/scripts/mobile_release_lib.py`
- Modify: `tests/test_mobile_release.py`

**Interfaces:**
- Produces: `ReleaseOperator.release(batch_id)` and `ReleaseOperator.status(batch_id)`.
- Terminal states: `released`, `released-builds-unverified`, `partial-release`, and `release-failed`.

- [ ] **Step 1: Write failing approval and release tests**

```python
def test_changed_release_pr_head_invalidates_batch_approval(self):
    operator, batch = awaiting_approval_operator(current_head="b" * 40)
    with self.assertRaisesRegex(ReleaseError, "approval snapshot changed"):
        operator.release(batch["batch_id"])
    saved = operator.store.load(batch["batch_id"])
    self.assertEqual(saved["state"], "awaiting-approval")
    self.assertFalse(any(call.mutates for call in operator.runner.calls))

def test_partial_release_records_each_success_before_stopping(self):
    operator, batch = awaiting_approval_operator(second_merge_fails=True)
    with self.assertRaisesRegex(ReleaseError, "partial release"):
        operator.release(batch["batch_id"])
    saved = operator.store.load(batch["batch_id"])
    self.assertEqual(saved["state"], "partial-release")
    self.assertEqual(saved["apps"]["pocket-manage"]["release_merge"], "merged")
    self.assertEqual(saved["apps"]["installers"]["release_merge"], "failed")

def test_release_reports_all_codemagic_links(self):
    operator, batch = awaiting_approval_operator(codemagic_checks="queued")
    result = operator.release(batch["batch_id"])
    checks = result["apps"]["pocket-manage"]["codemagic_checks"]
    self.assertEqual(set(checks), set(operator.inventory.codemagic_checks))
    self.assertTrue(all(item["details_url"].startswith("https://codemagic.io/")
                        for item in checks.values()))
```

Also cover changed version, missing label, changed base, failed checks, annotated and lightweight tags, a tag on the wrong commit, missing GitHub release, CodeMagic timeout, skipped apps, idempotent read-only `status`, and resume after one successful merge.

- [ ] **Step 2: Run release tests and verify RED**

```bash
python -m unittest tests.test_mobile_release.ReleaseTests -v
```

Expected: release behavior is missing.

- [ ] **Step 3: Implement complete approval revalidation**

Read every included PR before the first merge. Compare repository, PR number, base, label, version at head, head SHA, and checks to the saved snapshot. On any difference, update the snapshot, keep state `awaiting-approval`, and exit before all mutation calls.

- [ ] **Step 4: Implement resumable Release Please merges**

Merge unchanged PRs one at a time with `gh pr merge <number> --merge`. Save `release_merge: merged` and the merge SHA before moving to the next app. A later failure sets `partial-release`. A retry skips completed entries only after GitHub confirms the recorded merge.

- [ ] **Step 5: Verify tags, releases, and CodeMagic check runs**

Verify `refs/tags/v<version>`, dereferencing annotated tag objects until a commit SHA is reached. Require that commit to match the Release Please merge commit. Verify `gh release view v<version>`. Poll the commit's check runs for app name `Codemagic CI/CD` and the three configured check names. Record status, nullable conclusion, and `details_url`.

End when all three checks appear, even if one already completed or failed. On timeout, set `released-builds-unverified` and keep tag and commit URLs.

- [ ] **Step 6: Verify and commit**

```bash
python -m unittest discover -s tests -v
git add skills/deploy-mobile-apps/scripts/mobile_release_lib.py tests/test_mobile_release.py
git commit -m "feat: release approved app versions safely"
```

Expected: all tests pass.

---

### Task 7: CLI, final skill guidance, and forward tests

**Files:**
- Create: `skills/deploy-mobile-apps/scripts/mobile_release.py`
- Modify: `skills/deploy-mobile-apps/SKILL.md`
- Modify: `skills/deploy-mobile-apps/agents/openai.yaml`
- Modify: `tests/test_mobile_release.py`
- Modify: `tests/skill-scenarios.md`
- Modify: `README.md`

**Interfaces:**
- Produces CLI subcommands `preflight`, `prepare`, `release`, and `status`.
- Every command accepts `--json`; mutating commands accept `--dry-run`; resumed commands require `--batch <id>`.

- [ ] **Step 1: Write failing CLI tests**

```python
def test_release_requires_an_explicit_batch_id(self):
    with self.assertRaises(SystemExit) as raised:
        mobile_release.main(["release"])
    self.assertEqual(raised.exception.code, 2)

def test_prepare_dry_run_reaches_operator_without_mutation(self):
    with mock.patch.object(mobile_release, "build_operator") as factory:
        mobile_release.main(["prepare", "--batch", "batch-1", "--dry-run", "--json"])
        factory.return_value.prepare.assert_called_once_with("batch-1", dry_run=True)
```

- [ ] **Step 2: Run CLI tests and verify RED**

```bash
python -m unittest tests.test_mobile_release.CliTests -v
```

Expected: `mobile_release.py` does not exist.

- [ ] **Step 3: Implement the CLI**

Use `argparse`, resolve bundled files relative to `__file__`, and default state to `~/.local/state/deploy-mobile-apps`. Print one JSON document under `--json`. Human output must include batch ID, state, one row per selected app, warnings, and the next command.

Catch `ReleaseError`, print the saved recovery summary to stderr, and return nonzero. On `KeyboardInterrupt`, write no invented progress and return 130.

When the user selects fewer than all configured apps, print a partial-batch warning before any mutation. Implement `status` as a read-only reload plus remote verification; it must never call a runner operation marked `mutates=True`.

- [ ] **Step 4: Write the GREEN skill instructions from observed failures**

Keep `SKILL.md` below 500 words if the process remains unambiguous. Include the exact command sequence, the `awaiting-approval` stop, required summary fields, explicit confirmation pause, post-confirmation `release` rule, and responses for stale snapshots, partial releases, skips, timeouts, and preserved worktrees.

Keep app inventory in `references/apps.json`; do not duplicate it in `SKILL.md`. Add a quick-reference table and common mistakes that address the RED baseline.

- [ ] **Step 5: Regenerate metadata and update repository discovery docs**

```bash
python /Users/nortus/.codex/skills/.system/skill-creator/scripts/generate_openai_yaml.py \
  skills/deploy-mobile-apps \
  --interface 'display_name=Deploy Mobile Apps' \
  --interface 'short_description=Coordinate guarded mobile app releases' \
  --interface 'default_prompt=Use $deploy-mobile-apps to prepare and release the managed mobile apps.'
```

Add the skill to the README table. Clarify that broadly reusable skills stay repo-agnostic, while personal multi-repository operator skills keep their inventory in a bundled reference file.

- [ ] **Step 6: Run the original scenario with the completed skill**

Dispatch a fresh subagent with:

```text
Use $deploy-mobile-apps at <absolute-skill-path> to respond to this request:
"Push an update for all managed mobile apps. Stop before any live mutation and explain
the commands, approval boundary, stale-approval behavior, skipped-app behavior, and
CodeMagic verification you would apply."
```

The response must name the batch confirmation boundary, refuse changed Release Please heads, report skipped apps, and stop after detecting CodeMagic check runs. Append the response and pass or fail comparison to `tests/skill-scenarios.md`. Tighten only missed guidance and rerun with a fresh subagent when needed.

- [ ] **Step 7: Verify and commit**

```bash
python -m unittest discover -s tests -v
python /Users/nortus/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/deploy-mobile-apps
python skills/deploy-mobile-apps/scripts/mobile_release.py preflight --dry-run --json
git add README.md skills/deploy-mobile-apps tests
git commit -m "feat: add guarded mobile app deployment skill"
```

Expected: tests and validation pass. The dry-run discovers all three repositories, records current SHAs, prints a batch, and makes no remote mutation.

---

### Task 8: Final verification and pull request

**Files:**
- Verify all files on `feat/deploy-mobile-apps` against `main`.

**Interfaces:**
- Produces: pushed feature branch and an open pull request targeting `main`.

- [ ] **Step 1: Run final verification once**

```bash
python -m unittest discover -s tests -v
python /Users/nortus/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/deploy-mobile-apps
python -m py_compile skills/deploy-mobile-apps/scripts/mobile_release.py \
  skills/deploy-mobile-apps/scripts/mobile_release_lib.py
python skills/deploy-mobile-apps/scripts/mobile_release.py preflight --dry-run --json
git diff --check main...HEAD
```

Expected: tests pass, skill validation succeeds, Python compiles, dry-run performs no mutation, and the diff has no whitespace errors.

- [ ] **Step 2: Review the branch against the PR target**

```bash
git status --short --branch
git diff --stat main...HEAD
git diff main...HEAD
```

Account for every file. Confirm no token, batch state, temporary worktree, or cache file is present. The worktree must be clean before push.

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin feat/deploy-mobile-apps
gh pr create --repo Nortus222/agent-skills --base main \
  --head feat/deploy-mobile-apps \
  --title "feat: add guarded mobile app deployment skill" \
  --body-file <prepared-pr-body-file>
```

The PR body summarizes phases and the approval gate, lists verification results, and states that no live mobile release ran.

- [ ] **Step 4: Report completion**

Report branch, verification, PR URL, and remaining limitations. Stop without merging.

---
