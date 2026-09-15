from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .agent_registry import registered_agent_ids_from_registry
from .control_plane.runtime.public_safety import public_safe_compact_text
from .control_plane.runtime.shared_runtime_material_projection import (
    finalize_material_projection,
    prepare_material_projection_route,
)
from .control_plane.runtime.status_classifications import (
    DREAMING_ADVISORY_CLASSIFICATIONS,
    DREAMING_DECISION_CLASSIFICATIONS,
)
from .dreaming_proposals import (
    DREAMING_CONSOLIDATION_SCHEMA_VERSION,
    DREAMING_PROPOSAL_LEDGER_SCHEMA_VERSION,
    DREAMING_PROPOSAL_QUEUE_SCHEMA_VERSION,
    LEDGER_PROPOSAL_STATUSES,
    MAX_DREAMING_EVIDENCE_ITEMS,
    apply_proposal_lifecycle,
    candidate_proposals,
    classification_for_proposal_type,
    compact_ledger_entry,
    compact_conflict,
    compact_run,
    ingest_candidates,
    legacy_proposal_type,
    load_dreaming_policy,
    load_proposal_ledger,
    operator_question_for_proposal_type,
    proposal_ledger_path,
    proposal_summary_text,
    record_entry_decision,
    resolve_defer_expires_at,
    save_proposal_ledger,
)
from .feedback import validate_local_control_text, validate_public_safe_text
from .history import (
    STATUS_NEUTRAL_CLASSIFICATIONS,
    collect_history,
    load_registry,
    write_reserved_run_artifacts,
)
from .paths import resolve_runtime_root
from .registry import registry_goals
from .runtime import validate_goal_id_path_segment
from .state_refresh import now_local
from .todos import add_goal_todo, update_goal_todo

DREAMING_DRY_RUN_SCHEMA_VERSION = "dreaming_dry_run_proposal_v0"
DREAMING_PROPOSAL_SCHEMA_VERSION = "dreaming_proposal_v0"
DREAMING_PROPOSAL_DECISION_SCHEMA_VERSION = "dreaming_proposal_decision_v0"
SERVER_PLANNING_CONTRACT_SCHEMA_VERSION = "server_managed_planning_contract_v0"
DREAMING_PROPOSAL_DECISIONS = {"approve", "defer", "reject"}


def _compact_run(run: dict[str, Any]) -> dict[str, Any]:
    return compact_run(run)


def _goal_record(history_payload: dict[str, Any], goal_id: str) -> dict[str, Any] | None:
    for goal in history_payload.get("goals") or []:
        if isinstance(goal, dict) and str(goal.get("id") or "") == goal_id:
            return goal
    return None


def _signal_runs(goal: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    latest_runs = goal.get("latest_runs") if isinstance(goal.get("latest_runs"), list) else []
    signal_runs: list[dict[str, Any]] = []
    for run in latest_runs:
        if not isinstance(run, dict):
            continue
        classification = str(run.get("classification") or "")
        if not classification:
            continue
        if classification in STATUS_NEUTRAL_CLASSIFICATIONS:
            continue
        if classification in DREAMING_ADVISORY_CLASSIFICATIONS:
            continue
        # Adjudication records are not delivery evidence; re-ingesting them
        # would feed every decision back as a fresh proposal.
        if classification in DREAMING_DECISION_CLASSIFICATIONS:
            continue
        signal_runs.append(run)
        if len(signal_runs) >= limit:
            break
    return signal_runs


def _proposal_type(runs: list[dict[str, Any]]) -> str:
    return legacy_proposal_type(runs)


def _classification_for_proposal_type(proposal_type: str) -> str:
    return classification_for_proposal_type(proposal_type)


def _operator_question(goal_id: str, proposal_type: str) -> str:
    return operator_question_for_proposal_type(goal_id, proposal_type)


def _proposal_summary(runs: list[dict[str, Any]], proposal_type: str) -> str:
    return proposal_summary_text(runs, proposal_type)


def _proposal_id(
    *,
    goal_id: str,
    proposal_type: str,
    evidence_window: str,
    runs: list[dict[str, Any]],
) -> str:
    seed_parts = [goal_id, proposal_type, evidence_window]
    for run in runs[:MAX_DREAMING_EVIDENCE_ITEMS]:
        seed_parts.append(str(run.get("generated_at") or ""))
        seed_parts.append(str(run.get("classification") or ""))
    digest = hashlib.sha256("\n".join(seed_parts).encode("utf-8")).hexdigest()[:12]
    return f"dreaming_{digest}"


def _registry_goal(registry: dict[str, Any], goal_id: str) -> dict[str, Any] | None:
    for goal in registry_goals(registry):
        if str(goal.get("id") or "") == goal_id:
            return goal
    return None


def _compact_source_proposal(run: dict[str, Any]) -> dict[str, Any]:
    dreaming = run.get("dreaming") if isinstance(run.get("dreaming"), dict) else {}
    return {
        "generated_at": public_safe_compact_text(run.get("generated_at"), limit=120),
        "classification": public_safe_compact_text(run.get("classification"), limit=120),
        "proposal_id": public_safe_compact_text(dreaming.get("proposal_id"), limit=120),
        "proposal_type": public_safe_compact_text(dreaming.get("proposal_type"), limit=120),
        "evidence_window": public_safe_compact_text(dreaming.get("evidence_window"), limit=160),
        "summary": public_safe_compact_text(run.get("summary") or run.get("recommended_action"), limit=260),
    }


def _find_source_proposal(
    history_payload: dict[str, Any],
    *,
    goal_id: str,
    proposal_id: str,
) -> dict[str, Any] | None:
    goal = _goal_record(history_payload, goal_id)
    if not goal:
        return None
    latest_runs = goal.get("latest_runs") if isinstance(goal.get("latest_runs"), list) else []
    for run in latest_runs:
        if not isinstance(run, dict):
            continue
        dreaming = run.get("dreaming") if isinstance(run.get("dreaming"), dict) else {}
        if str(dreaming.get("proposal_id") or "") == proposal_id:
            return run
    return None


def build_server_managed_planning_contract() -> dict[str, Any]:
    """Return the default contract for server-managed planning proposals."""

    return {
        "schema_version": SERVER_PLANNING_CONTRACT_SCHEMA_VERSION,
        "lane": "dreaming_planning",
        "authority": "proposal_only_until_promoted",
        "may_rank_candidate_todos": True,
        "may_suggest_evidence_probes": True,
        "may_emit_refactor_warnings": True,
        "may_execute_protected_actions": False,
        "may_read_private_material": False,
        "may_mutate_active_state": False,
        "may_append_delivery_history": False,
        "may_spend_delivery_quota": False,
        "promotion_required": True,
        "promotion_requirements": [
            "operator_or_controller_approval",
            "normal_quota_should_run_decision",
            "goal_boundary_write_scope_approval",
            "public_private_boundary_scan_for_public_artifacts",
        ],
        "allowed_outputs": [
            "ranked_candidate_todos",
            "evidence_probe_suggestions",
            "refactor_warnings",
            "memory_consolidation_proposals",
        ],
        "forbidden_outputs": [
            "agent_command",
            "protected_action_execution",
            "private_material_read",
            "delivery_quota_spend",
            "active_state_mutation_without_promotion",
        ],
    }


def build_dreaming_dry_run_proposal(
    history_payload: dict[str, Any],
    *,
    goal_id: str,
    limit: int = 20,
) -> dict[str, Any]:
    """Build a local-only dreaming proposal preview from compact run history.

    The returned payload is intentionally advisory: it does not append runtime
    history, mutate active project truth, grant an agent command, or spend quota.
    """

    safe_limit = max(1, min(int(limit), 50))
    goal = _goal_record(history_payload, goal_id)
    if not goal:
        return {
            "ok": False,
            "schema_version": DREAMING_DRY_RUN_SCHEMA_VERSION,
            "goal_id": goal_id,
            "dry_run": True,
            "error": f"goal_id not found in history payload: {goal_id}",
            "side_effects": {
                "project_files_mutated": False,
                "active_state_mutated": False,
                "runtime_history_appended": False,
                "quota_spent": False,
            },
        }

    runs = _signal_runs(goal, safe_limit)
    proposal_type = _proposal_type(runs)
    classification = _classification_for_proposal_type(proposal_type)
    evidence_window = f"last_{len(runs)}_non_neutral_runs" if runs else "no_recent_non_neutral_runs"
    proposal_id = _proposal_id(
        goal_id=goal_id,
        proposal_type=proposal_type,
        evidence_window=evidence_window,
        runs=runs,
    )
    question = _operator_question(goal_id, proposal_type)
    server_planning_contract = build_server_managed_planning_contract()
    dreaming = {
        "schema_version": DREAMING_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "lane": "exploration",
        "evidence_window": evidence_window,
        "proposal_type": proposal_type,
        "confidence": "medium" if len(runs) >= 3 else "low",
        "requires_project_controller": True,
        "advisory": True,
        "promoted_to_delivery": False,
        "execution_allowed": False,
        "delivery_spend_allowed": False,
        "server_planning_contract": server_planning_contract,
    }
    preview = {
        "goal_id": goal_id,
        "classification": classification,
        "recommended_action": (
            "Review this advisory dreaming proposal; approve, defer, or reject "
            "it before converting it into active project truth."
        ),
        "operator_question": question,
        "agent_command": None,
        "dreaming": dreaming,
    }
    return {
        "ok": True,
        "schema_version": DREAMING_DRY_RUN_SCHEMA_VERSION,
        "goal_id": goal_id,
        "dry_run": True,
        "proposal_id": proposal_id,
        "classification": classification,
        "proposal_type": proposal_type,
        "summary": _proposal_summary(runs, proposal_type),
        "operator_question": question,
        "recommended_action": preview["recommended_action"],
        "run_record_preview": preview,
        "server_planning_contract": server_planning_contract,
        "recent_evidence": [_compact_run(run) for run in runs[:MAX_DREAMING_EVIDENCE_ITEMS]],
        "side_effects": {
            "project_files_mutated": False,
            "active_state_mutated": False,
            "runtime_history_appended": False,
            "quota_spent": False,
        },
        "write_policy": {
            "advisory": True,
            "append_runtime_history": False,
            "mutate_active_state": False,
            "grant_agent_command": False,
            "spend_quota": False,
        },
    }


def _no_side_effects() -> dict[str, bool]:
    return {
        "project_files_mutated": False,
        "active_state_mutated": False,
        "runtime_history_appended": False,
        "quota_spent": False,
    }


def _collect_goal_signal_runs(
    *,
    registry_path: Path,
    runtime_root: Path,
    goal_id: str,
    limit: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    history_payload = collect_history(
        registry_path=registry_path,
        runtime_root=runtime_root,
        goal_id=goal_id,
        limit=max(1, min(int(limit), 50)),
        include_runtime_goals=False,
    )
    goal = _goal_record(history_payload, goal_id)
    if not goal:
        return None, []
    return goal, _signal_runs(goal, max(1, min(int(limit), 50)))


def _compact_policy(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": policy.get("schema_version"),
        "conflict_priority": dict(policy.get("conflict_priority") or {}),
        "prefer_stronger_evidence": bool(policy.get("prefer_stronger_evidence", True)),
        "default_defer_ttl_hours": policy.get("default_defer_ttl_hours"),
    }


def consolidate_dreaming_proposals(
    *,
    registry_path: Path,
    runtime_root_override: str | None,
    goal_id: str,
    limit: int = 20,
    policy_file: str | Path | None = None,
    dry_run: bool = False,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Ingest one batch of signal runs into the persistent proposal ledger.

    Equivalent proposals and overlapping evidence windows merge, conflicting
    proposals are pairwise annotated, already-decided windows are suppressed,
    and expired defers reopen. Replaying the same batch is a no-op and never
    overwrites decisions. The ledger is advisory local state; it never appends
    runtime history, mutates active state, or spends quota.
    """

    safe_goal_id = validate_goal_id_path_segment(goal_id)
    safe_limit = max(1, min(int(limit), 50))
    registry = load_registry(registry_path)
    runtime_root = resolve_runtime_root(registry, runtime_root_override)
    policy = load_dreaming_policy(registry_path, policy_file)
    goal, runs = _collect_goal_signal_runs(
        registry_path=registry_path,
        runtime_root=runtime_root,
        goal_id=safe_goal_id,
        limit=safe_limit,
    )
    if not goal:
        return {
            "ok": False,
            "schema_version": DREAMING_CONSOLIDATION_SCHEMA_VERSION,
            "goal_id": safe_goal_id,
            "dry_run": dry_run,
            "error": f"goal_id not found in history payload: {safe_goal_id}",
            "side_effects": _no_side_effects(),
        }

    evidence_window = (
        f"last_{len(runs)}_non_neutral_runs" if runs else "no_recent_non_neutral_runs"
    )
    generated_at = now_iso or now_local()
    candidates = candidate_proposals(
        safe_goal_id,
        runs,
        evidence_window=evidence_window,
    )
    ledger_file = proposal_ledger_path(runtime_root, safe_goal_id)
    ledger = load_proposal_ledger(ledger_file)
    ledger, report, changed = ingest_candidates(
        ledger,
        safe_goal_id,
        candidates,
        policy=policy,
        now_iso=generated_at,
    )
    ledger_written = False
    if not dry_run and changed:
        save_proposal_ledger(ledger_file, ledger)
        ledger_written = True

    active_entries = [
        entry
        for entry in ledger.get("proposals", [])
        if entry.get("status") not in {"approved", "rejected"}
    ]
    return {
        "ok": True,
        "schema_version": DREAMING_CONSOLIDATION_SCHEMA_VERSION,
        "goal_id": safe_goal_id,
        "dry_run": dry_run,
        "generated_at": generated_at,
        "evidence_window": evidence_window,
        "ledger_schema_version": DREAMING_PROPOSAL_LEDGER_SCHEMA_VERSION,
        "ledger_path": str(ledger_file),
        "ledger_written": ledger_written,
        "policy": _compact_policy(policy),
        "candidates": report["candidates"],
        "counts": {
            "new": report["new"],
            "merged": report["merged"],
            "suppressed_decided": report["suppressed_decided"],
            "suppressed_deferred": report["suppressed_deferred"],
            "reopened": len(report["reopened"]),
            "conflicts": len(report["conflicts"]),
            "pending_human": report["pending_human"],
        },
        "new_proposal_ids": list(report["new_proposal_ids"]),
        "merged_proposal_ids": list(report["merged_proposal_ids"]),
        "suppressed": list(report["suppressed"]),
        "reopened_proposal_ids": list(report["reopened"]),
        "conflicts": [compact_conflict(item) for item in report["conflicts"]],
        "proposals": [
            compact_ledger_entry(entry) for entry in ledger.get("proposals", [])
        ],
        "active_proposal_count": len(active_entries),
        "side_effects": {
            "project_files_mutated": ledger_written,
            "active_state_mutated": False,
            "runtime_history_appended": False,
            "quota_spent": False,
        },
        "write_policy": {
            "advisory": True,
            "append_runtime_history": False,
            "mutate_active_state": False,
            "grant_agent_command": False,
            "spend_quota": False,
            "persist_proposal_ledger": not dry_run,
        },
    }


def list_dreaming_proposals(
    *,
    registry_path: Path,
    runtime_root_override: str | None,
    goal_id: str,
    status_filter: str | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Read the proposal adjudication queue; expired defers reopen in-memory."""

    safe_goal_id = validate_goal_id_path_segment(goal_id)
    if status_filter is not None and status_filter not in LEDGER_PROPOSAL_STATUSES:
        raise ValueError(
            "status must be one of: " + ", ".join(sorted(LEDGER_PROPOSAL_STATUSES))
        )
    registry = load_registry(registry_path)
    runtime_root = resolve_runtime_root(registry, runtime_root_override)
    ledger_file = proposal_ledger_path(runtime_root, safe_goal_id)
    ledger = load_proposal_ledger(ledger_file)
    generated_at = now_iso or now_local()
    if ledger is None:
        ledger = {"goal_id": safe_goal_id, "proposals": []}
    reopened = apply_proposal_lifecycle(ledger, now_iso=generated_at)

    entries = ledger.get("proposals", [])
    if status_filter is not None:
        entries = [entry for entry in entries if entry.get("status") == status_filter]
    status_counts = {
        status: sum(1 for entry in ledger.get("proposals", []) if entry.get("status") == status)
        for status in sorted(LEDGER_PROPOSAL_STATUSES)
    }
    conflicts: list[dict[str, Any]] = []
    seen_conflict_ids: set[str] = set()
    for entry in ledger.get("proposals", []):
        if entry.get("status") in {"approved", "rejected"}:
            continue
        for conflict in entry.get("conflicts") or []:
            conflict_id = str(conflict.get("conflict_id") or "")
            if conflict_id and conflict_id not in seen_conflict_ids:
                seen_conflict_ids.add(conflict_id)
                conflicts.append(conflict)
    return {
        "ok": True,
        "schema_version": DREAMING_PROPOSAL_QUEUE_SCHEMA_VERSION,
        "goal_id": safe_goal_id,
        "read_only": True,
        "generated_at": generated_at,
        "ledger_schema_version": DREAMING_PROPOSAL_LEDGER_SCHEMA_VERSION,
        "ledger_path": str(ledger_file),
        "reopened_proposal_ids": list(reopened),
        "status_counts": status_counts,
        "conflicts": conflicts,
        "pending_human": sum(
            1
            for conflict in conflicts
            if conflict.get("resolution") == "pending_human"
        ),
        "proposals": [compact_ledger_entry(entry) for entry in entries],
        "side_effects": _no_side_effects(),
    }


def classification_for_dreaming_decision(decision: str) -> str:
    if decision == "approve":
        return "dreaming_proposal_approved"
    if decision == "defer":
        return "dreaming_proposal_deferred"
    if decision == "reject":
        return "dreaming_proposal_rejected"
    raise ValueError(f"decision must be one of: {', '.join(sorted(DREAMING_PROPOSAL_DECISIONS))}")


def _recommended_action_for_decision(decision: str, promoted_todo_id: str | None) -> str:
    if decision == "approve":
        if promoted_todo_id:
            return (
                f"Approved dreaming proposal was promoted into Agent Todo {promoted_todo_id}; "
                "run quota should-run before executing it."
            )
        return (
            "Approved dreaming proposal matched an existing Agent Todo; run quota "
            "should-run before executing the promoted work."
        )
    if decision == "defer":
        return "Deferred dreaming proposal; no delivery todo was created and no quota was spent."
    return "Rejected dreaming proposal; no delivery todo was created and no quota was spent."


def build_dreaming_decision_record(
    *,
    goal_id: str,
    registry_goal: dict[str, Any] | None,
    generated_at: str,
    classification: str,
    recommended_action: str,
    source_proposal: dict[str, Any],
    decision_payload: dict[str, Any],
) -> dict[str, Any]:
    adapter = (
        registry_goal.get("adapter")
        if isinstance(registry_goal, dict) and isinstance(registry_goal.get("adapter"), dict)
        else {}
    )
    promoted_todo_id = decision_payload.get("promoted_todo_id")
    health_check = (
        f"dreaming_decision decision={decision_payload.get('decision')}; "
        "source_proposal 1/1; "
        f"promoted_todo {1 if promoted_todo_id else 0}/1; "
        "quota_spent 0/1"
    )
    return {
        "generated_at": generated_at,
        "goal_id": goal_id,
        "classification": classification,
        "recommended_action": recommended_action,
        "health_check": health_check,
        "delivery_batch_scale": "single_surface",
        "delivery_outcome": "outcome_progress",
        "dreaming_decision": decision_payload,
        "source_dreaming_proposal": source_proposal,
        "registry_goal": {
            "present": bool(registry_goal),
            "domain": registry_goal.get("domain") if registry_goal else None,
            "status": registry_goal.get("status") if registry_goal else None,
            "adapter": {
                "kind": adapter.get("kind"),
                "status": adapter.get("status"),
            },
        },
    }


def _ledger_source_proposal(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": public_safe_compact_text(entry.get("created_at"), limit=120),
        "classification": public_safe_compact_text(entry.get("classification"), limit=120),
        "proposal_id": public_safe_compact_text(entry.get("proposal_id"), limit=120),
        "proposal_type": public_safe_compact_text(entry.get("proposal_type"), limit=120),
        "evidence_window": public_safe_compact_text(entry.get("evidence_window"), limit=160),
        "summary": public_safe_compact_text(entry.get("summary"), limit=260),
    }


def record_dreaming_proposal_decision(
    *,
    registry_path: Path,
    runtime_root_override: str | None,
    goal_id: str,
    proposal_id: str,
    decision: str,
    reason_summary: str,
    todo_text: str | None,
    claimed_by: str | None,
    dry_run: bool,
    sync_global: bool = True,
    defer_until: str | None = None,
    defer_ttl_hours: float | None = None,
    policy_file: str | Path | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    safe_goal_id = validate_goal_id_path_segment(goal_id)
    validate_public_safe_text("proposal_id", proposal_id)
    validate_public_safe_text("reason_summary", reason_summary)
    validate_public_safe_text("todo_text", todo_text)
    if decision not in DREAMING_PROPOSAL_DECISIONS:
        raise ValueError(f"decision must be one of: {', '.join(sorted(DREAMING_PROPOSAL_DECISIONS))}")
    if decision == "approve" and not todo_text:
        raise ValueError("--todo-text is required when --decision approve")
    if decision != "approve" and todo_text:
        raise ValueError("--todo-text is only valid when --decision approve")
    if decision != "defer" and (defer_until is not None or defer_ttl_hours is not None):
        raise ValueError(
            "--defer-until/--defer-ttl-hours are only valid when --decision defer"
        )

    registry = load_registry(registry_path)
    runtime_root = resolve_runtime_root(registry, runtime_root_override)
    policy = load_dreaming_policy(registry_path, policy_file)
    ledger_file = proposal_ledger_path(runtime_root, safe_goal_id)
    ledger = load_proposal_ledger(ledger_file)
    ledger_entry: dict[str, Any] | None = None
    if ledger is not None:
        ledger_entry = next(
            (
                entry
                for entry in ledger.get("proposals", [])
                if isinstance(entry, dict) and entry.get("proposal_id") == proposal_id
            ),
            None,
        )
    if ledger_entry is None and (defer_until is not None or defer_ttl_hours is not None):
        raise ValueError(
            "--defer-until/--defer-ttl-hours require a ledger proposal; run "
            "`loopx dreaming consolidate` first"
        )

    projection_route, compact_projection_route = prepare_material_projection_route(
        registry_path=registry_path,
        goal_id=safe_goal_id,
        source_runtime_root=runtime_root,
        sync_global=sync_global,
    )
    history_payload = collect_history(
        registry_path=registry_path,
        runtime_root=runtime_root,
        goal_id=safe_goal_id,
        limit=50,
        include_runtime_goals=False,
    )
    source_run = _find_source_proposal(history_payload, goal_id=safe_goal_id, proposal_id=proposal_id)
    if ledger_entry is None and not source_run:
        raise ValueError(f"dreaming proposal not found for goal_id={safe_goal_id} proposal_id={proposal_id}")

    generated_at = now_iso or now_local()
    expires_at: str | None = None
    reopened: list[str] = []
    if ledger_entry is not None:
        # Expired defers re-enter adjudication before the new decision lands.
        reopened = apply_proposal_lifecycle(ledger, now_iso=generated_at)
        if decision == "defer":
            expires_at = resolve_defer_expires_at(
                policy,
                defer_until=defer_until,
                defer_ttl_hours=defer_ttl_hours,
                now_iso=generated_at,
            )
        # Record before any promotion side effect: terminal proposals cannot
        # be overwritten, so fail closed while nothing has mutated yet.
        record_entry_decision(
            ledger_entry,
            decision,
            reason_summary=str(public_safe_compact_text(reason_summary, limit=260) or ""),
            decided_at=generated_at,
            expires_at=expires_at,
        )

    source_proposal = (
        _ledger_source_proposal(ledger_entry)
        if ledger_entry is not None
        else _compact_source_proposal(source_run)
    )
    promoted_todo_id: str | None = None
    todo_result: dict[str, Any] | None = None
    todo_evidence_result: dict[str, Any] | None = None
    active_state_mutated = False
    if decision == "approve":
        if not dry_run:
            todo_result = add_goal_todo(
                registry_path=registry_path,
                goal_id=safe_goal_id,
                role="agent",
                text=str(todo_text or ""),
                task_class="advancement_task",
                action_kind="dreaming_proposal_promotion",
                claimed_by=claimed_by,
                dry_run=False,
            )
            promoted_todo_id = str(todo_result.get("todo_id") or "") or None
            registered_agents = registered_agent_ids_from_registry(
                registry_path,
                safe_goal_id,
            )
            if promoted_todo_id and (claimed_by or len(registered_agents) <= 1):
                todo_evidence_result = update_goal_todo(
                    registry_path=registry_path,
                    goal_id=safe_goal_id,
                    role="agent",
                    todo_id=promoted_todo_id,
                    evidence=f"dreaming_proposal:{proposal_id}",
                    note="Approved dreaming proposal promoted to normal Agent Todo.",
                    agent_id=claimed_by,
                    dry_run=False,
                )
            active_state_mutated = bool(
                todo_result
                and (todo_result.get("added") or todo_result.get("metadata_updated"))
                or todo_evidence_result
                and todo_evidence_result.get("changed")
            )

    classification = classification_for_dreaming_decision(decision)
    decision_payload = {
        "schema_version": DREAMING_PROPOSAL_DECISION_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "decision": decision,
        "reason_summary": public_safe_compact_text(reason_summary, limit=260),
        "promoted_to_delivery": decision == "approve",
        "promoted_todo_id": promoted_todo_id,
        "created_todo_id": promoted_todo_id if todo_result and todo_result.get("added") else None,
        "todo_added": bool(todo_result and todo_result.get("added")),
        "delivery_spend_allowed": False,
        "quota_spent": False,
    }
    if claimed_by and decision == "approve":
        decision_payload["claimed_by"] = claimed_by
    ledger_updated = False
    if ledger_entry is not None:
        decision_payload["proposal_ledger"] = {
            "ledger_path": str(ledger_file),
            "ledger_status": ledger_entry["status"],
            "expires_at": expires_at,
            "reopened_proposal_ids": list(reopened),
            "decision_history_count": len(ledger_entry.get("decision_history") or []),
        }
        ledger_updated = True
    ledger_written = bool(ledger_updated and not dry_run)
    recommended_action = _recommended_action_for_decision(decision, promoted_todo_id)
    if decision == "defer" and expires_at:
        recommended_action = (
            f"Deferred dreaming proposal until {expires_at}; it re-enters operator "
            "adjudication after expiry. No delivery todo was created and no quota was spent."
        )
    validate_local_control_text("recommended_action", recommended_action)
    registry_goal = _registry_goal(registry, safe_goal_id)
    record = build_dreaming_decision_record(
        goal_id=safe_goal_id,
        registry_goal=registry_goal,
        generated_at=generated_at,
        classification=classification,
        recommended_action=recommended_action,
        source_proposal=source_proposal,
        decision_payload=decision_payload,
    )
    record["runtime_projection_route"] = compact_projection_route

    runs_dir = runtime_root / "goals" / safe_goal_id / "runs"
    index_record = {
        "generated_at": generated_at,
        "goal_id": safe_goal_id,
        "classification": classification,
        "recommended_action": recommended_action,
        "health_check": record["health_check"],
        "dreaming_decision": decision_payload,
        "source_dreaming_proposal": source_proposal,
        "runtime_projection_route": compact_projection_route,
    }
    runtime_history_appended = not dry_run
    payload: dict[str, Any] = {
        "ok": True,
        "schema_version": DREAMING_PROPOSAL_DECISION_SCHEMA_VERSION,
        "dry_run": dry_run,
        "appended": not dry_run,
        "registry": str(registry_path),
        "runtime_root": str(runtime_root),
        "goal_id": safe_goal_id,
        "proposal_id": proposal_id,
        "classification": classification,
        "recommended_action": recommended_action,
        "generated_at": generated_at,
        "health_check": record["health_check"],
        "decision": decision,
        "dreaming_decision": decision_payload,
        "source_dreaming_proposal": source_proposal,
        "runtime_projection_route": compact_projection_route,
        "todo_result": todo_result,
        "todo_evidence_result": todo_evidence_result,
        "side_effects": {
            "project_files_mutated": bool(
                runtime_history_appended or active_state_mutated or ledger_written
            ),
            "active_state_mutated": bool(active_state_mutated),
            "runtime_history_appended": runtime_history_appended,
            "proposal_ledger_updated": ledger_written,
            "todo_added": bool(todo_result and todo_result.get("added")),
            "quota_spent": False,
        },
        "write_policy": {
            "append_runtime_history": True,
            "mutate_active_state": decision == "approve",
            "grant_agent_command": False,
            "spend_quota": False,
        },
    }
    if ledger_entry is not None:
        payload["proposal_ledger"] = decision_payload["proposal_ledger"]
        payload["reopened_proposal_ids"] = list(reopened)
    if dry_run:
        payload.update(
            {
                "json_path": None,
                "markdown_path": None,
                "index_path": str(runs_dir / "index.jsonl"),
            }
        )
    else:
        write_reserved_run_artifacts(
            runs_dir=runs_dir,
            generated_at=generated_at,
            record=record,
            index_record=index_record,
            payload=payload,
            render_markdown=render_dreaming_decision_markdown,
        )
        if ledger_entry is not None:
            save_proposal_ledger(ledger_file, ledger)
    projection_result = finalize_material_projection(
        registry_path=registry_path,
        source_runtime_root=runtime_root,
        goal_id=safe_goal_id,
        source_row=index_record,
        projection_kind="dreaming_decision",
        route=projection_route,
        sync_global=sync_global,
        dry_run=dry_run,
    )
    payload["global_sync"] = projection_result["global_sync"]
    payload["shared_runtime_material_projection"] = projection_result[
        "shared_runtime_material_projection"
    ]
    if not projection_result["ok"]:
        payload["ok"] = False
        payload["partial_write"] = projection_result["partial_write"]
    return payload


def render_dreaming_dry_run_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Dreaming Dry-Run Proposal",
        "",
        f"- Goal: `{payload.get('goal_id')}`",
        f"- OK: `{payload.get('ok')}`",
        f"- Dry run: `{payload.get('dry_run')}`",
    ]
    if payload.get("error"):
        lines.append(f"- Error: {payload.get('error')}")
        return "\n".join(lines) + "\n"

    side_effects = payload.get("side_effects") if isinstance(payload.get("side_effects"), dict) else {}
    contract = (
        payload.get("server_planning_contract")
        if isinstance(payload.get("server_planning_contract"), dict)
        else {}
    )
    lines.extend(
        [
            f"- Classification: `{payload.get('classification')}`",
            f"- Proposal id: `{payload.get('proposal_id')}`",
            f"- Proposal type: `{payload.get('proposal_type')}`",
            f"- Summary: {payload.get('summary')}",
            f"- Operator question: {payload.get('operator_question')}",
            f"- Runtime history appended: `{side_effects.get('runtime_history_appended')}`",
            f"- Active state mutated: `{side_effects.get('active_state_mutated')}`",
            f"- Quota spent: `{side_effects.get('quota_spent')}`",
        ]
    )
    if contract:
        lines.extend(
            [
                f"- Planning authority: `{contract.get('authority')}`",
                f"- May rank todos: `{contract.get('may_rank_candidate_todos')}`",
                f"- May execute protected actions: `{contract.get('may_execute_protected_actions')}`",
                f"- May read private material: `{contract.get('may_read_private_material')}`",
                f"- May spend delivery quota: `{contract.get('may_spend_delivery_quota')}`",
            ]
        )
    lines.extend(["", "## Recent Evidence", ""])
    evidence = payload.get("recent_evidence") if isinstance(payload.get("recent_evidence"), list) else []
    if not evidence:
        lines.append("- No recent non-neutral run evidence.")
    for item in evidence:
        if not isinstance(item, dict):
            continue
        lines.append(
            "- "
            f"`{item.get('classification')}` "
            f"{item.get('generated_at') or ''}: "
            f"{item.get('recommended_action') or item.get('delivery_outcome') or ''}"
        )
    return "\n".join(lines) + "\n"


def render_dreaming_decision_markdown(payload: dict[str, Any]) -> str:
    decision = (
        payload.get("dreaming_decision")
        if isinstance(payload.get("dreaming_decision"), dict)
        else {}
    )
    source = (
        payload.get("source_dreaming_proposal")
        if isinstance(payload.get("source_dreaming_proposal"), dict)
        else {}
    )
    side_effects = payload.get("side_effects") if isinstance(payload.get("side_effects"), dict) else {}
    lines = [
        "# Dreaming Proposal Decision",
        "",
        f"- Goal: `{payload.get('goal_id')}`",
        f"- OK: `{payload.get('ok')}`",
        f"- Dry run: `{payload.get('dry_run')}`",
        f"- Appended: `{payload.get('appended')}`",
        f"- Classification: `{payload.get('classification')}`",
        f"- Proposal id: `{payload.get('proposal_id')}`",
        f"- Decision: `{payload.get('decision')}`",
        f"- Promoted to delivery: `{decision.get('promoted_to_delivery')}`",
        f"- Promoted todo: `{decision.get('promoted_todo_id')}`",
        f"- Created todo: `{decision.get('created_todo_id')}`",
        f"- Runtime history appended: `{side_effects.get('runtime_history_appended')}`",
        f"- Active state mutated: `{side_effects.get('active_state_mutated')}`",
        f"- Quota spent: `{side_effects.get('quota_spent')}`",
        f"- Recommended action: {payload.get('recommended_action')}",
        "",
        "## Source Proposal",
        "",
        f"- Generated at: `{source.get('generated_at')}`",
        f"- Classification: `{source.get('classification')}`",
        f"- Proposal type: `{source.get('proposal_type')}`",
        f"- Evidence window: `{source.get('evidence_window')}`",
    ]
    if source.get("summary"):
        lines.append(f"- Summary: {source.get('summary')}")
    ledger = payload.get("proposal_ledger")
    if isinstance(ledger, dict):
        lines.extend(
            [
                "",
                "## Proposal Ledger",
                "",
                f"- Ledger status: `{ledger.get('ledger_status')}`",
                f"- Defer expires at: `{ledger.get('expires_at')}`",
                f"- Decision history entries: `{ledger.get('decision_history_count')}`",
            ]
        )
        if ledger.get("reopened_proposal_ids"):
            lines.append(
                "- Reopened on ingest: "
                + ", ".join(f"`{item}`" for item in ledger["reopened_proposal_ids"])
            )
    return "\n".join(lines) + "\n"


def _render_ledger_proposal_lines(entry: dict[str, Any]) -> list[str]:
    lines = [
        f"### `{entry.get('proposal_id')}` ({entry.get('proposal_type')})",
        "",
        f"- Status: `{entry.get('status')}`",
        f"- Evidence window: `{entry.get('evidence_window')}`",
        f"- Confidence: `{entry.get('confidence')}`",
        f"- Merged sources: `{entry.get('merged_source_count', len(entry.get('merged_from') or []))}`",
        f"- Summary: {entry.get('summary')}",
    ]
    decision = entry.get("decision") if isinstance(entry.get("decision"), dict) else None
    if decision:
        lines.append(
            f"- Decision: `{decision.get('decision')}` at `{decision.get('decided_at')}`"
            + (f" until `{decision.get('expires_at')}`" if decision.get("expires_at") else "")
        )
        if decision.get("reason_summary"):
            lines.append(f"- Reason: {decision.get('reason_summary')}")
    conflicts = entry.get("conflicts") if isinstance(entry.get("conflicts"), list) else []
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            continue
        preferred = conflict.get("preferred_proposal_id")
        lines.append(
            f"- Conflict `{conflict.get('conflict_type')}` "
            f"(`{conflict.get('conflict_id')}`): `{conflict.get('resolution')}`"
            + (f", preferred `{preferred}`" if preferred else "")
            + (
                f" via {conflict.get('priority_rule')}"
                if conflict.get("priority_rule")
                else " (pending human adjudication)"
            )
        )
    return lines


def render_dreaming_consolidation_markdown(payload: dict[str, Any]) -> str:
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    lines = [
        "# Dreaming Proposal Consolidation",
        "",
        f"- Goal: `{payload.get('goal_id')}`",
        f"- OK: `{payload.get('ok')}`",
        f"- Dry run: `{payload.get('dry_run')}`",
        f"- Evidence window: `{payload.get('evidence_window')}`",
        f"- Ledger written: `{payload.get('ledger_written')}`",
        f"- Candidates: `{payload.get('candidates')}`",
        f"- New: `{counts.get('new')}`; merged: `{counts.get('merged')}`",
        f"- Suppressed by decisions: `{counts.get('suppressed_decided')}`; "
        f"suppressed by active defer: `{counts.get('suppressed_deferred')}`",
        f"- Reopened: `{counts.get('reopened')}`; conflicts: `{counts.get('conflicts')}`; "
        f"pending human: `{counts.get('pending_human')}`",
    ]
    conflicts = payload.get("conflicts") if isinstance(payload.get("conflicts"), list) else []
    if conflicts:
        lines.extend(["", "## Conflicts", ""])
        for conflict in conflicts:
            if not isinstance(conflict, dict):
                continue
            sides = conflict.get("sides") if isinstance(conflict.get("sides"), list) else []
            side_text = ", ".join(
                f"{item.get('side')}=`{item.get('proposal_id')}`"
                for item in sides
                if isinstance(item, dict)
            )
            lines.append(
                f"- `{conflict.get('conflict_type')}` {side_text} -> "
                f"`{conflict.get('resolution')}`"
                + (
                    f" (preferred `{conflict.get('preferred_proposal_id')}` via "
                    f"{conflict.get('priority_rule')})"
                    if conflict.get("resolution") == "priority"
                    else " (pending human adjudication)"
                )
            )
    proposals = payload.get("proposals") if isinstance(payload.get("proposals"), list) else []
    if proposals:
        lines.extend(["", "## Proposals", ""])
        for entry in proposals:
            if isinstance(entry, dict):
                lines.extend(_render_ledger_proposal_lines(entry))
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_dreaming_proposal_queue_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Dreaming Proposal Adjudication Queue",
        "",
        f"- Goal: `{payload.get('goal_id')}`",
        f"- OK: `{payload.get('ok')}` (read-only)",
        f"- Status counts: `{payload.get('status_counts')}`",
        f"- Pending human conflicts: `{payload.get('pending_human')}`",
    ]
    if payload.get("reopened_proposal_ids"):
        lines.append(
            "- Expired defers reopened: "
            + ", ".join(f"`{item}`" for item in payload["reopened_proposal_ids"])
        )
    proposals = payload.get("proposals") if isinstance(payload.get("proposals"), list) else []
    if proposals:
        lines.extend(["", "## Proposals", ""])
        for entry in proposals:
            if isinstance(entry, dict):
                lines.extend(_render_ledger_proposal_lines(entry))
                lines.append("")
    else:
        lines.extend(["", "No dreaming proposals in the requested queue.", ""])
    return "\n".join(lines).rstrip() + "\n"


def render_dreaming_markdown(payload: dict[str, Any]) -> str:
    schema = payload.get("schema_version")
    if schema == DREAMING_PROPOSAL_DECISION_SCHEMA_VERSION:
        return render_dreaming_decision_markdown(payload)
    if schema == DREAMING_CONSOLIDATION_SCHEMA_VERSION:
        return render_dreaming_consolidation_markdown(payload)
    if schema == DREAMING_PROPOSAL_QUEUE_SCHEMA_VERSION:
        return render_dreaming_proposal_queue_markdown(payload)
    return render_dreaming_dry_run_markdown(payload)
