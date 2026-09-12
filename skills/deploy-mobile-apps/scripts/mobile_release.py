#!/usr/bin/env python3
"""Command-line entry point for guarded mobile application releases."""

from __future__ import annotations

import argparse
import copy
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Sequence, TextIO

from mobile_release_lib import (
    BatchStore,
    ReleaseError,
    ReleaseOperator,
    approval_rows,
    load_inventory,
)


SKILL_ROOT = Path(__file__).resolve().parent.parent
INVENTORY_PATH = SKILL_ROOT / "references" / "apps.json"
DEFAULT_STATE_ROOT = Path("~/.local/state/deploy-mobile-apps").expanduser()
COMMAND_PREFIX = "python scripts/mobile_release.py"


class _DiscardingStore:
    """Let read-only observation reuse verification code without saving state."""

    def save(self, batch: dict[str, Any]) -> Path:
        return Path()


def build_operator(state_root: Path | None = None) -> ReleaseOperator:
    """Build an operator from the files bundled with this skill."""
    inventory = load_inventory(INVENTORY_PATH)
    return ReleaseOperator(inventory, BatchStore(state_root or DEFAULT_STATE_ROOT))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    preflight = commands.add_parser("preflight", help="inspect and snapshot apps")
    preflight.add_argument(
        "--app",
        action="append",
        dest="apps",
        metavar="KEY",
        help="select an app; repeat for multiple apps (default: all)",
    )
    preflight.add_argument("--dry-run", action="store_true")
    preflight.add_argument("--json", action="store_true", dest="as_json")

    prepare = commands.add_parser("prepare", help="prepare a saved batch")
    prepare.add_argument("--batch", required=True)
    prepare.add_argument(
        "--staging-only",
        action="store_true",
        dest="staging_only",
        help="stop once staging carries the prepared commit; do not promote to release",
    )
    prepare.add_argument("--dry-run", action="store_true")
    prepare.add_argument("--json", action="store_true", dest="as_json")

    release = commands.add_parser("release", help="release an approved batch")
    release.add_argument("--batch", required=True)
    release.add_argument("--dry-run", action="store_true")
    release.add_argument("--json", action="store_true", dest="as_json")

    status = commands.add_parser("status", help="verify a saved batch read-only")
    status.add_argument("--batch", required=True)
    status.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _next_command(batch: dict[str, Any]) -> str | None:
    batch_id = batch.get("batch_id")
    if not batch_id:
        return None
    state = batch.get("state")
    if state in {
        "preflight-complete",
        "prepare-failed",
        "prepare-in-progress",
        "staging-complete",
    }:
        return f"{COMMAND_PREFIX} prepare --batch {batch_id}"
    if state == "dry-run-complete":
        return f"{COMMAND_PREFIX} prepare --batch {batch_id}"
    if state == "awaiting-approval":
        return f"{COMMAND_PREFIX} release --batch {batch_id}"
    if state in {"partial-release", "release-failed"}:
        return f"{COMMAND_PREFIX} release --batch {batch_id}"
    if state == "released-builds-unverified":
        return f"{COMMAND_PREFIX} status --batch {batch_id}"
    return None


def _batch_warnings(batch: dict[str, Any]) -> list[str]:
    warnings = []
    for app_key in batch.get("selected_apps", []):
        app = batch.get("apps", {}).get(app_key, {})
        if app.get("error"):
            warnings.append(f"{app_key}: {app['error']}")
        if app.get("worktree"):
            warnings.append(f"{app_key}: preserved worktree {app['worktree']}")
        if app.get("release_ahead_of_dev"):
            warnings.append(
                f"{app_key}: release is ahead of dev; merge release back into dev so its "
                "version, changelog and manifest reach the branch work starts from"
            )
    if batch.get("state") == "released-builds-unverified":
        warnings.append(
            "no CodeMagic build was observed yet; a queued build registers no check run"
        )
    return warnings


def _format_checks(checks: Any) -> str:
    if not isinstance(checks, list) or not checks:
        return "-"
    values = []
    for check in checks:
        if not isinstance(check, dict):
            values.append(str(check))
            continue
        name = check.get("name", "unknown")
        outcome = (
            check.get("conclusion")
            or check.get("state")
            or check.get("status")
            or "unknown"
        )
        values.append(f"{name}={outcome}")
    return ",".join(values)


def _summary_document(
    batch: dict[str, Any],
    extra_warnings: Sequence[str] = (),
    next_command_override: str | None = None,
) -> dict[str, Any]:
    document = copy.deepcopy(batch)
    document["rows"] = approval_rows(batch)
    document["warnings"] = [*extra_warnings, *_batch_warnings(batch)]
    document["next_command"] = next_command_override or _next_command(batch)
    return document


def _observe_status(operator: ReleaseOperator, batch_id: str) -> dict[str, Any]:
    batch = operator.status(batch_id)
    if batch.get("state") != "released-builds-unverified":
        return batch

    observed = copy.deepcopy(batch)
    observer = copy.copy(operator)
    observer.store = _DiscardingStore()
    all_verified = True
    for app_key in observed.get("selected_apps", []):
        app_record = observed.get("apps", {}).get(app_key, {})
        if app_record.get("skip_reason"):
            continue
        app = observer.inventory.apps[app_key]
        all_verified = observer._verify_release(observed, app, app_record) and all_verified
    observed["state"] = "released" if all_verified else "released-builds-unverified"
    return observed


def _print_human(
    batch: dict[str, Any],
    extra_warnings: Sequence[str] = (),
    *,
    next_command_override: str | None = None,
    stream: TextIO | None = None,
) -> None:
    stream = stream or sys.stdout
    document = _summary_document(batch, extra_warnings, next_command_override)
    print(f"Batch: {document.get('batch_id', '<unknown>')}", file=stream)
    print(f"State: {document.get('state', '<unknown>')}", file=stream)
    print("Apps:", file=stream)
    for row in document["rows"]:
        fields = [
            row["app"],
            f"version={row.get('version') or '-'}",
            f"pr={row.get('url') or row.get('pr_number') or '-'}",
            f"checks={_format_checks(row.get('checks'))}",
            f"head={row.get('head_sha') or '-'}",
            f"staging={row.get('staging_sha') or '-'}",
            f"branches={' → '.join(row.get('branches', [])) or '-'}",
            f"submodule={row.get('submodule_sha') or '-'}",
            f"result={row.get('result') or '-'}",
        ]
        print("  " + " | ".join(fields), file=stream)
    if document.get("planned_release_merges"):
        print("Planned Release Please merges:", file=stream)
        for plan in document["planned_release_merges"]:
            print(
                "  "
                + f"{plan['repository']} PR #{plan['pull_request_number']} "
                + f"at {plan['head_sha']}: {shlex.join(plan['command'])}",
                file=stream,
            )
    print("Warnings:", file=stream)
    if document["warnings"]:
        for warning in document["warnings"]:
            print(f"  - {warning}", file=stream)
    else:
        print("  - none", file=stream)
    print(f"Next command: {document['next_command'] or 'none'}", file=stream)


def _print_result(
    batch: dict[str, Any],
    *,
    as_json: bool,
    warnings: Sequence[str] = (),
    next_command_override: str | None = None,
) -> None:
    if as_json:
        print(
            json.dumps(
                _summary_document(batch, warnings, next_command_override),
                sort_keys=True,
            )
        )
    else:
        _print_human(batch, warnings, next_command_override=next_command_override)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    operator = build_operator()
    recovery_batch = getattr(args, "batch", None)
    warnings: list[str] = []
    next_command_override = None

    try:
        if args.command == "preflight":
            selected_apps = args.apps or list(operator.inventory.apps)
            if len(selected_apps) < len(operator.inventory.apps):
                warning = "partial batch: fewer than all configured apps are selected"
                warnings.append(warning)
                print(f"Warning: {warning}", file=sys.stderr, flush=True)
            batch = operator.preflight(selected_apps, dry_run=args.dry_run)
            if args.dry_run:
                warnings.append("dry-run preflight was not saved")
                next_command_override = f"{COMMAND_PREFIX} preflight --json"
        elif args.command == "prepare":
            if args.staging_only:
                warnings.append(
                    "staging-only: prepare pushes dev to staging, starting TestFlight "
                    "internal dev-group and Play internal-track builds, and stops "
                    "without promoting to release"
                )
            else:
                warnings.append(
                    "prepare pushes dev to staging, starting TestFlight internal dev-group "
                    "and Play internal-track builds before staging-to-release promotion"
                )
            if args.dry_run:
                warnings.append(
                    "dry run only plans staging and promotion; staging CI, branch protection, "
                    "and webhooks must be ready before a real prepare"
                )
            batch = operator.prepare(
                args.batch, dry_run=args.dry_run, staging_only=args.staging_only
            )
        elif args.command == "release":
            if args.dry_run:
                warnings.append(
                    "dry run: approval was revalidated and no release merge ran"
                )
            batch = operator.release(args.batch, dry_run=args.dry_run)
        else:
            batch = _observe_status(operator, args.batch)
        _print_result(
            batch,
            as_json=args.as_json,
            warnings=warnings,
            next_command_override=next_command_override,
        )
        return 0
    except ReleaseError as error:
        saved = error.recovery_batch
        if saved is None and recovery_batch:
            try:
                saved = operator.status(recovery_batch)
            except (OSError, ValueError, ReleaseError):
                saved = None
        if args.as_json:
            recovery = _summary_document(saved) if saved is not None else None
            print(json.dumps({"error": str(error), "recovery": recovery}, sort_keys=True))
        else:
            print(f"Release failed: {error}", file=sys.stderr)
            if saved is not None:
                _print_human(saved, stream=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
