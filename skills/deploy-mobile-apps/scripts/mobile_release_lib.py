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


_PROMOTED_COMMIT_LIMIT = 20
_CHECKS_NOT_PASSING = "release pull request checks are not passing"
_CARRIED_SUBJECT_LIMIT = 20
# Captures the type and summary while dropping any breaking marker, so a bump
# commit rendered from these can never read as a breaking change.
_CONVENTIONAL_SUBJECT = re.compile(
    r"(?P<type>feat|fix)(?:\([^)]*\))?!?:\s*(?P<summary>.+)"
)

import codemagic

PACKAGES_REPOSITORY = "MarketplaceSoftware/packages"
_LOG_TAIL_LINES = 15
_FAILING_CONCLUSIONS = {"failure", "cancelled", "timed_out"}


class ReleaseError(RuntimeError):
    """Report a release guard failure without exposing process credentials."""

    def __init__(
        self,
        message: str,
        *,
        recovery_batch: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.recovery_batch = recovery_batch


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
    # CodeMagic queues builds, and a queued build registers no check run. Waiting
    # the full timeout cannot outlast a queue, so observation gets its own budget
    # and `status` is the way to watch from there.
    observation_timeout_seconds: float = 60.0


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
    staging_branch: str
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
    staging_checks: tuple[str, ...]
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
        staging_checks=tuple(data["staging_checks"]),
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
        for app_key in selected_apps:
            self._verify_repository_permission(self.inventory.apps[app_key])
        for app_key in selected_apps:
            self._reject_conflicting_deployment_worktrees(
                self.inventory.apps[app_key], repositories[app_key]
            )
        batch = new_batch(selected_apps)

        for app_key in selected_apps:
            app = self.inventory.apps[app_key]
            repository_path = repositories[app_key]
            app_record = self._preflight_app(app, repository_path)
            if app_record["packages_pointer_sha"] != app_record["packages_sha"]:
                self._verify_deployment_worktree_is_ignored(app, repository_path)
            app_record["release_ahead_of_dev"] = not self._release_is_contained_in_dev(
                app, repository_path
            )
            batch["apps"][app_key] = app_record

        batch["state"] = "preflight-complete"
        if not dry_run:
            self.store.save(batch)
        return batch

    def prepare(
        self,
        batch_id: str,
        dry_run: bool = False,
        staging_only: bool = False,
    ) -> dict[str, Any]:
        """Update development pointers and merge each exact promotion PR.

        With [staging_only] the batch stops once staging carries the prepared
        commit, so a TestFlight build can be proven before anything reaches the
        release branch. Resuming the same batch with a plain prepare promotes
        it; the bump is not repeated, because the completed preparation is
        still current.
        """
        batch = self.store.load(batch_id)
        if batch.get("state") not in {
            "preflight-complete",
            "prepare-in-progress",
            "prepare-failed",
            "prepare-complete",
            "staging-complete",
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
                self._fast_forward_staging(batch, app, app_record, dry_run=dry_run)
                if staging_only:
                    if not dry_run:
                        self._verify_staging(batch, app, app_record)
                    continue
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
                error.recovery_batch = batch
                raise

        if dry_run:
            batch["state"] = "dry-run-complete"
            return batch
        if staging_only:
            batch["state"] = "staging-complete"
            self.store.save(batch)
            return batch
        batch["state"] = "prepare-complete"
        self.store.save(batch)
        return self.discover_versions(batch)

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
                        "unreleased_commits": self._promoted_commit_subjects(
                            app, app_record
                        ),
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
                    "release_pr_base": pull_request["baseRefName"],
                    "release_pr_labels": _release_label_names(
                        pull_request["labels"]
                    ),
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

    def release(self, batch_id: str, dry_run: bool = False) -> dict[str, Any]:
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
        checks_blocked = False
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
                    if not dry_run:
                        self.store.save(batch)
                continue

            snapshot = self._current_release_snapshot(app, app_record)
            if self._merged_snapshot_matches_approval(app, app_record, snapshot):
                app_record.update(
                    {
                        "release_merge": "merged",
                        "release_merge_sha": snapshot["release_pr_merge_sha"],
                        "state": "release-merged",
                        "status": "released",
                        "error": None,
                        "result": "Release Please pull request merged",
                    }
                )
                if not dry_run:
                    self.store.save(batch)
                continue
            if not self._approval_snapshot_matches(app, app_record, snapshot):
                self._replace_approval_snapshot(app_record, snapshot)
                approval_changed = True
            elif not _release_checks_allow_merge(snapshot):
                self._replace_approval_snapshot(
                    app_record, snapshot, result=_CHECKS_NOT_PASSING
                )
                checks_blocked = True
            unfinished.append((app, app_record))

        if approval_changed or checks_blocked:
            batch["state"] = "awaiting-approval"
            if not dry_run:
                self.store.save(batch)
            raise ReleaseError(
                f"batch {batch_id}: approval snapshot changed; review and approve again"
                if approval_changed
                else f"batch {batch_id}: {_CHECKS_NOT_PASSING}",
                recovery_batch=batch,
            )

        if dry_run:
            batch["planned_release_merges"] = [
                {
                    "repository": app.repository,
                    "pull_request_number": app_record["release_pr_number"],
                    "head_sha": app_record["release_pr_head_sha"],
                    "command": _guarded_merge_command(
                        app.repository,
                        str(app_record["release_pr_number"]),
                        app_record["release_pr_head_sha"],
                    ),
                }
                for app, app_record in unfinished
            ]
            return batch

        for app, app_record in unfinished:
            number = str(app_record["release_pr_number"])
            merge_command = _guarded_merge_command(
                app.repository,
                number,
                app_record["release_pr_head_sha"],
            )
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
            "release_pr_labels": _release_label_names(
                pull_request.get("labels", [])
            ),
            "release_pr_checks": pull_request.get("statusCheckRollup", []),
            "release_pr_mergeable": pull_request.get("mergeable"),
            "release_pr_head_sha": head_sha,
            "release_pr_merge_sha": (
                pull_request["mergeCommit"].get("oid")
                if isinstance(pull_request.get("mergeCommit"), dict)
                else None
            ),
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
                    "number,url,state,headRefOid,baseRefName,labels,statusCheckRollup,mergeable,mergeCommit",
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
        checks = snapshot["release_pr_checks"]
        return (
            app_record.get("repository") == app.repository
            and snapshot["release_pr_number"] == app_record.get("release_pr_number")
            and snapshot["release_pr_state"] == "OPEN"
            and snapshot["release_pr_base"] == app_record.get("release_pr_base")
            and labels == app_record.get("release_pr_labels")
            and snapshot["release_pr_base"] == app.release_branch
            and isinstance(labels, list)
            and app.release_label in labels
            and snapshot["version"] == app_record.get("version")
            and snapshot["release_pr_head_sha"]
            == app_record.get("release_pr_head_sha")
            and checks == app_record.get("release_pr_checks")
        )

    @staticmethod
    def _merged_snapshot_matches_approval(
        app: AppConfig,
        app_record: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> bool:
        merge_sha = snapshot["release_pr_merge_sha"]
        return (
            snapshot["release_pr_state"] == "MERGED"
            and app_record.get("repository") == app.repository
            and snapshot["release_pr_number"] == app_record.get("release_pr_number")
            and snapshot["release_pr_base"] == app_record.get("release_pr_base")
            and snapshot["release_pr_base"] == app.release_branch
            and snapshot["release_pr_head_sha"]
            == app_record.get("release_pr_head_sha")
            and snapshot["version"] == app_record.get("version")
            and isinstance(merge_sha, str)
            and _is_sha(merge_sha)
        )

    @staticmethod
    def _replace_approval_snapshot(
        app_record: dict[str, Any],
        snapshot: dict[str, Any],
        result: str = "approval snapshot changed",
    ) -> None:
        app_record.update(snapshot)
        app_record.update(
            {
                "state": "awaiting-approval",
                "status": "prepared",
                "result": result,
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
        checks, missing_checks = self._wait_for_codemagic_checks(app, tag_commit_sha)
        if not checks:
            app_record.update(
                {
                    "build_verification": "unverified",
                    "codemagic_checks": {},
                    "codemagic_missing_checks": missing_checks,
                    "state": "released-builds-unverified",
                    "status": "released",
                    "error": None,
                    "result": "released; no CodeMagic build observed yet",
                }
            )
            return False

        app_record.update(
            {
                "build_verification": "started" if missing_checks else "verified",
                "codemagic_checks": checks,
                "codemagic_missing_checks": missing_checks,
                "state": "released",
                "status": "released",
                "error": None,
                "result": "released; CodeMagic build checks found",
            }
        )
        return True

    def _verify_staging(
        self,
        batch: dict[str, Any],
        app: AppConfig,
        app_record: dict[str, Any],
    ) -> bool:
        """Observe the staging builds the fast-forward push started."""
        staging_sha = app_record.get("staging_sha")
        if not staging_sha:
            return False
        checks, missing = self._wait_for_codemagic_checks(
            app, staging_sha, self.inventory.staging_checks
        )
        failed = sorted(
            name
            for name, check in checks.items()
            if check.get("conclusion") in _FAILING_CONCLUSIONS
        )
        app_record.update(
            {
                "staging_checks": checks,
                "staging_missing_checks": missing,
                "staging_failed_checks": failed,
                "staging_diagnosis": self._diagnose_staging(
                    app, staging_sha, checks, missing
                ),
                "state": "staging-complete",
                "status": "staged",
                "error": None,
                "result": _staging_result(checks, missing, failed),
            }
        )
        self.store.save(batch)
        return bool(checks) and not missing and not failed

    def _diagnose_staging(
        self,
        app: AppConfig,
        staging_sha: str,
        checks: dict[str, dict[str, Any]],
        missing: Sequence[str],
    ) -> dict[str, Any]:
        """Explain each failed or absent staging build, as far as CodeMagic will say."""
        client = self._codemagic_client()
        if client is None:
            return {
                "unavailable": (
                    "no CodeMagic API token; set CODEMAGIC_API_TOKEN or store it in the "
                    "login keychain as CODEMAGIC_API_TOKEN to see why a build failed"
                )
            }

        diagnosis: dict[str, Any] = {}
        for name, check in checks.items():
            if check.get("conclusion") not in _FAILING_CONCLUSIONS:
                continue
            reference = codemagic.build_reference(check.get("details_url"))
            if reference is None:
                continue
            try:
                build = client.build(reference.build_id)
                steps = codemagic.failed_steps(build)
                entry: dict[str, Any] = {
                    "build_status": build.get("status"),
                    "version": build.get("version"),
                    "failed_steps": [step.get("name") for step in steps],
                }
                if steps and steps[-1].get("_id"):
                    entry["log_tail"] = _log_tail(
                        client.step_log(reference.build_id, steps[-1]["_id"])
                    )
                diagnosis[name] = entry
            except codemagic.CodemagicError as error:
                diagnosis[name] = {"error": str(error)}

        if missing:
            diagnosis["missing"] = self._explain_missing_builds(
                client, app, staging_sha, checks, list(missing)
            )
        return diagnosis

    def _explain_missing_builds(
        self,
        client: "codemagic.CodemagicClient",
        app: AppConfig,
        staging_sha: str,
        checks: dict[str, dict[str, Any]],
        missing: list[str],
    ) -> dict[str, Any]:
        """Separate a workflow that never ran from one that ran and left no check run.

        A build cancelled before it starts registers no GitHub check run at all, so
        an absent check is not evidence that the trigger is misconfigured.
        """
        app_id = self._codemagic_app_id(client, app, checks)
        if app_id is None:
            return {"workflows": missing, "note": "no CodeMagic application matched"}
        try:
            builds = client.builds_for_branch(app_id, app.staging_branch)
        except codemagic.CodemagicError as error:
            return {"workflows": missing, "error": str(error)}
        for_commit = [
            {"id": build.get("_id"), "status": build.get("status")}
            for build in builds
            if (build.get("commit") or {}).get("hash") == staging_sha
            or build.get("commit") is None
        ]
        return {
            "workflows": missing,
            "builds_on_branch": for_commit[:5],
            "note": (
                "builds exist but registered no check run; a build cancelled before it "
                "starts never reports one"
                if for_commit
                else "no CodeMagic build exists for this branch; check the push trigger"
            ),
        }

    def _codemagic_app_id(
        self,
        client: "codemagic.CodemagicClient",
        app: AppConfig,
        checks: dict[str, dict[str, Any]],
    ) -> str | None:
        """The CodeMagic application id, from an observed check run if one exists."""
        for check in checks.values():
            reference = codemagic.build_reference(check.get("details_url"))
            if reference is not None:
                return reference.app_id
        return None

    def _codemagic_client(self) -> "codemagic.CodemagicClient | None":
        """The diagnostics client, or None when no token is configured."""
        if not hasattr(self, "_codemagic_cached"):
            token = codemagic.resolve_token(dict(self.environ))
            self._codemagic_cached = (
                codemagic.CodemagicClient(token) if token else None
            )
        return self._codemagic_cached

    def _resolve_tag_commit(self, app: AppConfig, tag: str) -> str:
        command = ["gh", "api", f"repos/{app.repository}/git/ref/tags/{tag}"]
        deadline = self.clock.monotonic() + self.poll_policy.timeout_seconds
        while True:
            # Release Please tags after its pull request merges, so the tag is
            # absent for a moment rather than wrong.
            result = self.runner.run(command)
            if result.returncode == 0:
                break
            if self.clock.monotonic() >= deadline:
                raise ReleaseError(
                    self._command_error(app.repository, command, result)
                )
            self.clock.sleep(self.poll_policy.interval_seconds)
        reference = self._load_json(
            result.stdout,
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
        self,
        app: AppConfig,
        commit_sha: str,
        expected_names: Sequence[str] | None = None,
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        deadline = (
            self.clock.monotonic() + self.poll_policy.observation_timeout_seconds
        )
        command = [
            "gh",
            "api",
            f"repos/{app.repository}/commits/{commit_sha}/check-runs",
        ]
        wanted = list(
            self.inventory.codemagic_checks if expected_names is None else expected_names
        )
        expected = set(wanted)
        observed: dict[str, dict[str, Any]] = {}
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
            observed.update(found)
            missing = [name for name in wanted if name not in observed]
            # A started build proves CodeMagic reacted to the tag. The rest register
            # minutes later, and a release should not be held open watching for them.
            if observed:
                return observed, missing
            if self.clock.monotonic() >= deadline:
                return observed, missing
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
                or not all(
                    isinstance(label, dict)
                    and isinstance(label.get("name"), str)
                    for label in labels
                )
                or app.release_label
                not in _release_label_names(labels)
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
        app_record.setdefault("preparation_pr", app_record.get("staging_to_release_pr"))
        app_record.setdefault("release_sha", app_record["release_sha"])
        app_record.setdefault("release_sha_before", app_record["release_sha"])
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
                    "number,url,state,mergedAt,mergeable,mergeStateStatus,headRefName,headRefOid,baseRefName",
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
            or pull_request.get("headRefName") != app.staging_branch
            or pull_request.get("headRefOid") != app_record.get("staging_sha")
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
        bump_message = self._submodule_bump_message(app, app_record, repository_path)
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
            ["git", "commit", "-m", bump_message],
            ["git", "push", "origin", f"HEAD:{app.dev_branch}"],
        ]
        self._verify_deployment_worktree_is_ignored(app, repository_path)
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
                # Git refuses to remove a worktree holding an initialised submodule;
                # the clean status checked above is what makes forcing safe here.
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=repository_path,
                repository=app.repository,
                mutates=True,
            )
            self._run(
                ["git", "branch", "-D", branch],
                cwd=repository_path,
                repository=app.repository,
                mutates=True,
            )
            app_record["worktree"] = None

    def _fast_forward_staging(
        self,
        batch: dict[str, Any],
        app: AppConfig,
        app_record: dict[str, Any],
        *,
        dry_run: bool,
    ) -> None:
        """Advance an existing staging branch to the prepared dev commit without merging."""
        repository_path = Path(app_record["repository_path"])
        dev_ref = f"refs/remotes/origin/{app.dev_branch}"
        staging_ref = f"refs/remotes/origin/{app.staging_branch}"
        fetch = ["git", "fetch", "origin", _remote_tracking_refspec(app.dev_branch),
                 _remote_tracking_refspec(app.staging_branch)]
        ancestry_command = ["git", "merge-base", "--is-ancestor", staging_ref, dev_ref]
        app_record["branches"] = [app.dev_branch, app.staging_branch, app.release_branch]
        if dry_run:
            # A planned bump has no commit SHA until prepare actually creates it.
            planned_sha = (
                "<prepared-dev-sha>" if app_record["dev_push"] == "planned"
                else app_record["dev_sha"]
            )
            app_record["planned_staging_sha"] = planned_sha
            app_record["staging_push"] = "planned"
            app_record["planned_commands"].extend([
                fetch, ancestry_command,
                ["git", "push", "origin", f"{planned_sha}:refs/heads/{app.staging_branch}"],
            ])
            return

        command = ["git", "ls-remote", "--exit-code", "origin",
                   f"refs/heads/{app.staging_branch}"]
        remote = self.runner.run(command, cwd=repository_path, mutates=False)
        if remote.returncode == 2:
            raise ReleaseError(
                f"repository {app.repository}: missing prerequisite origin/{app.staging_branch}; "
                "land staging CI, create the branch, and configure protection rules and webhooks first"
            )
        if remote.returncode != 0:
            raise ReleaseError(self._command_error(app.repository, command, remote))
        self._run(fetch, cwd=repository_path, repository=app.repository)
        dev_sha = self._run(
            ["git", "rev-parse", f"{dev_ref}^{{commit}}"],
            cwd=repository_path, repository=app.repository,
        ).stdout.strip()
        if dev_sha != app_record["dev_sha"]:
            raise ReleaseError(
                f"repository {app.repository}: origin/{app.dev_branch} changed before staging push"
            )
        ancestry = self.runner.run(ancestry_command, cwd=repository_path, mutates=False)
        if ancestry.returncode == 1:
            raise ReleaseError(
                f"repository {app.repository}: origin/{app.staging_branch} has diverged from "
                f"origin/{app.dev_branch}; a human must investigate direct staging pushes "
                "before resuming; staging will not be merged or force-pushed"
            )
        if ancestry.returncode != 0:
            raise ReleaseError(self._command_error(app.repository, ancestry_command, ancestry))
        self._run(
            ["git", "push", "origin", f"{dev_sha}:refs/heads/{app.staging_branch}"],
            cwd=repository_path, repository=app.repository, mutates=True,
        )
        app_record["staging_sha"] = dev_sha
        app_record["staging_push"] = "pushed"
        self.store.save(batch)

    def _promote_development(
        self,
        batch: dict[str, Any],
        app: AppConfig,
        app_record: dict[str, Any],
        *,
        dry_run: bool,
    ) -> None:
        repository_path = Path(app_record["repository_path"])
        if dry_run:
            app_record["planned_commands"].extend([
                self._preparation_pr_create_command(app),
                _guarded_merge_command(
                    app.repository, "<preparation-pr-number>",
                    app_record["planned_staging_sha"],
                ),
            ])
            app_record["status"] = "planned"
            return
        staging_ref = f"refs/remotes/origin/{app.staging_branch}"
        release_ref = f"refs/remotes/origin/{app.release_branch}"
        self._run(
            [
                "git",
                "fetch",
                "origin",
                _remote_tracking_refspec(app.staging_branch),
                _remote_tracking_refspec(app.release_branch),
            ],
            cwd=repository_path,
            repository=app.repository,
        )
        branch_shas = self._run(
            ["git", "rev-parse", f"{staging_ref}^{{commit}}", f"{release_ref}^{{commit}}"],
            cwd=repository_path,
            repository=app.repository,
        ).stdout.splitlines()
        if len(branch_shas) != 2 or not all(_is_sha(value) for value in branch_shas):
            raise ReleaseError(
                f"repository {app.repository}: invalid fetched staging or release branch"
            )
        staging_sha, release_sha = branch_shas
        if staging_sha != app_record["staging_sha"]:
            raise ReleaseError(
                f"repository {app.repository}: origin/staging changed before promotion"
            )
        ancestry_command = ["git", "merge-base", "--is-ancestor", staging_ref, release_ref]
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
            app_record["skip_reason"] = "no staging to release changes"
            return

        pull_requests = self._list_preparation_pull_requests(app)
        if len(pull_requests) > 1:
            raise ReleaseError(
                f"repository {app.repository}: duplicate preparation pull requests"
            )

        if pull_requests:
            pull_request = self._validate_exact_preparation_pr(
                app, pull_requests[0], staging_sha
            )
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
                "headRefName": app.staging_branch,
                "headRefOid": staging_sha,
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
                    "number,url,state,mergedAt,mergeable,mergeStateStatus,headRefName,headRefOid,baseRefName",
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
        pull_request_state = self._await_known_mergeability(
            app, number, pull_request_state
        )
        self._validate_exact_preparation_pr(app, pull_request_state, staging_sha)
        if (
            pull_request_state.get("state") != "OPEN"
            or pull_request_state.get("mergeable") != "MERGEABLE"
            or pull_request_state.get("mergeStateStatus") == "DIRTY"
        ):
            raise ReleaseError(
                f"repository {app.repository}: preparation pull request is not mergeable "
                f"({pull_request_state.get('mergeable')}/"
                f"{pull_request_state.get('mergeStateStatus')})"
            )

        self._run(
            _guarded_merge_command(
                app.repository,
                number,
                staging_sha,
            ),
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

    def _await_known_mergeability(
        self, app: AppConfig, number: str, pull_request: dict[str, Any]
    ) -> dict[str, Any]:
        """GitHub computes mergeability asynchronously; not yet known is not a refusal."""
        deadline = self.clock.monotonic() + self.poll_policy.timeout_seconds
        while pull_request.get("mergeable") == "UNKNOWN":
            if self.clock.monotonic() >= deadline:
                return pull_request
            self.clock.sleep(self.poll_policy.interval_seconds)
            refreshed = self._load_json(
                self._run(
                    [
                        "gh",
                        "pr",
                        "view",
                        number,
                        "--repo",
                        app.repository,
                        "--json",
                        "number,url,state,mergedAt,mergeable,mergeStateStatus,"
                        "headRefName,headRefOid,baseRefName",
                    ],
                    repository=app.repository,
                ).stdout,
                repository=app.repository,
                subject="preparation pull request response",
            )
            if not isinstance(refreshed, dict):
                return pull_request
            pull_request = refreshed
        return pull_request

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
                    app.staging_branch,
                    "--json",
                    "number,url,headRefName,headRefOid,baseRefName",
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
            app.staging_branch,
            "--title",
            "chore: promote staging to release",
            "--body",
            "Promote the prepared staging branch to release.",
        ]

    @staticmethod
    def _validate_exact_preparation_pr(
        app: AppConfig,
        pull_request: dict[str, Any],
        expected_head_sha: str,
    ) -> dict[str, Any]:
        if (
            not isinstance(pull_request.get("number"), int)
            or not isinstance(pull_request.get("url"), str)
            or pull_request.get("headRefName") != app.staging_branch
            or pull_request.get("headRefOid") != expected_head_sha
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

    def _release_is_contained_in_dev(
        self, app: AppConfig, repository_path: Path
    ) -> bool:
        """Whether every release commit has been merged back into dev.

        Release Please writes the version, changelog and manifest on the release
        branch and nothing carries them back, so the two drift silently. The
        drift is reported rather than repaired: merging into the branch everyone
        works on is a human's decision.
        """
        command = [
            "git",
            "merge-base",
            "--is-ancestor",
            f"refs/remotes/origin/{app.release_branch}",
            f"refs/remotes/origin/{app.dev_branch}",
        ]
        result = self.runner.run(command, cwd=repository_path, mutates=False)
        if result.returncode not in (0, 1):
            raise ReleaseError(self._command_error(app.repository, command, result))
        return result.returncode == 0

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
        release_codemagic = self._run(
            ["git", "show", f"{release_sha}:codemagic.yaml"],
            cwd=repository_path,
            repository=app.repository,
        ).stdout
        workflow_names = _codemagic_workflow_names(release_codemagic)
        missing_workflows = [
            name for name in self.inventory.codemagic_checks if name not in workflow_names
        ]
        if missing_workflows:
            raise ReleaseError(
                f"repository {app.repository}: missing workflow names: {', '.join(missing_workflows)}"
            )

        # Staging workflows are validated against dev, not release: they reach the
        # release branch only once a promotion carries them there.
        dev_codemagic = self._run(
            ["git", "show", f"{dev_sha}:codemagic.yaml"],
            cwd=repository_path,
            repository=app.repository,
        ).stdout
        dev_workflow_names = _codemagic_workflow_names(dev_codemagic)
        missing_staging = [
            name for name in self.inventory.staging_checks if name not in dev_workflow_names
        ]
        if missing_staging:
            raise ReleaseError(
                f"repository {app.repository}: missing staging workflow names on "
                f"{app.dev_branch}: {', '.join(missing_staging)}"
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
                    app.staging_branch,
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
                f"repository {app.repository}: duplicate staging to release pull requests"
            )

        return {
            "state": "preflight-complete",
            "repository": app.repository,
            "repository_path": str(repository_path),
            "branches": [app.dev_branch, app.staging_branch, app.release_branch],
            "dev_sha": dev_sha,
            "release_sha": release_sha,
            "packages_pointer_sha": match.group(1),
            "packages_sha": packages_sha,
            "staging_to_release_pr": pull_requests[0] if pull_requests else None,
        }

    def _promoted_commit_subjects(
        self, app: AppConfig, app_record: dict[str, Any]
    ) -> list[str]:
        """Commits this batch promoted, so an operator can spot work typed as a chore.

        Reporting only: a comparison this cannot read leaves the skip unexplained
        rather than failing the batch.
        """
        before = app_record.get("release_sha_before")
        after = app_record.get("release_sha")
        if not before or not after or before == after:
            return []
        result = self.runner.run(
            [
                "gh",
                "api",
                f"repos/{app.repository}/compare/{before}...{after}",
                "--jq",
                '[.commits[].commit.message | split("\n")[0]]',
            ]
        )
        if result.returncode != 0:
            return []
        try:
            subjects = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        if not isinstance(subjects, list):
            return []
        return [str(subject) for subject in subjects][:_PROMOTED_COMMIT_LIMIT]

    def _verify_repository_permission(self, app: AppConfig) -> None:
        response = self._load_json(
            self._run(
                [
                    "gh",
                    "repo",
                    "view",
                    app.repository,
                    "--json",
                    "viewerPermission",
                ],
                repository=app.repository,
            ).stdout,
            repository=app.repository,
            subject="repository permission response",
        )
        if (
            not isinstance(response, dict)
            or response.get("viewerPermission") not in {"WRITE", "MAINTAIN", "ADMIN"}
        ):
            raise ReleaseError(
                f"repository {app.repository}: authenticated user lacks write or merge permission"
            )

    def _reject_conflicting_deployment_worktrees(
        self, app: AppConfig, repository_path: Path
    ) -> None:
        output = self._run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repository_path,
            repository=app.repository,
        ).stdout
        for record in output.strip().split("\n\n"):
            fields = dict(
                line.split(" ", 1)
                for line in record.splitlines()
                if " " in line
            )
            if fields.get("branch", "").startswith(
                "refs/heads/deploy-mobile-apps/"
            ):
                path = fields.get("worktree", "<unknown>")
                raise ReleaseError(
                    f"repository {app.repository}: conflicting deployment worktree {path}"
                )

    def _submodule_bump_message(
        self, app: AppConfig, app_record: dict[str, Any], repository_path: Path
    ) -> str:
        """Type the bump by the work it carries, so a shared fix still cuts a release.

        A bump is never breaking. Whether a change breaks the package says nothing
        about whether it breaks the app, and no automated commit should cut a major.
        """
        result = self.runner.run(
            [
                "git",
                "log",
                "--format=%s",
                f"{app_record['submodule_before']}..{app_record['submodule_after']}",
            ],
            cwd=repository_path / app.submodule_path,
        )
        if result.returncode != 0:
            return "chore: update shared packages"

        carried = []
        kind = "chore"
        for subject in result.stdout.splitlines():
            match = _CONVENTIONAL_SUBJECT.match(subject.strip())
            if match is None:
                continue
            if match.group("type") == "feat":
                kind = "feat"
            elif kind != "feat":
                kind = "fix"
            carried.append(f"{match.group('type')}: {match.group('summary')}")

        summary = f"{kind}: update shared packages"
        if not carried:
            return summary
        shown = carried[:_CARRIED_SUBJECT_LIMIT]
        remainder = len(carried) - len(shown)
        lines = [summary, "", f"Carried from {app_record['submodule_after'][:7]}:", ""]
        lines.extend(f"- {subject}" for subject in shown)
        if remainder:
            lines.append(f"- and {remainder} more")
        return "\n".join(lines)

    def _verify_deployment_worktree_is_ignored(
        self, app: AppConfig, repository_path: Path
    ) -> None:
        """Preparation adds .claude/worktrees inside the checkout; it must stay untracked."""
        result = self.runner.run(
            ["git", "check-ignore", "-q", ".claude/worktrees"], cwd=repository_path
        )
        if result.returncode != 0:
            raise ReleaseError(
                f"repository {app.repository}: .claude/worktrees is not ignored; "
                "add `.claude/worktrees` to .gitignore before preparing"
            )

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
            {
                value
                for name, value in self.environ.items()
                if value and _carries_a_secret(name, value)
            },
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
                "base": app.get("release_pr_base"),
                "labels": app.get("release_pr_labels", []),
                "head_sha": app.get("release_pr_head_sha"),
                "staging_sha": app.get("staging_sha"),
                "branches": app.get("branches", []),
                "submodule_sha": app.get("submodule_sha", app.get("packages_sha")),
                "result": app.get("result", app.get("skip_reason")),
                "unreleased_commits": app.get("unreleased_commits", []),
            }
        )
    return rows


def _log_tail(log: str) -> list[str]:
    """The last few non-blank log lines, which is where the reason lives."""
    lines = [line.rstrip() for line in log.splitlines() if line.strip()]
    return lines[-_LOG_TAIL_LINES:]


def _staging_result(
    checks: dict[str, dict[str, Any]],
    missing: Sequence[str],
    failed: Sequence[str],
) -> str:
    if not checks:
        return "staged; no CodeMagic build observed yet"
    if failed:
        return f"staged; CodeMagic builds failed: {', '.join(failed)}"
    if missing:
        return f"staged; builds started, not yet reported: {', '.join(missing)}"
    return "staged; CodeMagic staging builds passed"


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
    return re.fullmatch(r"[0-9a-fA-F]{40}", value) is not None


def _release_label_names(labels: Any) -> list[str] | None:
    if not isinstance(labels, list) or not all(
        isinstance(label, dict) and isinstance(label.get("name"), str)
        for label in labels
    ):
        return None
    return sorted({label["name"] for label in labels})


def _release_checks_allow_merge(snapshot: dict[str, Any]) -> bool:
    """A pull request with no checks is only mergeable if GitHub says so itself."""
    checks = snapshot.get("release_pr_checks")
    if _release_checks_pass(checks):
        return True
    return not checks and snapshot.get("release_pr_mergeable") == "MERGEABLE"


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


_SECRET_ENVIRONMENT_NAME = re.compile(
    r"TOKEN|SECRET|KEY|PASSWORD|PASSPHRASE|CREDENTIAL", re.IGNORECASE
)
_OPAQUE_VALUE_LENGTH = 8


def _carries_a_secret(name: str, value: str) -> bool:
    """Short mundane values such as 1 or 0 corrupt the shas and ids they appear inside."""
    return bool(_SECRET_ENVIRONMENT_NAME.search(name)) or len(value) >= _OPAQUE_VALUE_LENGTH


def _guarded_merge_command(
    repository: str,
    pull_request_number: str,
    head_sha: str,
) -> list[str]:
    return [
        "gh",
        "pr",
        "merge",
        pull_request_number,
        "--repo",
        repository,
        "--merge",
        # release branches require an approving review the release account cannot
        # give itself, so the guarded merge carries administrator privileges.
        "--admin",
        "--match-head-commit",
        head_sha,
    ]


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
