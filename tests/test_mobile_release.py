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
