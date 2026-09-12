"""Preflight rejects a codemagic.yaml Codemagic itself would reject.

An empty value under a workflow's vars fails Codemagic's schema, and a
configuration that fails schema validation starts no build at all: the webhook
is accepted with 202 and nothing is created, so there is no build, no check run
and no log to read. Catching it before the push is the only cheap moment.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "deploy-mobile-apps"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mobile_release_lib import (  # noqa: E402
    _NO_BUILD_NOTE,
    _unusable_codemagic_values,
)


class EmptyValueTests(unittest.TestCase):
    def test_finds_a_double_quoted_empty_value(self) -> None:
        content = 'workflows:\n  ios:\n    vars:\n      FLAGS: ""\n'
        self.assertEqual(_unusable_codemagic_values(content), ["FLAGS"])

    def test_finds_a_single_quoted_empty_value(self) -> None:
        content = "workflows:\n  ios:\n    vars:\n      FLAGS: ''\n"
        self.assertEqual(_unusable_codemagic_values(content), ["FLAGS"])

    def test_finds_a_value_that_is_absent_entirely(self) -> None:
        content = "workflows:\n  ios:\n    vars:\n      FLAGS:\n      OTHER: keep\n"
        self.assertEqual(_unusable_codemagic_values(content), ["FLAGS"])

    def test_finds_an_empty_value_inside_a_definitions_anchor(self) -> None:
        # The real defect lived here, merged into vars with `<<:`, which is why
        # scanning only under `vars:` would have missed it.
        content = (
            "definitions:\n"
            "  prod_vars: &prod_vars\n"
            '    FLUTTER_TARGET: lib/main_prod.dart\n'
            '    EMRELEASE_VERSION_FLAGS: ""\n'
        )
        self.assertEqual(
            _unusable_codemagic_values(content), ["EMRELEASE_VERSION_FLAGS"]
        )

    def test_accepts_a_key_that_introduces_a_block(self) -> None:
        content = (
            "workflows:\n"
            "  ios:\n"
            "    environment:\n"
            "      vars:\n"
            "        FLAGS: --staging\n"
        )
        self.assertEqual(_unusable_codemagic_values(content), [])

    def test_accepts_a_key_whose_block_is_a_list(self) -> None:
        content = "publishing:\n  beta_groups:\n    - Dev\n"
        self.assertEqual(_unusable_codemagic_values(content), [])

    def test_accepts_numbers_and_booleans(self) -> None:
        content = (
            "vars:\n"
            "  RETRIES: 0\n"
            "  RATIO: 0.0\n"
            "  ENABLED: false\n"
        )
        self.assertEqual(_unusable_codemagic_values(content), [])

    def test_ignores_comments_and_blank_lines(self) -> None:
        content = (
            "vars:\n"
            "  # FLAGS: \"\"\n"
            "\n"
            "  FLAGS: --staging\n"
        )
        self.assertEqual(_unusable_codemagic_values(content), [])

    def test_reports_every_offending_key_once_and_in_order(self) -> None:
        content = 'vars:\n  A: ""\n  B: fine\n  C:\n  D: \'\'\n'
        self.assertEqual(_unusable_codemagic_values(content), ["A", "C", "D"])


class RealConfigTests(unittest.TestCase):
    def test_a_representative_valid_config_is_accepted(self) -> None:
        content = (
            "definitions:\n"
            "  prod_vars: &prod_vars\n"
            "    FLUTTER_TARGET: lib/main_prod.dart\n"
            "  staging_vars: &staging_vars\n"
            "    FLUTTER_TARGET: lib/main_staging.dart\n"
            '    EMRELEASE_VERSION_FLAGS: "--staging"\n'
            "workflows:\n"
            "  ios-staging:\n"
            "    environment:\n"
            "      vars:\n"
            "        <<: *staging_vars\n"
            "        PLATFORM: ios\n"
        )
        self.assertEqual(_unusable_codemagic_values(content), [])


class MissingBuildNoteTests(unittest.TestCase):
    def test_names_config_rejection_as_a_cause(self) -> None:
        # "check the push trigger" alone sent a real investigation at the trigger
        # when the cause was a schema rejection.
        self.assertIn("validation", _NO_BUILD_NOTE.lower())

    def test_still_mentions_the_trigger(self) -> None:
        self.assertIn("trigger", _NO_BUILD_NOTE.lower())


if __name__ == "__main__":
    unittest.main()
