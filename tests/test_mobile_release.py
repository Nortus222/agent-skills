import copy
import json
import sys
import tempfile
import unittest
from collections import deque
from dataclasses import dataclass
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "deploy-mobile-apps"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from mobile_release_lib import (
    BatchStore,
    CommandResult,
    ReleaseError,
    ReleaseOperator,
    load_inventory,
    new_batch,
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
        no_checks=False,
        merge_conflict=False,
        dirty_worktree=False,
        push_rejected=False,
        remote_dev=None,
        remote_release=None,
        resumed=False,
    ):
        self.calls = []
        self.existing_pr = existing_pr
        self.duplicate_pr = duplicate_pr
        self.failed_checks = failed_checks
        self.no_checks = no_checks
        self.merge_conflict = merge_conflict
        self.dirty_worktree = dirty_worktree
        self.push_rejected = push_rejected
        self.remote_dev = remote_dev or "d" * 40
        self.remote_release = remote_release or "r" * 40
        self.resumed = resumed

    def run(self, args, *, cwd=None, mutates=False):
        self.calls.append(RecordedCall(tuple(args), cwd, mutates))
        command = tuple(args)

        if command == ("git", "ls-remote", "--exit-code", "origin", "refs/heads/dev"):
            return CommandResult(0, f"{self.remote_dev}\trefs/heads/dev\n", "")
        if command == ("git", "ls-remote", "--exit-code", "origin", "refs/heads/release"):
            return CommandResult(0, f"{self.remote_release}\trefs/heads/release\n", "")
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
        if command == ("git", "commit", "-m", "chore: update shared packages"):
            return CommandResult(0, "", "")
        if command == ("git", "rev-parse", "HEAD^{commit}"):
            return CommandResult(0, f"{'n' * 40}\n", "")
        if command == ("git", "push", "origin", "HEAD:dev"):
            if self.push_rejected:
                return CommandResult(1, "", "non-fast-forward")
            self.remote_dev = "n" * 40
            return CommandResult(0, "", "")
        if command == ("git", "status", "--porcelain"):
            output = "?? unexpected.txt\n" if self.dirty_worktree else ""
            return CommandResult(0, output, "")
        if command[:3] == ("git", "worktree", "remove"):
            return CommandResult(0, "", "")
        if command[:3] == ("gh", "pr", "list"):
            pull_requests = []
            if self.existing_pr or self.duplicate_pr:
                pull_requests.append(
                    {
                        "number": 17,
                        "url": "https://github.com/MarketplaceSoftware/pocketmanage/pull/17",
                        "headRefName": "dev",
                        "baseRefName": "release",
                    }
                )
            if self.duplicate_pr:
                pull_requests.append(
                    {
                        "number": 18,
                        "url": "https://github.com/MarketplaceSoftware/pocketmanage/pull/18",
                        "headRefName": "dev",
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
            if self.no_checks:
                return CommandResult(1, "", "no checks reported")
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
                        "headRefName": "dev",
                        "baseRefName": "release",
                    }
                ),
                "",
            )
        if command[:3] == ("gh", "pr", "merge"):
            self.remote_release = "m" * 40
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected command: {args}")


def command_args(calls):
    return [list(call.args) for call in calls]


def flatten_command_args(calls):
    return [argument for call in calls for argument in call.args]


def prepared_operator(
    *,
    submodule_changed=True,
    no_dev_release_diff=False,
    existing_pr=False,
    duplicate_pr=False,
    failed_checks=False,
    no_checks=False,
    merge_conflict=False,
    dirty_worktree=False,
    push_rejected=False,
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
    release_sha = dev_sha if no_dev_release_diff else "r" * 40
    remote_dev = "n" * 40 if resumed else dev_sha
    remote_release = "m" * 40 if resumed else release_sha
    runner = PreparationRunner(
        existing_pr=existing_pr,
        duplicate_pr=duplicate_pr,
        failed_checks=failed_checks,
        no_checks=no_checks,
        merge_conflict=merge_conflict,
        dirty_worktree=dirty_worktree,
        push_rejected=push_rejected,
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
        "packages_pointer_sha": "c" * 40 if submodule_changed else "p" * 40,
        "packages_sha": "p" * 40,
        "dev_to_release_pr": None,
    }
    if resumed:
        batch["state"] = "prepare-failed"
        batch["apps"][app_key].update(
            {
                "worktree": None,
                "submodule_before": "c" * 40,
                "submodule_after": "p" * 40,
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


def successful_preflight_responses(app_count=3):
    responses = [CommandResult(0, "github.com\n", "")]
    repositories = (
        "MarketplaceSoftware/pocketmanage",
        "MarketplaceSoftware/pocketmanage_installers",
        "MarketplaceSoftware/pocketmanage_partner",
    )
    responses.extend(CommandResult(0, f"git@github.com:{repository}.git\n", "") for repository in repositories)
    for _ in range(app_count):
        responses.extend(
            [
                CommandResult(0, "", ""),
                CommandResult(0, f"{'d' * 40}\n{'r' * 40}\n", ""),
                CommandResult(0, f"160000 commit {'c' * 40}\tpackages\n", ""),
                CommandResult(0, "git@github.com:MarketplaceSoftware/packages.git\n", ""),
                CommandResult(0, "", ""),
                CommandResult(0, f"{'p' * 40}\n", ""),
                CommandResult(0, "{}\n{}\n", ""),
                CommandResult(
                    0,
                    "\n".join(
                        [
                            "workflows:",
                            "  android:",
                            "    name: Build Android AppBundle and Publish",
                            "  ios:",
                            "    name: Build IPA and Publish To AppStore Connect",
                            "  web:",
                            "    name: Build Web and Publish to Firebase Hosting",
                        ]
                    ),
                    "",
                ),
                CommandResult(0, "[]\n", ""),
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


class PreflightTests(unittest.TestCase):
    def test_preflight_records_exact_remote_shas_for_every_app(self):
        operator = make_operator(successful_preflight_responses())
        self.addCleanup(operator._test_temporary_directory.cleanup)

        batch = operator.preflight(["pocket-manage", "installers", "partner"])

        self.assertEqual(batch["state"], "preflight-complete")
        self.assertEqual(batch["apps"]["pocket-manage"]["dev_sha"], "d" * 40)
        self.assertEqual(batch["apps"]["pocket-manage"]["packages_sha"], "p" * 40)
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
        responses[-1] = CommandResult(
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

        with self.assertRaisesRegex(ReleaseError, "duplicate dev to release pull requests"):
            operator.preflight(["pocket-manage"])

        self.assertEqual(list(operator.store.root.glob("*.json")), [])

    def test_preflight_rejects_mismatched_packages_origin(self):
        responses = successful_preflight_responses(app_count=1)
        responses[7] = CommandResult(
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
        responses[-2] = CommandResult(
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


class PrepareTests(unittest.TestCase):
    def test_prepare_pushes_only_changed_packages_pointer_to_dev(self):
        operator, batch = prepared_operator(submodule_changed=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.prepare(batch["batch_id"])

        calls = command_args(operator.runner.calls)
        self.assertIn(["git", "add", "--", "packages"], calls)
        self.assertIn(["git", "push", "origin", "HEAD:dev"], calls)
        self.assertNotIn("--force", flatten_command_args(operator.runner.calls))
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
            "no dev to release changes",
        )

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

    def test_prepare_resumes_after_completed_preparation_merge(self):
        operator, batch = prepared_operator(resumed=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        result = operator.prepare(batch["batch_id"])

        self.assertEqual(result["state"], "prepare-complete")
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

    def test_prepare_dry_run_observes_existing_pr_mergeability(self):
        operator, batch = prepared_operator(
            existing_pr=True,
            merge_conflict=True,
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)

        with self.assertRaisesRegex(ReleaseError, "preparation pull request is not mergeable"):
            operator.prepare(batch["batch_id"], dry_run=True)

        self.assertFalse(any(call.mutates for call in operator.runner.calls))

    def test_prepare_saves_after_each_successful_remote_mutation(self):
        operator, batch = prepared_operator(submodule_changed=True)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        operator.prepare(batch["batch_id"])

        app_snapshots = [
            snapshot["apps"]["pocket-manage"] for snapshot in operator.store.snapshots
        ]
        self.assertTrue(any(app.get("dev_push") == "pushed" for app in app_snapshots))
        self.assertTrue(any(app.get("dev_sha") == "n" * 40 for app in app_snapshots))
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
                "submodule_after": "p" * 40,
                "dev_push": "pushed",
                "preparation_pr": None,
                "dev_sha": "n" * 40,
                "status": "failed",
                "error": "interrupted after push",
            }
        )
        batch["state"] = "prepare-failed"
        operator.runner.remote_dev = "n" * 40
        operator.store.save(batch)

        result = operator.prepare(batch["batch_id"])

        self.assertEqual(result["apps"]["pocket-manage"]["status"], "prepared")
        pushes = [
            call
            for call in operator.runner.calls
            if call.args == ("git", "push", "origin", "HEAD:dev")
        ]
        self.assertEqual(pushes, [])
