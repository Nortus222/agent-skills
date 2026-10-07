"""Saved staging observations follow the current checks for the staged commit."""

import copy
import io
import json
import unittest
from unittest import mock

from test_mobile_release import (
    RecordedCall,
    awaiting_approval_operator,
    prepared_operator,
)
from mobile_release_lib import CommandResult, ReleaseError
import mobile_release


def staging_observer(operator, outcome="success"):
    """Replay staging checks while retaining the existing git and PR fixtures."""
    original_run = operator.runner.run
    operator._codemagic_cached = None
    state = {"outcome": outcome}

    def run(args, *, cwd=None, mutates=False):
        if args[:2] == ["gh", "api"] and args[-1].endswith("/check-runs"):
            operator.runner.calls.append(RecordedCall(tuple(args), cwd, mutates))
            checks = [
                {
                    "name": name,
                    "status": (
                        "in_progress" if state["outcome"] is None else "completed"
                    ),
                    "conclusion": state["outcome"],
                    "details_url": "https://codemagic.io/app/6279a0f0460c338024f89175/build/6ac5cd662e8bb15cf3973e51",
                    "app": {"name": "Codemagic CI/CD"},
                }
                for name in operator.inventory.staging_checks
            ]
            return CommandResult(0, json.dumps({"check_runs": checks}), "")
        return original_run(args, cwd=cwd, mutates=mutates)

    operator.runner.run = run
    return state


class StagingRefreshTests(unittest.TestCase):
    def prepare_operator(self):
        operator, batch = prepared_operator(submodule_changed=False)
        self.addCleanup(operator._test_temporary_directory.cleanup)
        return operator, batch

    def test_status_human_report_replaces_stale_staging_result_and_shows_checks(self):
        operator, batch = self.prepare_operator()
        state = staging_observer(operator, "failure")
        operator.prepare(batch["batch_id"], staging_only=True)
        state["outcome"] = "success"
        output = io.StringIO()

        with mock.patch.object(mobile_release, "build_operator", return_value=operator):
            with mock.patch("sys.stdout", output):
                self.assertEqual(
                    mobile_release.main(["status", "--batch", batch["batch_id"]]), 0
                )

        self.assertIn("staging builds passed", output.getvalue())
        self.assertNotIn("builds failed", output.getvalue())
        for name in operator.inventory.staging_checks:
            self.assertIn(f"{name}=success", output.getvalue())

    def test_running_staging_checks_are_not_reported_as_passed(self):
        operator, batch = self.prepare_operator()
        staging_observer(operator, None)

        staged = operator.prepare(batch["batch_id"], staging_only=True)

        self.assertIn("in progress", staged["apps"]["pocket-manage"]["result"])

    def test_prepare_reports_checks_that_finish_while_waiting_for_promotion(self):
        operator, batch = prepared_operator(
            submodule_changed=False, pending_check_outcome="success"
        )
        self.addCleanup(operator._test_temporary_directory.cleanup)
        state = staging_observer(operator, None)
        original_run = operator.runner.run

        def run(args, **kwargs):
            response = original_run(args, **kwargs)
            if args[:3] == ["gh", "pr", "checks"] and "--watch" in args:
                state["outcome"] = "success"
            return response

        operator.runner.run = run
        result = operator.prepare(batch["batch_id"])

        self.assertTrue(
            all(
                check["conclusion"] == "success"
                for check in result["apps"]["pocket-manage"]["staging_checks"].values()
            )
        )

    def test_observation_error_recovery_loads_the_saved_batch_without_retrying_remote(
        self,
    ):
        operator, batch = self.prepare_operator()
        batch["state"] = "awaiting-approval"
        batch["apps"]["pocket-manage"]["staging_sha"] = "d" * 40
        operator.store.save(batch)
        output = io.StringIO()

        with mock.patch.object(
            operator.runner, "run", return_value=CommandResult(1, "", "HTTP 502")
        ) as run:
            with mock.patch.object(
                mobile_release, "build_operator", return_value=operator
            ):
                with mock.patch("sys.stdout", output):
                    self.assertEqual(
                        mobile_release.main(
                            ["status", "--batch", batch["batch_id"], "--json"]
                        ),
                        1,
                    )

        recovery = json.loads(output.getvalue())["recovery"]
        self.assertIsNotNone(recovery)
        self.assertEqual(recovery["batch_id"], batch["batch_id"])
        self.assertEqual(recovery["state"], "awaiting-approval")
        self.assertIn("release --batch", recovery["next_command"])
        run.assert_called_once()

    def test_resumed_prepare_replaces_queued_snapshot_before_promotion(self):
        operator, batch = self.prepare_operator()
        state = staging_observer(operator, None)
        staged = operator.prepare(batch["batch_id"], staging_only=True)
        self.assertTrue(
            all(
                c["conclusion"] is None
                for c in staged["apps"]["pocket-manage"]["staging_checks"].values()
            )
        )
        # Reproduce the missing Android check and old application diagnosis.
        record = staged["apps"]["pocket-manage"]
        record["staging_checks"].pop(operator.inventory.staging_checks[1])
        record["staging_missing_checks"] = [operator.inventory.staging_checks[1]]
        record["staging_diagnosis"] = {
            "missing": {"note": "no CodeMagic application matched"}
        }
        operator.store.save(staged)
        state["outcome"] = "success"

        result = operator.prepare(batch["batch_id"])

        refreshed = result["apps"]["pocket-manage"]
        self.assertEqual(result["state"], "awaiting-approval")
        self.assertTrue(
            all(
                c["conclusion"] == "success"
                for c in refreshed["staging_checks"].values()
            )
        )
        self.assertEqual(refreshed["staging_missing_checks"], [])
        self.assertEqual(refreshed["staging_failed_checks"], [])
        self.assertEqual(refreshed["staging_diagnosis"], {})
        self.assertEqual(
            operator.store.load(batch["batch_id"])["apps"]["pocket-manage"][
                "staging_checks"
            ],
            refreshed["staging_checks"],
        )

    def test_refresh_failure_blocks_promotion_even_if_preparation_checks_pass(self):
        operator, batch = self.prepare_operator()
        state = staging_observer(operator)
        operator.prepare(batch["batch_id"], staging_only=True)
        state["outcome"] = "failure"
        operator.runner.calls.clear()

        with self.assertRaisesRegex(ReleaseError, "staging checks failed"):
            operator.prepare(batch["batch_id"])

        saved = operator.store.load(batch["batch_id"])
        self.assertEqual(saved["state"], "prepare-failed")
        self.assertEqual(
            saved["apps"]["pocket-manage"]["staging_failed_checks"],
            sorted(operator.inventory.staging_checks),
        )
        self.assertFalse(
            any(
                c.args[:3] in {("gh", "pr", "create"), ("gh", "pr", "merge")}
                for c in operator.runner.calls
            )
        )

    def test_status_refreshes_all_staged_apps_without_saving_or_changing_lifecycle(
        self,
    ):
        operator, batch = self.prepare_operator()
        staging_observer(operator)
        record = batch["apps"]["pocket-manage"]
        record.update(
            staging_sha="d" * 40,
            status="prepared",
            result="ready for approval",
            staging_checks={},
            staging_missing_checks=list(operator.inventory.staging_checks),
            staging_diagnosis={"missing": {"note": "no CodeMagic application matched"}},
        )
        batch["state"] = "awaiting-approval"
        batch["selected_apps"] = list(operator.inventory.apps)
        batch["apps"] = {
            key: dict(copy.deepcopy(record), repository=app.repository)
            for key, app in operator.inventory.apps.items()
        }
        operator.store.save(batch)
        snapshot_count = len(operator.store.snapshots)

        observed = operator.status(batch["batch_id"])

        for record in observed["apps"].values():
            self.assertTrue(record["staging_checks"])
            self.assertEqual(record["staging_missing_checks"], [])
            self.assertEqual(record["staging_diagnosis"], {})
            self.assertEqual(record["status"], "prepared")
            self.assertEqual(record["result"], "ready for approval")
        self.assertEqual(observed["state"], "awaiting-approval")
        self.assertEqual(len(operator.store.snapshots), snapshot_count)
        self.assertEqual(operator.store.load(batch["batch_id"]), batch)
        self.assertFalse(any(c.mutates for c in operator.runner.calls))

    def test_release_refreshes_staging_without_replacing_release_results(self):
        operator, batch = awaiting_approval_operator()
        self.addCleanup(operator._test_temporary_directory.cleanup)
        record = batch["apps"]["pocket-manage"]
        record.update(
            staging_sha="d" * 40,
            staging_checks={},
            staging_missing_checks=list(operator.inventory.staging_checks),
        )
        operator.store.save(batch)
        # This test exercises release bookkeeping; release build verification has
        # its own fixtures and tests.
        staging_observer(operator)
        with mock.patch.object(operator, "_verify_release", return_value=True):
            result = operator.release(batch["batch_id"])

        refreshed = result["apps"]["pocket-manage"]
        self.assertTrue(refreshed["staging_checks"])
        self.assertEqual(refreshed["staging_missing_checks"], [])
        self.assertEqual(refreshed["status"], "released")
        self.assertEqual(result["state"], "released")
