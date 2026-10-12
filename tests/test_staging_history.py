"""Common staging promotion against local bare remotes and fake GitHub APIs."""

import base64
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_mobile_release import SKILL_ROOT
import mobile_release
from mobile_release_lib import (
    BatchStore,
    CommandResult,
    PollPolicy,
    ReleaseError,
    ReleaseOperator,
    SubprocessRunner,
    load_inventory,
    new_batch,
)


def git(path, *args):
    return subprocess.check_output(
        ["git", *args], cwd=path, text=True, stderr=subprocess.DEVNULL
    ).strip()


class LocalReleaseRunner(SubprocessRunner):
    """Use Git's real staging transport; simulate GitHub and package access."""

    def __init__(self, app, repo, remote, packages_sha):
        super().__init__()
        self.app, self.repo, self.remote = app, repo, remote
        self.packages_sha = packages_sha
        self.calls = []
        self.lose_push_response = False
        self.before_push_guard = None
        self.fail_merge = False
        self.checked_shas = []
        self.production_heads = []

    def run(self, args, *, cwd=None, mutates=False):
        self.calls.append((list(args), cwd, mutates))
        if args[:3] == ["git", "remote", "get-url"]:
            if cwd.name == "packages":
                return CommandResult(
                    0, "git@github.com:MarketplaceSoftware/packages.git", ""
                )
            result = super().run(args, cwd=cwd, mutates=mutates)
            urls = [
                (
                    f"git@github.com:{self.app.repository}.git"
                    if url == str(self.remote)
                    else url
                )
                for url in result.stdout.splitlines()
            ]
            return CommandResult(result.returncode, "\n".join(urls), result.stderr)
        if cwd and cwd.name == "packages" and args[:2] == ["git", "fetch"]:
            return CommandResult(0, "", "")
        if cwd and cwd.name == "packages" and args[:2] == ["git", "rev-parse"]:
            return CommandResult(0, self.packages_sha, "")
        if args[:2] == ["git", "ls-remote"] and self.before_push_guard:
            if self.before_push_guard():
                self.before_push_guard = None
        if args[:2] == ["git", "merge"] and self.fail_merge:
            return CommandResult(1, "", "injected merge conflict")
        if args[0] == "gh":
            return self.github(args)
        result = super().run(args, cwd=cwd, mutates=mutates)
        if self.lose_push_response and args[:2] == ["git", "push"]:
            self.lose_push_response = False
            return CommandResult(1, "", "connection lost after accepted push")
        return result

    def github(self, args):
        if args[:2] == ["gh", "auth"]:
            return CommandResult(0, "", "")
        if args[:3] == ["gh", "repo", "view"]:
            return CommandResult(0, json.dumps({"viewerPermission": "WRITE"}), "")
        if args[:2] == ["gh", "api"]:
            if args[2].endswith("/check-runs"):
                sha = args[2].split("/commits/")[1].split("/")[0]
                self.checked_shas.append(sha)
                checks = [
                    {
                        "name": name,
                        "status": "completed",
                        "conclusion": "success",
                        "details_url": "https://codemagic.io/fixture",
                        "app": {"name": "Codemagic CI/CD"},
                    }
                    for name in self.app.staging_checks
                ]
                return CommandResult(0, json.dumps({"check_runs": checks}), "")
            content = base64.b64encode(json.dumps({".": "1.2.0"}).encode()).decode()
            return CommandResult(
                0,
                json.dumps({"type": "file", "encoding": "base64", "content": content}),
                "",
            )
        if args[:3] == ["gh", "pr", "list"]:
            if "--label" not in args:
                return CommandResult(0, "[]", "")
            return CommandResult(
                0,
                json.dumps(
                    [
                        {
                            "number": 42,
                            "url": f"https://github.com/{self.app.repository}/pull/42",
                            "headRefOid": "a" * 40,
                            "baseRefName": "release",
                            "labels": [{"name": "autorelease: pending"}],
                            "statusCheckRollup": [],
                        }
                    ]
                ),
                "",
            )
        if args[:3] == ["gh", "pr", "create"]:
            return CommandResult(
                0, f"https://github.com/{self.app.repository}/pull/17", ""
            )
        if args[:3] == ["gh", "pr", "checks"]:
            return CommandResult(
                0,
                json.dumps([{"name": "tests", "state": "SUCCESS", "bucket": "pass"}]),
                "",
            )
        if args[:3] == ["gh", "pr", "view"]:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "number": 17,
                        "url": f"https://github.com/{self.app.repository}/pull/17",
                        "state": "OPEN",
                        "headRefName": "staging",
                        "headRefOid": git(self.remote, "rev-parse", "staging"),
                        "baseRefName": "release",
                        "mergeable": "MERGEABLE",
                        "mergeStateStatus": "CLEAN",
                    }
                ),
                "",
            )
        if args[:3] == ["gh", "pr", "merge"]:
            sha = args[args.index("--match-head-commit") + 1]
            self.production_heads.append(sha)
            git(self.remote, "update-ref", "refs/heads/release", sha)
            return CommandResult(0, "", "")
        if args[:3] == ["gh", "run", "list"]:
            sha = args[args.index("--commit") + 1]
            return CommandResult(
                0,
                json.dumps(
                    [
                        {
                            "databaseId": 1,
                            "status": "completed",
                            "conclusion": "success",
                            "headSha": sha,
                            "url": "https://github.com/fixture/actions/runs/1",
                        }
                    ]
                ),
                "",
            )
        raise AssertionError(args)


class StagingHistoryTests(unittest.TestCase):
    def fixture(
        self,
        key="go-measure",
        *,
        divergent=True,
        staging_content=False,
        incoming_config=True,
    ):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        remote, repo = root / "origin.git", root / "task"
        app = load_inventory(SKILL_ROOT / "references/apps.json").apps[key]
        git(None, "init", "--bare", str(remote))
        git(None, "clone", str(remote), str(repo))
        git(repo, "config", "user.name", "Fixture Owner")
        git(repo, "config", "user.email", "owner@example.test")
        (repo / ".gitignore").write_text(".claude/worktrees/\n")
        (repo / "app.txt").write_text("old source\n")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "initial")
        packages = git(repo, "rev-parse", "HEAD")
        git(repo, "update-index", "--add", "--cacheinfo", f"160000,{packages},packages")
        (repo / "packages").mkdir()
        git(repo, "commit", "-m", "pin packages")
        base = git(repo, "rev-parse", "HEAD")
        for branch in ("dev", "staging", "release"):
            git(repo, "branch", branch, base)
            git(repo, "push", "origin", branch)
        if divergent:
            git(repo, "checkout", "staging")
            git(repo, "commit", "--allow-empty", "-m", "prior staging history")
            git(repo, "checkout", "-b", "prior-dev", base)
            git(repo, "commit", "--allow-empty", "-m", "prior reviewed source")
            git(repo, "checkout", "staging")
            git(repo, "merge", "--no-ff", "prior-dev", "-m", "previous promotion merge")
            if staging_content:
                (repo / "app.txt").write_text("independent staging content\n")
                git(repo, "commit", "-am", "staging-only content")
            git(repo, "push", "origin", "staging")
        stage = git(repo, "rev-parse", "staging")
        git(repo, "checkout", "dev")
        (repo / "app.txt").write_text("reviewed source\n")
        if incoming_config:
            (repo / ".release-please-manifest.json").write_text('{".":"1.1.0"}\n')
            (repo / "release-please-config.json").write_text(
                '{"release-type":"simple","packages":{".":{}}}\n'
            )
            names = [*app.staging_checks, *app.codemagic_checks]
            (repo / "codemagic.yaml").write_text(
                "workflows:\n"
                + "".join(
                    f"  workflow-{index}:\n    name: {name}\n"
                    for index, name in enumerate(names)
                )
            )
        git(repo, "add", ".")
        git(repo, "commit", "-m", "fix: reviewed application changes")
        source = git(repo, "rev-parse", "HEAD")
        git(repo, "push", "origin", "dev")
        runner = LocalReleaseRunner(app, repo, remote, packages)
        operator = ReleaseOperator(
            load_inventory(SKILL_ROOT / "references/apps.json"),
            BatchStore(root / "state"),
            runner=runner,
            environ={},
            poll_policy=PollPolicy(observation_timeout_seconds=0),
        )
        operator._codemagic_cached = None
        batch = new_batch([key])
        batch["state"] = "preflight-complete"
        batch["apps"][key] = {
            "state": "preflight-complete",
            "repository": app.repository,
            "repository_path": str(repo),
            "dev_sha": source,
            "release_sha": base,
            "packages_pointer_sha": packages,
            "packages_sha": packages,
            "staging_to_release_pr": None,
        }
        operator.store.save(batch)
        return operator, batch, repo, remote, source, stage

    def test_history_only_merges_publish_exact_source_for_go_and_pocket(self):
        for key in ("go-measure", "pocket-manage"):
            with self.subTest(key=key):
                operator, batch, repo, remote, source, stage = self.fixture(key)
                result = operator.prepare(batch["batch_id"], staging_only=True)
                record = result["apps"][key]
                candidate = record["staging_sha"]
                self.assertNotEqual(candidate, source)
                self.assertEqual(
                    git(repo, "rev-parse", f"{candidate}^{{tree}}"),
                    git(repo, "rev-parse", f"{source}^{{tree}}"),
                )
                self.assertEqual(
                    git(repo, "ls-tree", candidate, "--", "packages"),
                    git(repo, "ls-tree", source, "--", "packages"),
                )
                git(repo, "merge-base", "--is-ancestor", stage, candidate)
                self.assertEqual(git(remote, "rev-parse", "staging"), candidate)
                self.assertEqual(record["dev_sha"], source)
                self.assertEqual(operator.runner.checked_shas, [candidate])
                self.assertEqual(record["staging_before_sha"], stage)

    def test_dry_run_describes_history_promotion_without_creating_or_pushing_it(self):
        operator, batch, _, remote, _, stage = self.fixture()
        before = operator.store._path_for(batch["batch_id"]).read_bytes()
        result = operator.prepare(batch["batch_id"], dry_run=True, staging_only=True)
        record = result["apps"]["go-measure"]
        self.assertEqual(record["planned_staging_sha"], "<staging-promotion-sha>")
        self.assertTrue(
            any(
                command[:2] == ["git", "merge"]
                for command in record["planned_commands"]
            )
        )
        self.assertEqual(git(remote, "rev-parse", "staging"), stage)
        self.assertFalse(any(mutates for _, _, mutates in operator.runner.calls))
        self.assertEqual(
            before, operator.store._path_for(batch["batch_id"]).read_bytes()
        )

    def test_dry_run_planned_package_bump_requires_merge_above_existing_source(self):
        operator, batch, repo, _, source, _ = self.fixture(divergent=False)
        git(repo, "checkout", "--detach", source)
        git(repo, "commit", "--allow-empty", "-m", "existing source promotion")
        existing = git(repo, "rev-parse", "HEAD")
        git(repo, "push", "origin", f"{existing}:staging")
        batch["apps"]["go-measure"]["packages_sha"] = "f" * 40
        operator.store.save(batch)
        before = operator.store._path_for(batch["batch_id"]).read_bytes()
        result = operator.prepare(batch["batch_id"], dry_run=True, staging_only=True)
        record = result["apps"]["go-measure"]
        self.assertEqual(record["planned_staging_sha"], "<staging-promotion-sha>")
        merge = next(
            command
            for command in record["planned_commands"]
            if command[:2] == ["git", "merge"]
        )
        self.assertIn("<prepared-dev-sha>", merge)
        self.assertFalse(any(mutates for _, _, mutates in operator.runner.calls))
        self.assertEqual(
            before, operator.store._path_for(batch["batch_id"]).read_bytes()
        )

    def test_fast_forward_remains_the_exact_dev_commit(self):
        operator, batch, repo, remote, source, _ = self.fixture(divergent=False)
        result = operator.prepare(batch["batch_id"], staging_only=True)
        self.assertEqual(result["apps"]["go-measure"]["staging_sha"], source)
        self.assertEqual(git(remote, "rev-parse", "staging"), source)

    def test_staging_already_contains_source_reuses_existing_commit_and_checks(self):
        operator, batch, repo, remote, source, _ = self.fixture(divergent=False)
        git(repo, "checkout", "--detach", source)
        git(repo, "commit", "--allow-empty", "-m", "existing promotion history")
        existing = git(repo, "rev-parse", "HEAD")
        git(repo, "push", "origin", f"{existing}:staging")
        result = operator.prepare(batch["batch_id"], staging_only=True)
        record = result["apps"]["go-measure"]
        self.assertEqual(record["staging_sha"], existing)
        self.assertEqual(record["staging_push"], "unchanged")
        self.assertEqual(operator.runner.checked_shas, [existing])
        self.assertFalse(any(mutates for _, _, mutates in operator.runner.calls))

    def test_candidate_tree_mismatch_preserves_worktree_without_publishing(self):
        operator, batch, _, remote, _, stage = self.fixture()
        original = operator.runner.run

        def run(args, **kwargs):
            result = original(args, **kwargs)
            if args[:2] == ["git", "merge"] and result.returncode == 0:
                worktree = kwargs["cwd"]
                (worktree / "app.txt").write_text("incorrect merged content")
                git(worktree, "commit", "-am", "unexpected merge result")
            return result

        operator.runner.run = run
        with self.assertRaisesRegex(ReleaseError, "candidate tree differs"):
            operator.prepare(batch["batch_id"], staging_only=True)
        record = operator.store.load(batch["batch_id"])["apps"]["go-measure"]
        self.assertTrue(Path(record["staging_worktree"]).is_dir())
        self.assertTrue(record["staging_candidate_sha"])
        self.assertEqual(git(remote, "rev-parse", "staging"), stage)

    def test_uncertain_unaccepted_push_does_not_publish_on_resume(self):
        operator, batch, _, remote, _, stage = self.fixture()
        original = operator.runner.run

        def run(args, **kwargs):
            if args[:2] == ["git", "push"]:
                operator.runner.calls.append((list(args), kwargs.get("cwd"), True))
                return CommandResult(1, "", "network timeout; outcome unknown")
            return original(args, **kwargs)

        operator.runner.run = run
        with self.assertRaises(ReleaseError):
            operator.prepare(batch["batch_id"], staging_only=True)
        with self.assertRaisesRegex(ReleaseError, "outcome is unverified"):
            operator.prepare(batch["batch_id"], dry_run=True)
        with self.assertRaisesRegex(ReleaseError, "outcome is unverified"):
            operator.prepare(batch["batch_id"])
        self.assertEqual(git(remote, "rev-parse", "staging"), stage)
        self.assertEqual(
            sum(args[:2] == ["git", "push"] for args, _, _ in operator.runner.calls), 1
        )
        self.assertEqual(operator.runner.production_heads, [])

    def test_prepare_missing_checkout_observes_receipt_with_status_only_recovery(self):
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain):
                operator, batch, repo, _, _, _ = self.fixture()
                operator.runner.lose_push_response = uncertain
                if uncertain:
                    with self.assertRaises(ReleaseError):
                        operator.prepare(batch["batch_id"], staging_only=True)
                else:
                    operator.prepare(batch["batch_id"], staging_only=True)
                receipt = operator.store.load(batch["batch_id"])["apps"]["go-measure"]
                candidate = receipt["staging_candidate_sha"]
                repo.rename(repo.parent / "removed-task")
                started = len(operator.runner.calls)
                output = io.StringIO()
                with patch.object(mobile_release, "build_operator", return_value=operator), patch("sys.stdout", output):
                    self.assertEqual(mobile_release.main(["prepare", "--batch", batch["batch_id"], "--json"]), 1)
                recovery = json.loads(output.getvalue())["recovery"]
                self.assertEqual(recovery["apps"]["go-measure"]["staging_candidate_sha"], candidate)
                self.assertIn("status --batch", recovery["next_command"])
                self.assertEqual(operator.runner.checked_shas[-1], candidate)
                self.assertFalse(any(mutates or args[0] == "git" for args, _, mutates in operator.runner.calls[started:]))
                self.assertEqual(operator.runner.production_heads, [])

    def test_status_observes_saved_candidate_after_source_worktree_disappears(self):
        operator, batch, repo, _, _, _ = self.fixture()
        operator.prepare(batch["batch_id"], staging_only=True)
        record = operator.store.load(batch["batch_id"])["apps"]["go-measure"]
        repo.rename(repo.parent / "removed-task")
        before = operator.store._path_for(batch["batch_id"]).read_bytes()
        operator.status(batch["batch_id"])
        self.assertEqual(operator.runner.checked_shas[-1], record["staging_sha"])
        self.assertEqual(
            before, operator.store._path_for(batch["batch_id"]).read_bytes()
        )

    def test_real_staging_content_stops_without_publishing(self):
        operator, batch, repo, remote, _, stage = self.fixture(staging_content=True)
        with self.assertRaisesRegex(ReleaseError, "staging-only content"):
            operator.prepare(batch["batch_id"], staging_only=True)
        self.assertEqual(git(remote, "rev-parse", "staging"), stage)
        self.assertFalse(
            any(args[:2] == ["git", "push"] for args, _, _ in operator.runner.calls)
        )
        self.assertEqual(git(repo, "status", "--porcelain"), "")

    def test_merge_failure_preserves_worktree_and_receipt(self):
        operator, batch, _, remote, _, stage = self.fixture()
        operator.runner.fail_merge = True
        with self.assertRaises(ReleaseError):
            operator.prepare(batch["batch_id"], staging_only=True)
        record = operator.store.load(batch["batch_id"])["apps"]["go-measure"]
        self.assertTrue(Path(record["staging_worktree"]).is_dir())
        self.assertEqual(record["staging_before_sha"], stage)
        self.assertEqual(git(remote, "rev-parse", "staging"), stage)

    def test_lost_push_response_resumes_without_another_publishing_push(self):
        operator, batch, _, remote, source, _ = self.fixture()
        operator.runner.lose_push_response = True
        with self.assertRaises(ReleaseError):
            operator.prepare(batch["batch_id"], staging_only=True)
        saved = operator.store.load(batch["batch_id"])["apps"]["go-measure"]
        self.assertTrue(saved["staging_push_attempted"])
        self.assertEqual(
            git(remote, "rev-parse", "staging"), saved["staging_candidate_sha"]
        )
        result = operator.prepare(batch["batch_id"], staging_only=True)
        self.assertEqual(result["state"], "staging-complete")
        self.assertEqual(result["apps"]["go-measure"]["dev_sha"], source)
        self.assertEqual(
            sum(args[:2] == ["git", "push"] for args, _, _ in operator.runner.calls), 1
        )

    def test_saved_candidate_checks_survive_newer_staging_and_status_is_read_only(self):
        operator, batch, repo, remote, _, _ = self.fixture()
        operator.runner.lose_push_response = True
        with self.assertRaises(ReleaseError):
            operator.prepare(batch["batch_id"], staging_only=True)
        saved = operator.store.load(batch["batch_id"])["apps"]["go-measure"]
        candidate = saved["staging_candidate_sha"]
        git(repo, "checkout", "--detach", candidate)
        git(repo, "commit", "--allow-empty", "-m", "later staging history")
        later = git(repo, "rev-parse", "HEAD")
        git(repo, "push", "origin", f"{later}:staging")
        before = operator.store._path_for(batch["batch_id"]).read_bytes()
        operator.status(batch["batch_id"])
        self.assertEqual(operator.runner.checked_shas[-1], candidate)
        self.assertEqual(
            before, operator.store._path_for(batch["batch_id"]).read_bytes()
        )
        result = operator.prepare(batch["batch_id"], staging_only=True)
        self.assertEqual(result["apps"]["go-measure"]["staging_sha"], candidate)
        self.assertEqual(git(remote, "rev-parse", "staging"), later)
        self.assertEqual(
            sum(args[:2] == ["git", "push"] for args, _, _ in operator.runner.calls), 1
        )

    def test_staging_only_resumes_production_promotion_using_merge_candidate(self):
        operator, batch, _, _, source, _ = self.fixture()
        staged = operator.prepare(batch["batch_id"], staging_only=True)
        candidate = staged["apps"]["go-measure"]["staging_sha"]
        result = operator.prepare(batch["batch_id"])
        self.assertEqual(result["state"], "awaiting-approval")
        self.assertEqual(operator.runner.production_heads, [candidate])
        self.assertNotEqual(candidate, source)
        self.assertEqual(
            sum(args[:2] == ["git", "push"] for args, _, _ in operator.runner.calls), 1
        )

    def test_source_or_target_movement_blocks_push_and_preserves_candidate(self):
        for branch in ("dev", "staging"):
            with self.subTest(branch=branch):
                operator, batch, repo, remote, source, stage = self.fixture()

                def move_after_candidate():
                    record = operator.store.load(batch["batch_id"])["apps"][
                        "go-measure"
                    ]
                    if not record.get("staging_candidate_sha"):
                        return False
                    previous = source if branch == "dev" else stage
                    tree = git(repo, "rev-parse", f"{previous}^{{tree}}")
                    moved = git(
                        repo,
                        "commit-tree",
                        tree,
                        "-p",
                        previous,
                        "-m",
                        "concurrent branch change",
                    )
                    git(repo, "push", "origin", f"{moved}:{branch}")
                    return True

                operator.runner.before_push_guard = move_after_candidate
                with self.assertRaisesRegex(
                    ReleaseError, "changed before staging push"
                ):
                    operator.prepare(batch["batch_id"], staging_only=True)
                record = operator.store.load(batch["batch_id"])["apps"]["go-measure"]
                self.assertTrue(Path(record["staging_worktree"]).is_dir())
                self.assertFalse(record.get("staging_push_attempted"))
                self.assertFalse(
                    any(
                        args[:2] == ["git", "push"]
                        for args, _, _ in operator.runner.calls
                    )
                )

    def test_missing_staging_remains_a_prerequisite(self):
        operator, batch, _, remote, _, _ = self.fixture()
        git(remote, "update-ref", "-d", "refs/heads/staging")
        with self.assertRaisesRegex(
            ReleaseError, "missing prerequisite origin/staging"
        ):
            operator.prepare(batch["batch_id"], staging_only=True)
        self.assertFalse(
            any(args[:2] == ["git", "push"] for args, _, _ in operator.runner.calls)
        )

    def test_candidate_packages_must_equal_recorded_prepared_pointer(self):
        operator, batch, _, _, _, _ = self.fixture(divergent=False)
        record = batch["apps"]["go-measure"]
        record.update(dev_push="pushed", submodule_after="f" * 40)
        operator.store.save(batch)
        with self.assertRaisesRegex(ReleaseError, "packages pointer"):
            operator.prepare(batch["batch_id"], staging_only=True)
        self.assertFalse(
            any(args[:2] == ["git", "push"] for args, _, _ in operator.runner.calls)
        )

    def test_preflight_validates_incoming_config_with_unbootstrapped_release(self):
        operator, _, repo, _, source, _ = self.fixture()
        result = operator.preflight(["go-measure"], repository_path=repo)
        self.assertEqual(
            result["apps"]["go-measure"]["configuration_source_sha"], source
        )
        reads = [
            args for args, _, _ in operator.runner.calls if args[:2] == ["git", "show"]
        ]
        self.assertTrue(
            all(arg.startswith(source + ":") for args in reads for arg in args[2:])
        )
        self.assertFalse(any(mutates for _, _, mutates in operator.runner.calls))

    def test_missing_or_invalid_incoming_release_config_is_rejected(self):
        for content in (None, "[]", '{"packages":{}}'):
            with self.subTest(content=content):
                operator, _, repo, remote, _, _ = self.fixture()
                if content is None:
                    (repo / "release-please-config.json").unlink()
                else:
                    (repo / "release-please-config.json").write_text(content)
                git(repo, "add", ".")
                git(repo, "commit", "-m", "invalid incoming configuration")
                git(repo, "push", "origin", "dev")
                with self.assertRaises(ReleaseError):
                    operator.preflight(["go-measure"], repository_path=repo)
                self.assertFalse(
                    any(mutates for _, _, mutates in operator.runner.calls)
                )

    def test_missing_or_invalid_incoming_manifest_is_rejected(self):
        for content in (None, "[]", '{".":42}', "not json"):
            with self.subTest(content=content):
                operator, _, repo, _, _, _ = self.fixture()
                manifest = repo / ".release-please-manifest.json"
                if content is None:
                    manifest.unlink()
                else:
                    manifest.write_text(content)
                git(repo, "add", ".")
                git(repo, "commit", "-m", "invalid incoming manifest")
                git(repo, "push", "origin", "dev")
                with self.assertRaises(ReleaseError):
                    operator.preflight(["go-measure"], repository_path=repo)
                self.assertFalse(
                    any(mutates for _, _, mutates in operator.runner.calls)
                )

    def test_missing_incoming_production_workflow_is_rejected(self):
        operator, _, repo, _, _, _ = self.fixture()
        yaml = (
            (repo / "codemagic.yaml")
            .read_text()
            .replace("Android production", "unrelated")
        )
        (repo / "codemagic.yaml").write_text(yaml)
        git(repo, "commit", "-am", "incomplete publishing configuration")
        git(repo, "push", "origin", "dev")
        with self.assertRaisesRegex(ReleaseError, "production workflow"):
            operator.preflight(["go-measure"], repository_path=repo)
        self.assertFalse(any(mutates for _, _, mutates in operator.runner.calls))

    def test_directory_ignore_guard_works_before_worktree_exists(self):
        operator, _, repo, _, _, _ = self.fixture()
        app = operator.inventory.apps["go-measure"]
        self.assertFalse((repo / ".claude/worktrees").exists())
        operator._verify_deployment_worktree_is_ignored(app, repo)
        (repo / ".gitignore").unlink()
        with self.assertRaisesRegex(ReleaseError, "not ignored"):
            operator._verify_deployment_worktree_is_ignored(app, repo)
