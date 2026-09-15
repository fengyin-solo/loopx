from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from ..dreaming import (
    DREAMING_PROPOSAL_DECISIONS,
    LEDGER_PROPOSAL_STATUSES,
    build_dreaming_dry_run_proposal,
    consolidate_dreaming_proposals,
    list_dreaming_proposals,
    record_dreaming_proposal_decision,
    render_dreaming_markdown,
)
from ..history import collect_history, load_registry
from ..paths import resolve_runtime_root


PrintPayload = Callable[
    [dict[str, object], str, Callable[[dict[str, object]], str]],
    None,
]
FormatSelector = Callable[..., str]


def register_dreaming_commands(
    subparsers: argparse._SubParsersAction,
    add_subcommand_format: Callable[[argparse.ArgumentParser], None],
) -> None:
    dreaming_parser = subparsers.add_parser(
        "dreaming",
        help="Build local-only advisory dreaming proposals from compact project history.",
    )
    dreaming_sub = dreaming_parser.add_subparsers(dest="dreaming_command", required=True)

    dry_run_parser = dreaming_sub.add_parser(
        "dry-run",
        help="Preview a dreaming proposal without writing project truth or runtime history.",
    )
    add_subcommand_format(dry_run_parser)
    dry_run_parser.add_argument("--goal-id", required=True, help="Goal id whose recent compact history to inspect.")
    dry_run_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Recent compact non-neutral runs to inspect. Defaults to 20; capped at 50.",
    )

    consolidate_parser = dreaming_sub.add_parser(
        "consolidate",
        help=(
            "De-duplicate one batch of dreaming proposals into the persistent "
            "ledger, merge equivalents, and annotate conflicting proposals."
        ),
    )
    add_subcommand_format(consolidate_parser)
    consolidate_parser.add_argument(
        "--goal-id",
        required=True,
        help="Goal id whose recent compact history to consolidate.",
    )
    consolidate_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Recent compact non-neutral runs to inspect. Defaults to 20; capped at 50.",
    )
    consolidate_parser.add_argument(
        "--policy-file",
        help=(
            "Dreaming policy JSON for conflict priority and defer TTL defaults. "
            "Defaults to the .loopx/dreaming-policy.json sidecar when present."
        ),
    )
    consolidate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview de-duplication and conflicts without writing the proposal ledger.",
    )

    proposals_parser = dreaming_sub.add_parser(
        "proposals",
        help="List the read-only dreaming proposal adjudication queue.",
    )
    add_subcommand_format(proposals_parser)
    proposals_parser.add_argument(
        "--goal-id",
        required=True,
        help="Goal id whose dreaming proposal ledger to inspect.",
    )
    proposals_parser.add_argument(
        "--status",
        choices=sorted(LEDGER_PROPOSAL_STATUSES),
        help="Only show proposals with this lifecycle status.",
    )

    decide_parser = dreaming_sub.add_parser(
        "decide",
        help="Record an explicit operator/controller decision for a recorded dreaming proposal.",
    )
    add_subcommand_format(decide_parser)
    decide_parser.add_argument("--goal-id", required=True, help="Goal id that owns the dreaming proposal.")
    decide_parser.add_argument(
        "--proposal-id",
        required=True,
        help="Recorded dreaming proposal id to approve, defer, or reject.",
    )
    decide_parser.add_argument(
        "--decision",
        required=True,
        choices=sorted(DREAMING_PROPOSAL_DECISIONS),
        help="Decision to apply to the recorded dreaming proposal.",
    )
    decide_parser.add_argument(
        "--reason-summary",
        required=True,
        help="Public-safe compact reason for the decision.",
    )
    decide_parser.add_argument(
        "--todo-text",
        help="Agent Todo text to create when approving the proposal.",
    )
    decide_parser.add_argument(
        "--claimed-by",
        help="Optional registered agent id to claim the approved follow-up todo.",
    )
    decide_parser.add_argument(
        "--defer-until",
        help=(
            "ISO 8601 expiry timestamp when --decision defer; the proposal "
            "re-enters adjudication after this time."
        ),
    )
    decide_parser.add_argument(
        "--defer-ttl-hours",
        type=float,
        help=(
            "Positive defer duration in hours (mutually exclusive with "
            "--defer-until); defaults to the policy default_defer_ttl_hours."
        ),
    )
    decide_parser.add_argument(
        "--policy-file",
        help=(
            "Dreaming policy JSON for the defer TTL default and conflict "
            "priority. Defaults to the .loopx/dreaming-policy.json sidecar."
        ),
    )
    decide_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the decision without appending runtime history or mutating active state.",
    )
    decide_parser.add_argument(
        "--no-global-sync",
        action="store_true",
        help="Do not sync the project registry projection into the global registry after writing.",
    )


def handle_dreaming_command(
    args: argparse.Namespace,
    *,
    registry_path: Path,
    runtime_root_arg: str | None,
    output_format: FormatSelector,
    print_payload: PrintPayload,
) -> int:
    selected_format = output_format(args)
    try:
        if args.dreaming_command == "dry-run":
            registry = load_registry(registry_path)
            runtime_root = resolve_runtime_root(registry, runtime_root_arg)
            history_payload = collect_history(
                registry_path=registry_path,
                runtime_root=runtime_root,
                goal_id=args.goal_id,
                limit=max(1, min(int(args.limit), 50)),
                include_runtime_goals=False,
            )
            payload = build_dreaming_dry_run_proposal(
                history_payload,
                goal_id=args.goal_id,
                limit=args.limit,
            )
        elif args.dreaming_command == "consolidate":
            payload = consolidate_dreaming_proposals(
                registry_path=registry_path,
                runtime_root_override=runtime_root_arg,
                goal_id=args.goal_id,
                limit=args.limit,
                policy_file=args.policy_file,
                dry_run=bool(args.dry_run),
            )
        elif args.dreaming_command == "proposals":
            payload = list_dreaming_proposals(
                registry_path=registry_path,
                runtime_root_override=runtime_root_arg,
                goal_id=args.goal_id,
                status_filter=args.status,
            )
        elif args.dreaming_command == "decide":
            payload = record_dreaming_proposal_decision(
                registry_path=registry_path,
                runtime_root_override=runtime_root_arg,
                goal_id=args.goal_id,
                proposal_id=args.proposal_id,
                decision=args.decision,
                reason_summary=args.reason_summary,
                todo_text=args.todo_text,
                claimed_by=args.claimed_by,
                dry_run=bool(args.dry_run),
                sync_global=not bool(args.no_global_sync),
                defer_until=args.defer_until,
                defer_ttl_hours=args.defer_ttl_hours,
                policy_file=args.policy_file,
            )
        else:
            raise ValueError(f"unsupported dreaming command: {args.dreaming_command}")
    except Exception as exc:
        payload = {
            "ok": False,
            "goal_id": getattr(args, "goal_id", None),
            "dry_run": bool(getattr(args, "dry_run", True)),
            "error": str(exc),
            "side_effects": {
                "project_files_mutated": False,
                "active_state_mutated": False,
                "runtime_history_appended": False,
                "quota_spent": False,
            },
        }
    print_payload(payload, selected_format, render_dreaming_markdown)
    return 0 if payload.get("ok") else 1
