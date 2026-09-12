import base64
import copy
import io
import json
import sys
import tempfile
import unittest
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "deploy-mobile-apps"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import mobile_release
from mobile_release_lib import (
    BatchStore,
    CommandResult,
    PollPolicy,
    ReleaseError,
    ReleaseOperator,
    approval_rows,
    load_inventory,
    new_batch,
    _is_sha,
)


class CliTests(unittest.TestCase):
    @staticmethod
    def _batch(state="awaiting-approval"):
        return {
            "batch_id": "batch-1",
            "state": state,
            "selected_apps": ["pocket-manage"],
            "apps": {
                "pocket-manage": {
                    "version": "2.7.0",
                    "release_pr_number": 42,
                    "release_pr_url": "https://github.com/example/repo/pull/42",
                    "release_pr_checks": [
                        {"name": "release-please", "conclusion": "SUCCESS"}
                    ],
                    "release_pr_head_sha": "a" * 40,
                    "submodule_sha": "b" * 40,
                    "result": "ready for approval",
                }
            },
        }

    def test_release_requires_an_explicit_batch_id(self):
        with self.assertRaises(SystemExit) as raised:
            mobile_release.main(["release"])
        self.assertEqual(raised.exception.code, 2)

    def test_prepare_dry_run_reaches_operator_without_mutation(self):
        with mock.patch.object(mobile_release, "build_operator") as factory:
            factory.return_value.prepare.return_value = self._batch("dry-run-complete")
            with mock.patch("sys.stdout", io.StringIO()):
                mobile_release.main(
                    ["prepare", "--batch", "batch-1", "--dry-run", "--json"]
                )
            factory.return_value.prepare.assert_called_once_with(
                "batch-1", dry_run=True, staging_only=False
            )

    def test_preflight_json_defaults_to_all_configured_apps(self):
        with mock.patch.object(mobile_release, "build_operator") as factory:
            operator = factory.return_value
            operator.inventory.apps = {"pocket-manage": object(), "partner": object()}
            operator.preflight.return_value = self._batch("preflight-complete")
            stdout = io.StringIO()

            with mock.patch("sys.stdout", stdout):
                result = mobile_release.main(["preflight", "--dry-run", "--json"])

        self.assertEqual(result, 0)
        operator.preflight.assert_called_once_with(
            ["pocket-manage", "partner"], dry_run=True
        )
        document = json.loads(stdout.getvalue())
        self.assertEqual(document["batch_id"], "batch-1")
        self.assertEqual(
            document["next_command"],
            "python scripts/mobile_release.py preflight --json",
        )
        self.assertIn("dry-run preflight was not saved", document["warnings"])

    def test_partial_batch_warning_is_printed_before_preflight(self):
        with mock.patch.object(mobile_release, "build_operator") as factory:
            operator = factory.return_value
            operator.inventory.apps = {"pocket-manage": object(), "partner": object()}
            stderr = io.StringIO()

            def preflight(apps, dry_run=False):
                self.assertIn("partial batch", stderr.getvalue())
                return self._batch("preflight-complete")

            operator.preflight.side_effect = preflight
            with mock.patch("sys.stderr", stderr), mock.patch("sys.stdout", io.StringIO()):
                mobile_release.main(
                    ["preflight", "--app", "partner", "--dry-run", "--json"]
                )

        operator.preflight.assert_called_once_with(["partner"], dry_run=True)

    def test_human_summary_includes_checks_warnings_and_next_command(self):
        batch = self._batch()
        batch["apps"]["pocket-manage"]["worktree"] = "/tmp/preserved"
        with mock.patch.object(mobile_release, "build_operator") as factory:
            factory.return_value.status.return_value = batch
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                mobile_release.main(["status", "--batch", "batch-1"])

        output = stdout.getvalue()
        self.assertIn("Batch: batch-1", output)
        self.assertIn("State: awaiting-approval", output)
        self.assertIn("release-please=SUCCESS", output)
        self.assertIn(f"submodule={'b' * 40}", output)
        self.assertIn("preserved worktree /tmp/preserved", output)
        self.assertIn(
            "python scripts/mobile_release.py release --batch batch-1", output
        )

    def test_release_error_prints_one_json_recovery_document(self):
        with mock.patch.object(mobile_release, "build_operator") as factory:
            operator = factory.return_value
            operator.release.side_effect = ReleaseError("approval snapshot changed")
            operator.status.return_value = self._batch()
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
                result = mobile_release.main(
                    ["release", "--batch", "batch-1", "--json"]
                )

        self.assertEqual(result, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        document = json.loads(stdout.getvalue())
        self.assertEqual(document["error"], "approval snapshot changed")
        self.assertEqual(document["recovery"]["batch_id"], "batch-1")
        self.assertEqual(document["recovery"]["state"], "awaiting-approval")
        self.assertEqual(
            document["recovery"]["next_command"],
            "python scripts/mobile_release.py release --batch batch-1",
        )

    def test_preflight_json_error_has_null_recovery_without_a_saved_batch(self):
        with mock.patch.object(mobile_release, "build_operator") as factory:
            operator = factory.return_value
            operator.inventory.apps = {"pocket-manage": object()}
            operator.preflight.side_effect = ReleaseError("authentication failed")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
                result = mobile_release.main(["preflight", "--json"])

        self.assertEqual(result, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {"error": "authentication failed", "recovery": None},
        )

    def test_next_commands_match_the_skill_invocation(self):
        expected = {
            "preflight-complete": "python scripts/mobile_release.py prepare --batch batch-1",
            "dry-run-complete": "python scripts/mobile_release.py prepare --batch batch-1",
            "awaiting-approval": "python scripts/mobile_release.py release --batch batch-1",
            "partial-release": "python scripts/mobile_release.py release --batch batch-1",
            "release-failed": "python scripts/mobile_release.py release --batch batch-1",
            "released-builds-unverified": "python scripts/mobile_release.py status --batch batch-1",
        }

        for state, command in expected.items():
            with self.subTest(state=state):
                document = mobile_release._summary_document(self._batch(state))
                self.assertEqual(document["next_command"], command)

    def test_keyboard_interrupt_returns_130_without_inventing_progress(self):
        with mock.patch.object(mobile_release, "build_operator") as factory:
            operator = factory.return_value
            operator.prepare.side_effect = KeyboardInterrupt
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
                result = mobile_release.main(
                    ["prepare", "--batch", "batch-1", "--json"]
                )

        self.assertEqual(result, 130)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        operator.status.assert_not_called()

    def test_release_dry_run_calls_release_revalidation_and_prints_merge_plan(self):
        with mock.patch.object(mobile_release, "build_operator") as factory:
            operator = factory.return_value
            batch = self._batch()
            batch["planned_release_merges"] = [
                {
                    "repository": "MarketplaceSoftware/pocketmanage",
                    "pull_request_number": 42,
                    "head_sha": "a" * 40,
                    "command": ["gh", "pr", "merge", "42"],
                }
            ]
            operator.release.return_value = batch
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                result = mobile_release.main(
                    ["release", "--batch", "batch-1", "--dry-run", "--json"]
                )

        self.assertEqual(result, 0)
        operator.release.assert_called_once_with("batch-1", dry_run=True)
        operator.status.assert_not_called()
        document = json.loads(stdout.getvalue())
        self.assertEqual(document["planned_release_merges"], batch["planned_release_merges"])

    def test_release_dry_run_reports_the_refreshed_head_without_saving_it(self):
        operator, batch = awaiting_approval_operator(current_head="b" * 40)
        self.addCleanup(operator._test_temporary_directory.cleanup)
        snapshot_count = len(operator.store.snapshots)
        stdout = io.StringIO()

        with mock.patch.object(mobile_release, "build_operator", return_value=operator):
            with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", io.StringIO()):
                result = mobile_release.main(
                    ["release", "--batch", batch["batch_id"], "--dry-run", "--json"]
                )

        document = json.loads(stdout.getvalue())
        self.assertEqual(result, 1)
        self.assertEqual(document["recovery"]["state"], "awaiting-approval")
        self.assertEqual(
            document["recovery"]["apps"]["pocket-manage"]["release_pr_head_sha"],
            "b" * 40,
        )
        self.assertEqual(
            operator.store.load(batch["batch_id"])["apps"]["pocket-manage"][
                "release_pr_head_sha"
            ],
            "a" * 40,
        )
        self.assertEqual(len(operator.store.snapshots), snapshot_count)
        self.assertFalse(any(call.mutates for call in operator.runner.calls))

    def test_status_rechecks_unverified_builds_without_remote_mutation(self):
        operator, batch = awaiting_approval_operator(codemagic_checks="timeout")
        self.addCleanup(operator._test_temporary_directory.cleanup)
        released = operator.release(batch["batch_id"])
        self.assertEqual(released["state"], "released-builds-unverified")
        previous_call_count = len(operator.runner.calls)
        previous_snapshot_count = len(operator.store.snapshots)
        operator.runner.codemagic_checks = "queued"
        stdout = io.StringIO()

        with mock.patch.object(mobile_release, "build_operator", return_value=operator):
            with mock.patch("sys.stdout", stdout):
                result = mobile_release.main(
                    ["status", "--batch", batch["batch_id"], "--json"]
                )

        document = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(document["state"], "released")
        self.assertTrue(operator.runner.calls[previous_call_count:])
        self.assertFalse(
            any(call.mutates for call in operator.runner.calls[previous_call_count:])
        )
        self.assertEqual(len(operator.store.snapshots), previous_snapshot_count)
        self.assertEqual(
            operator.store.load(batch["batch_id"])["state"],
            "released-builds-unverified",
        )


@dataclass(frozen=True)
class RecordedCall:
    args: tuple[str, ...]
    cwd: Path | None
    mutates: bool


class FakeRunner:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls = []

    def run(self, args, *, cwd=None, mutates=False):
        self.calls.append(RecordedCall(tuple(args), cwd, mutates))
        if not self.responses:
            raise AssertionError(f"unexpected command: {args}")
        return self.responses.popleft()


class RecordingBatchStore(BatchStore):
    def __init__(self, root):
        super().__init__(root)
        self.snapshots = []

    def save(self, batch):
        self.snapshots.append(copy.deepcopy(batch))
        return super().save(batch)


class PreparationRunner:
    def __init__(
        self,
        *,
        existing_pr=False,
        duplicate_pr=False,
        failed_checks=False,
        pending_check_outcome=None,
        no_checks=False,
        release_contains_dev=False,
        release_object_requires_fetch=False,
        merge_conflict=False,
        mergeable_unknown_first=False,
        carried_commits=None,
        dirty_worktree=False,
        push_rejected=False,
        promotion_head_race=False,
        remote_dev=None,
        remote_release=None,
        resumed=False,
    ):
        self.calls = []
        self.existing_pr = existing_pr
        self.duplicate_pr = duplicate_pr
        self.failed_checks = failed_checks
        self.pending_check_outcome = pending_check_outcome
        self.check_calls = 0
        self.no_checks = no_checks
        self.release_contains_dev = release_contains_dev
        self.release_object_requires_fetch = release_object_requires_fetch
        self.promotion_refs_fetched = False
        self.merge_conflict = merge_conflict
        self.mergeable_unknown_first = mergeable_unknown_first
        self.carried_commits = (
            ["fix: show every delivery group's cartons"]
            if carried_commits is None
            else carried_commits
        )
        self.dirty_worktree = dirty_worktree
        self.push_rejected = push_rejected
        self.promotion_head_race = promotion_head_race
        self.remote_dev = remote_dev or "d" * 40
        self.remote_release = remote_release or "e" * 40
        self.resumed = resumed
        self.staging_failure = None
        self.remote_staging = self.remote_dev

    def run(self, args, *, cwd=None, mutates=False):
        self.calls.append(RecordedCall(tuple(args), cwd, mutates))
        command = tuple(args)

        if command == ("git", "ls-remote", "--exit-code", "origin", "refs/heads/staging"):
            if self.staging_failure == "missing":
                return CommandResult(2, "", "")
            return CommandResult(0, f"{self.remote_staging}\trefs/heads/staging\n", "")
        if command == ("git", "fetch", "origin",
                       "+refs/heads/dev:refs/remotes/origin/dev",
                       "+refs/heads/staging:refs/remotes/origin/staging"):
            return CommandResult(0, "", "")
        if command == ("git", "rev-parse", "refs/remotes/origin/dev^{commit}"):
            return CommandResult(0, self.remote_dev + "\n", "")
        if command == ("git", "merge-base", "--is-ancestor",
                       "refs/remotes/origin/staging", "refs/remotes/origin/dev"):
            return CommandResult(1 if self.staging_failure == "diverged" else 0, "", "")
        if command[:3] == ("git", "push", "origin") and command[-1].endswith(":refs/heads/staging"):
            if self.staging_failure == "push-rejected":
                return CommandResult(1, "", "non-fast-forward")
            self.remote_staging = command[-1].split(":")[0]
            return CommandResult(0, "", "")
        if command == ("git", "ls-remote", "--exit-code", "origin", "refs/heads/dev"):
            return CommandResult(0, f"{self.remote_dev}\trefs/heads/dev\n", "")
        if command == ("git", "ls-remote", "--exit-code", "origin", "refs/heads/release"):
            return CommandResult(0, f"{self.remote_release}\trefs/heads/release\n", "")
        if command == (
            "git",
            "fetch",
            "origin",
            "+refs/heads/staging:refs/remotes/origin/staging",
            "+refs/heads/release:refs/remotes/origin/release",
        ):
            self.promotion_refs_fetched = True
            return CommandResult(0, "", "")
        if command == (
            "git",
            "rev-parse",
            "refs/remotes/origin/staging^{commit}",
            "refs/remotes/origin/release^{commit}",
        ):
            return CommandResult(0, f"{self.remote_dev}\n{self.remote_release}\n", "")
        if command[:3] == ("git", "merge-base", "--is-ancestor"):
            if self.release_object_requires_fetch and not self.promotion_refs_fetched:
                return CommandResult(128, "", "fatal: Not a valid commit name")
            is_ancestor = self.release_contains_dev or self.remote_dev == self.remote_release
            return CommandResult(0 if is_ancestor else 1, "", "")
        if command == ("git", "check-ignore", "-q", ".claude/worktrees"):
            return CommandResult(0, "", "")
        if command[:3] == ("git", "worktree", "add"):
            return CommandResult(0, "", "")
        if command == ("git", "submodule", "update", "--init", "--", "packages"):
            return CommandResult(0, "", "")
        if command[:4] == ("git", "-C", "packages", "checkout"):
            return CommandResult(0, "", "")
        if command == ("git", "add", "--", "packages"):
            return CommandResult(0, "", "")
        if command == ("git", "diff", "--cached", "--name-only"):
            return CommandResult(0, "packages\n", "")
        if command[:3] == ("git", "log", "--format=%s"):
            return CommandResult(0, "\n".join(self.carried_commits) + "\n", "")
        if command[:2] == ("git", "commit"):
            return CommandResult(0, "", "")
        if command == ("git", "rev-parse", "HEAD^{commit}"):
            return CommandResult(0, f"{'1' * 40}\n", "")
        if command == ("git", "push", "origin", "HEAD:dev"):
            if self.push_rejected:
                return CommandResult(1, "", "non-fast-forward")
            self.remote_dev = "1" * 40
            return CommandResult(0, "", "")
        if command == ("git", "status", "--porcelain"):
            output = "?? unexpected.txt\n" if self.dirty_worktree else ""
            return CommandResult(0, output, "")
        if command[:3] == ("git", "branch", "-D"):
            return CommandResult(0, "", "")
        if command[:3] == ("git", "worktree", "remove"):
            if "--force" not in command:
                return CommandResult(
                    128,
                    "",
                    "fatal: working trees containing submodules cannot be moved or removed",
                )
            return CommandResult(0, "", "")
        if command[:3] == ("gh", "run", "list"):
            return CommandResult(
                0,
                json.dumps(
                    [
                        {
                            "databaseId": 91,
                            "status": "completed",
                            "conclusion": "success",
                            "headSha": self.remote_release,
                            "url": "https://github.com/example/actions/runs/91",
                        }
                    ]
                ),
                "",
            )
        if command[:3] == ("gh", "pr", "list") and "--label" in command:
            return CommandResult(
                0,
                json.dumps(
                    [
                        {
                            "number": 42,
                            "url": "https://github.com/MarketplaceSoftware/pocketmanage/pull/42",
                            "headRefOid": "a" * 40,
                            "baseRefName": "release",
                            "labels": [{"name": "autorelease: pending"}],
                            "statusCheckRollup": [],
                        }
                    ]
                ),
                "",
            )
        if command[:3] == ("gh", "pr", "list"):
            pull_requests = []
            if self.existing_pr or self.duplicate_pr:
                pull_requests.append(
                    {
                        "number": 17,
                        "url": "https://github.com/MarketplaceSoftware/pocketmanage/pull/17",
                        "headRefName": "staging",
                        "headRefOid": self.remote_dev,
                        "baseRefName": "release",
                    }
                )
            if self.duplicate_pr:
                pull_requests.append(
                    {
                        "number": 18,
                        "url": "https://github.com/MarketplaceSoftware/pocketmanage/pull/18",
                        "headRefName": "staging",
                        "headRefOid": self.remote_dev,
                        "baseRefName": "release",
                    }
                )
            return CommandResult(0, json.dumps(pull_requests), "")
        if command[:3] == ("gh", "pr", "create"):
            return CommandResult(
                0,
                "https://github.com/MarketplaceSoftware/pocketmanage/pull/17\n",
                "",
            )
        if command[:3] == ("gh", "pr", "checks"):
            self.check_calls += 1
            if self.no_checks:
                return CommandResult(1, "", "no checks reported")
            if self.pending_check_outcome and self.check_calls == 1:
                return CommandResult(
                    8,
                    json.dumps(
                        [{"name": "tests", "state": "PENDING", "bucket": "pending"}]
                    ),
                    "",
                )
            if self.pending_check_outcome:
                if "--watch" not in command or "--fail-fast" not in command:
                    raise AssertionError(f"pending checks were not watched: {command}")
                failed = self.pending_check_outcome == "fail"
                return CommandResult(
                    1 if failed else 0,
                    json.dumps(
                        [
                            {
                                "name": "tests",
                                "state": "FAILURE" if failed else "SUCCESS",
                                "bucket": "fail" if failed else "pass",
                            }
                        ]
                    ),
                    "",
                )
            bucket = "fail" if self.failed_checks else "pass"
            state = "FAILURE" if self.failed_checks else "SUCCESS"
            return CommandResult(
                1 if self.failed_checks else 0,
                json.dumps([{"name": "tests", "state": state, "bucket": bucket}]),
                "",
            )
        if command[:3] == ("gh", "pr", "view"):
            state = "MERGED" if self.resumed else "OPEN"
            mergeable = "CONFLICTING" if self.merge_conflict else "MERGEABLE"
            if self.mergeable_unknown_first:
                self.mergeable_unknown_first = False
                mergeable = "UNKNOWN"
            return CommandResult(
                0,
                json.dumps(
                    {
                        "number": 17,
                        "url": "https://github.com/MarketplaceSoftware/pocketmanage/pull/17",
                        "state": state,
                        "mergedAt": "2026-08-14T13:00:00Z" if self.resumed else None,
                        "mergeable": mergeable,
                        "mergeStateStatus": "DIRTY" if self.merge_conflict else "CLEAN",
                        "headRefName": "staging",
                        "headRefOid": self.remote_dev,
                        "baseRefName": "release",
                    }
                ),
                "",
            )
        if command[:3] == ("gh", "pr", "merge"):
            if self.promotion_head_race and "--match-head-commit" in command:
                return CommandResult(1, "", "head branch was modified")
            self.remote_release = "2" * 40
            return CommandResult(0, "", "")
        if command[:2] == ("gh", "api"):
            content = base64.b64encode(json.dumps({".": "2.7.0"}).encode()).decode()
            return CommandResult(
                0,
                json.dumps({"type": "file", "encoding": "base64", "content": content}),
                "",
            )
        raise AssertionError(f"unexpected command: {args}")


class DiscoveryRunner:
    def __init__(
        self,
        *,
        no_release_pr_for=None,
        workflow_statuses=None,
        wrap_manifest_content=False,
        duplicate_release_pr=False,
    ):
        self.calls = []
        self.no_release_pr_for = no_release_pr_for
        self.workflow_statuses = deque(workflow_statuses or ["completed"])
        self.last_workflow_status = self.workflow_statuses[-1]
        self.wrap_manifest_content = wrap_manifest_content
        self.duplicate_release_pr = duplicate_release_pr
        self.versions = {
            "MarketplaceSoftware/pocketmanage": "2.7.0",
            "MarketplaceSoftware/pocketmanage_installers": "1.3.0",
            "MarketplaceSoftware/pocketmanage_partner": "4.2.1",
        }

    def run(self, args, *, cwd=None, mutates=False):
        self.calls.append(RecordedCall(tuple(args), cwd, mutates))
        command = tuple(args)
        repository = command[command.index("--repo") + 1] if "--repo" in command else None

        if command[:3] == ("gh", "run", "list"):
            status = (
                self.workflow_statuses.popleft()
                if self.workflow_statuses
                else self.last_workflow_status
            )
            return CommandResult(
                0,
                json.dumps(
                    [
                        {
                            "databaseId": 91,
                            "status": status,
                            "conclusion": "success" if status == "completed" else None,
                            "headSha": "e" * 40,
                            "url": f"https://github.com/{repository}/actions/runs/91",
                        }
                    ]
                ),
                "",
            )
        if command[:2] == ("gh", "api") and "/compare/" in command[2]:
            return CommandResult(
                0, json.dumps(["chore: notifications", "chore: migrate more pages"]), ""
            )
        if command[:3] == ("gh", "pr", "list"):
            app_key = next(
                key
                for key, app in load_inventory(
                    SKILL_ROOT / "references/apps.json"
                ).apps.items()
                if app.repository == repository
            )
            if app_key == self.no_release_pr_for:
                return CommandResult(0, "[]", "")
            pull_request = {
                "number": 42,
                "url": f"https://github.com/{repository}/pull/42",
                "headRefOid": "a" * 40,
                "baseRefName": "release",
                "labels": [{"name": "autorelease: pending"}],
                "statusCheckRollup": [
                    {"name": "release-please", "conclusion": "SUCCESS"}
                ],
            }
            pull_requests = [pull_request]
            if self.duplicate_release_pr:
                pull_requests.append({**pull_request, "number": 43})
            return CommandResult(0, json.dumps(pull_requests), "")
        if command[:2] == ("gh", "api"):
            repository = command[2].split("/contents/", 1)[0].removeprefix("repos/")
            manifest = json.dumps({".": self.versions[repository]}).encode()
            content = base64.b64encode(manifest).decode()
            if self.wrap_manifest_content:
                content = "\n".join((content[:8], content[8:]))
            return CommandResult(
                0,
                json.dumps(
                    {
                        "type": "file",
                        "encoding": "base64",
                        "content": content,
                    }
                ),
                "",
            )
        raise AssertionError(f"unexpected command: {args}")


class ReleaseRunner:
    def __init__(
        self,
        *,
        current_head=None,
        changed_version=False,
        missing_label=False,
        changed_base=False,
        failed_checks=False,
        second_merge_fails=False,
        annotated_tag=False,
        wrong_tag=False,
        missing_release=False,
        codemagic_checks="queued",
        resumed_merge=False,
        merge_confirmation_fails=False,
        no_release_checks=False,
        tag_missing_first=False,
        release_mergeable="MERGEABLE",
        extra_label=False,
        head_race=False,
        already_merged=False,
    ):
        self.calls = []
        self.current_head = current_head
        self.changed_version = changed_version
        self.missing_label = missing_label
        self.changed_base = changed_base
        self.failed_checks = failed_checks
        self.second_merge_fails = second_merge_fails
        self.annotated_tag = annotated_tag
        self.wrong_tag = wrong_tag
        self.missing_release = missing_release
        self.codemagic_checks = codemagic_checks
        self.merge_confirmation_fails = merge_confirmation_fails
        self.no_release_checks = no_release_checks
        self.tag_missing_first = tag_missing_first
        self.release_mergeable = release_mergeable
        self.extra_label = extra_label
        self.head_race = head_race
        self.successful_merges = []
        self.merged = (
            {"MarketplaceSoftware/pocketmanage"}
            if resumed_merge or already_merged
            else set()
        )
        self.merge_shas = {
            "MarketplaceSoftware/pocketmanage": "4" * 40,
            "MarketplaceSoftware/pocketmanage_installers": "5" * 40,
            "MarketplaceSoftware/pocketmanage_partner": "6" * 40,
        }

    def run(self, args, *, cwd=None, mutates=False):
        self.calls.append(RecordedCall(tuple(args), cwd, mutates))
        command = tuple(args)
        repository = command[command.index("--repo") + 1] if "--repo" in command else None
        if command[:2] == ("gh", "api"):
            repository_parts = command[2].removeprefix("repos/").split("/", 2)
            repository = "/".join(repository_parts[:2])

        if command[:3] == ("gh", "pr", "view"):
            if self.merge_confirmation_fails and repository in self.merged:
                return CommandResult(1, "", "temporary read failure")
            head = self.current_head or "a" * 40
            checks = (
                []
                if self.no_release_checks
                else [
                    {
                        "name": "release-please",
                        "status": "COMPLETED",
                        "conclusion": "FAILURE" if self.failed_checks else "SUCCESS",
                    }
                ]
            )
            return CommandResult(
                0,
                json.dumps(
                    {
                        "number": int(command[3]),
                        "url": f"https://github.com/{repository}/pull/{command[3]}",
                        "state": "MERGED" if repository in self.merged else "OPEN",
                        "headRefOid": head,
                        "baseRefName": "main" if self.changed_base else "release",
                        "labels": []
                        if self.missing_label
                        else [
                            {"name": "autorelease: pending"},
                            *([{"name": "unexpected"}] if self.extra_label else []),
                        ],
                        "statusCheckRollup": checks,
                        "mergeable": self.release_mergeable,
                        "mergeCommit": {
                            "oid": self.merge_shas[repository]
                        }
                        if repository in self.merged
                        else None,
                    }
                ),
                "",
            )
        if command[:3] == ("gh", "pr", "merge"):
            if self.second_merge_fails and repository.endswith("_installers"):
                return CommandResult(1, "", "merge failed")
            if self.head_race and "--match-head-commit" in command:
                return CommandResult(1, "", "head branch was modified")
            self.merged.add(repository)
            self.successful_merges.append(repository)
            return CommandResult(0, "", "")
        if command[:2] == ("gh", "api") and "/contents/" in command[2]:
            version = {
                "MarketplaceSoftware/pocketmanage": "2.7.1"
                if self.changed_version
                else "2.7.0",
                "MarketplaceSoftware/pocketmanage_installers": "1.3.0",
                "MarketplaceSoftware/pocketmanage_partner": "4.2.1",
            }[repository]
            content = base64.b64encode(json.dumps({".": version}).encode()).decode()
            return CommandResult(
                0,
                json.dumps({"type": "file", "encoding": "base64", "content": content}),
                "",
            )
        if command[:2] == ("gh", "api") and "/git/ref/tags/" in command[2]:
            if self.tag_missing_first:
                self.tag_missing_first = False
                return CommandResult(1, "", "gh: Not Found (HTTP 404)")
            merge_sha = self.merge_shas[repository]
            tag_sha = "7" * 40 if self.annotated_tag else merge_sha
            if self.wrong_tag:
                tag_sha = "8" * 40
            return CommandResult(
                0,
                json.dumps(
                    {
                        "ref": command[2].split("/git/ref/", 1)[1],
                        "object": {
                            "type": "tag" if self.annotated_tag else "commit",
                            "sha": tag_sha,
                            "url": f"https://api.github.com/repos/{repository}/git/{tag_sha}",
                        },
                    }
                ),
                "",
            )
        if command[:2] == ("gh", "api") and "/git/tags/" in command[2]:
            target = "8" * 40 if self.wrong_tag else self.merge_shas[repository]
            return CommandResult(
                0,
                json.dumps(
                    {
                        "sha": "7" * 40,
                        "object": {"type": "commit", "sha": target},
                    }
                ),
                "",
            )
        if command[:3] == ("gh", "release", "view"):
            if self.missing_release:
                return CommandResult(1, "", "release not found")
            tag = command[3]
            return CommandResult(
                0,
                json.dumps(
                    {
                        "tagName": tag,
                        "url": f"https://github.com/{repository}/releases/tag/{tag}",
                    }
                ),
                "",
            )
        if command[:2] == ("gh", "api") and "/check-runs" in command[2]:
            check_runs = []
            if self.codemagic_checks != "timeout":
                names = load_inventory(
                    SKILL_ROOT / "references/apps.json"
                ).codemagic_checks
                returned_names = (
                    names[:1] if self.codemagic_checks == "partial-timeout" else names
                )
                for index, name in enumerate(returned_names):
                    status = "completed" if self.codemagic_checks == "mixed" and index == 0 else "queued"
                    check_runs.append(
                        {
                            "name": name,
                            "status": status,
                            "conclusion": "failure" if status == "completed" else None,
                            "details_url": f"https://codemagic.io/app/check-{index}",
                            "app": {"slug": "codemagic-ci-cd", "name": "Codemagic CI/CD"},
                        }
                    )
                check_runs.append(
                    {
                        "name": names[0],
                        "status": "completed",
                        "conclusion": "success",
                        "details_url": "https://example.com/wrong-app",
                        "app": {"slug": "other", "name": "Other CI"},
                    }
                )
            return CommandResult(0, json.dumps({"check_runs": check_runs}), "")
        raise AssertionError(f"unexpected command: {args}")


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def operator_after_preparation(*, no_release_pr_for=None):
    temporary = tempfile.TemporaryDirectory()
    store = RecordingBatchStore(Path(temporary.name) / "state")
    inventory = load_inventory(SKILL_ROOT / "references/apps.json")
    runner = DiscoveryRunner(no_release_pr_for=no_release_pr_for)
    operator = ReleaseOperator(inventory, store, runner=runner, environ={})
    operator._test_temporary_directory = temporary
    app_keys = ["installers", "partner"] if no_release_pr_for else ["pocket-manage"]
    batch = new_batch(app_keys, now="2026-08-14T12:00:00Z")
    batch["state"] = "prepare-complete"
    for app_key in app_keys:
        app = inventory.apps[app_key]
        batch["apps"][app_key] = {
            "state": "prepare-complete",
            "status": "prepared",
            "repository": app.repository,
            "release_sha": "e" * 40,
            "release_sha_before": "d" * 40,
            "packages_sha": "f" * 40,
        }
    store.save(batch)
    return operator, batch


def awaiting_approval_operator(
    *,
    app_keys=None,
    skipped_apps=None,
    current_head=None,
    changed_version=False,
    missing_label=False,
    changed_base=False,
    failed_checks=False,
    second_merge_fails=False,
    annotated_tag=False,
    wrong_tag=False,
    missing_release=False,
    codemagic_checks="queued",
    resumed_merge=False,
    merge_confirmation_fails=False,
    no_release_checks=False,
    tag_missing_first=False,
    release_mergeable="MERGEABLE",
    extra_label=False,
    head_race=False,
    already_merged=False,
):
    app_keys = app_keys or ["pocket-manage"]
    skipped_apps = set(skipped_apps or [])
    temporary = tempfile.TemporaryDirectory()
    store = RecordingBatchStore(Path(temporary.name) / "state")
    inventory = load_inventory(SKILL_ROOT / "references/apps.json")
    runner = ReleaseRunner(
        current_head=current_head,
        changed_version=changed_version,
        missing_label=missing_label,
        changed_base=changed_base,
        failed_checks=failed_checks,
        second_merge_fails=second_merge_fails,
        annotated_tag=annotated_tag,
        wrong_tag=wrong_tag,
        missing_release=missing_release,
        codemagic_checks=codemagic_checks,
        resumed_merge=resumed_merge,
        merge_confirmation_fails=merge_confirmation_fails,
        no_release_checks=no_release_checks,
        tag_missing_first=tag_missing_first,
        release_mergeable=release_mergeable,
        extra_label=extra_label,
        head_race=head_race,
        already_merged=already_merged,
    )
    clock = FakeClock()
    operator = ReleaseOperator(
        inventory,
        store,
        runner=runner,
        environ={},
        clock=clock,
        poll_policy=PollPolicy(
            interval_seconds=2, timeout_seconds=4, observation_timeout_seconds=4
        ),
    )
    operator._test_temporary_directory = temporary
    batch = new_batch(app_keys, now="2026-08-14T12:00:00Z")
    batch["state"] = "partial-release" if resumed_merge else "awaiting-approval"
    versions = {"pocket-manage": "2.7.0", "installers": "1.3.0", "partner": "4.2.1"}
    for index, app_key in enumerate(app_keys, start=42):
        app = inventory.apps[app_key]
        if app_key in skipped_apps:
            batch["apps"][app_key] = {
                "state": "awaiting-approval",
                "status": "skipped",
                "repository": app.repository,
                "skip_reason": "no releasable changes",
                "result": "no releasable changes",
            }
            continue
        batch["apps"][app_key] = {
            "state": "awaiting-approval",
            "status": "prepared",
            "repository": app.repository,
            "version": versions[app_key],
            "release_pr_number": index,
            "release_pr_url": f"https://github.com/{app.repository}/pull/{index}",
            "release_pr_checks": []
            if no_release_checks
            else [
                {
                    "name": "release-please",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                }
            ],
            "release_pr_base": "release",
            "release_pr_labels": ["autorelease: pending"],
            "release_pr_head_sha": "a" * 40,
            "submodule_sha": "f" * 40,
            "result": "ready for approval",
        }
    if resumed_merge:
        first = batch["apps"][app_keys[0]]
        first.update(
            {
                "release_merge": "merged",
                "release_merge_sha": runner.merge_shas[
                    inventory.apps[app_keys[0]].repository
                ],
            }
        )
    store.save(batch)
    return operator, batch


def command_args(calls):
    return [list(call.args) for call in calls]


def prepared_operator(
    *,
    submodule_changed=True,
    no_dev_release_diff=False,
    existing_pr=False,
    duplicate_pr=False,
    failed_checks=False,
    pending_check_outcome=None,
    no_checks=False,
    release_contains_dev=False,
    release_object_requires_fetch=False,
    merge_conflict=False,
    mergeable_unknown_first=False,
    carried_commits=None,
    dirty_worktree=False,
    push_rejected=False,
    promotion_head_race=False,
    resumed=False,
):
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name) / "workspace"
    app_key = "partner" if no_dev_release_diff else "pocket-manage"
    directory = "pocketmanage_partner" if app_key == "partner" else "pocketmanage"
    repository_path = root / directory
    (repository_path / ".git").mkdir(parents=True)
    (repository_path / "packages").mkdir()

    dev_sha = "d" * 40
    release_sha = dev_sha if no_dev_release_diff else "e" * 40
    remote_dev = "1" * 40 if resumed else dev_sha
    remote_release = "2" * 40 if resumed else release_sha
    runner = PreparationRunner(
        existing_pr=existing_pr,
        duplicate_pr=duplicate_pr,
        failed_checks=failed_checks,
        pending_check_outcome=pending_check_outcome,
        no_checks=no_checks,
        release_contains_dev=release_contains_dev,
        release_object_requires_fetch=release_object_requires_fetch,
        merge_conflict=merge_conflict,
        mergeable_unknown_first=mergeable_unknown_first,
        carried_commits=carried_commits,
        dirty_worktree=dirty_worktree,
        push_rejected=push_rejected,
        promotion_head_race=promotion_head_race,
        remote_dev=remote_dev,
        remote_release=remote_release,
        resumed=resumed,
    )
    store = RecordingBatchStore(Path(temporary.name) / "state")
    operator = ReleaseOperator(
        load_inventory(SKILL_ROOT / "references/apps.json"),
        store,
        runner=runner,
        environ={"EMANAGE_MOBILE_ROOT": str(root)},
    )
    operator._test_temporary_directory = temporary

    batch = new_batch([app_key], now="2026-08-14T12:00:00Z")
    batch["state"] = "preflight-complete"
    batch["apps"][app_key] = {
        "state": "preflight-complete",
        "repository": f"MarketplaceSoftware/{directory}",
        "repository_path": str(repository_path.resolve()),
        "dev_sha": dev_sha,
        "release_sha": release_sha,
        "packages_pointer_sha": "c" * 40 if submodule_changed else "f" * 40,
        "packages_sha": "f" * 40,
        "staging_to_release_pr": None,
    }
    if resumed:
        batch["state"] = "prepare-failed"
        batch["apps"][app_key].update(
            {
                "dev_sha": remote_dev,
                "staging_sha": remote_dev,
                "worktree": None,
                "submodule_before": "c" * 40,
                "submodule_after": "f" * 40,
                "dev_push": "pushed",
                "preparation_pr": {
                    "number": 17,
                    "url": "https://github.com/MarketplaceSoftware/pocketmanage/pull/17",
                    "status": "merged",
                },
                "status": "failed",
                "error": "interrupted after merge",
            }
        )
    store.save(batch)
    return operator, batch


CODEMAGIC_YAML = "\n".join(
    [
        "workflows:",
        "  android:",
        "    name: Build Android AppBundle and Publish",
        "  ios:",
        "    name: Build IPA and Publish To AppStore Connect",
        "  web:",
        "    name: Build Web and Publish to Firebase Hosting",
        "  ios-staging:",
        "    name: Build IPA and Publish To TestFlight (Staging)",
        "  android-staging:",
        "    name: Build Android AppBundle and Publish to Internal Testing",
    ]
)


def successful_preflight_responses(app_count=3):
    responses = [CommandResult(0, "github.com\n", "")]
    repositories = (
        "MarketplaceSoftware/pocketmanage",
        "MarketplaceSoftware/pocketmanage_installers",
        "MarketplaceSoftware/pocketmanage_partner",
    )
    responses.extend(CommandResult(0, f"git@github.com:{repository}.git\n", "") for repository in repositories)
    responses.extend(
        CommandResult(0, json.dumps({"viewerPermission": "WRITE"}), "")
        for _ in range(app_count)
    )
    responses.extend(CommandResult(0, "", "") for _ in range(app_count))
    for _ in range(app_count):
        responses.extend(
            [
                CommandResult(0, "", ""),
                CommandResult(0, f"{'d' * 40}\n{'e' * 40}\n", ""),
                CommandResult(0, f"160000 commit {'c' * 40}\tpackages\n", ""),
                CommandResult(0, "git@github.com:MarketplaceSoftware/packages.git\n", ""),
                CommandResult(0, "", ""),
                CommandResult(0, f"{'f' * 40}\n", ""),
                CommandResult(0, "{}\n{}\n", ""),
                CommandResult(0, CODEMAGIC_YAML, ""),
                # Preflight reads codemagic.yaml twice: release for the release
                # workflows, dev for the staging ones.
                CommandResult(0, CODEMAGIC_YAML, ""),
                CommandResult(0, "[]\n", ""),
                CommandResult(0, "", ""),
                # release is an ancestor of dev: no back-merge outstanding
                CommandResult(0, "", ""),
            ]
        )
    return responses


def make_operator(fake_responses, *, duplicate_remote=False, environment=None):
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name) / "workspace"
    root.mkdir()
    for name in ("pocketmanage", "pocketmanage_installers", "pocketmanage_partner"):
        repository = root / name
        (repository / ".git").mkdir(parents=True)
        (repository / "packages").mkdir()
    if duplicate_remote:
        duplicate = root / "pocketmanage-copy"
        (duplicate / ".git").mkdir(parents=True)
        (duplicate / "packages").mkdir()

    runner = FakeRunner(fake_responses)
    store = BatchStore(Path(temporary.name) / "state")
    operator = ReleaseOperator(
        load_inventory(SKILL_ROOT / "references/apps.json"),
        store,
        runner=runner,
        environ={"EMANAGE_MOBILE_ROOT": str(root), **(environment or {})},
    )
    operator._test_temporary_directory = temporary
    return operator


class InventoryTests(unittest.TestCase):
    def test_inventory_defines_the_three_managed_apps(self):
        inventory = load_inventory(SKILL_ROOT / "references/apps.json")

        self.assertEqual(set(inventory.apps), {"pocket-manage", "installers", "partner"})
        for app in inventory.apps.values():
            self.assertEqual(app.dev_branch, "dev")
            self.assertEqual(app.staging_branch, "staging")
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

    def test_sha_validation_accepts_only_40_hexadecimal_characters(self):
        self.assertTrue(_is_sha("0123456789abcdefABCDEF0123456789abcdefAB"))
        self.assertFalse(_is_sha("g" * 40))
        self.assertFalse(_is_sha("a" * 39))
        self.assertFalse(_is_sha("a" * 41))
        self.assertFalse(_is_sha(f" {'a' * 40}"))
        self.assertFalse(_is_sha(f"{'a' * 40} "))


class PreflightTests(unittest.TestCase):
    def test_preflight_rejects_a_repository_without_write_permission(self):
        responses = successful_preflight_responses()
        responses[5] = CommandResult(
            0,
            json.dumps({"viewerPermission": "READ"}),
            "",
        )
        operator = make_operator(responses)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(
            ReleaseError,
            "MarketplaceSoftware/pocketmanage_installers.*write or merge permission",
        ):
            operator.preflight(["pocket-manage", "installers", "partner"])

        self.assertFalse(
            any(call.args[:2] == ("git", "fetch") for call in operator.runner.calls)
        )

    def test_preflight_rejects_an_active_deployment_worktree(self):
        responses = successful_preflight_responses(app_count=1)
        responses.insert(
            5,
            CommandResult(
                0,
                "\n".join(
                    [
                        "worktree /tmp/pocketmanage/.claude/worktrees/old-batch-pocket-manage",
                        f"HEAD {'1' * 40}",
                        "branch refs/heads/deploy-mobile-apps/old-batch/pocket-manage",
                        "",
                    ]
                ),
                "",
            ),
        )
        operator = make_operator(responses)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(
            ReleaseError,
            "MarketplaceSoftware/pocketmanage.*conflicting deployment worktree.*old-batch-pocket-manage",
        ):
            operator.preflight(["pocket-manage"])

    def test_preflight_rejects_a_repository_that_does_not_ignore_the_deployment_worktree(self):
        responses = successful_preflight_responses(app_count=1)
        responses[16] = CommandResult(1, "", "")
        operator = make_operator(responses)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(
            ReleaseError,
            r"MarketplaceSoftware/pocketmanage: \.claude/worktrees is not ignored",
        ):
            operator.preflight(["pocket-manage"])

    def test_preflight_skips_the_ignore_check_when_the_submodule_pointer_is_current(self):
        responses = successful_preflight_responses(app_count=1)
        responses[11] = CommandResult(0, f"{'c' * 40}\n", "")
        operator = make_operator(responses)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        operator.preflight(["pocket-manage"])

        self.assertFalse(
            any(
                call.args == ("git", "check-ignore", "-q", ".claude/worktrees")
                for call in operator.runner.calls
            )
        )

    def test_preflight_records_exact_remote_shas_for_every_app(self):
        operator = make_operator(successful_preflight_responses())
        self.addCleanup(operator._test_temporary_directory.cleanup)

        batch = operator.preflight(["pocket-manage", "installers", "partner"])

        self.assertEqual(batch["state"], "preflight-complete")
        self.assertEqual(batch["apps"]["pocket-manage"]["dev_sha"], "d" * 40)
        self.assertEqual(batch["apps"]["pocket-manage"]["packages_sha"], "f" * 40)
        self.assertTrue(all(app["state"] == "preflight-complete" for app in batch["apps"].values()))
        self.assertFalse(any(call.mutates for call in operator.runner.calls))
        self.assertIn(
            (
                "git",
                "fetch",
                "origin",
                "+refs/heads/dev:refs/remotes/origin/dev",
                "+refs/heads/release:refs/remotes/origin/release",
            ),
            [call.args for call in operator.runner.calls],
        )
        self.assertIn(
            ("git", "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"),
            [call.args for call in operator.runner.calls],
        )
        packages_path = (
            Path(operator._test_temporary_directory.name) / "workspace/pocketmanage/packages"
        ).resolve()
        self.assertIn(
            RecordedCall(
                ("git", "remote", "get-url", "--all", "origin"),
                packages_path,
                False,
            ),
            operator.runner.calls,
        )
        self.assertEqual(list(operator.runner.responses), [])

    def test_preflight_failure_saves_no_batch(self):
        responses = [
            CommandResult(0, "github.com\n", ""),
            CommandResult(0, "git@github.com:MarketplaceSoftware/pocketmanage.git\n", ""),
            CommandResult(0, "https://github.com/marketplacesoftware/pocketmanage\n", ""),
            CommandResult(0, "git@github.com:MarketplaceSoftware/pocketmanage_installers.git\n", ""),
            CommandResult(0, "git@github.com:MarketplaceSoftware/pocketmanage_partner.git\n", ""),
        ]
        operator = make_operator(responses, duplicate_remote=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "ambiguous repository"):
            operator.preflight(["pocket-manage", "installers"])

        self.assertEqual(list(operator.store.root.glob("*.json")), [])

    def test_preflight_rejects_duplicate_dev_to_release_prs(self):
        responses = successful_preflight_responses(app_count=1)
        responses[15] = CommandResult(
            0,
            json.dumps(
                [
                    {"number": 12, "url": "https://github.com/example/pr/12"},
                    {"number": 14, "url": "https://github.com/example/pr/14"},
                ]
            ),
            "",
        )
        operator = make_operator(responses)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "duplicate staging to release pull requests"):
            operator.preflight(["pocket-manage"])

        self.assertEqual(list(operator.store.root.glob("*.json")), [])

    def test_preflight_rejects_mismatched_packages_origin(self):
        responses = successful_preflight_responses(app_count=1)
        responses[9] = CommandResult(
            0,
            "git@github.com:MarketplaceSoftware/not-packages.git\n",
            "",
        )
        operator = make_operator(responses)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "packages origin does not match"):
            operator.preflight(["pocket-manage"])

        self.assertFalse(
            any(
                call.args
                == ("git", "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main")
                for call in operator.runner.calls
            )
        )
        self.assertEqual(list(operator.store.root.glob("*.json")), [])

    def test_preflight_rejects_workflow_names_found_only_in_nested_fields(self):
        responses = successful_preflight_responses(app_count=1)
        responses[13] = CommandResult(
            0,
            "\n".join(
                [
                    "workflows:",
                    "  unrelated:",
                    "    name: An unrelated workflow",
                    "    nested_metadata:",
                    "      android:",
                    "        name: Build Android AppBundle and Publish",
                    "      ios:",
                    "        name: Build IPA and Publish To AppStore Connect",
                    "      web:",
                    "        name: Build Web and Publish to Firebase Hosting",
                ]
            ),
            "",
        )
        operator = make_operator(responses)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "missing workflow names"):
            operator.preflight(["pocket-manage"])

        self.assertEqual(list(operator.store.root.glob("*.json")), [])

    def test_command_failure_redacts_environment_values(self):
        secret = "a-secret-token-value"
        operator = make_operator(
            [CommandResult(1, "", f"authentication failed for {secret}")],
            environment={"GH_TOKEN": secret},
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaises(ReleaseError) as raised:
            operator.preflight(["pocket-manage"])

        message = str(raised.exception)
        self.assertIn("gh auth status", message)
        self.assertIn("exit code 1", message)
        self.assertIn("authentication failed", message)
        self.assertNotIn(secret, message)

    def test_command_failure_redacts_short_environment_values(self):
        short_key = "abc"
        operator = make_operator(
            [CommandResult(1, "", f"authentication failed for {short_key}")],
            environment={"API_KEY": short_key},
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaises(ReleaseError) as raised:
            operator.preflight(["pocket-manage"])

        self.assertIn("authentication failed", str(raised.exception))
        self.assertNotIn(short_key, str(raised.exception))

    def test_command_failure_keeps_identifiers_beside_mundane_environment_values(self):
        sha = "2684bfaa406bb9c57b519b969a4f5c142ae34d6a"
        operator = make_operator(
            [CommandResult(1, "", f"merge of {sha} failed")],
            environment={"CLICOLOR": "0", "PYTHONUNBUFFERED": "1"},
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaises(ReleaseError) as raised:
            operator.preflight(["pocket-manage"])

        self.assertIn(sha, str(raised.exception))



class PrepareTests(unittest.TestCase):
    def test_staging_failures_save_state_and_print_resume_without_promotion(self):
        for failure, reason in [("missing", "missing prerequisite origin/staging"),
                                ("diverged", "origin/staging has diverged"),
                                ("push-rejected", "non-fast-forward")]:
            with self.subTest(failure=failure):
                operator, batch = prepared_operator(submodule_changed=False)
                self.addCleanup(operator._test_temporary_directory.cleanup)
                operator.runner.staging_failure = failure
                output = io.StringIO()
                with mock.patch.object(mobile_release, "build_operator", return_value=operator):
                    with mock.patch("sys.stdout", output):
                        code = mobile_release.main([
                            "prepare", "--batch", batch["batch_id"], "--json",
                        ])
                self.assertEqual(code, 1)
                response = json.loads(output.getvalue())
                self.assertIn(reason, response["error"])
                saved = operator.store.load(batch["batch_id"])
                self.assertEqual(saved["state"], "prepare-failed")
                self.assertIn(reason, saved["apps"]["pocket-manage"]["error"])
                self.assertEqual(response["recovery"]["next_command"],
                                 f"python scripts/mobile_release.py prepare --batch {batch['batch_id']}")
                self.assertFalse(any(c.args[:2] == ("gh", "pr") for c in operator.runner.calls))
                if failure != "push-rejected":
                    self.assertFalse(any(c.mutates for c in operator.runner.calls))

    def test_staging_push_uses_prepared_commit_and_precedes_promotion(self):
        operator, batch = prepared_operator(submodule_changed=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)
        result = operator.prepare(batch["batch_id"])
        calls = command_args(operator.runner.calls)
        ancestry = ["git", "merge-base", "--is-ancestor",
                    "refs/remotes/origin/staging", "refs/remotes/origin/dev"]
        push = ["git", "push", "origin", "1" * 40 + ":refs/heads/staging"]
        create = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
        self.assertLess(calls.index(["git", "push", "origin", "HEAD:dev"]), calls.index(ancestry))
        self.assertLess(calls.index(ancestry), calls.index(push))
        self.assertLess(calls.index(push), calls.index(create))
        self.assertEqual(create[create.index("--head") + 1], "staging")
        self.assertEqual(create[create.index("--base") + 1], "release")
        self.assertEqual(approval_rows(result)[0]["staging_sha"], "1" * 40)
        self.assertTrue(any(
            snapshot["apps"]["pocket-manage"].get("staging_sha") == "1" * 40
            and snapshot["apps"]["pocket-manage"].get("preparation_pr") is None
            for snapshot in operator.store.snapshots
        ))

    def test_staging_failure_resumes_same_batch_after_human_repair(self):
        operator, batch = prepared_operator(submodule_changed=False)
        self.addCleanup(operator._test_temporary_directory.cleanup)
        operator.runner.staging_failure = "diverged"
        with self.assertRaises(ReleaseError):
            operator.prepare(batch["batch_id"])
        operator.runner.staging_failure = None
        result = operator.prepare(batch["batch_id"])
        self.assertEqual(result["state"], "awaiting-approval")
        self.assertIsNone(result["apps"]["pocket-manage"]["error"])

    def test_dry_run_plans_staging_for_each_configured_app_without_mutations(self):
        operator, batch = prepared_operator()
        self.addCleanup(operator._test_temporary_directory.cleanup)
        record = batch["apps"]["pocket-manage"]
        batch["selected_apps"] = list(operator.inventory.apps)
        batch["apps"] = {}
        for key, app in operator.inventory.apps.items():
            batch["apps"][key] = dict(record, repository=app.repository)
        operator.store.save(batch)
        result = operator.prepare(batch["batch_id"], dry_run=True)
        for app in result["apps"].values():
            commands = app["planned_commands"]
            self.assertIn(["git", "merge-base", "--is-ancestor",
                           "refs/remotes/origin/staging", "refs/remotes/origin/dev"], commands)
            self.assertIn(["git", "push", "origin",
                           "<prepared-dev-sha>:refs/heads/staging"], commands)
            create = next(c for c in commands if c[:3] == ["gh", "pr", "create"])
            self.assertEqual(create[create.index("--head") + 1], "staging")
            self.assertEqual(create[create.index("--base") + 1], "release")
        self.assertFalse(any(c.mutates for c in operator.runner.calls))
        self.assertEqual(operator.store.load(batch["batch_id"]), batch)


    def test_prepare_dry_run_and_execution_use_the_same_guarded_merge_command(self):
        dry_operator, dry_batch = prepared_operator(
            existing_pr=True,
            submodule_changed=False,
        )
        live_operator, live_batch = prepared_operator(
            existing_pr=True,
            submodule_changed=False,
        )
        self.addCleanup(dry_operator._test_temporary_directory.cleanup)
        self.addCleanup(live_operator._test_temporary_directory.cleanup)

        preview = dry_operator.prepare(dry_batch["batch_id"], dry_run=True)
        live_operator.prepare(live_batch["batch_id"])

        planned = next(
            command
            for command in preview["apps"]["pocket-manage"]["planned_commands"]
            if command[:3] == ["gh", "pr", "merge"]
        )
        executed = list(
            next(
                call.args
                for call in live_operator.runner.calls
                if call.args[:3] == ("gh", "pr", "merge")
            )
        )
        self.assertEqual(planned[planned.index("--match-head-commit") + 1],
                         executed[executed.index("--match-head-commit") + 1])
        self.assertIn("--admin", planned)
        self.assertIn("--admin", executed)

    def test_prepare_stops_when_dev_changes_at_the_atomic_merge_guard(self):
        operator, batch = prepared_operator(
            existing_pr=True,
            promotion_head_race=True,
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "head branch was modified"):
            operator.prepare(batch["batch_id"])

        saved = operator.store.load(batch["batch_id"])
        self.assertEqual(saved["state"], "prepare-failed")
        self.assertEqual(operator.runner.remote_release, "e" * 40)

    def test_prepare_discovers_versions_after_successful_non_dry_run(self):
        operator, batch = prepared_operator(existing_pr=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.prepare(batch["batch_id"])

        self.assertEqual(result["state"], "awaiting-approval")
        self.assertEqual(result["apps"]["pocket-manage"]["version"], "2.7.0")

    def test_prepare_pushes_only_changed_packages_pointer_to_dev(self):
        operator, batch = prepared_operator(submodule_changed=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.prepare(batch["batch_id"])

        calls = command_args(operator.runner.calls)
        self.assertIn(["git", "add", "--", "packages"], calls)
        self.assertIn(["git", "push", "origin", "HEAD:dev"], calls)
        forced = [call.args for call in operator.runner.calls if "--force" in call.args]
        self.assertEqual([args[:3] for args in forced], [("git", "worktree", "remove")])
        self.assertEqual(result["apps"]["pocket-manage"]["dev_push"], "pushed")

    def test_prepare_reports_no_release_changes_as_skip(self):
        operator, batch = prepared_operator(
            submodule_changed=False,
            no_dev_release_diff=True,
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.prepare(batch["batch_id"])

        self.assertEqual(result["apps"]["partner"]["status"], "skipped")
        self.assertEqual(
            result["apps"]["partner"]["skip_reason"],
            "no staging to release changes",
        )

    def test_prepare_skips_when_release_contains_a_different_dev_tip(self):
        operator, batch = prepared_operator(
            submodule_changed=False,
            release_contains_dev=True,
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.prepare(batch["batch_id"])

        app = result["apps"]["pocket-manage"]
        self.assertNotEqual(operator.runner.remote_dev, operator.runner.remote_release)
        self.assertEqual(app["status"], "skipped")
        self.assertEqual(app["skip_reason"], "no staging to release changes")
        self.assertFalse(
            any(call.args[:3] == ("gh", "pr", "list") for call in operator.runner.calls)
        )

    def test_prepare_fetches_an_advanced_release_before_checking_ancestry(self):
        operator, batch = prepared_operator(
            submodule_changed=False,
            release_contains_dev=True,
            release_object_requires_fetch=True,
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)
        batch["apps"]["pocket-manage"]["release_sha"] = "3" * 40
        operator.store.save(batch)

        result = operator.prepare(batch["batch_id"])

        calls = command_args(operator.runner.calls)
        fetch = [
            "git",
            "fetch",
            "origin",
            "+refs/heads/staging:refs/remotes/origin/staging",
            "+refs/heads/release:refs/remotes/origin/release",
        ]
        fetch_index = calls.index(fetch)
        ancestry_index = next(
            index
            for index, command in enumerate(calls)
            if command == ["git", "merge-base", "--is-ancestor",
                           "refs/remotes/origin/staging", "refs/remotes/origin/release"]
        )
        self.assertLess(fetch_index, ancestry_index)
        self.assertEqual(result["apps"]["pocket-manage"]["status"], "skipped")

    def test_prepare_records_rejected_dev_push(self):
        operator, batch = prepared_operator(push_rejected=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "dev push rejected"):
            operator.prepare(batch["batch_id"])

        saved = operator.store.load(batch["batch_id"])
        self.assertEqual(saved["state"], "prepare-failed")
        self.assertEqual(saved["apps"]["pocket-manage"]["status"], "failed")
        self.assertIn("dev push rejected", saved["apps"]["pocket-manage"]["error"])

    def test_prepare_reuses_one_exact_preparation_pr(self):
        operator, batch = prepared_operator(existing_pr=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.prepare(batch["batch_id"])

        self.assertEqual(
            result["apps"]["pocket-manage"]["preparation_pr"]["number"],
            17,
        )
        self.assertFalse(
            any(call.args[:3] == ("gh", "pr", "create") for call in operator.runner.calls)
        )

    def test_prepare_rejects_duplicate_preparation_prs(self):
        operator, batch = prepared_operator(duplicate_pr=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "duplicate preparation pull requests"):
            operator.prepare(batch["batch_id"])

        self.assertEqual(operator.store.load(batch["batch_id"])["state"], "prepare-failed")

    def test_prepare_rejects_failed_preparation_checks(self):
        operator, batch = prepared_operator(existing_pr=True, failed_checks=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "preparation checks failed"):
            operator.prepare(batch["batch_id"])

        self.assertFalse(
            any(call.args[:3] == ("gh", "pr", "merge") for call in operator.runner.calls)
        )

    def test_prepare_waits_for_pending_checks_to_pass(self):
        operator, batch = prepared_operator(
            existing_pr=True,
            pending_check_outcome="pass",
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.prepare(batch["batch_id"])

        self.assertEqual(result["apps"]["pocket-manage"]["status"], "prepared")
        self.assertEqual(operator.runner.check_calls, 2)

    def test_prepare_waits_for_pending_checks_to_fail(self):
        operator, batch = prepared_operator(
            existing_pr=True,
            pending_check_outcome="fail",
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "preparation checks failed"):
            operator.prepare(batch["batch_id"])

        self.assertEqual(operator.runner.check_calls, 2)
        self.assertFalse(
            any(call.args[:3] == ("gh", "pr", "merge") for call in operator.runner.calls)
        )

    def test_prepare_accepts_no_checks_only_when_github_reports_mergeable(self):
        operator, batch = prepared_operator(existing_pr=True, no_checks=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.prepare(batch["batch_id"])

        self.assertEqual(result["apps"]["pocket-manage"]["status"], "prepared")

    def test_prepare_rejects_merge_conflicts(self):
        operator, batch = prepared_operator(existing_pr=True, merge_conflict=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "preparation pull request is not mergeable"):
            operator.prepare(batch["batch_id"])

        self.assertFalse(
            any(call.args[:3] == ("gh", "pr", "merge") for call in operator.runner.calls)
        )

    def test_prepare_preserves_dirty_worktree(self):
        operator, batch = prepared_operator(dirty_worktree=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.prepare(batch["batch_id"])

        app = result["apps"]["pocket-manage"]
        self.assertIn(".claude/worktrees/", app["worktree"])
        self.assertFalse(
            any(call.args[:3] == ("git", "worktree", "remove") for call in operator.runner.calls)
        )

    def test_prepare_removes_the_worktree_holding_the_packages_submodule(self):
        operator, batch = prepared_operator()
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.prepare(batch["batch_id"])

        app = result["apps"]["pocket-manage"]
        self.assertIsNone(app["worktree"])
        self.assertEqual(app["status"], "prepared")

    def test_prepare_merges_the_preparation_pr_with_administrator_privileges(self):
        operator, batch = prepared_operator(existing_pr=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        operator.prepare(batch["batch_id"])

        merge = next(
            call
            for call in operator.runner.calls
            if call.args[:3] == ("gh", "pr", "merge")
        )
        self.assertIn("--admin", merge.args)

    def _bump_commit_message(self, operator):
        commit = next(
            call for call in operator.runner.calls if call.args[:2] == ("git", "commit")
        )
        return commit.args[commit.args.index("-m") + 1]

    def test_a_bump_carrying_a_feature_is_committed_as_a_feature(self):
        operator, batch = prepared_operator(
            carried_commits=["feat: new telemetry module", "fix: text size"]
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        operator.prepare(batch["batch_id"])

        message = self._bump_commit_message(operator)
        self.assertTrue(message.startswith("feat: update shared packages"), message)
        self.assertIn("feat: new telemetry module", message)

    def test_a_bump_carrying_only_fixes_is_committed_as_a_fix(self):
        operator, batch = prepared_operator(
            carried_commits=["fix: text size", "fix: wo approval"]
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        operator.prepare(batch["batch_id"])

        self.assertTrue(
            self._bump_commit_message(operator).startswith("fix: update shared packages")
        )

    def test_a_bump_carrying_no_releasable_work_stays_a_chore(self):
        operator, batch = prepared_operator(
            carried_commits=["chore: bump lints", "docs: readme"]
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        operator.prepare(batch["batch_id"])

        self.assertTrue(
            self._bump_commit_message(operator).startswith("chore: update shared packages")
        )

    def test_a_bump_carrying_a_breaking_change_never_cuts_a_major(self):
        operator, batch = prepared_operator(
            carried_commits=["feat!: drop the legacy client", "fix: text size"]
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        operator.prepare(batch["batch_id"])

        message = self._bump_commit_message(operator)
        self.assertTrue(message.startswith("feat: update shared packages"), message)
        self.assertNotIn("BREAKING CHANGE:", message)
        self.assertNotIn("!:", message.splitlines()[0])

    def test_unknown_mergeability_is_waited_for_rather_than_refused(self):
        operator, batch = prepared_operator(
            existing_pr=True, mergeable_unknown_first=True
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)
        operator.clock = FakeClock()
        operator.poll_policy = PollPolicy(interval_seconds=2, timeout_seconds=10)

        operator.prepare(batch["batch_id"])

        self.assertEqual(operator.clock.sleeps, [2])
        self.assertTrue(
            any(call.args[:3] == ("gh", "pr", "merge") for call in operator.runner.calls)
        )

    def test_prepare_deletes_the_deployment_branch_with_its_worktree(self):
        operator, batch = prepared_operator()
        self.addCleanup(operator._test_temporary_directory.cleanup)

        operator.prepare(batch["batch_id"])

        branch = f"deploy-mobile-apps/{batch['batch_id']}/pocket-manage"
        self.assertIn(["git", "branch", "-D", branch], command_args(operator.runner.calls))

    def test_prepare_names_why_the_preparation_pr_is_not_mergeable(self):
        operator, batch = prepared_operator(existing_pr=True, merge_conflict=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "not mergeable.*CONFLICTING.*DIRTY"):
            operator.prepare(batch["batch_id"])

    def test_prepare_resumes_after_completed_preparation_merge(self):
        operator, batch = prepared_operator(resumed=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.prepare(batch["batch_id"])

        self.assertEqual(result["state"], "awaiting-approval")
        self.assertEqual(result["apps"]["pocket-manage"]["status"], "prepared")
        self.assertFalse(any(call.mutates for call in operator.runner.calls))

    def test_prepare_dry_run_records_plans_without_mutating(self):
        operator, batch = prepared_operator(submodule_changed=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.prepare(batch["batch_id"], dry_run=True)

        self.assertEqual(result["state"], "dry-run-complete")
        self.assertFalse(any(call.mutates for call in operator.runner.calls))
        planned = result["apps"]["pocket-manage"]["planned_commands"]
        self.assertIn(["git", "push", "origin", "HEAD:dev"], planned)
        self.assertTrue(any(command[:3] == ["gh", "pr", "create"] for command in planned))

    def test_prepare_dry_run_plans_future_head_without_checking_current_pr(self):
        operator, batch = prepared_operator(existing_pr=True, merge_conflict=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.prepare(batch["batch_id"], dry_run=True)

        self.assertEqual(result["state"], "dry-run-complete")
        app = result["apps"]["pocket-manage"]
        self.assertEqual(app["planned_staging_sha"], "<prepared-dev-sha>")
        self.assertFalse(any(call.mutates for call in operator.runner.calls))
        self.assertEqual(operator.store.load(batch["batch_id"]), batch)

    def test_prepare_saves_after_each_successful_remote_mutation(self):
        operator, batch = prepared_operator(submodule_changed=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        operator.prepare(batch["batch_id"])

        app_snapshots = [
            snapshot["apps"]["pocket-manage"] for snapshot in operator.store.snapshots
        ]
        self.assertTrue(any(app.get("dev_push") == "pushed" for app in app_snapshots))
        self.assertTrue(any(app.get("dev_sha") == "1" * 40 for app in app_snapshots))
        self.assertTrue(
            any((app.get("preparation_pr") or {}).get("status") == "open" for app in app_snapshots)
        )
        self.assertTrue(
            any(
                (app.get("preparation_pr") or {}).get("status") == "merged"
                for app in app_snapshots
            )
        )

    def test_prepare_resumes_after_a_recorded_dev_push(self):
        operator, batch = prepared_operator(submodule_changed=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)
        app = batch["apps"]["pocket-manage"]
        app.update(
            {
                "worktree": None,
                "submodule_before": "c" * 40,
                "submodule_after": "f" * 40,
                "dev_push": "pushed",
                "preparation_pr": None,
                "dev_sha": "1" * 40,
                "status": "failed",
                "error": "interrupted after push",
            }
        )
        batch["state"] = "prepare-failed"
        operator.runner.remote_dev = "1" * 40
        operator.store.save(batch)

        result = operator.prepare(batch["batch_id"])

        self.assertEqual(result["apps"]["pocket-manage"]["status"], "prepared")
        pushes = [
            call
            for call in operator.runner.calls
            if call.args == ("git", "push", "origin", "HEAD:dev")
        ]
        self.assertEqual(pushes, [])


class DiscoveryTests(unittest.TestCase):
    def test_discover_versions_reads_manifest_at_release_pr_head(self):
        operator, batch = operator_after_preparation()
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.discover_versions(batch)

        app = result["apps"]["pocket-manage"]
        self.assertEqual(app["version"], "2.7.0")
        self.assertEqual(app["release_pr_head_sha"], "a" * 40)
        self.assertEqual(app["release_pr_base"], "release")
        self.assertEqual(app["release_pr_labels"], ["autorelease: pending"])
        self.assertEqual(result["state"], "awaiting-approval")

    def test_no_release_pr_is_a_nonblocking_reported_skip(self):
        operator, batch = operator_after_preparation(no_release_pr_for="partner")
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.discover_versions(batch)

        self.assertEqual(
            result["apps"]["partner"]["skip_reason"],
            "no releasable changes",
        )
        self.assertEqual(result["apps"]["installers"]["version"], "1.3.0")

    def test_a_skipped_app_records_the_commits_that_produced_no_version(self):
        operator, batch = operator_after_preparation(no_release_pr_for="partner")
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.discover_versions(batch)

        self.assertEqual(
            result["apps"]["partner"]["unreleased_commits"],
            ["chore: notifications", "chore: migrate more pages"],
        )

    def test_a_released_app_records_no_unreleased_commits(self):
        operator, batch = operator_after_preparation(no_release_pr_for="partner")
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.discover_versions(batch)

        self.assertEqual(result["apps"]["installers"].get("unreleased_commits", []), [])

    def test_approval_rows_include_complete_release_snapshot_data(self):
        operator, batch = operator_after_preparation()
        self.addCleanup(operator._test_temporary_directory.cleanup)
        result = operator.discover_versions(batch)

        self.assertEqual(
            approval_rows(result),
            [
                {
                    "app": "pocket-manage",
                    "version": "2.7.0",
                    "pr_number": 42,
                    "url": "https://github.com/MarketplaceSoftware/pocketmanage/pull/42",
                    "checks": [
                        {"name": "release-please", "conclusion": "SUCCESS"}
                    ],
                    "base": "release",
                    "labels": ["autorelease: pending"],
                    "head_sha": "a" * 40,
                    "staging_sha": None,
                    "branches": [],
                    "submodule_sha": "f" * 40,
                    "result": "ready for approval",
                    "unreleased_commits": [],
                }
            ],
        )

    def test_discovery_polls_with_injected_clock_until_workflow_completes(self):
        operator, batch = operator_after_preparation()
        self.addCleanup(operator._test_temporary_directory.cleanup)
        clock = FakeClock()
        operator.clock = clock
        operator.poll_policy = PollPolicy(interval_seconds=2, timeout_seconds=10)
        operator.runner.workflow_statuses = deque(["queued", "in_progress", "completed"])
        operator.runner.last_workflow_status = "completed"

        result = operator.discover_versions(batch)

        self.assertEqual(result["apps"]["pocket-manage"]["version"], "2.7.0")
        self.assertEqual(clock.sleeps, [2, 2])

    def test_discovery_times_out_without_waiting_in_real_time(self):
        operator, batch = operator_after_preparation()
        self.addCleanup(operator._test_temporary_directory.cleanup)
        clock = FakeClock()
        operator.clock = clock
        operator.poll_policy = PollPolicy(interval_seconds=2, timeout_seconds=4)
        operator.runner.workflow_statuses = deque(["queued"])
        operator.runner.last_workflow_status = "queued"

        with self.assertRaisesRegex(ReleaseError, "timed out"):
            operator.discover_versions(batch)

        self.assertEqual(clock.sleeps, [2, 2])

    def test_discovery_decodes_line_wrapped_github_content(self):
        operator, batch = operator_after_preparation()
        self.addCleanup(operator._test_temporary_directory.cleanup)
        operator.runner.wrap_manifest_content = True

        result = operator.discover_versions(batch)

        self.assertEqual(result["apps"]["pocket-manage"]["version"], "2.7.0")

    def test_discovery_rejects_duplicate_release_please_pull_requests(self):
        operator, batch = operator_after_preparation()
        self.addCleanup(operator._test_temporary_directory.cleanup)
        operator.runner.duplicate_release_pr = True

        with self.assertRaisesRegex(ReleaseError, "duplicate Release Please"):
            operator.discover_versions(batch)


class ReleaseTests(unittest.TestCase):
    def test_release_dry_run_and_execution_use_the_same_guarded_merge_command(self):
        dry_operator, dry_batch = awaiting_approval_operator()
        live_operator, live_batch = awaiting_approval_operator()
        self.addCleanup(dry_operator._test_temporary_directory.cleanup)
        self.addCleanup(live_operator._test_temporary_directory.cleanup)

        preview = dry_operator.release(dry_batch["batch_id"], dry_run=True)
        live_operator.release(live_batch["batch_id"])

        planned = preview["planned_release_merges"][0]["command"]
        executed = list(
            next(
                call.args
                for call in live_operator.runner.calls
                if call.args[:3] == ("gh", "pr", "merge")
            )
        )
        self.assertEqual(planned, executed)

    def test_release_dry_run_revalidates_and_reports_the_exact_merge(self):
        operator, batch = awaiting_approval_operator()
        self.addCleanup(operator._test_temporary_directory.cleanup)
        snapshot_count = len(operator.store.snapshots)

        result = operator.release(batch["batch_id"], dry_run=True)

        self.assertEqual(
            result["planned_release_merges"],
            [
                {
                    "repository": "MarketplaceSoftware/pocketmanage",
                    "pull_request_number": 42,
                    "head_sha": "a" * 40,
                    "command": [
                        "gh",
                        "pr",
                        "merge",
                        "42",
                        "--repo",
                        "MarketplaceSoftware/pocketmanage",
                        "--merge",
                        "--admin",
                        "--match-head-commit",
                        "a" * 40,
                    ],
                }
            ],
        )
        self.assertFalse(any(call.mutates for call in operator.runner.calls))
        self.assertTrue(
            any(call.args[:3] == ("gh", "pr", "view") for call in operator.runner.calls)
        )
        self.assertTrue(
            any(
                call.args[:2] == ("gh", "api") and "/contents/" in call.args[2]
                for call in operator.runner.calls
            )
        )
        self.assertEqual(len(operator.store.snapshots), snapshot_count)
        self.assertNotIn(
            "planned_release_merges",
            operator.store.load(batch["batch_id"]),
        )

    def test_changed_release_pr_head_invalidates_batch_approval(self):
        operator, batch = awaiting_approval_operator(current_head="b" * 40)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "approval snapshot changed"):
            operator.release(batch["batch_id"])

        saved = operator.store.load(batch["batch_id"])
        self.assertEqual(saved["state"], "awaiting-approval")
        self.assertEqual(saved["apps"]["pocket-manage"]["release_pr_head_sha"], "b" * 40)
        self.assertFalse(any(call.mutates for call in operator.runner.calls))

    def test_changed_release_version_invalidates_batch_approval(self):
        operator, batch = awaiting_approval_operator(changed_version=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "approval snapshot changed"):
            operator.release(batch["batch_id"])

        saved = operator.store.load(batch["batch_id"])
        self.assertEqual(saved["apps"]["pocket-manage"]["version"], "2.7.1")
        self.assertFalse(any(call.mutates for call in operator.runner.calls))

    def test_missing_release_label_invalidates_batch_approval(self):
        operator, batch = awaiting_approval_operator(missing_label=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "approval snapshot changed"):
            operator.release(batch["batch_id"])

        self.assertEqual(operator.store.load(batch["batch_id"])["state"], "awaiting-approval")
        self.assertFalse(any(call.mutates for call in operator.runner.calls))

    def test_extra_release_label_invalidates_batch_approval(self):
        operator, batch = awaiting_approval_operator(extra_label=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "approval snapshot changed"):
            operator.release(batch["batch_id"])

        saved = operator.store.load(batch["batch_id"])
        self.assertEqual(
            saved["apps"]["pocket-manage"]["release_pr_labels"],
            ["autorelease: pending", "unexpected"],
        )
        self.assertFalse(any(call.mutates for call in operator.runner.calls))

    def test_changed_release_base_invalidates_batch_approval(self):
        operator, batch = awaiting_approval_operator(changed_base=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "approval snapshot changed"):
            operator.release(batch["batch_id"])

        self.assertEqual(operator.store.load(batch["batch_id"])["state"], "awaiting-approval")
        self.assertFalse(any(call.mutates for call in operator.runner.calls))

    def test_failed_release_checks_invalidate_batch_approval(self):
        operator, batch = awaiting_approval_operator(failed_checks=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "approval snapshot changed"):
            operator.release(batch["batch_id"])

        saved_checks = operator.store.load(batch["batch_id"])["apps"]["pocket-manage"][
            "release_pr_checks"
        ]
        self.assertEqual(saved_checks[0]["conclusion"], "FAILURE")
        self.assertFalse(any(call.mutates for call in operator.runner.calls))

    def test_changed_repository_invalidates_batch_approval(self):
        operator, batch = awaiting_approval_operator()
        self.addCleanup(operator._test_temporary_directory.cleanup)
        batch["apps"]["pocket-manage"]["repository"] = "MarketplaceSoftware/other"
        operator.store.save(batch)

        with self.assertRaisesRegex(ReleaseError, "approval snapshot changed"):
            operator.release(batch["batch_id"])

        self.assertFalse(any(call.mutates for call in operator.runner.calls))

    def test_release_revalidates_every_app_before_first_merge(self):
        operator, batch = awaiting_approval_operator(
            app_keys=["pocket-manage", "installers"]
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        operator.release(batch["batch_id"])

        first_mutation = next(
            index for index, call in enumerate(operator.runner.calls) if call.mutates
        )
        repositories_read = {
            call.args[call.args.index("--repo") + 1]
            for call in operator.runner.calls[:first_mutation]
            if call.args[:3] == ("gh", "pr", "view")
        }
        self.assertEqual(
            repositories_read,
            {
                "MarketplaceSoftware/pocketmanage",
                "MarketplaceSoftware/pocketmanage_installers",
            },
        )

    def test_partial_release_records_each_success_before_stopping(self):
        operator, batch = awaiting_approval_operator(
            app_keys=["pocket-manage", "installers"], second_merge_fails=True
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "partial release"):
            operator.release(batch["batch_id"])

        saved = operator.store.load(batch["batch_id"])
        self.assertEqual(saved["state"], "partial-release")
        self.assertEqual(saved["apps"]["pocket-manage"]["release_merge"], "merged")
        self.assertEqual(saved["apps"]["installers"]["release_merge"], "failed")
        merged_snapshot = next(
            snapshot
            for snapshot in operator.store.snapshots
            if snapshot["apps"]["pocket-manage"].get("release_merge") == "merged"
        )
        self.assertNotEqual(merged_snapshot["state"], "partial-release")

    def test_release_merge_is_bound_to_the_approved_head(self):
        operator, batch = awaiting_approval_operator()
        self.addCleanup(operator._test_temporary_directory.cleanup)

        operator.release(batch["batch_id"])

        merge = next(
            call
            for call in operator.runner.calls
            if call.args[:3] == ("gh", "pr", "merge")
        )
        self.assertIn("--match-head-commit", merge.args)
        self.assertEqual(merge.args[-1], "a" * 40)

    def test_release_proceeds_when_the_release_pr_carries_no_checks(self):
        operator, batch = awaiting_approval_operator(no_release_checks=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.release(batch["batch_id"])

        self.assertEqual(result["apps"]["pocket-manage"]["release_merge"], "merged")

    def test_release_refuses_an_uncheckable_pr_github_will_not_merge(self):
        operator, batch = awaiting_approval_operator(
            no_release_checks=True, release_mergeable="CONFLICTING"
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaises(ReleaseError):
            operator.release(batch["batch_id"])

        self.assertFalse(
            any(call.args[:3] == ("gh", "pr", "merge") for call in operator.runner.calls)
        )

    def test_checks_failing_since_approval_are_named_as_checks(self):
        operator, batch = awaiting_approval_operator(failed_checks=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)
        approved = operator.store.load(batch["batch_id"])
        for record in approved["apps"].values():
            record["release_pr_checks"] = [
                {
                    "name": "release-please",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                }
            ]
        operator.store.save(approved)

        with self.assertRaisesRegex(ReleaseError, "checks are not passing"):
            operator.release(batch["batch_id"])

    def test_a_tag_that_has_not_appeared_yet_is_waited_for(self):
        operator, batch = awaiting_approval_operator(tag_missing_first=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.release(batch["batch_id"])

        self.assertEqual(result["apps"]["pocket-manage"]["tag"], "v2.7.0")

    def test_head_race_rejection_records_no_successful_merge(self):
        operator, batch = awaiting_approval_operator(head_race=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "release failed"):
            operator.release(batch["batch_id"])

        saved = operator.store.load(batch["batch_id"])
        self.assertEqual(saved["state"], "release-failed")
        self.assertEqual(saved["apps"]["pocket-manage"]["release_merge"], "failed")
        self.assertEqual(operator.runner.successful_merges, [])

    def test_failed_first_merge_sets_release_failed(self):
        operator, batch = awaiting_approval_operator(
            app_keys=["installers"], second_merge_fails=True
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "release failed"):
            operator.release(batch["batch_id"])

        self.assertEqual(operator.store.load(batch["batch_id"])["state"], "release-failed")

    def test_release_resumes_after_confirming_one_successful_merge(self):
        operator, batch = awaiting_approval_operator(
            app_keys=["pocket-manage", "installers"], resumed_merge=True
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.release(batch["batch_id"])

        merges = [call for call in operator.runner.calls if call.args[:3] == ("gh", "pr", "merge")]
        confirmed = [
            call
            for call in operator.runner.calls
            if call.args[:4] == ("gh", "pr", "view", "42")
        ]
        self.assertTrue(confirmed)
        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0].args[3], "43")
        self.assertEqual(result["state"], "released")

    def test_release_resumes_a_successful_merge_whose_confirmation_was_interrupted(self):
        operator, batch = awaiting_approval_operator(merge_confirmation_fails=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "partial release"):
            operator.release(batch["batch_id"])

        saved = operator.store.load(batch["batch_id"])
        self.assertEqual(saved["state"], "partial-release")
        self.assertEqual(saved["apps"]["pocket-manage"]["release_merge"], "merge-unverified")

        operator.runner.merge_confirmation_fails = False
        result = operator.release(batch["batch_id"])
        merges = [call for call in operator.runner.calls if call.args[:3] == ("gh", "pr", "merge")]
        self.assertEqual(len(merges), 1)
        self.assertEqual(result["state"], "released")

    def test_release_reconciles_a_merge_completed_before_the_first_local_save(self):
        operator, batch = awaiting_approval_operator(already_merged=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.release(batch["batch_id"])

        app = result["apps"]["pocket-manage"]
        self.assertEqual(app["release_merge"], "merged")
        self.assertEqual(app["release_merge_sha"], "4" * 40)
        self.assertEqual(result["state"], "released")
        self.assertFalse(any(call.mutates for call in operator.runner.calls))

    def test_release_accepts_a_lightweight_tag_on_the_merge_commit(self):
        operator, batch = awaiting_approval_operator()
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.release(batch["batch_id"])

        app = result["apps"]["pocket-manage"]
        self.assertEqual(app["tag_commit_sha"], app["release_merge_sha"])
        self.assertEqual(result["state"], "released")

    def test_release_dereferences_an_annotated_tag(self):
        operator, batch = awaiting_approval_operator(annotated_tag=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.release(batch["batch_id"])

        self.assertEqual(result["apps"]["pocket-manage"]["tag_commit_sha"], "4" * 40)
        self.assertTrue(
            any("/git/tags/" in call.args[2] for call in operator.runner.calls if call.args[:2] == ("gh", "api"))
        )

    def test_release_rejects_a_tag_on_the_wrong_commit(self):
        operator, batch = awaiting_approval_operator(wrong_tag=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "tag does not match release merge"):
            operator.release(batch["batch_id"])

        self.assertEqual(operator.store.load(batch["batch_id"])["state"], "release-failed")

    def test_release_rejects_a_missing_github_release(self):
        operator, batch = awaiting_approval_operator(missing_release=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "GitHub release"):
            operator.release(batch["batch_id"])

        self.assertEqual(operator.store.load(batch["batch_id"])["state"], "release-failed")

    def test_release_reports_all_codemagic_links(self):
        operator, batch = awaiting_approval_operator(codemagic_checks="queued")
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.release(batch["batch_id"])

        checks = result["apps"]["pocket-manage"]["codemagic_checks"]
        self.assertEqual(set(checks), set(operator.inventory.codemagic_checks))
        self.assertTrue(
            all(item["details_url"].startswith("https://codemagic.io/") for item in checks.values())
        )

    def test_release_stops_polling_when_all_exact_checks_appear(self):
        operator, batch = awaiting_approval_operator(codemagic_checks="mixed")
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.release(batch["batch_id"])

        checks = result["apps"]["pocket-manage"]["codemagic_checks"]
        self.assertEqual(checks[operator.inventory.codemagic_checks[0]]["conclusion"], "failure")
        check_calls = [
            call for call in operator.runner.calls if call.args[:2] == ("gh", "api") and "/check-runs" in call.args[2]
        ]
        self.assertEqual(len(check_calls), 1)

    def test_observing_builds_uses_its_own_budget_not_the_full_timeout(self):
        operator, batch = awaiting_approval_operator(codemagic_checks="timeout")
        self.addCleanup(operator._test_temporary_directory.cleanup)
        operator.poll_policy = PollPolicy(
            interval_seconds=2, timeout_seconds=300, observation_timeout_seconds=4
        )

        operator.release(batch["batch_id"])

        self.assertEqual(operator.clock.sleeps, [2, 2])

    def test_an_unobserved_build_is_reported_as_unobserved_not_absent(self):
        operator, batch = awaiting_approval_operator(codemagic_checks="timeout")
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.release(batch["batch_id"])

        self.assertIn("observed", result["apps"]["pocket-manage"]["result"])
        self.assertNotIn("started", result["apps"]["pocket-manage"]["result"])

    def test_codemagic_timeout_preserves_release_links(self):
        operator, batch = awaiting_approval_operator(codemagic_checks="timeout")
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.release(batch["batch_id"])

        app = result["apps"]["pocket-manage"]
        self.assertEqual(result["state"], "released-builds-unverified")
        self.assertEqual(app["build_verification"], "unverified")
        self.assertIn("/releases/tag/v2.7.0", app["tag_url"])
        self.assertIn("/commit/", app["commit_url"])
        self.assertEqual(operator.clock.sleeps, [2, 2])

    def test_a_started_build_finishes_the_release_and_names_the_rest(self):
        operator, batch = awaiting_approval_operator(
            codemagic_checks="partial-timeout"
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.release(batch["batch_id"])

        app = result["apps"]["pocket-manage"]
        observed_name = operator.inventory.codemagic_checks[0]
        self.assertEqual(
            app["codemagic_checks"],
            {
                observed_name: {
                    "status": "queued",
                    "conclusion": None,
                    "details_url": "https://codemagic.io/app/check-0",
                }
            },
        )
        self.assertEqual(
            app["codemagic_missing_checks"],
            list(operator.inventory.codemagic_checks[1:]),
        )
        self.assertEqual(app["build_verification"], "started")
        self.assertEqual(result["state"], "released")
        self.assertIsNone(app["error"])

    def test_release_preserves_skipped_apps_without_remote_calls(self):
        operator, batch = awaiting_approval_operator(
            app_keys=["partner"], skipped_apps=["partner"]
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.release(batch["batch_id"])

        self.assertEqual(result["state"], "released")
        self.assertEqual(result["apps"]["partner"]["skip_reason"], "no releasable changes")
        self.assertEqual(operator.runner.calls, [])

    def test_status_is_idempotent_and_read_only(self):
        operator, batch = awaiting_approval_operator()
        self.addCleanup(operator._test_temporary_directory.cleanup)
        snapshot_count = len(operator.store.snapshots)

        first = operator.status(batch["batch_id"])
        second = operator.status(batch["batch_id"])

        self.assertEqual(first, second)
        self.assertEqual(operator.runner.calls, [])
        self.assertEqual(len(operator.store.snapshots), snapshot_count)
