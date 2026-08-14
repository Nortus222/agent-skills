"""Inventory and on-disk state helpers for mobile release batches."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit


PACKAGES_REPOSITORY = "MarketplaceSoftware/packages"


class ReleaseError(RuntimeError):
    """Report a release guard failure without exposing process credentials."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        mutates: bool = False,
    ) -> CommandResult: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class PollPolicy:
    interval_seconds: float = 5.0
    timeout_seconds: float = 300.0


class SubprocessRunner:
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        mutates: bool = False,
    ) -> CommandResult:
        completed = subprocess.run(
            list(args), cwd=cwd, text=True, capture_output=True, check=False
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class AppConfig:
    key: str
    display_name: str
    repository: str
    directory: str
    dev_branch: str
    release_branch: str
    submodule_path: str
    submodule_branch: str
    release_label: str
    release_manifest: str


@dataclass(frozen=True)
class Inventory:
    workspace_root_env: str
    default_workspace_root: str
    codemagic_checks: tuple[str, ...]
    apps: dict[str, AppConfig]


class BatchStore:
    """Persist release batches as private, atomically replaced JSON files."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, batch: dict[str, Any]) -> Path:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        batch_id = batch["batch_id"]
        destination = self._path_for(batch_id)

        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.root, prefix=f".{batch_id}.", suffix=".tmp", delete=False
        ) as temporary:
            json.dump(batch, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary_path = Path(temporary.name)

        try:
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, destination)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

        return destination

    def load(self, batch_id: str) -> dict[str, Any]:
        with self._path_for(batch_id).open(encoding="utf-8") as source:
            return json.load(source)

    def _path_for(self, batch_id: str) -> Path:
        if not batch_id or Path(batch_id).name != batch_id:
            raise ValueError("batch_id must be a filename, not a path")
        return self.root / f"{batch_id}.json"


def load_inventory(path: Path) -> Inventory:
    with path.open(encoding="utf-8") as source:
        data = json.load(source)

    defaults = data["defaults"]
    apps = {
        key: AppConfig(key=key, **app, **defaults)
        for key, app in data["apps"].items()
    }
    return Inventory(
        workspace_root_env=data["workspace_root_env"],
        default_workspace_root=data["default_workspace_root"],
        codemagic_checks=tuple(data["codemagic_checks"]),
        apps=apps,
    )


def new_batch(selected_apps: Sequence[str], now: str | None = None) -> dict[str, Any]:
    """Create a pending batch with independent pending records for each app."""
    app_keys = list(selected_apps)
    return {
        "schema_version": 1,
        "batch_id": uuid.uuid4().hex,
        "state": "pending",
        "created_at": now or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "selected_apps": app_keys,
        "apps": {app_key: {"state": "pending"} for app_key in app_keys},
    }


class ReleaseOperator:
    """Guard phase-oriented mobile release reads and writes."""

    def __init__(
        self,
        inventory: Inventory,
        store: BatchStore,
        *,
        runner: Runner | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Clock | None = None,
        poll_policy: PollPolicy | None = None,
    ) -> None:
        self.inventory = inventory
        self.store = store
        self.runner = runner or SubprocessRunner()
        self.environ = dict(os.environ if environ is None else environ)
        self.clock = clock or SystemClock()
        self.poll_policy = poll_policy or PollPolicy()

    def preflight(self, app_keys: Sequence[str], dry_run: bool = False) -> dict[str, Any]:
        selected_apps = list(app_keys)
        self._validate_selection(selected_apps)
        self._run(
            ["gh", "auth", "status", "--hostname", "github.com"],
            repository="GitHub",
        )

        workspace_root = self._workspace_root()
        repositories = self._discover_repositories(workspace_root, selected_apps)
        batch = new_batch(selected_apps)

        for app_key in selected_apps:
            app = self.inventory.apps[app_key]
            repository_path = repositories[app_key]
            app_record = self._preflight_app(app, repository_path)
            batch["apps"][app_key] = app_record

        batch["state"] = "preflight-complete"
        if not dry_run:
            self.store.save(batch)
        return batch

    def prepare(self, batch_id: str, dry_run: bool = False) -> dict[str, Any]:
        """Update development pointers and merge each exact promotion PR."""
        batch = self.store.load(batch_id)
        if batch.get("state") not in {
            "preflight-complete",
            "prepare-in-progress",
            "prepare-failed",
            "prepare-complete",
        }:
            raise ReleaseError(f"batch {batch_id}: prepare is not allowed from its current state")

        batch["state"] = "prepare-in-progress"
        for app_key in batch["selected_apps"]:
            app = self.inventory.apps[app_key]
            app_record = batch["apps"][app_key]
            self._initialize_preparation_record(app_record)
            try:
                if self._completed_preparation_is_current(app, app_record):
                    continue
                self._prepare_development_pointer(
                    batch,
                    app,
                    app_record,
                    dry_run=dry_run,
                )
                self._promote_development(
                    batch,
                    app,
                    app_record,
                    dry_run=dry_run,
                )
            except ReleaseError as error:
                app_record["status"] = "failed"
                app_record["state"] = "prepare-failed"
                app_record["error"] = str(error)
                batch["state"] = "prepare-failed"
                if not dry_run:
                    self.store.save(batch)
                raise

        batch["state"] = "dry-run-complete" if dry_run else "prepare-complete"
        if not dry_run:
            self.store.save(batch)
            return self.discover_versions(batch)
        return batch

    def discover_versions(self, batch: dict[str, Any]) -> dict[str, Any]:
        """Snapshot Release Please proposals for one human approval decision."""
        if batch.get("state") not in {"prepare-complete", "awaiting-approval"}:
            raise ReleaseError(
                f"batch {batch.get('batch_id', '<unknown>')}: version discovery is not allowed "
                "from its current state"
            )

        for app_key in batch["selected_apps"]:
            app = self.inventory.apps[app_key]
            app_record = batch["apps"][app_key]
            if app_record.get("skip_reason"):
                continue
            if app_record.get("status") != "prepared":
                raise ReleaseError(
                    f"repository {app.repository}: preparation is incomplete"
                )

            self._wait_for_release_workflow(app, app_record["release_sha"])
            pull_requests = self._list_release_pull_requests(app)
            if not pull_requests:
                app_record.update(
                    {
                        "state": "awaiting-approval",
                        "status": "skipped",
                        "skip_reason": "no releasable changes",
                        "result": "no releasable changes",
                    }
                )
                self.store.save(batch)
                continue

            pull_request = pull_requests[0]
            head_sha = pull_request.get("headRefOid")
            if not _is_sha(head_sha or ""):
                raise ReleaseError(
                    f"repository {app.repository}: invalid Release Please pull request head"
                )
            version = self._manifest_version_at_head(app, head_sha)
            app_record.update(
                {
                    "state": "awaiting-approval",
                    "status": "prepared",
                    "version": version,
                    "release_pr_number": pull_request["number"],
                    "release_pr_url": pull_request["url"],
                    "release_pr_checks": pull_request.get("statusCheckRollup", []),
                    "release_pr_head_sha": head_sha,
                    "submodule_sha": app_record["packages_sha"],
                    "result": "ready for approval",
                }
            )
            self.store.save(batch)

        batch["state"] = "awaiting-approval"
        self.store.save(batch)
        return batch

    def release(self, batch_id: str) -> dict[str, Any]:
        """Merge an unchanged approval snapshot and verify release build startup."""
        batch = self.store.load(batch_id)
        allowed_states = {
            "awaiting-approval",
            "partial-release",
            "release-failed",
            "released-builds-unverified",
        }
        if batch.get("state") not in allowed_states:
            raise ReleaseError(
                f"batch {batch_id}: release is not allowed from its current state"
            )

        included = [
            app_key
            for app_key in batch["selected_apps"]
            if not batch["apps"][app_key].get("skip_reason")
        ]
        unfinished = []
        approval_changed = False
        for app_key in included:
            app = self.inventory.apps[app_key]
            app_record = batch["apps"][app_key]
            release_merge = app_record.get("release_merge")
            if release_merge in {"merged", "merge-unverified"}:
                merge_sha = self._confirm_recorded_release_merge(
                    app,
                    app_record,
                    require_recorded_sha=release_merge == "merged",
                )
                if release_merge == "merge-unverified":
                    app_record.update(
                        {
                            "release_merge": "merged",
                            "release_merge_sha": merge_sha,
                            "state": "release-merged",
                            "status": "released",
                            "error": None,
                            "result": "Release Please pull request merged",
                        }
                    )
                    self.store.save(batch)
                continue

            snapshot = self._current_release_snapshot(app, app_record)
            if not self._approval_snapshot_matches(app, app_record, snapshot):
                self._replace_approval_snapshot(app_record, snapshot)
                approval_changed = True
            unfinished.append((app, app_record))

        if approval_changed:
            batch["state"] = "awaiting-approval"
            self.store.save(batch)
            raise ReleaseError(
                f"batch {batch_id}: approval snapshot changed; review and approve again"
            )

        for app, app_record in unfinished:
            number = str(app_record["release_pr_number"])
            merge_command = [
                "gh",
                "pr",
                "merge",
                number,
                "--repo",
                app.repository,
                "--merge",
            ]
            merge_result = self.runner.run(merge_command, mutates=True)
            if merge_result.returncode != 0:
                self._record_release_failure(
                    batch,
                    app_record,
                    self._command_error(app.repository, merge_command, merge_result),
                )

            app_record.update(
                {
                    "release_merge": "merge-unverified",
                    "state": "release-merge-unverified",
                    "status": "pending",
                    "result": "Release Please merge confirmation pending",
                }
            )
            self.store.save(batch)
            try:
                merged_pull_request = self._read_release_pull_request(app, number)
                merge_sha = self._merged_pull_request_sha(app, merged_pull_request)
            except ReleaseError as error:
                app_record["error"] = str(error)
                batch["state"] = "partial-release"
                self.store.save(batch)
                raise ReleaseError(
                    f"batch {batch_id}: partial release: {error}"
                ) from error

            app_record.update(
                {
                    "release_merge": "merged",
                    "release_merge_sha": merge_sha,
                    "state": "release-merged",
                    "status": "released",
                    "error": None,
                    "result": "Release Please pull request merged",
                }
            )
            self.store.save(batch)

        builds_unverified = False
        for app_key in included:
            app = self.inventory.apps[app_key]
            app_record = batch["apps"][app_key]
            try:
                verified = self._verify_release(batch, app, app_record)
            except ReleaseError as error:
                app_record.update(
                    {
                        "state": "release-failed",
                        "status": "failed",
                        "error": str(error),
                    }
                )
                batch["state"] = "release-failed"
                self.store.save(batch)
                raise
            builds_unverified = builds_unverified or not verified
            self.store.save(batch)

        batch["state"] = (
            "released-builds-unverified" if builds_unverified else "released"
        )
        self.store.save(batch)
        return batch

    def status(self, batch_id: str) -> dict[str, Any]:
        """Load persisted batch status without querying or changing remote state."""
        return self.store.load(batch_id)

    def _current_release_snapshot(
        self, app: AppConfig, app_record: dict[str, Any]
    ) -> dict[str, Any]:
        pull_request = self._read_release_pull_request(
            app, str(app_record["release_pr_number"])
        )
        head_sha = pull_request.get("headRefOid")
        version = None
        if isinstance(head_sha, str) and _is_sha(head_sha):
            version = self._manifest_version_at_head(app, head_sha)
        return {
            "repository": app.repository,
            "release_pr_number": pull_request.get("number"),
            "release_pr_url": pull_request.get("url"),
            "release_pr_state": pull_request.get("state"),
            "release_pr_base": pull_request.get("baseRefName"),
            "release_pr_labels": pull_request.get("labels", []),
            "release_pr_checks": pull_request.get("statusCheckRollup", []),
            "release_pr_head_sha": head_sha,
            "version": version,
        }

    def _read_release_pull_request(
        self, app: AppConfig, number: str
    ) -> dict[str, Any]:
        response = self._load_json(
            self._run(
                [
                    "gh",
                    "pr",
                    "view",
                    number,
                    "--repo",
                    app.repository,
                    "--json",
                    "number,url,state,headRefOid,baseRefName,labels,statusCheckRollup,mergeCommit",
                ],
                repository=app.repository,
            ).stdout,
            repository=app.repository,
            subject="Release Please pull request response",
        )
        if not isinstance(response, dict):
            raise ReleaseError(
                f"repository {app.repository}: invalid Release Please pull request response"
            )
        return response

    @staticmethod
    def _approval_snapshot_matches(
        app: AppConfig,
        app_record: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> bool:
        labels = snapshot["release_pr_labels"]
        label_names = {
            label.get("name") for label in labels if isinstance(label, dict)
        } if isinstance(labels, list) else set()
        checks = snapshot["release_pr_checks"]
        return (
            app_record.get("repository") == app.repository
            and snapshot["release_pr_number"] == app_record.get("release_pr_number")
            and snapshot["release_pr_state"] == "OPEN"
            and snapshot["release_pr_base"] == app.release_branch
            and app.release_label in label_names
            and snapshot["version"] == app_record.get("version")
            and snapshot["release_pr_head_sha"]
            == app_record.get("release_pr_head_sha")
            and checks == app_record.get("release_pr_checks")
            and _release_checks_pass(checks)
        )

    @staticmethod
    def _replace_approval_snapshot(
        app_record: dict[str, Any], snapshot: dict[str, Any]
    ) -> None:
        app_record.update(snapshot)
        app_record.update(
            {
                "state": "awaiting-approval",
                "status": "prepared",
                "result": "approval snapshot changed",
            }
        )

    def _confirm_recorded_release_merge(
        self,
        app: AppConfig,
        app_record: dict[str, Any],
        *,
        require_recorded_sha: bool = True,
    ) -> str:
        pull_request = self._read_release_pull_request(
            app, str(app_record["release_pr_number"])
        )
        merge_sha = self._merged_pull_request_sha(app, pull_request)
        if require_recorded_sha and merge_sha != app_record.get("release_merge_sha"):
            raise ReleaseError(
                f"repository {app.repository}: recorded release merge changed"
            )
        return merge_sha

    @staticmethod
    def _merged_pull_request_sha(
        app: AppConfig, pull_request: dict[str, Any]
    ) -> str:
        merge_commit = pull_request.get("mergeCommit")
        merge_sha = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
        if pull_request.get("state") != "MERGED" or not isinstance(
            merge_sha, str
        ) or not _is_sha(merge_sha):
            raise ReleaseError(
                f"repository {app.repository}: Release Please merge was not confirmed"
            )
        return merge_sha

    def _record_release_failure(
        self,
        batch: dict[str, Any],
        app_record: dict[str, Any],
        error: str,
    ) -> None:
        app_record.update(
            {
                "release_merge": "failed",
                "state": "release-failed",
                "status": "failed",
                "error": error,
            }
        )
        partial = any(
            record.get("release_merge") == "merged"
            for record in batch["apps"].values()
        )
        batch["state"] = "partial-release" if partial else "release-failed"
        self.store.save(batch)
        description = "partial release" if partial else "release failed"
        raise ReleaseError(f"batch {batch['batch_id']}: {description}: {error}")

    def _verify_release(
        self,
        batch: dict[str, Any],
        app: AppConfig,
        app_record: dict[str, Any],
    ) -> bool:
        version = app_record["version"]
        tag = f"v{version}"
        merge_sha = app_record["release_merge_sha"]
        tag_commit_sha = self._resolve_tag_commit(app, tag)
        if tag_commit_sha != merge_sha:
            raise ReleaseError(
                f"repository {app.repository}: tag does not match release merge"
            )

        release_result = self.runner.run(
            [
                "gh",
                "release",
                "view",
                tag,
                "--repo",
                app.repository,
                "--json",
                "tagName,url",
            ],
            mutates=False,
        )
        if release_result.returncode != 0:
            raise ReleaseError(
                f"repository {app.repository}: matching GitHub release is unavailable"
            )
        release = self._load_json(
            release_result.stdout,
            repository=app.repository,
            subject="GitHub release response",
        )
        if (
            not isinstance(release, dict)
            or release.get("tagName") != tag
            or not isinstance(release.get("url"), str)
        ):
            raise ReleaseError(
                f"repository {app.repository}: invalid matching GitHub release"
            )

        app_record.update(
            {
                "tag": tag,
                "tag_commit_sha": tag_commit_sha,
                "tag_url": release["url"],
                "commit_url": f"https://github.com/{app.repository}/commit/{tag_commit_sha}",
            }
        )
        self.store.save(batch)
        checks = self._wait_for_codemagic_checks(app, tag_commit_sha)
        if checks is None:
            app_record.update(
                {
                    "build_verification": "unverified",
                    "codemagic_checks": {},
                    "state": "released-builds-unverified",
                    "status": "released",
                    "result": "released; CodeMagic build startup unverified",
                }
            )
            return False

        app_record.update(
            {
                "build_verification": "verified",
                "codemagic_checks": checks,
                "state": "released",
                "status": "released",
                "error": None,
                "result": "released; CodeMagic build checks found",
            }
        )
        return True

    def _resolve_tag_commit(self, app: AppConfig, tag: str) -> str:
        reference = self._load_json(
            self._run(
                ["gh", "api", f"repos/{app.repository}/git/ref/tags/{tag}"],
                repository=app.repository,
            ).stdout,
            repository=app.repository,
            subject="tag reference response",
        )
        if not isinstance(reference, dict) or not isinstance(
            reference.get("object"), dict
        ):
            raise ReleaseError(f"repository {app.repository}: invalid tag reference")
        target = reference["object"]
        seen = set()
        while target.get("type") == "tag":
            tag_sha = target.get("sha")
            if not isinstance(tag_sha, str) or not _is_sha(tag_sha) or tag_sha in seen:
                raise ReleaseError(f"repository {app.repository}: invalid annotated tag")
            seen.add(tag_sha)
            tag_object = self._load_json(
                self._run(
                    ["gh", "api", f"repos/{app.repository}/git/tags/{tag_sha}"],
                    repository=app.repository,
                ).stdout,
                repository=app.repository,
                subject="annotated tag response",
            )
            if not isinstance(tag_object, dict) or not isinstance(
                tag_object.get("object"), dict
            ):
                raise ReleaseError(
                    f"repository {app.repository}: invalid annotated tag"
                )
            target = tag_object["object"]
        commit_sha = target.get("sha")
        if target.get("type") != "commit" or not isinstance(
            commit_sha, str
        ) or not _is_sha(commit_sha):
            raise ReleaseError(f"repository {app.repository}: tag does not resolve to a commit")
        return commit_sha

    def _wait_for_codemagic_checks(
        self, app: AppConfig, commit_sha: str
    ) -> dict[str, dict[str, Any]] | None:
        deadline = self.clock.monotonic() + self.poll_policy.timeout_seconds
        command = [
            "gh",
            "api",
            f"repos/{app.repository}/commits/{commit_sha}/check-runs",
        ]
        expected = set(self.inventory.codemagic_checks)
        while True:
            response = self._load_json(
                self._run(command, repository=app.repository).stdout,
                repository=app.repository,
                subject="commit check run response",
            )
            if not isinstance(response, dict) or not isinstance(
                response.get("check_runs"), list
            ):
                raise ReleaseError(
                    f"repository {app.repository}: invalid commit check run response"
                )
            found = {}
            for check in response["check_runs"]:
                if not isinstance(check, dict):
                    raise ReleaseError(
                        f"repository {app.repository}: invalid commit check run response"
                    )
                check_app = check.get("app")
                name = check.get("name")
                if (
                    not isinstance(check_app, dict)
                    or check_app.get("name") != "Codemagic CI/CD"
                    or name not in expected
                ):
                    continue
                status = check.get("status")
                conclusion = check.get("conclusion")
                details_url = check.get("details_url")
                if (
                    not isinstance(status, str)
                    or conclusion is not None
                    and not isinstance(conclusion, str)
                    or not isinstance(details_url, str)
                ):
                    raise ReleaseError(
                        f"repository {app.repository}: invalid CodeMagic check run"
                    )
                found[name] = {
                    "status": status,
                    "conclusion": conclusion,
                    "details_url": details_url,
                }
            if set(found) == expected:
                return found
            if self.clock.monotonic() >= deadline:
                return None
            self.clock.sleep(self.poll_policy.interval_seconds)

    def _command_error(
        self,
        repository: str,
        args: Sequence[str],
        result: CommandResult,
    ) -> str:
        command = self._redact(shlex.join(args))
        stderr = self._redact(result.stderr.strip()) or "no stderr"
        return (
            f"repository {repository}: command `{command}` failed with exit code "
            f"{result.returncode}: {stderr}"
        )

    def _wait_for_release_workflow(self, app: AppConfig, release_sha: str) -> None:
        deadline = self.clock.monotonic() + self.poll_policy.timeout_seconds
        command = [
            "gh",
            "run",
            "list",
            "--repo",
            app.repository,
            "--commit",
            release_sha,
            "--workflow",
            "release-please",
            "--limit",
            "1",
            "--json",
            "databaseId,status,conclusion,headSha,url",
        ]
        while True:
            runs = self._load_json(
                self._run(command, repository=app.repository).stdout,
                repository=app.repository,
                subject="Release Please workflow response",
            )
            if not isinstance(runs, list) or len(runs) > 1 or not all(
                isinstance(run, dict) for run in runs
            ):
                raise ReleaseError(
                    f"repository {app.repository}: invalid Release Please workflow response"
                )
            if runs:
                run = runs[0]
                if run.get("headSha") != release_sha:
                    raise ReleaseError(
                        f"repository {app.repository}: unexpected Release Please workflow commit"
                    )
                if run.get("status") == "completed":
                    if run.get("conclusion") != "success":
                        raise ReleaseError(
                            f"repository {app.repository}: Release Please workflow failed"
                        )
                    return
            if self.clock.monotonic() >= deadline:
                raise ReleaseError(
                    f"repository {app.repository}: timed out waiting for Release Please workflow"
                )
            self.clock.sleep(self.poll_policy.interval_seconds)

    def _list_release_pull_requests(self, app: AppConfig) -> list[dict[str, Any]]:
        pull_requests = self._load_json(
            self._run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    app.repository,
                    "--state",
                    "open",
                    "--base",
                    app.release_branch,
                    "--label",
                    app.release_label,
                    "--json",
                    "number,url,headRefOid,baseRefName,labels,statusCheckRollup",
                ],
                repository=app.repository,
            ).stdout,
            repository=app.repository,
            subject="Release Please pull request response",
        )
        if not isinstance(pull_requests, list) or not all(
            isinstance(item, dict) for item in pull_requests
        ):
            raise ReleaseError(
                f"repository {app.repository}: invalid Release Please pull request response"
            )
        if len(pull_requests) > 1:
            raise ReleaseError(
                f"repository {app.repository}: duplicate Release Please pull requests"
            )
        if pull_requests:
            pull_request = pull_requests[0]
            labels = pull_request.get("labels")
            if (
                not isinstance(pull_request.get("number"), int)
                or not isinstance(pull_request.get("url"), str)
                or pull_request.get("baseRefName") != app.release_branch
                or not isinstance(labels, list)
                or app.release_label
                not in {
                    label.get("name")
                    for label in labels
                    if isinstance(label, dict)
                }
                or not isinstance(pull_request.get("statusCheckRollup", []), list)
            ):
                raise ReleaseError(
                    f"repository {app.repository}: unexpected Release Please pull request"
                )
        return pull_requests

    def _manifest_version_at_head(self, app: AppConfig, head_sha: str) -> str:
        response = self._load_json(
            self._run(
                [
                    "gh",
                    "api",
                    f"repos/{app.repository}/contents/{app.release_manifest}?ref={head_sha}",
                ],
                repository=app.repository,
            ).stdout,
            repository=app.repository,
            subject="release manifest response",
        )
        if (
            not isinstance(response, dict)
            or response.get("type") != "file"
            or response.get("encoding") != "base64"
            or not isinstance(response.get("content"), str)
        ):
            raise ReleaseError(f"repository {app.repository}: invalid release manifest response")
        try:
            encoded = "".join(response["content"].split())
            content = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as error:
            raise ReleaseError(
                f"repository {app.repository}: invalid release manifest content"
            ) from error
        manifest = self._load_json(
            content,
            repository=app.repository,
            subject="release manifest content",
        )
        if not isinstance(manifest, dict) or not isinstance(manifest.get("."), str):
            raise ReleaseError(f"repository {app.repository}: missing release manifest version")
        return manifest["."]

    @staticmethod
    def _initialize_preparation_record(app_record: dict[str, Any]) -> None:
        app_record.setdefault("worktree", None)
        app_record.setdefault("submodule_before", app_record["packages_pointer_sha"])
        app_record.setdefault("submodule_after", app_record["packages_sha"])
        app_record.setdefault("dev_push", "pending")
        app_record.setdefault("preparation_pr", app_record.get("dev_to_release_pr"))
        app_record.setdefault("release_sha", app_record["release_sha"])
        app_record.setdefault("status", "pending")
        app_record.setdefault("error", None)
        app_record.setdefault("planned_commands", [])

    def _completed_preparation_is_current(
        self,
        app: AppConfig,
        app_record: dict[str, Any],
    ) -> bool:
        preparation_pr = app_record.get("preparation_pr") or {}
        if preparation_pr.get("status") != "merged" or not preparation_pr.get("number"):
            return False

        preparation_was_complete = app_record.get("status") == "prepared"

        pull_request = self._load_json(
            self._run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(preparation_pr["number"]),
                    "--repo",
                    app.repository,
                    "--json",
                    "number,url,state,mergedAt,mergeable,mergeStateStatus,headRefName,baseRefName",
                ],
                repository=app.repository,
            ).stdout,
            repository=app.repository,
            subject="preparation pull request response",
        )
        if not isinstance(pull_request, dict):
            raise ReleaseError(
                f"repository {app.repository}: invalid preparation pull request response"
            )
        if (
            pull_request.get("state") != "MERGED"
            or pull_request.get("headRefName") != app.dev_branch
            or pull_request.get("baseRefName") != app.release_branch
        ):
            raise ReleaseError(
                f"repository {app.repository}: recorded preparation merge is no longer current"
            )
        release_sha = self._remote_branch_sha(
            app,
            app.release_branch,
            Path(app_record["repository_path"]),
        )
        if preparation_was_complete and release_sha != app_record.get("release_sha"):
            raise ReleaseError(
                f"repository {app.repository}: release changed after preparation merge"
            )
        app_record["release_sha"] = release_sha
        app_record["error"] = None
        app_record["status"] = "prepared"
        app_record["state"] = "prepare-complete"
        return True

    def _prepare_development_pointer(
        self,
        batch: dict[str, Any],
        app: AppConfig,
        app_record: dict[str, Any],
        *,
        dry_run: bool,
    ) -> None:
        repository_path = Path(app_record["repository_path"])
        current_dev = self._remote_branch_sha(app, app.dev_branch, repository_path)
        if app_record["dev_push"] == "pushed":
            if current_dev != app_record["dev_sha"]:
                raise ReleaseError(
                    f"repository {app.repository}: origin/dev changed after recorded push"
                )
            return
        if current_dev != app_record["dev_sha"]:
            raise ReleaseError(f"repository {app.repository}: origin/dev changed after preflight")

        if app_record["submodule_before"] == app_record["submodule_after"]:
            app_record["dev_push"] = "unchanged"
            return

        worktree_path = (
            repository_path
            / ".claude"
            / "worktrees"
            / f"{batch['batch_id']}-{app.key}"
        )
        branch = f"deploy-mobile-apps/{batch['batch_id']}/{app.key}"
        app_record["worktree"] = str(worktree_path)
        mutation_commands = [
            ["git", "worktree", "add", "-b", branch, str(worktree_path), app_record["dev_sha"]],
            ["git", "submodule", "update", "--init", "--", app.submodule_path],
            [
                "git",
                "-C",
                app.submodule_path,
                "checkout",
                "--detach",
                app_record["submodule_after"],
            ],
            ["git", "add", "--", app.submodule_path],
            ["git", "commit", "-m", "chore: update shared packages"],
            ["git", "push", "origin", f"HEAD:{app.dev_branch}"],
        ]
        self._run(
            ["git", "check-ignore", "-q", ".claude/worktrees"],
            cwd=repository_path,
            repository=app.repository,
        )
        if dry_run:
            app_record["planned_commands"].extend(mutation_commands)
            app_record["dev_push"] = "planned"
            return

        self._run(
            mutation_commands[0],
            cwd=repository_path,
            repository=app.repository,
            mutates=True,
        )
        for command in mutation_commands[1:4]:
            self._run(
                command,
                cwd=worktree_path,
                repository=app.repository,
                mutates=True,
            )

        staged_paths = self._run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=worktree_path,
            repository=app.repository,
        ).stdout.splitlines()
        if staged_paths not in ([], [app.submodule_path]):
            raise ReleaseError(
                f"repository {app.repository}: preparation staged paths other than {app.submodule_path}"
            )
        if staged_paths:
            self._run(
                mutation_commands[4],
                cwd=worktree_path,
                repository=app.repository,
                mutates=True,
            )
            prepared_dev_sha = self._run(
                ["git", "rev-parse", "HEAD^{commit}"],
                cwd=worktree_path,
                repository=app.repository,
            ).stdout.strip()
            if not _is_sha(prepared_dev_sha):
                raise ReleaseError(
                    f"repository {app.repository}: invalid prepared development commit"
                )
            current_dev = self._remote_branch_sha(app, app.dev_branch, repository_path)
            if current_dev != app_record["dev_sha"]:
                raise ReleaseError(f"repository {app.repository}: origin/dev changed before push")
            push = self.runner.run(mutation_commands[5], cwd=worktree_path, mutates=True)
            if push.returncode != 0:
                stderr = self._redact(push.stderr.strip()) or "no stderr"
                raise ReleaseError(f"repository {app.repository}: dev push rejected: {stderr}")
            app_record["dev_sha"] = prepared_dev_sha
            app_record["dev_push"] = "pushed"
            app_record["state"] = "dev-prepared"
            self.store.save(batch)
        else:
            app_record["dev_push"] = "unchanged"

        worktree_status = self._run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            repository=app.repository,
        ).stdout
        if not worktree_status.strip():
            self._run(
                ["git", "worktree", "remove", str(worktree_path)],
                cwd=repository_path,
                repository=app.repository,
                mutates=True,
            )
            app_record["worktree"] = None

    def _promote_development(
        self,
        batch: dict[str, Any],
        app: AppConfig,
        app_record: dict[str, Any],
        *,
        dry_run: bool,
    ) -> None:
        repository_path = Path(app_record["repository_path"])
        dev_ref = f"refs/remotes/origin/{app.dev_branch}"
        release_ref = f"refs/remotes/origin/{app.release_branch}"
        self._run(
            [
                "git",
                "fetch",
                "origin",
                _remote_tracking_refspec(app.dev_branch),
                _remote_tracking_refspec(app.release_branch),
            ],
            cwd=repository_path,
            repository=app.repository,
        )
        branch_shas = self._run(
            ["git", "rev-parse", f"{dev_ref}^{{commit}}", f"{release_ref}^{{commit}}"],
            cwd=repository_path,
            repository=app.repository,
        ).stdout.splitlines()
        if len(branch_shas) != 2 or not all(_is_sha(value) for value in branch_shas):
            raise ReleaseError(
                f"repository {app.repository}: invalid fetched development or release branch"
            )
        _, release_sha = branch_shas
        ancestry_command = ["git", "merge-base", "--is-ancestor", dev_ref, release_ref]
        ancestry = self.runner.run(ancestry_command, cwd=repository_path, mutates=False)
        if ancestry.returncode not in {0, 1}:
            command = self._redact(shlex.join(ancestry_command))
            stderr = self._redact(ancestry.stderr.strip()) or "no stderr"
            raise ReleaseError(
                f"repository {app.repository}: command `{command}` failed with exit code "
                f"{ancestry.returncode}: {stderr}"
            )
        if ancestry.returncode == 0:
            app_record["status"] = "skipped"
            app_record["state"] = "prepare-complete"
            app_record["release_sha"] = release_sha
            app_record["skip_reason"] = "no dev to release changes"
            return

        pull_requests = self._list_preparation_pull_requests(app)
        if len(pull_requests) > 1:
            raise ReleaseError(
                f"repository {app.repository}: duplicate preparation pull requests"
            )

        if dry_run and not pull_requests:
            create = self._preparation_pr_create_command(app)
            app_record["planned_commands"].append(create)
            number = "<preparation-pr-number>"
            app_record["planned_commands"].append(
                ["gh", "pr", "merge", number, "--repo", app.repository, "--merge"]
            )
            app_record["status"] = "planned"
            return

        if pull_requests:
            pull_request = self._validate_exact_preparation_pr(app, pull_requests[0])
            pull_request["status"] = "open"
        else:
            created = self._run(
                self._preparation_pr_create_command(app),
                repository=app.repository,
                mutates=True,
            ).stdout.strip()
            match = re.fullmatch(r"https://github\.com/[^/]+/[^/]+/pull/(\d+)", created)
            if match is None:
                raise ReleaseError(
                    f"repository {app.repository}: invalid preparation pull request URL"
                )
            pull_request = {
                "number": int(match.group(1)),
                "url": created,
                "headRefName": app.dev_branch,
                "baseRefName": app.release_branch,
                "status": "open",
            }
            app_record["preparation_pr"] = pull_request
            self.store.save(batch)

        app_record["preparation_pr"] = pull_request
        number = str(pull_request["number"])
        checks_command = [
            "gh",
            "pr",
            "checks",
            number,
            "--repo",
            app.repository,
            "--json",
            "name,state,bucket",
        ]
        checks_result = self.runner.run(checks_command, mutates=False)
        no_checks_reported = "no checks" in checks_result.stderr.lower()
        if no_checks_reported and not checks_result.stdout.strip():
            checks = []
        else:
            checks = self._load_json(
                checks_result.stdout,
                repository=app.repository,
                subject="preparation check response",
            )
        if not isinstance(checks, list):
            raise ReleaseError(f"repository {app.repository}: invalid preparation check response")
        if checks and all(
            isinstance(check, dict)
            and check.get("bucket") in {"pass", "pending", "skipping"}
            for check in checks
        ) and any(check.get("bucket") == "pending" for check in checks):
            checks_command.extend(["--watch", "--fail-fast"])
            checks_result = self.runner.run(checks_command, mutates=False)
            checks = self._load_json(
                checks_result.stdout,
                repository=app.repository,
                subject="preparation check response",
            )
            no_checks_reported = False
            if not isinstance(checks, list):
                raise ReleaseError(
                    f"repository {app.repository}: invalid preparation check response"
                )
        if any(
            not isinstance(check, dict)
            or check.get("bucket") not in {"pass", "skipping"}
            for check in checks
        ):
            raise ReleaseError(f"repository {app.repository}: preparation checks failed")
        if checks_result.returncode != 0 and not (not checks and no_checks_reported):
            command = self._redact(shlex.join(checks_command))
            stderr = self._redact(checks_result.stderr.strip()) or "no stderr"
            raise ReleaseError(
                f"repository {app.repository}: command `{command}` failed with exit code "
                f"{checks_result.returncode}: {stderr}"
            )
        pull_request_state = self._load_json(
            self._run(
                [
                    "gh",
                    "pr",
                    "view",
                    number,
                    "--repo",
                    app.repository,
                    "--json",
                    "number,url,state,mergedAt,mergeable,mergeStateStatus,headRefName,baseRefName",
                ],
                repository=app.repository,
            ).stdout,
            repository=app.repository,
            subject="preparation pull request response",
        )
        if not isinstance(pull_request_state, dict):
            raise ReleaseError(
                f"repository {app.repository}: invalid preparation pull request response"
            )
        self._validate_exact_preparation_pr(app, pull_request_state)
        if (
            pull_request_state.get("state") != "OPEN"
            or pull_request_state.get("mergeable") != "MERGEABLE"
            or pull_request_state.get("mergeStateStatus") == "DIRTY"
        ):
            raise ReleaseError(
                f"repository {app.repository}: preparation pull request is not mergeable"
            )

        if dry_run:
            app_record["planned_commands"].append(
                ["gh", "pr", "merge", number, "--repo", app.repository, "--merge"]
            )
            app_record["status"] = "planned"
            return

        self._run(
            ["gh", "pr", "merge", number, "--repo", app.repository, "--merge"],
            repository=app.repository,
            mutates=True,
        )
        pull_request.update({"status": "merged", "checks": checks})
        app_record["preparation_pr"] = pull_request
        self.store.save(batch)

        app_record["release_sha"] = self._remote_branch_sha(
            app, app.release_branch, repository_path
        )
        app_record["status"] = "prepared"
        app_record["state"] = "prepare-complete"
        app_record["error"] = None

    def _list_preparation_pull_requests(self, app: AppConfig) -> list[dict[str, Any]]:
        response = self._load_json(
            self._run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    app.repository,
                    "--state",
                    "open",
                    "--base",
                    app.release_branch,
                    "--head",
                    app.dev_branch,
                    "--json",
                    "number,url,headRefName,baseRefName",
                ],
                repository=app.repository,
            ).stdout,
            repository=app.repository,
            subject="preparation pull request response",
        )
        if not isinstance(response, list) or not all(isinstance(item, dict) for item in response):
            raise ReleaseError(
                f"repository {app.repository}: invalid preparation pull request response"
            )
        return response

    @staticmethod
    def _preparation_pr_create_command(app: AppConfig) -> list[str]:
        return [
            "gh",
            "pr",
            "create",
            "--repo",
            app.repository,
            "--base",
            app.release_branch,
            "--head",
            app.dev_branch,
            "--title",
            "chore: promote dev to release",
            "--body",
            "Promote the prepared development branch to release.",
        ]

    @staticmethod
    def _validate_exact_preparation_pr(
        app: AppConfig, pull_request: dict[str, Any]
    ) -> dict[str, Any]:
        if (
            not isinstance(pull_request.get("number"), int)
            or not isinstance(pull_request.get("url"), str)
            or pull_request.get("headRefName") != app.dev_branch
            or pull_request.get("baseRefName") != app.release_branch
        ):
            raise ReleaseError(
                f"repository {app.repository}: unexpected preparation pull request"
            )
        return dict(pull_request)

    def _remote_branch_sha(
        self, app: AppConfig, branch: str, repository_path: Path
    ) -> str:
        ref = f"refs/heads/{branch}"
        output = self._run(
            ["git", "ls-remote", "--exit-code", "origin", ref],
            cwd=repository_path,
            repository=app.repository,
        ).stdout.splitlines()
        if len(output) != 1:
            raise ReleaseError(f"repository {app.repository}: ambiguous remote branch {branch}")
        fields = output[0].split()
        if len(fields) != 2 or not _is_sha(fields[0]) or fields[1] != ref:
            raise ReleaseError(f"repository {app.repository}: invalid remote branch {branch}")
        return fields[0]

    def _validate_selection(self, app_keys: list[str]) -> None:
        if not app_keys:
            raise ReleaseError("preflight requires at least one application")
        if len(set(app_keys)) != len(app_keys):
            raise ReleaseError("preflight application selection contains duplicates")
        unknown = [app_key for app_key in app_keys if app_key not in self.inventory.apps]
        if unknown:
            raise ReleaseError(f"unknown application: {', '.join(unknown)}")

    def _workspace_root(self) -> Path:
        configured = self.environ.get(
            self.inventory.workspace_root_env,
            self.inventory.default_workspace_root,
        )
        root = Path(configured).expanduser().resolve()
        if not root.is_dir():
            raise ReleaseError("configured mobile workspace root is not a directory")
        return root

    def _discover_repositories(
        self, workspace_root: Path, app_keys: Sequence[str]
    ) -> dict[str, Path]:
        wanted = {
            _normalize_repository(self.inventory.apps[app_key].repository): app_key
            for app_key in app_keys
        }
        matches = {app_key: [] for app_key in app_keys}

        for candidate in sorted(workspace_root.iterdir()):
            if not candidate.is_dir() or not (candidate / ".git").exists():
                continue
            result = self.runner.run(
                ["git", "remote", "get-url", "origin"],
                cwd=candidate,
                mutates=False,
            )
            if result.returncode != 0:
                continue
            app_key = wanted.get(_normalize_repository(result.stdout))
            if app_key is not None:
                matches[app_key].append(candidate)

        discovered = {}
        for app_key, paths in matches.items():
            repository = self.inventory.apps[app_key].repository
            if not paths:
                raise ReleaseError(f"repository {repository}: no matching local repository")
            if len(paths) > 1:
                raise ReleaseError(f"repository {repository}: ambiguous repository match")
            discovered[app_key] = paths[0]
        return discovered

    def _preflight_app(self, app: AppConfig, repository_path: Path) -> dict[str, Any]:
        self._run(
            [
                "git",
                "fetch",
                "origin",
                _remote_tracking_refspec(app.dev_branch),
                _remote_tracking_refspec(app.release_branch),
            ],
            cwd=repository_path,
            repository=app.repository,
        )
        branches = self._run(
            [
                "git",
                "rev-parse",
                f"refs/remotes/origin/{app.dev_branch}^{{commit}}",
                f"refs/remotes/origin/{app.release_branch}^{{commit}}",
            ],
            cwd=repository_path,
            repository=app.repository,
        ).stdout.splitlines()
        if len(branches) != 2 or not all(_is_sha(value) for value in branches):
            raise ReleaseError(f"repository {app.repository}: missing configured branches")
        dev_sha, release_sha = branches

        submodule = self._run(
            ["git", "ls-tree", dev_sha, "--", app.submodule_path],
            cwd=repository_path,
            repository=app.repository,
        ).stdout.strip()
        match = re.fullmatch(
            rf"160000 commit (\S{{40}})\t{re.escape(app.submodule_path)}",
            submodule,
        )
        if match is None:
            raise ReleaseError(
                f"repository {app.repository}: missing configured submodule {app.submodule_path}"
            )

        submodule_path = repository_path / app.submodule_path
        if not submodule_path.is_dir():
            raise ReleaseError(
                f"repository {app.repository}: submodule checkout {app.submodule_path} is unavailable"
            )
        submodule_origins = [
            value.strip()
            for value in self._run(
                ["git", "remote", "get-url", "--all", "origin"],
                cwd=submodule_path,
                repository=app.repository,
            ).stdout.splitlines()
            if value.strip()
        ]
        if len(submodule_origins) != 1:
            raise ReleaseError(
                f"repository {app.repository}: ambiguous packages origin configuration"
            )
        if _normalize_repository(submodule_origins[0]) != _normalize_repository(
            PACKAGES_REPOSITORY
        ):
            raise ReleaseError(
                f"repository {app.repository}: packages origin does not match "
                f"{PACKAGES_REPOSITORY}"
            )
        self._run(
            ["git", "fetch", "origin", _remote_tracking_refspec(app.submodule_branch)],
            cwd=submodule_path,
            repository=app.repository,
        )
        packages_sha = self._run(
            [
                "git",
                "rev-parse",
                "--verify",
                f"refs/remotes/origin/{app.submodule_branch}^{{commit}}",
            ],
            cwd=submodule_path,
            repository=app.repository,
        ).stdout.strip()
        if not _is_sha(packages_sha):
            raise ReleaseError(
                f"repository {app.repository}: missing configured submodule branch {app.submodule_branch}"
            )

        self._run(
            [
                "git",
                "show",
                f"{release_sha}:{app.release_manifest}",
                f"{release_sha}:release-please-config.json",
            ],
            cwd=repository_path,
            repository=app.repository,
        )
        codemagic = self._run(
            ["git", "show", f"{release_sha}:codemagic.yaml"],
            cwd=repository_path,
            repository=app.repository,
        ).stdout
        workflow_names = _codemagic_workflow_names(codemagic)
        missing_workflows = [
            name for name in self.inventory.codemagic_checks if name not in workflow_names
        ]
        if missing_workflows:
            raise ReleaseError(
                f"repository {app.repository}: missing workflow names: {', '.join(missing_workflows)}"
            )

        pull_requests = self._load_json(
            self._run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    app.repository,
                    "--state",
                    "open",
                    "--base",
                    app.release_branch,
                    "--head",
                    app.dev_branch,
                    "--json",
                    "number,url,headRefName,baseRefName",
                ],
                repository=app.repository,
            ).stdout,
            repository=app.repository,
            subject="pull request response",
        )
        if not isinstance(pull_requests, list):
            raise ReleaseError(f"repository {app.repository}: invalid pull request response")
        if len(pull_requests) > 1:
            raise ReleaseError(
                f"repository {app.repository}: duplicate dev to release pull requests"
            )

        return {
            "state": "preflight-complete",
            "repository": app.repository,
            "repository_path": str(repository_path),
            "dev_sha": dev_sha,
            "release_sha": release_sha,
            "packages_pointer_sha": match.group(1),
            "packages_sha": packages_sha,
            "dev_to_release_pr": pull_requests[0] if pull_requests else None,
        }

    def _run(
        self,
        args: Sequence[str],
        *,
        repository: str,
        cwd: Path | None = None,
        mutates: bool = False,
    ) -> CommandResult:
        result = self.runner.run(args, cwd=cwd, mutates=mutates)
        if result.returncode == 0:
            return result
        command = self._redact(shlex.join(args))
        stderr = self._redact(result.stderr.strip()) or "no stderr"
        raise ReleaseError(
            f"repository {repository}: command `{command}` failed with exit code "
            f"{result.returncode}: {stderr}"
        )

    def _redact(self, text: str) -> str:
        values = sorted(
            {value for value in self.environ.values() if value},
            key=len,
            reverse=True,
        )
        if not values:
            return text
        return re.sub("|".join(re.escape(value) for value in values), "<redacted>", text)

    @staticmethod
    def _load_json(value: str, *, repository: str, subject: str) -> Any:
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise ReleaseError(f"repository {repository}: invalid {subject}") from error


def approval_rows(batch: dict[str, Any]) -> list[dict[str, Any]]:
    """Return plain approval data suitable for JSON or human formatting."""
    rows = []
    for app_key in batch["selected_apps"]:
        app = batch["apps"][app_key]
        rows.append(
            {
                "app": app_key,
                "version": app.get("version"),
                "pr_number": app.get("release_pr_number"),
                "url": app.get("release_pr_url"),
                "checks": app.get("release_pr_checks", []),
                "head_sha": app.get("release_pr_head_sha"),
                "submodule_sha": app.get("submodule_sha", app.get("packages_sha")),
                "result": app.get("result", app.get("skip_reason")),
            }
        )
    return rows


def _normalize_repository(value: str) -> str:
    remote = value.strip().rstrip("/")
    if remote.endswith(".git"):
        remote = remote[:-4]
    scp_match = re.fullmatch(r"(?:[^@]+@)?([^:]+):(.+)", remote)
    if scp_match and "://" not in remote:
        host, path = scp_match.groups()
        return f"{host}/{path}".lower()
    if "://" in remote:
        parsed = urlsplit(remote)
        return f"{parsed.hostname or ''}/{parsed.path.lstrip('/')}".lower()
    if remote.count("/") == 1:
        return f"github.com/{remote}".lower()
    return remote.lower()


def _is_sha(value: str) -> bool:
    return re.fullmatch(r"[0-9a-fA-F]{40}", value.strip()) is not None


def _release_checks_pass(checks: Any) -> bool:
    return (
        isinstance(checks, list)
        and bool(checks)
        and all(
            isinstance(check, dict)
            and str(check.get("conclusion", "")).upper()
            in {"SUCCESS", "NEUTRAL", "SKIPPED"}
            for check in checks
        )
    )


def _remote_tracking_refspec(branch: str) -> str:
    return f"+refs/heads/{branch}:refs/remotes/origin/{branch}"


def _codemagic_workflow_names(content: str) -> set[str]:
    lines = content.splitlines()
    workflow_lines = []
    in_workflows = False
    for line in lines:
        stripped = line.strip()
        indentation = len(line) - len(line.lstrip(" "))
        if stripped and not stripped.startswith("#") and indentation == 0:
            if in_workflows:
                break
            in_workflows = stripped == "workflows:"
            continue
        if in_workflows and stripped and not stripped.startswith("#"):
            workflow_lines.append((indentation, stripped))

    indentation_levels = sorted({indentation for indentation, _ in workflow_lines})
    if len(indentation_levels) < 2:
        return set()
    property_indentation = indentation_levels[1]
    names = set()
    for indentation, stripped in workflow_lines:
        if indentation != property_indentation:
            continue
        match = re.fullmatch(r"name:\s*['\"]?(.+?)['\"]?", stripped)
        if match:
            names.add(match.group(1))
    return names
