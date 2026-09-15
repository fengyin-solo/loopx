from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from ..state_restore import (
    ALL_CATEGORIES,
    build_state_restore_plan,
    execute_state_restore,
    render_state_restore_markdown,
    render_state_verify_markdown,
    verify_state_archive,
)


PrintPayload = Callable[
    [dict[str, object], str, Callable[[dict[str, object]], str]],
    None,
]
FormatSelector = Callable[..., str]
AddFormat = Callable[[argparse.ArgumentParser], None]


def register_verify_state_command(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    add_subcommand_format: AddFormat,
) -> None:
    parser = subparsers.add_parser(
        "verify-state",
        help="Verify a state backup archive: per-file existence, size, and sha256.",
    )
    add_subcommand_format(parser)
    parser.add_argument(
        "--archive",
        required=True,
        help="Path to the loopx-state-<id>.tar.gz backup archive.",
    )
    parser.add_argument(
        "--manifest",
        help=(
            "Sidecar manifest to compare against. Defaults to the matching "
            "loopx-state-<id>.manifest.json next to the archive."
        ),
    )


def register_restore_state_command(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    add_subcommand_format: AddFormat,
) -> None:
    parser = subparsers.add_parser(
        "restore-state",
        help="Preview or perform an atomic restore from a state backup archive.",
        description=(
            "Without --execute this prints a read-only plan listing paths that "
            "would be added, overwritten, or conflict. Use --category to select "
            "backup categories. --abort safely gives up an interrupted restore."
        ),
    )
    add_subcommand_format(parser)
    parser.add_argument(
        "--archive",
        required=True,
        help="Path to the loopx-state-<id>.tar.gz backup archive.",
    )
    parser.add_argument(
        "--project",
        help="Project root for current-project state. Defaults to the archived project.",
    )
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        help=(
            "Restore only one category (repeatable; comma separated accepted). "
            f"Choices: {', '.join(ALL_CATEGORIES)}. Defaults to all categories."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the restore. Omit for a read-only dry run.",
    )
    parser.add_argument(
        "--abort",
        action="store_true",
        help="Safely abandon an interrupted restore and roll back to the pre-restore state.",
    )


def handle_verify_state_command(
    args: argparse.Namespace,
    *,
    print_payload: PrintPayload,
    output_format: FormatSelector,
) -> int:
    try:
        payload = verify_state_archive(
            Path(args.archive).expanduser(),
            manifest_path=Path(args.manifest).expanduser() if args.manifest else None,
        )
    except Exception as exc:
        payload = {
            "ok": False,
            "schema_version": "loopx_state_restore_v0",
            "mode": "state_verify",
            "dry_run": True,
            "error": str(exc),
            "recommended_action": "fix archive verification inputs before retrying",
        }
    print_payload(payload, output_format(args), render_state_verify_markdown)
    return 0 if payload.get("ok") else 1


def handle_restore_state_command(
    args: argparse.Namespace,
    *,
    print_payload: PrintPayload,
    output_format: FormatSelector,
) -> int:
    try:
        runtime_root = (
            Path(args.runtime_root).expanduser() if args.runtime_root else None
        )
        plan = build_state_restore_plan(
            Path(args.archive).expanduser(),
            project=Path(args.project).expanduser() if args.project else None,
            runtime_root=runtime_root,
            categories=args.category or None,
        )
        if args.abort:
            payload = execute_state_restore(plan, abort=True)
        elif args.execute:
            payload = execute_state_restore(plan)
        else:
            payload = plan
    except Exception as exc:
        payload = {
            "ok": False,
            "schema_version": "loopx_state_restore_v0",
            "mode": "state_restore",
            "dry_run": not bool(getattr(args, "execute", False)),
            "execute_requested": bool(getattr(args, "execute", False)),
            "error": str(exc),
            "recommended_action": "fix restore planning before retrying",
        }
    print_payload(payload, output_format(args), render_state_restore_markdown)
    return 0 if payload.get("ok") else 1
