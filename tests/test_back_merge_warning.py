"""Preflight warns when release carries commits dev never received.

Release Please writes the version, changelog and manifest on `release`. Those
reach `dev` only through a back-merge that nothing automates, so the branches
drift silently. Installers and partner drifted for two releases before anyone
noticed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "deploy-mobile-apps"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mobile_release  # noqa: E402
from mobile_release_lib import CommandResult  # noqa: E402
from test_mobile_release import (  # noqa: E402
    make_operator,
    successful_preflight_responses,
)


class BackMergeWarningTests(unittest.TestCase):
    def test_records_the_drift_when_release_is_not_contained_in_dev(self) -> None:
        responses = successful_preflight_responses(app_count=1)
        # The ancestry probe is the last command of an app's preflight.
        responses[-1] = CommandResult(1, "", "")
        operator = make_operator(responses)
        self.addCleanup(operator._test_temporary_directory.cleanup)

        batch = operator.preflight(["pocket-manage"])

        self.assertTrue(batch["apps"]["pocket-manage"]["release_ahead_of_dev"])

    def test_records_no_drift_when_release_is_an_ancestor_of_dev(self) -> None:
        operator = make_operator(successful_preflight_responses(app_count=1))
        self.addCleanup(operator._test_temporary_directory.cleanup)

        batch = operator.preflight(["pocket-manage"])

        self.assertFalse(batch["apps"]["pocket-manage"]["release_ahead_of_dev"])

    def test_probes_ancestry_with_a_read_only_command(self) -> None:
        operator = make_operator(successful_preflight_responses(app_count=1))
        self.addCleanup(operator._test_temporary_directory.cleanup)

        operator.preflight(["pocket-manage"])

        probe = operator.runner.calls[-1]
        self.assertEqual(
            probe.args,
            (
                "git",
                "merge-base",
                "--is-ancestor",
                "refs/remotes/origin/release",
                "refs/remotes/origin/dev",
            ),
        )
        self.assertFalse(probe.mutates)

    def test_the_drift_reaches_the_human_as_a_warning(self) -> None:
        batch = {
            "batch_id": "batch-1",
            "state": "preflight-complete",
            "selected_apps": ["pocket-manage"],
            "apps": {"pocket-manage": {"release_ahead_of_dev": True}},
        }

        warnings = mobile_release._batch_warnings(batch)

        self.assertTrue(
            any("release is ahead of dev" in warning for warning in warnings),
            warnings,
        )

    def test_a_contained_release_produces_no_warning(self) -> None:
        batch = {
            "batch_id": "batch-1",
            "state": "preflight-complete",
            "selected_apps": ["pocket-manage"],
            "apps": {"pocket-manage": {"release_ahead_of_dev": False}},
        }

        self.assertEqual(mobile_release._batch_warnings(batch), [])


if __name__ == "__main__":
    unittest.main()
