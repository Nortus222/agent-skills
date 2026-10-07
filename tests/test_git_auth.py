import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "deploy-mobile-apps"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import mobile_release
from mobile_release_lib import (
    BatchStore,
    CommandResult,
    ReleaseError,
    ReleaseOperator,
    SubprocessRunner,
    load_inventory,
)


class GitAuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.operator = ReleaseOperator(
            load_inventory(SKILL_ROOT / "references" / "apps.json"),
            BatchStore(Path(self.temporary.name)),
            runner=mock.Mock(),
            environ={},
        )

    def test_git_auth_failures_have_actionable_recovery(self):
        for stderr in (
            "git@github.com: Permission denied (publickey).",
            "fatal: Authentication failed for 'https://github.com/example/repo/'",
            "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
            "fatal: could not read Password for 'https://github.com': terminal prompts disabled",
        ):
            with self.subTest(stderr=stderr):
                self.operator.runner.run.return_value = CommandResult(128, "", stderr)
                with self.assertRaises(ReleaseError) as raised:
                    self.operator._run(
                        ["git", "fetch", "origin"], repository="example/repo"
                    )
                recovery = getattr(raised.exception, "recovery", None)
                self.assertIsNotNone(recovery)
                text = json.dumps(recovery)
                self.assertIn("ssh-add", text)
                self.assertIn("--git-auth gh", text)
                self.assertIn("gh auth status", text)
                self.assertIn("admin", text)
                self.assertIn("--git-auth gh", str(raised.exception))

    def test_non_auth_failures_do_not_suggest_switching_identity(self):
        for command, stderr in (
            (["git", "fetch"], "fatal: unable to resolve host github.com"),
            (["gh", "api", "user"], "Permission denied (publickey)."),
        ):
            with self.subTest(command=command):
                self.operator.runner.run.return_value = CommandResult(1, "", stderr)
                with self.assertRaises(ReleaseError) as raised:
                    self.operator._run(command, repository="example/repo")
                self.assertIsNone(getattr(raised.exception, "recovery", None))
                self.assertNotIn("--git-auth gh", str(raised.exception))

    def test_preflight_json_auth_error_has_recovery_without_batch(self):
        self.operator.runner.run.return_value = CommandResult(
            128, "", "git@github.com: Permission denied (publickey)."
        )
        self.operator.preflight = lambda *args, **kwargs: self.operator._run(
            ["git", "fetch", "origin"], repository="example/repo"
        )
        stdout = io.StringIO()
        with mock.patch.object(
            mobile_release, "build_operator", return_value=self.operator
        ):
            with mock.patch("sys.stdout", stdout):
                result = mobile_release.main(["preflight", "--json"])
        self.assertEqual(result, 1)
        recovery = json.loads(stdout.getvalue())["recovery"]
        self.assertIsNotNone(recovery)
        self.assertIn("--git-auth gh", json.dumps(recovery))

    def test_saved_batch_recovery_retains_batch_and_auth_advice(self):
        batch = {
            "batch_id": "batch-1",
            "state": "prepare-failed",
            "selected_apps": [],
            "apps": {},
        }
        self.operator.store.save(batch)
        self.operator.runner.run.return_value = CommandResult(
            128, "", "Permission denied (publickey)."
        )
        self.operator.prepare = lambda *args, **kwargs: self.operator._run(
            ["git", "fetch", "origin"], repository="example/repo"
        )
        stdout = io.StringIO()
        with mock.patch.object(
            mobile_release, "build_operator", return_value=self.operator
        ):
            with mock.patch("sys.stdout", stdout):
                result = mobile_release.main(
                    ["prepare", "--batch", "batch-1", "--json"]
                )
        self.assertEqual(result, 1)
        recovery = json.loads(stdout.getvalue())["recovery"]
        self.assertEqual(recovery["batch_id"], "batch-1")
        self.assertIn("--git-auth gh", json.dumps(recovery))

    def test_each_subcommand_accepts_explicit_gh_auth(self):
        for command in ("preflight", "prepare", "release", "status"):
            with self.subTest(command=command):
                argv = [command, "--git-auth", "gh"]
                if command != "preflight":
                    argv.extend(["--batch", "batch-1"])
                self.assertEqual(
                    mobile_release._parser().parse_args(argv).git_auth, "gh"
                )

    def test_cli_passes_auth_choice_to_operator(self):
        batch = {
            "batch_id": "batch-1",
            "state": "staging-complete",
            "selected_apps": [],
            "apps": {},
        }
        with mock.patch.object(mobile_release, "build_operator") as factory:
            factory.return_value.status.return_value = batch
            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(
                    mobile_release.main(
                        ["status", "--batch", "batch-1", "--git-auth", "gh"]
                    ),
                    0,
                )
            factory.assert_called_once_with(git_auth="gh")

    def test_operator_factory_configures_git_auth_runner(self):
        self.assertEqual(
            mobile_release.build_operator(git_auth="gh").runner.git_auth, "gh"
        )

    def test_gh_auth_config_is_git_only_and_preserves_existing_env_config(self):
        initial = {
            "PATH": os.environ.get("PATH", ""),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "fetch.prune",
            "GIT_CONFIG_VALUE_0": "true",
        }
        runner = SubprocessRunner(git_auth="gh")
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.dict(os.environ, initial, clear=True):
            with mock.patch(
                "mobile_release_lib.subprocess.run", return_value=completed
            ) as run:
                runner.run(["git", "submodule", "update", "--init", "--recursive"])
                env = run.call_args.kwargs["env"]
                self.assertEqual(env["GIT_CONFIG_COUNT"], "4")
                self.assertEqual(env["GIT_CONFIG_KEY_0"], "fetch.prune")
                self.assertEqual(env["GIT_CONFIG_VALUE_0"], "true")
                self.assertEqual(
                    env["GIT_CONFIG_KEY_1"], "url.https://github.com/.insteadOf"
                )
                self.assertEqual(env["GIT_CONFIG_VALUE_1"], "git@github.com:")
                self.assertEqual(
                    env["GIT_CONFIG_KEY_2"], "credential.https://github.com.helper"
                )
                self.assertEqual(env["GIT_CONFIG_VALUE_2"], "")
                self.assertEqual(
                    env["GIT_CONFIG_KEY_3"], "credential.https://github.com.helper"
                )
                self.assertEqual(env["GIT_CONFIG_VALUE_3"], "!gh auth git-credential")
                self.assertEqual(dict(os.environ), initial)
                runner.run(["gh", "pr", "merge", "42", "--admin"])
                self.assertNotIn("env", run.call_args.kwargs)
                self.assertEqual(dict(os.environ), initial)

    def test_default_auth_preserves_git_environment(self):
        with mock.patch("mobile_release_lib.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            SubprocessRunner().run(["git", "fetch", "origin"])
            self.assertNotIn("env", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
