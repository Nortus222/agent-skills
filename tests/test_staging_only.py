"""Tests for the staging-only rollout path."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "deploy-mobile-apps"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import mobile_release  # noqa: E402
from mobile_release_lib import BatchStore, ReleaseOperator, load_inventory  # noqa: E402

INVENTORY = SKILL_ROOT / "references" / "apps.json"


class RecordingStore(BatchStore):
    """Keep the batch in memory so a test never writes to the state directory."""

    def __init__(self, batch: dict[str, Any]) -> None:
        self.batch = batch
        self.saves = 0

    def load(self, batch_id: str) -> dict[str, Any]:
        return self.batch

    def save(self, batch: dict[str, Any]) -> Path:
        self.batch = batch
        self.saves += 1
        return Path()


class StepRecorder(ReleaseOperator):
    """Replace the three git-touching steps with a record of the calls made."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    def _prepare_development_pointer(self, batch, app, app_record, *, dry_run):
        self.calls.append("bump")
        app_record["dev_sha"] = "d" * 40

    def _fast_forward_staging(self, batch, app, app_record, *, dry_run):
        self.calls.append("fast-forward")
        app_record["staging_sha"] = "d" * 40

    def _promote_development(self, batch, app, app_record, *, dry_run):
        self.calls.append("promote")

    def discover_versions(self, batch):
        self.calls.append("discover-versions")
        return batch

    def _verify_staging(self, batch, app, app_record):
        self.calls.append("verify-staging")
        return True


def _batch(state: str = "preflight-complete") -> dict[str, Any]:
    """A batch shaped the way preflight leaves one, for a single app."""
    return {
        "schema_version": 1,
        "batch_id": "b" * 32,
        "state": state,
        "selected_apps": ["pocket-manage"],
        "apps": {
            "pocket-manage": {
                "state": "preflight-complete",
                "repository": "MarketplaceSoftware/pocketmanage",
                "repository_path": "/tmp/pocketmanage",
                "branches": ["dev", "staging", "release"],
                "dev_sha": "a" * 40,
                "release_sha": "c" * 40,
                "packages_pointer_sha": "e" * 40,
                "packages_sha": "f" * 40,
                "staging_to_release_pr": None,
            }
        },
    }


def _operator(batch: dict[str, Any]) -> StepRecorder:
    return StepRecorder(load_inventory(INVENTORY), RecordingStore(batch))


class InventoryTests(unittest.TestCase):
    def test_staging_checks_are_configured_separately_from_release_checks(self) -> None:
        inventory = load_inventory(INVENTORY)

        self.assertTrue(inventory.staging_checks)
        self.assertFalse(
            set(inventory.staging_checks) & set(inventory.codemagic_checks),
            "a staging workflow must never gate a release",
        )

    def test_every_app_still_carries_the_three_branches(self) -> None:
        inventory = load_inventory(INVENTORY)
        for app in inventory.apps.values():
            self.assertEqual(
                [app.dev_branch, app.staging_branch, app.release_branch],
                ["dev", "staging", "release"],
            )


class StagingOnlyPrepareTests(unittest.TestCase):
    def test_bumps_and_fast_forwards_without_promoting(self) -> None:
        operator = _operator(_batch())

        result = operator.prepare("b" * 32, staging_only=True)

        self.assertEqual(operator.calls, ["bump", "fast-forward", "verify-staging"])
        self.assertNotIn("promote", operator.calls)
        self.assertEqual(result["state"], "staging-complete")

    def test_does_not_discover_release_versions(self) -> None:
        operator = _operator(_batch())

        operator.prepare("b" * 32, staging_only=True)

        self.assertNotIn("discover-versions", operator.calls)

    def test_resuming_the_same_batch_promotes_to_release(self) -> None:
        operator = _operator(_batch(state="staging-complete"))

        operator.prepare("b" * 32)

        self.assertIn("promote", operator.calls)
        self.assertIn("discover-versions", operator.calls)

    def test_a_dry_run_stops_at_staging_without_touching_anything(self) -> None:
        operator = _operator(_batch())

        result = operator.prepare("b" * 32, staging_only=True, dry_run=True)

        self.assertNotIn("promote", operator.calls)
        self.assertEqual(result["state"], "dry-run-complete")


class NextCommandTests(unittest.TestCase):
    def test_staging_complete_resumes_the_same_batch(self) -> None:
        command = mobile_release._next_command(_batch(state="staging-complete"))
        self.assertEqual(command, f"{mobile_release.COMMAND_PREFIX} prepare --batch {'b' * 32}")

    def test_awaiting_approval_still_points_at_release(self) -> None:
        command = mobile_release._next_command(_batch(state="awaiting-approval"))
        self.assertEqual(command, f"{mobile_release.COMMAND_PREFIX} release --batch {'b' * 32}")


if __name__ == "__main__":
    unittest.main()
