"""Inventory and on-disk state helpers for mobile release batches."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit


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
    """Inspect every selected repository before any release write is allowed."""

    def __init__(
        self,
        inventory: Inventory,
        store: BatchStore,
        *,
        runner: Runner | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.inventory = inventory
        self.store = store
        self.runner = runner or SubprocessRunner()
        self.environ = dict(os.environ if environ is None else environ)

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
        redacted = text
        for key, value in self.environ.items():
            secret_name = any(
                marker in key.upper() for marker in ("TOKEN", "SECRET", "PASSWORD")
            )
            if value and (len(value) >= 8 or secret_name):
                redacted = redacted.replace(value, "<redacted>")
        return redacted

    @staticmethod
    def _load_json(value: str, *, repository: str, subject: str) -> Any:
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise ReleaseError(f"repository {repository}: invalid {subject}") from error


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
    return re.fullmatch(r"\S{40}", value.strip()) is not None


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
