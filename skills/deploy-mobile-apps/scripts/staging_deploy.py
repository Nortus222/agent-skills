"""Prepare exact-tree candidates and publish only the pinned staging branch."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

from mobile_release_lib import (
    AppConfig,
    ReleaseError,
    _FAILING_CONCLUSIONS,
    _codemagic_workflow_names,
    _is_sha,
    _normalize_repository,
    new_batch,
)


class StagingDeploy:
    """Compose pinned-staging policy with the shared operator's services."""

    def __init__(self, operator, app: AppConfig) -> None:
        self.operator = operator
        self.app = app

    def _git(self, path: Path, *args: str, mutates: bool = False) -> str:
        return self.operator._run(
            ["git", *args], cwd=path, repository=self.app.repository, mutates=mutates
        ).stdout.strip()

    def _fetch(self, path: Path) -> dict[str, str]:
        remote = self._git(path, "remote", "get-url", "origin")
        if _normalize_repository(remote) != _normalize_repository(self.app.repository):
            raise ReleaseError(
                "origin changed or does not match the configured repository"
            )
        push_urls = self._git(
            path, "remote", "get-url", "--push", "--all", "origin"
        ).splitlines()
        if len(push_urls) != 1:
            raise ReleaseError("origin requires exactly one effective push destination")
        if _normalize_repository(push_urls[0]) != _normalize_repository(
            self.app.repository
        ):
            raise ReleaseError(
                "origin push destination does not match the configured repository"
            )
        branches = (
            self.app.dev_branch,
            self.app.staging_branch,
            self.app.release_branch,
        )
        self._git(
            path,
            "fetch",
            "origin",
            *[
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
                for branch in branches
            ],
        )
        return {
            branch: self._git(path, "rev-parse", f"origin/{branch}^{{commit}}")
            for branch in branches
        }

    def _ensure_snapshot(self, batch: dict[str, Any]) -> dict[str, str]:
        record = batch["apps"][self.app.key]
        refs = self._fetch(Path(record["repository_path"]))
        if refs[self.app.dev_branch] != record["dev_sha"]:
            raise ReleaseError("dev changed after preflight; prepare a new batch")
        if refs[self.app.staging_branch] != record["staging_sha"]:
            raise ReleaseError("staging changed after preflight; prepare a new batch")
        return refs

    def preflight(
        self,
        dry_run: bool = False,
        repository_path: Path | None = None,
        source_sha: str | None = None,
    ) -> dict[str, Any]:
        path = repository_path
        if path is None:
            path = self.operator._discover_repositories(
                self.operator._workspace_root(), [self.app.key]
            )[self.app.key]
        path = path.expanduser().resolve()
        remote = self._git(path, "remote", "get-url", "origin")
        if _normalize_repository(remote) != _normalize_repository(self.app.repository):
            raise ReleaseError("origin does not match the configured repository")
        self.operator._run(
            ["gh", "auth", "status", "--hostname", "github.com"],
            repository=self.app.repository,
        )
        refs = self._fetch(path)
        source = source_sha or refs[self.app.dev_branch]
        if not _is_sha(source):
            raise ReleaseError("source must be a full commit SHA")
        self._git(path, "cat-file", "-e", f"{source}^{{commit}}")
        self._git(
            path, "merge-base", "--is-ancestor", refs[self.app.dev_branch], source
        )
        notes_file = self.app.release_notes_file
        changed = set(
            self._git(
                path, "diff", "--name-only", refs[self.app.dev_branch], source
            ).splitlines()
        )
        if changed - ({notes_file} if notes_file else set()):
            raise ReleaseError(
                f'source may differ from merged dev only in {notes_file or "no files"}'
            )
        notes = None
        if notes_file:
            notes = self.operator._load_json(
                self._git(path, "show", f"{source}:{notes_file}"),
                repository=self.app.repository,
                subject="release notes",
            )
            if (
                not isinstance(notes, list)
                or not notes
                or any(
                    not isinstance(note, dict)
                    or not isinstance(note.get("language"), str)
                    or not note["language"].strip()
                    or not isinstance(note.get("text"), str)
                    or not note["text"].strip()
                    for note in notes
                )
            ):
                raise ReleaseError(
                    "release notes require language and nonempty text entries"
                )
        yaml = self._git(path, "show", f"{source}:codemagic.yaml")
        self.operator._reject_unusable_codemagic(self.app, self.app.dev_branch, yaml)
        missing = set(
            self.operator._checks(self.app, staging=True)
        ) - _codemagic_workflow_names(yaml)
        if missing:
            raise ReleaseError(
                f"configured staging workflow names are missing: {sorted(missing)}"
            )
        packages = self._git(path, "ls-tree", source, "--", self.app.submodule_path)
        match = re.fullmatch(
            r"160000 commit ([0-9a-f]{40})\t" + re.escape(self.app.submodule_path),
            packages,
        )
        if not match:
            raise ReleaseError("selected source has no pinned packages gitlink")
        batch = new_batch([self.app.key])
        batch.update(
            state="preflight-complete", channel="staging", workflow="pinned-staging"
        )
        batch["apps"][self.app.key] = {
            "routing": self.operator._routing_snapshot(self.app),
            "state": "preflight-complete",
            "branches": [self.app.dev_branch, self.app.staging_branch],
            "repository": self.app.repository,
            "repository_path": str(path),
            "dev_sha": refs[self.app.dev_branch],
            "staging_sha": refs[self.app.staging_branch],
            "release_sha": refs[self.app.release_branch],
            "source_sha": source,
            "submodule_sha": match.group(1),
            "release_notes": notes,
            "worktree_status": self._git(path, "status", "--porcelain"),
            "pending_commits": self._git(
                path, "log", "--oneline", f"{refs[self.app.staging_branch]}..{source}"
            ),
            "staging_only_commits": self._git(
                path, "log", "--oneline", f"{source}..{refs[self.app.staging_branch]}"
            ),
            "diff_stat": self._git(
                path, "diff", "--stat", refs[self.app.staging_branch], source
            ),
            "result": "staging source snapshotted; release notes need content review",
        }
        if not dry_run:
            self.operator.store.save(batch)
        return batch

    def _verify_candidate(
        self, path: Path, record: dict[str, Any], candidate: str
    ) -> None:
        source_tree = self._git(path, "rev-parse", f"{record['source_sha']}^{{tree}}")
        candidate_tree = self._git(path, "rev-parse", f"{candidate}^{{tree}}")
        if source_tree != candidate_tree:
            raise ReleaseError(
                "promotion candidate tree differs from selected source; inspect preserved worktree"
            )
        pointer = self._git(path, "ls-tree", candidate, "--", self.app.submodule_path)
        if (
            pointer
            != f"160000 commit {record['submodule_sha']}\t{self.app.submodule_path}"
        ):
            raise ReleaseError(
                "promotion candidate packages pointer differs from selected source"
            )

    def prepare(self, batch: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        if batch["state"] not in {"preflight-complete", "awaiting-approval"}:
            raise ReleaseError("prepare requires a preflight or prepared batch")
        batch_id = batch["batch_id"]
        record = batch["apps"][self.app.key]
        path = Path(record["repository_path"])
        self._ensure_snapshot(batch)
        if record.get("candidate_sha"):
            return batch
        ancestry = self.operator.runner.run(
            [
                "git",
                "merge-base",
                "--is-ancestor",
                record["staging_sha"],
                record["source_sha"],
            ],
            cwd=path,
        )
        if ancestry.returncode not in {0, 1}:
            raise ReleaseError("could not establish staging ancestry")
        if dry_run:
            batch["state"] = "dry-run-complete"
            record["result"] = (
                "direct push candidate"
                if ancestry.returncode == 0
                else "promotion merge required in an isolated worktree"
            )
            return batch
        try:
            if ancestry.returncode == 0:
                candidate = record["source_sha"]
            else:
                worktree = (
                    Path(tempfile.mkdtemp(prefix="pinned-staging-")) / "promotion"
                )
                record["worktree"] = str(worktree)
                record["promotion_branch"] = f"deploy-pinned-staging/{batch_id}"
                self.operator.store.save(batch)
                self._git(
                    path,
                    "worktree",
                    "add",
                    "-b",
                    record["promotion_branch"],
                    str(worktree),
                    record["staging_sha"],
                    mutates=True,
                )
                self._git(
                    worktree,
                    "merge",
                    "--no-ff",
                    record["source_sha"],
                    "-m",
                    "Promote merged dev updates to staging",
                    mutates=True,
                )
                candidate = self._git(worktree, "rev-parse", "HEAD")
                if self._git(worktree, "status", "--porcelain"):
                    raise ReleaseError(
                        "promotion worktree is dirty; inspect it before continuing"
                    )
            self._verify_candidate(path, record, candidate)
            record.update(
                state="awaiting-approval",
                candidate_sha=candidate,
                result="prepared; no publishing push yet",
            )
            batch["state"] = "awaiting-approval"
            self.operator.store.save(batch)
            return batch
        except ReleaseError as error:
            record["error"] = str(error)
            batch["state"] = "prepare-failed"
            self.operator.store.save(batch)
            raise

    def release(self, batch: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        if batch["state"] not in {
            "awaiting-approval",
            "push-in-progress",
            "push-failed",
            "released",
            "released-builds-unverified",
        }:
            raise ReleaseError("release requires a prepared staging batch")
        record = batch["apps"][self.app.key]
        candidate = record["candidate_sha"]
        if record.get("pushed_sha"):
            return self._observe(batch, save=not dry_run)
        path = Path(record["repository_path"])
        if record.get("push_attempted") and not path.is_dir():
            return self.status(batch)
        refs = self._fetch(path)
        current = refs[self.app.staging_branch]
        if record.get("push_attempted") and current == candidate:
            record["pushed_sha"] = candidate
            batch["state"] = "released-builds-unverified"
            if not dry_run:
                self.operator.store.save(batch)
        elif record.get("push_attempted"):
            # An uncertain earlier push must not become another publishing batch.
            return self.status(batch)
        else:
            self._ensure_snapshot(batch)
            self._verify_candidate(path, record, candidate)
            self._git(
                path, "merge-base", "--is-ancestor", record["staging_sha"], candidate
            )
            if candidate == record["staging_sha"]:
                batch["state"] = "no-changes"
                record["result"] = (
                    "staging already contains this source; no build triggered"
                )
                if not dry_run:
                    self.operator.store.save(batch)
                return batch
            if dry_run:
                record["planned_commands"] = [
                    [
                        "git",
                        "push",
                        "origin",
                        f"{candidate}:refs/heads/{self.app.staging_branch}",
                    ]
                ]
                return batch
            # Save before the external mutation so a lost response can be reconciled.
            record["push_attempted"] = True
            batch["state"] = "push-in-progress"
            self.operator.store.save(batch)
            try:
                self._git(
                    path,
                    "push",
                    "origin",
                    f"{candidate}:refs/heads/{self.app.staging_branch}",
                    mutates=True,
                )
                remote = self._git(
                    path, "ls-remote", "origin", f"refs/heads/{self.app.staging_branch}"
                ).split()[0]
                if remote != candidate:
                    raise ReleaseError(
                        "staging changed after push; publishing result needs inspection"
                    )
                record["pushed_sha"] = candidate
                batch["state"] = "released-builds-unverified"
                self.operator.store.save(batch)
            except ReleaseError as error:
                batch["state"] = "push-failed"
                record["error"] = str(error)
                self.operator.store.save(batch)
                raise
        return self._observe(batch, save=not dry_run)

    def _observe(self, batch: dict[str, Any], *, save: bool) -> dict[str, Any]:
        record = batch["apps"][self.app.key]
        sha = record.get("pushed_sha", record["candidate_sha"])
        checks, missing = self.operator._wait_for_codemagic_checks(
            self.app, sha, self.operator._checks(self.app, staging=True)
        )
        record.update(
            staging_checks=checks,
            staging_missing_checks=missing,
            staging_failed_checks=sorted(
                name
                for name, check in checks.items()
                if check.get("conclusion") in _FAILING_CONCLUSIONS
            ),
            staging_diagnosis=self.operator._diagnose_staging(
                self.app, sha, checks, missing
            ),
            codemagic_checks=checks,
            codemagic_missing_checks=missing,
            result=(
                "build checks observed; inspect publishing results"
                if checks
                else "pushed; builds unverified"
            ),
        )
        if record["staging_failed_checks"]:
            record["result"] = "publishing build failed: " + ", ".join(
                record["staging_failed_checks"]
            )
        ref = self.operator._load_json(
            self.operator._run(
                [
                    "gh",
                    "api",
                    f"repos/{self.app.repository}/git/ref/heads/{self.app.staging_branch}",
                ],
                repository=self.app.repository,
            ).stdout,
            repository=self.app.repository,
            subject="staging ref response",
        )
        object_ref = ref.get("object") if isinstance(ref, dict) else None
        current = object_ref.get("sha") if isinstance(object_ref, dict) else None
        if not isinstance(current, str) or not _is_sha(current):
            raise ReleaseError("GitHub returned an invalid staging ref")
        record["current_staging_sha"] = current
        batch["state"] = "released" if checks else "released-builds-unverified"
        if not record.get("pushed_sha"):
            batch["state"] = "push-outcome-unverified"
            record["result"] = (
                "push outcome unverified; inspecting attempted candidate checks"
            )
        if save:
            self.operator.store.save(batch)
        return batch

    def status(self, batch: dict[str, Any]) -> dict[str, Any]:
        if batch["apps"][self.app.key].get("push_attempted"):
            return self._observe(batch, save=False)
        return batch
