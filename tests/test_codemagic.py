"""Tests for the read-only CodeMagic diagnostics client."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "deploy-mobile-apps"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from codemagic import (  # noqa: E402
    CodemagicClient,
    CodemagicError,
    build_reference,
    failed_steps,
    resolve_token,
)


class FakeTransport:
    """Record requests and replay canned responses keyed by path."""

    def __init__(self, responses: dict[str, object], status: int = 200) -> None:
        self.responses = responses
        self.status = status
        self.requests: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        self.requests.append((url, headers))
        for path, payload in self.responses.items():
            if url.endswith(path):
                body = payload if isinstance(payload, str) else json.dumps(payload)
                return self.status, body
        return 404, '{"error":"not found"}'


class BuildReferenceTests(unittest.TestCase):
    def test_reads_the_app_and_build_id_out_of_a_check_run_url(self) -> None:
        reference = build_reference(
            "https://codemagic.io/app/646e7f2bc6cf72605af3f3f7/build/6aa48690688764aa17ba2705"
        )
        self.assertEqual(reference.app_id, "646e7f2bc6cf72605af3f3f7")
        self.assertEqual(reference.build_id, "6aa48690688764aa17ba2705")

    def test_ignores_a_url_that_is_not_a_codemagic_build(self) -> None:
        self.assertIsNone(build_reference("https://github.com/owner/repo/runs/1"))
        self.assertIsNone(build_reference("https://codemagic.io/app/abc"))
        self.assertIsNone(build_reference(""))


class FailedStepTests(unittest.TestCase):
    def test_names_only_the_steps_that_failed(self) -> None:
        build = {
            "buildActions": [
                {"name": "Get Flutter packages", "status": "success", "_id": "1"},
                {"name": "Flutter analyze", "status": "failed", "_id": "2"},
                {"name": "Publishing", "status": "failed", "_id": "3"},
            ]
        }
        self.assertEqual(
            [step["name"] for step in failed_steps(build)],
            ["Flutter analyze", "Publishing"],
        )

    def test_is_empty_when_the_build_succeeded(self) -> None:
        build = {"buildActions": [{"name": "Publishing", "status": "success"}]}
        self.assertEqual(failed_steps(build), [])

    def test_tolerates_a_build_with_no_actions(self) -> None:
        self.assertEqual(failed_steps({}), [])


class ClientTests(unittest.TestCase):
    def test_build_unwraps_the_build_envelope(self) -> None:
        transport = FakeTransport(
            {"/builds/abc": {"application": {}, "build": {"_id": "abc", "status": "failed"}}}
        )
        client = CodemagicClient("secret-token", transport=transport)

        build = client.build("abc")

        self.assertEqual(build["status"], "failed")
        _, headers = transport.requests[0]
        self.assertEqual(headers["x-auth-token"], "secret-token")

    def test_build_raises_on_an_error_status(self) -> None:
        client = CodemagicClient("t", transport=FakeTransport({}, status=500))
        with self.assertRaises(CodemagicError):
            client.build("missing")

    def test_step_log_returns_the_raw_text(self) -> None:
        transport = FakeTransport({"/builds/abc/step/2": "Publishing failed :|"})
        client = CodemagicClient("t", transport=transport)

        self.assertEqual(client.step_log("abc", "2"), "Publishing failed :|")

    def test_builds_for_branch_filters_by_repository_branch(self) -> None:
        transport = FakeTransport(
            {
                "/builds": {
                    "builds": [
                        {"_id": "1", "appId": "app-a", "branch": "staging"},
                        {"_id": "2", "appId": "app-b", "branch": "staging"},
                        {"_id": "3", "appId": "app-a", "branch": "release"},
                    ]
                }
            }
        )
        client = CodemagicClient("t", transport=transport)

        found = client.builds_for_branch("app-a", "staging")

        self.assertEqual([build["_id"] for build in found], ["1"])


class TokenTests(unittest.TestCase):
    def test_prefers_the_environment_variable(self) -> None:
        token = resolve_token(
            environ={"CODEMAGIC_API_TOKEN": "from-env"},
            keychain=lambda: "from-keychain",
        )
        self.assertEqual(token, "from-env")

    def test_falls_back_to_the_keychain(self) -> None:
        token = resolve_token(environ={}, keychain=lambda: "from-keychain")
        self.assertEqual(token, "from-keychain")

    def test_is_none_when_neither_is_configured(self) -> None:
        self.assertIsNone(resolve_token(environ={}, keychain=lambda: None))

    def test_ignores_a_blank_environment_variable(self) -> None:
        token = resolve_token(
            environ={"CODEMAGIC_API_TOKEN": "  "}, keychain=lambda: "from-keychain"
        )
        self.assertEqual(token, "from-keychain")


if __name__ == "__main__":
    unittest.main()
