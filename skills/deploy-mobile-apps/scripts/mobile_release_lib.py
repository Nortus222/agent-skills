"""Inventory and on-disk state helpers for mobile release batches."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence


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
