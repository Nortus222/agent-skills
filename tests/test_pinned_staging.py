"""Exercise promotion safety against a local bare Git remote, without publishing."""

import json
import io
from unittest.mock import patch
from dataclasses import replace
import subprocess
import tempfile
import unittest
from pathlib import Path

import sys

SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "deploy-mobile-apps"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from mobile_release_lib import (
    BatchStore,
    CommandResult,
    PollPolicy,
    ReleaseError,
    ReleaseOperator,
    SubprocessRunner,
    load_inventory,
)


class LocalRunner(SubprocessRunner):
    def __init__(self):
        super().__init__()
        self.commands = []
        self.checks = []
        self.lose_push_response = False
        self.fail_observation = False
        self.reported_remote = "https://github.com/MarketplaceSoftware/go_measure.git"

    def run(self, args, *, cwd=None, mutates=False):
        self.commands.append((list(args), mutates))
        if args[:4] == ["git", "remote", "get-url", "origin"]:
            return CommandResult(0, self.reported_remote, "")
        if args[:2] == ["gh", "auth"]:
            return CommandResult(0, "", "")
        if args[:2] == ["gh", "api"]:
            if self.fail_observation:
                return CommandResult(1, "", "GitHub API unavailable")
            if "/git/ref/heads/" in args[2]:
                sha = subprocess.check_output(
                    ["git", "rev-parse", "staging"], cwd=self.remote, text=True
                ).strip()
                return CommandResult(0, json.dumps({"object": {"sha": sha}}), "")
            return CommandResult(0, json.dumps({"check_runs": self.checks}), "")
        if args == ["git", "remote", "get-url", "--push", "--all", "origin"]:
            result = super().run(args, cwd=cwd, mutates=mutates)
            # Only the fixture's intended local transport stands in for GitHub.
            urls = [
                self.reported_remote if url == str(self.remote) else url
                for url in result.stdout.splitlines()
            ]
            return CommandResult(result.returncode, "\n".join(urls), result.stderr)
        result = super().run(args, cwd=cwd, mutates=mutates)
        if self.lose_push_response and args[:2] == ["git", "push"]:
            self.lose_push_response = False
            return CommandResult(1, "", "connection lost after push")
        return result


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = self.root / "remote.git"
        self.repo = self.root / "task"
        self.git(None, "init", "--bare", str(self.remote))
        self.git(None, "clone", str(self.remote), str(self.repo))
        self.git(self.repo, "config", "user.name", "Repository Owner")
        self.git(self.repo, "config", "user.email", "owner@example.test")
        (self.repo / "codemagic.yaml").write_text(
            """workflows:
  android-staging:
    name: Android internal testing
  ios-staging:
    name: iOS external TestFlight
  web-staging:
    name: Firebase staging
"""
        )
        (self.repo / "release_notes.json").write_text(
            '[{"language":"en-US","text":"Updated room measurements."}]'
        )
        (self.repo / "app.txt").write_text("original")
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-m", "initial")
        self.base = self.git(self.repo, "rev-parse", "HEAD")
        self.git(
            self.repo,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{self.base},packages",
        )
        (self.repo / "packages").mkdir()
        self.git(self.repo, "commit", "-m", "pin packages")
        self.base = self.git(self.repo, "rev-parse", "HEAD")
        for branch in ("dev", "staging", "release"):
            self.git(self.repo, "branch", branch, self.base)
            self.git(self.repo, "push", "origin", branch)
        self.git(self.repo, "checkout", "dev")
        (self.repo / "app.txt").write_text("new measurements")
        self.git(self.repo, "commit", "-am", "fix: update measurements")
        self.source = self.git(self.repo, "rev-parse", "HEAD")
        self.git(self.repo, "push", "origin", "dev")
        self.runner = LocalRunner()
        self.runner.remote = self.remote
        inventory = load_inventory(SKILL_ROOT / "references/apps.json")
        self.store = BatchStore(self.root / "state")
        self.operator = ReleaseOperator(
            inventory,
            self.store,
            runner=self.runner,
            poll_policy=PollPolicy(observation_timeout_seconds=0),
        )

    def git(self, cwd, *args):
        return subprocess.check_output(
            ["git", *args], cwd=cwd, text=True, stderr=subprocess.DEVNULL
        ).strip()

    def batch(self):
        return self.operator.preflight(["go-measure"], repository_path=self.repo)

    def register(self, conclusion=None):
        self.runner.checks = [
            {
                "app": {"name": "Codemagic CI/CD"},
                "name": "iOS external TestFlight",
                "status": "completed" if conclusion else "in_progress",
                "conclusion": conclusion,
                "details_url": "https://codemagic.io/test-build",
            }
        ]

    def test_dry_run_and_prepare_do_not_publish(self):
        batch = self.operator.preflight(
            ["go-measure"], dry_run=True, repository_path=self.repo
        )
        self.assertFalse(self.store.root.exists())
        batch = self.batch()
        prepared = self.operator.prepare(batch["batch_id"])
        self.assertEqual(prepared["state"], "awaiting-approval")
        self.operator.release(batch["batch_id"], dry_run=True)
        self.assertEqual(
            self.git(self.repo, "ls-remote", "origin", "staging").split()[0], self.base
        )
        self.assertFalse(any(m for _, m in self.runner.commands))

    def test_preserves_staging_merge_history_and_exact_source_tree(self):
        self.git(self.repo, "checkout", "staging")
        self.git(self.repo, "commit", "--allow-empty", "-m", "prior promotion")
        stage = self.git(self.repo, "rev-parse", "HEAD")
        self.git(self.repo, "push", "origin", "staging")
        batch = self.batch()
        prepared = self.operator.prepare(batch["batch_id"])
        record = prepared["apps"]["go-measure"]
        candidate = record["candidate_sha"]
        self.git(self.repo, "merge-base", "--is-ancestor", stage, candidate)
        self.assertEqual(
            self.git(self.repo, "rev-parse", f"{candidate}^{{tree}}"),
            self.git(self.repo, "rev-parse", f"{self.source}^{{tree}}"),
        )
        self.addCleanup(
            lambda: self.git(self.repo, "worktree", "remove", record["worktree"])
        )
        self.register()
        result = self.operator.release(batch["batch_id"])
        self.assertEqual(result["state"], "released")
        self.assertEqual(
            self.git(self.repo, "ls-remote", "origin", "staging").split()[0], candidate
        )
        self.assertEqual(
            self.git(self.repo, "ls-remote", "origin", "dev").split()[0], self.source
        )
        self.assertEqual(
            len(result["apps"]["go-measure"]["codemagic_missing_checks"]), 2
        )

    def test_changed_dev_snapshot_blocks_publishing(self):
        batch = self.batch()
        self.operator.prepare(batch["batch_id"])
        self.git(self.repo, "commit", "--allow-empty", "-m", "another dev commit")
        self.git(self.repo, "push", "origin", "dev")
        with self.assertRaisesRegex(ReleaseError, "dev changed"):
            self.operator.release(batch["batch_id"])
        self.assertFalse(
            any(args[:2] == ["git", "push"] for args, _ in self.runner.commands)
        )

    def test_changed_push_destination_blocks_publication_without_receipt(self):
        batch = self.batch()
        self.operator.prepare(batch["batch_id"])
        other = self.root / "other.git"
        self.git(None, "init", "--bare", str(other))
        self.git(self.repo, "config", "remote.origin.pushurl", str(other))
        with self.assertRaisesRegex(ReleaseError, "push destination"):
            self.operator.release(batch["batch_id"])
        saved = self.store.load(batch["batch_id"])
        self.assertFalse(saved["apps"]["go-measure"].get("push_attempted"))
        self.assertFalse(
            any(args[:2] == ["git", "push"] for args, _ in self.runner.commands)
        )
        self.assertEqual(self.git(other, "for-each-ref", "--format=%(refname)"), "")
        self.assertEqual(self.git(self.remote, "rev-parse", "staging"), self.base)

    def test_multiple_effective_push_destinations_are_rejected(self):
        batch = self.batch()
        self.operator.prepare(batch["batch_id"])
        self.git(
            self.repo, "config", "--add", "remote.origin.pushurl", str(self.remote)
        )
        self.git(
            self.repo, "config", "--add", "remote.origin.pushurl", str(self.remote)
        )
        with self.assertRaisesRegex(ReleaseError, "exactly one.*push destination"):
            self.operator.release(batch["batch_id"])
        self.assertFalse(
            self.store.load(batch["batch_id"])["apps"]["go-measure"].get(
                "push_attempted"
            )
        )
        self.assertFalse(
            any(args[:2] == ["git", "push"] for args, _ in self.runner.commands)
        )

    def test_failed_preflight_json_keeps_inspection_retry_options(self):
        import mobile_release
        import shlex

        self.runner.reported_remote = "https://github.com/Example/wrong.git"
        output = io.StringIO()
        args = [
            "preflight",
            "--app",
            "go-measure",
            "--repository",
            str(self.repo),
            "--source",
            self.source,
            "--git-auth",
            "gh",
            "--dry-run",
            "--json",
        ]
        with patch.object(
            mobile_release, "build_operator", return_value=self.operator
        ), patch("sys.stdout", output):
            self.assertEqual(mobile_release.main(args), 1)
        recovery = json.loads(output.getvalue())["recovery"]
        self.assertIsNotNone(recovery)
        self.assertEqual(
            shlex.split(recovery["next_command"]),
            ["python3", "scripts/mobile_release.py", *args],
        )
        self.assertFalse(self.store.root.exists())

    def test_failed_preflight_human_shows_targeted_retry(self):
        import mobile_release

        self.runner.reported_remote = "https://github.com/Example/wrong.git"
        stderr = io.StringIO()
        with patch.object(
            mobile_release, "build_operator", return_value=self.operator
        ), patch("sys.stderr", stderr):
            self.assertEqual(
                mobile_release.main(
                    [
                        "preflight",
                        "--app",
                        "go-measure",
                        "--repository",
                        str(self.repo),
                        "--source",
                        self.source,
                        "--git-auth",
                        "gh",
                    ]
                ),
                1,
            )
        retry = stderr.getvalue().split("Next command: ")[-1]
        self.assertIn("--app go-measure", retry)
        self.assertIn("--repository", retry)
        self.assertIn(self.source, retry)
        self.assertIn("--git-auth gh", retry)
        self.assertNotIn("--dry-run", retry)

    def test_changed_remote_blocks_publishing(self):
        batch = self.batch()
        self.operator.prepare(batch["batch_id"])
        self.runner.reported_remote = (
            "https://github.com/MarketplaceSoftware/another-app.git"
        )
        with self.assertRaisesRegex(ReleaseError, "origin changed"):
            self.operator.release(batch["batch_id"])
        self.assertFalse(
            any(args[:2] == ["git", "push"] for args, _ in self.runner.commands)
        )

    def test_stage_content_is_not_silently_discarded(self):
        self.git(self.repo, "checkout", "staging")
        (self.repo / "staging-only.txt").write_text("preserve")
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-m", "staging-only content")
        self.git(self.repo, "push", "origin", "staging")
        batch = self.batch()
        with self.assertRaises(ReleaseError):
            self.operator.prepare(batch["batch_id"])
        record = self.store.load(batch["batch_id"])["apps"]["go-measure"]
        self.assertTrue(Path(record["worktree"]).exists())
        self.addCleanup(
            lambda: self.git(self.repo, "worktree", "remove", record["worktree"])
        )
        self.assertFalse(
            any(args[:2] == ["git", "push"] for args, _ in self.runner.commands)
        )

    def test_lost_push_response_resumes_without_second_push(self):
        batch = self.batch()
        self.operator.prepare(batch["batch_id"])
        self.runner.lose_push_response = True
        with self.assertRaises(ReleaseError):
            self.operator.release(batch["batch_id"])
        self.register()
        result = self.operator.release(batch["batch_id"])
        self.assertEqual(result["state"], "released")
        self.assertEqual(
            sum(args[:2] == ["git", "push"] for args, _ in self.runner.commands), 1
        )

    def test_status_refreshes_saved_sha_after_target_advances_without_writes(self):
        batch = self.batch()
        self.operator.prepare(batch["batch_id"])
        self.operator.release(batch["batch_id"])
        saved = self.store._path_for(batch["batch_id"]).read_bytes()
        self.git(self.repo, "checkout", "staging")
        self.git(self.repo, "merge", "--ff-only", self.source)
        self.git(self.repo, "commit", "--allow-empty", "-m", "newer staging")
        self.git(self.repo, "push", "origin", "staging")
        self.register("failure")
        result = self.operator.status(batch["batch_id"])
        record = result["apps"]["go-measure"]
        self.assertEqual(record["pushed_sha"], self.source)
        self.assertNotEqual(record["current_staging_sha"], self.source)
        self.assertEqual(
            record["codemagic_checks"]["iOS external TestFlight"]["conclusion"],
            "failure",
        )
        self.assertEqual(saved, self.store._path_for(batch["batch_id"]).read_bytes())
        self.assertIn(
            f"repos/MarketplaceSoftware/go_measure/commits/{self.source}/check-runs",
            self.runner.commands[-2][0],
        )

    def test_observation_failure_preserves_push_receipt_in_cli_recovery(self):
        import mobile_release

        batch = self.batch()
        self.operator.prepare(batch["batch_id"])
        self.runner.fail_observation = True
        output = io.StringIO()
        with patch.object(
            mobile_release, "build_operator", return_value=self.operator
        ), patch("sys.stdout", output):
            result = mobile_release.main(
                ["release", "--batch", batch["batch_id"], "--json"]
            )
        self.assertEqual(result, 1)
        recovery = json.loads(output.getvalue())["recovery"]
        self.assertEqual(recovery["apps"]["go-measure"]["pushed_sha"], self.source)
        self.assertEqual(recovery["state"], "released-builds-unverified")
        self.assertIn("status --batch", recovery["next_command"])

    def test_uncertain_push_remains_observable_after_staging_advances(self):
        batch = self.batch()
        self.operator.prepare(batch["batch_id"])
        self.runner.lose_push_response = True
        with self.assertRaises(ReleaseError):
            self.operator.release(batch["batch_id"])
        self.git(self.repo, "checkout", "staging")
        self.git(self.repo, "merge", "--ff-only", self.source)
        self.git(self.repo, "commit", "--allow-empty", "-m", "later promotion")
        self.git(self.repo, "push", "origin", "staging")
        self.register()
        saved = self.store._path_for(batch["batch_id"]).read_bytes()
        result = self.operator.release(batch["batch_id"])
        self.assertEqual(result["state"], "push-outcome-unverified")
        self.assertIn(
            "iOS external TestFlight", result["apps"]["go-measure"]["codemagic_checks"]
        )
        self.assertEqual(
            sum(args[:2] == ["git", "push"] for args, _ in self.runner.commands), 1
        )
        self.assertEqual(saved, self.store._path_for(batch["batch_id"]).read_bytes())

    def test_changed_staging_snapshot_blocks_publishing(self):
        batch = self.batch()
        self.operator.prepare(batch["batch_id"])
        self.git(self.repo, "checkout", "staging")
        self.git(self.repo, "commit", "--allow-empty", "-m", "another staging commit")
        self.git(self.repo, "push", "origin", "staging")
        with self.assertRaisesRegex(ReleaseError, "staging changed"):
            self.operator.release(batch["batch_id"])
        self.assertFalse(
            any(args[:2] == ["git", "push"] for args, _ in self.runner.commands)
        )

    def test_notes_commit_is_allowed_and_packages_stay_pinned(self):
        (self.repo / "release_notes.json").write_text(
            '[{"language":"en-US","text":"New staging notes."}]'
        )
        self.git(self.repo, "commit", "-am", "docs: release notes")
        source = self.git(self.repo, "rev-parse", "HEAD")
        batch = self.operator.preflight(
            ["go-measure"], repository_path=self.repo, source_sha=source
        )
        self.operator.prepare(batch["batch_id"])
        self.operator.release(batch["batch_id"])
        self.assertEqual(
            self.git(self.repo, "ls-remote", "origin", "staging").split()[0], source
        )
        self.assertEqual(
            self.git(self.repo, "ls-tree", source, "packages").split()[2],
            batch["apps"]["go-measure"]["submodule_sha"],
        )

    def test_source_with_unmerged_app_changes_is_rejected(self):
        (self.repo / "app.txt").write_text("unreviewed")
        self.git(self.repo, "commit", "-am", "unmerged app changes")
        with self.assertRaisesRegex(ReleaseError, "only in release_notes"):
            self.operator.preflight(
                ["go-measure"],
                repository_path=self.repo,
                source_sha=self.git(self.repo, "rev-parse", "HEAD"),
            )

    def test_saved_routing_cannot_change_before_release(self):
        batch = self.batch()
        self.operator.prepare(batch["batch_id"])
        app = self.operator.inventory.apps["go-measure"]
        for override in (
            {"staging_branch": "release"},
            {"release_notes_file": "other.json"},
            {"submodule_path": "other"},
            {"staging_checks": ("other",)},
            {"repository": "Other/app"},
            {"workflow": "managed-release"},
        ):
            with self.subTest(override=override):
                self.operator.inventory.apps["go-measure"] = replace(app, **override)
                with self.assertRaisesRegex(ReleaseError, "saved batch"):
                    self.operator.release(batch["batch_id"])
        self.assertFalse(
            any(args[:2] == ["git", "push"] for args, _ in self.runner.commands)
        )

    def test_generic_pinned_policy_uses_inventory_metadata(self):
        app = replace(
            self.operator.inventory.apps.pop("go-measure"),
            key="survey",
            repository="Example/survey",
            release_notes_file=None,
            staging_checks=("Firebase staging",),
        )
        self.operator.inventory.apps["survey"] = app
        self.runner.reported_remote = "https://github.com/Example/survey.git"
        batch = self.operator.preflight(["survey"], repository_path=self.repo)
        self.operator.prepare(batch["batch_id"], staging_only=True)
        self.runner.checks = [
            {
                "app": {"name": "Codemagic CI/CD"},
                "name": "Firebase staging",
                "status": "in_progress",
                "conclusion": None,
                "details_url": "https://codemagic.io/test-build",
            }
        ]
        result = self.operator.release(batch["batch_id"])
        self.assertEqual(result["state"], "released")
        self.assertEqual(result["apps"]["survey"]["codemagic_missing_checks"], [])
        self.assertFalse(
            any(
                args[:3] == ["gh", "pr", "merge"] or args[:2] == ["git", "tag"]
                for args, _ in self.runner.commands
            )
        )
        pushes = [
            args for args, _ in self.runner.commands if args[:2] == ["git", "push"]
        ]
        self.assertEqual(
            pushes, [["git", "push", "origin", f"{self.source}:refs/heads/staging"]]
        )

    def test_unknown_and_mixed_policies_fail_before_commands(self):
        for apps in (["unknown"], ["go-measure", "pocket-manage"]):
            with self.subTest(apps=apps), self.assertRaises(ReleaseError):
                self.operator.preflight(apps, repository_path=self.repo)
        self.assertEqual(self.runner.commands, [])

    def test_failed_detected_build_reports_failure(self):
        batch = self.batch()
        self.operator.prepare(batch["batch_id"])
        self.register("failure")
        result = self.operator.release(batch["batch_id"])
        self.assertIn("failed", result["apps"]["go-measure"]["result"])
        self.assertEqual(
            result["apps"]["go-measure"]["staging_failed_checks"],
            ["iOS external TestFlight"],
        )

    def test_uncertain_push_with_unchanged_target_never_pushes_again(self):
        batch = self.batch()
        self.operator.prepare(batch["batch_id"])
        original = self.runner.run

        def failed_push(args, **kwargs):
            if args[:2] == ["git", "push"]:
                self.runner.commands.append((list(args), True))
                return CommandResult(1, "", "connection failed before receipt")
            return original(args, **kwargs)

        with patch.object(self.runner, "run", side_effect=failed_push):
            with self.assertRaises(ReleaseError):
                self.operator.release(batch["batch_id"])
        result = self.operator.release(batch["batch_id"])
        self.assertEqual(result["state"], "push-outcome-unverified")
        self.assertEqual(
            sum(args[:2] == ["git", "push"] for args, _ in self.runner.commands), 1
        )

    def test_cli_dry_run_preserves_explicit_app_repository_and_source(self):
        import mobile_release

        output = io.StringIO()
        with patch.object(
            mobile_release, "build_operator", return_value=self.operator
        ), patch("sys.stdout", output):
            result = mobile_release.main(
                [
                    "preflight",
                    "--app",
                    "go-measure",
                    "--repository",
                    str(self.repo),
                    "--source",
                    self.source,
                    "--dry-run",
                    "--json",
                ]
            )
        self.assertEqual(result, 0)
        import shlex

        command = shlex.split(json.loads(output.getvalue())["next_command"])
        self.assertEqual(command[command.index("--app") + 1], "go-measure")
        self.assertEqual(command[command.index("--repository") + 1], str(self.repo))
        self.assertEqual(command[command.index("--source") + 1], self.source)

    def test_pinned_policy_rejects_production_branch_alias_before_commands(self):
        app = self.operator.inventory.apps["go-measure"]
        self.operator.inventory.apps["go-measure"] = replace(
            app, staging_branch="release"
        )
        with self.assertRaisesRegex(ReleaseError, "distinct"):
            self.batch()
        self.assertEqual(self.runner.commands, [])

    def test_status_works_after_original_worktree_is_removed(self):
        batch = self.batch()
        self.operator.prepare(batch["batch_id"])
        self.operator.release(batch["batch_id"])
        self.repo.rename(self.root / "removed-task")
        self.register("success")
        result = self.operator.status(batch["batch_id"])
        self.assertEqual(result["apps"]["go-measure"]["pushed_sha"], self.source)
        self.assertEqual(
            result["apps"]["go-measure"]["current_staging_sha"], self.source
        )


if __name__ == "__main__":
    unittest.main()
