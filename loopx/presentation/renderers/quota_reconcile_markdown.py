"""Markdown renderer for the quota reconciliation report."""

from __future__ import annotations

from typing import Any

KIND_TITLES = {
    "duplicate_billing": "Duplicate billing",
    "missing_void": "Missing void",
    "reimbursement_without_consumption": "Reimbursement without consumption",
    "timestamp_drift": "Timestamp drift",
    "ambiguous_timestamp_drift": "Ambiguous timestamp drift",
    "orphan_void_receipt": "Orphan void receipt",
    "orphan_void_event": "Orphan void event",
    "amount_mismatch": "Amount mismatch",
    "receipt_without_target_key": "Void receipt without target key",
}


def _value(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False or value is None:
        return "" if value is None else "no"
    return str(value)


def _render_correction(correction: dict[str, Any]) -> list[str]:
    action = str(correction.get("action") or "")
    if action == "append_void":
        return [
            f"  - correction: append `{correction.get('slots')}` void slot(s) "
            f"targeting `{correction.get('target_run_generated_at')}`",
        ]
    if action == "append_spend":
        return [
            f"  - correction: append `{correction.get('slots')}` backfill slot(s) "
            f"at run `{correction.get('run_generated_at')}`",
        ]
    return ["  - correction: not auto-fixable; manual review required"]


def _render_discrepancy(discrepancy: dict[str, Any]) -> list[str]:
    kind = str(discrepancy.get("kind") or "")
    identity = discrepancy.get("run_identity")
    identity_text = ""
    if isinstance(identity, dict):
        run_generated_at = identity.get("run_generated_at")
        effect_id = identity.get("settlement_effect_id")
        if effect_id:
            identity_text = f" settlement `{effect_id}`"
        elif run_generated_at:
            identity_text = f" run `{run_generated_at}`"
    lines = [
        f"- **{KIND_TITLES.get(kind, kind)}** "
        f"(`{discrepancy.get('discrepancy_id')}`){identity_text}",
        f"  - fixable: `{_value(bool(discrepancy.get('fixable')))}`",
        f"  - reason: {discrepancy.get('reason') or ''}",
    ]
    correction = discrepancy.get("correction")
    if isinstance(correction, dict):
        lines.extend(_render_correction(correction))
    evidence = discrepancy.get("evidence")
    if isinstance(evidence, dict) and evidence:
        lines.append(
            "  - evidence: "
            + ", ".join(f"{key}={value}" for key, value in evidence.items())
        )
    return lines


def render_quota_reconcile_report_markdown(payload: dict[str, Any]) -> str:
    lines = ["# LoopX Quota Reconciliation", ""]
    summary = payload.get("summary")
    if isinstance(summary, dict):
        by_kind = summary.get("by_kind")
        if isinstance(by_kind, dict):
            counts = ", ".join(
                f"{KIND_TITLES.get(str(kind), kind)}={by_kind.get(kind, 0)}"
                for kind in (
                    "duplicate_billing",
                    "missing_void",
                    "reimbursement_without_consumption",
                    "timestamp_drift",
                )
            )
            lines.append(f"- discrepancies: `{counts}`")
        lines.extend(
            [
                f"- goals scanned: `{summary.get('goals_scanned', 0)}`",
                f"- goals with discrepancies: `{summary.get('goals_with_discrepancies', 0)}`",
                f"- fixable corrections: `{summary.get('fixable', 0)}`",
                f"- correction events planned/applied: `{summary.get('correction_count', 0)}`",
                f"- affected quota entries: `{summary.get('affected_quota_entry_count', 0)}`",
                f"- would deduct slots: `{summary.get('would_deduct_slots', 0)}`",
                f"- would backfill slots: `{summary.get('would_backfill_slots', 0)}`",
                f"- current-window net slot delta: `{summary.get('current_window_slot_delta', 0)}`",
            ]
        )
        diagnostics = summary.get("diagnostics")
        if isinstance(diagnostics, dict):
            lines.append(
                f"- diagnostics (report-only): `{diagnostics.get('total', 0)}`"
            )
    lines.append(
        f"- mode: `{'execute' if payload.get('dry_run') is False else 'dry-run'}`"
    )
    if payload.get("appended") is not None:
        lines.append(f"- appended: `{_value(bool(payload.get('appended')))}`")
    lines.append(
        f"- timestamp tolerance: `{payload.get('timestamp_tolerance_seconds', 60)}` second(s)"
    )

    discrepancies = payload.get("discrepancies")
    if isinstance(discrepancies, list) and discrepancies:
        lines.extend(["", "## Discrepancies", ""])
        for discrepancy in discrepancies:
            if isinstance(discrepancy, dict):
                lines.extend(_render_discrepancy(discrepancy))

    diagnostics_flat = payload.get("diagnostics")
    if isinstance(diagnostics_flat, list) and diagnostics_flat:
        lines.extend(["", "## Diagnostics (not auto-fixed)", ""])
        for diagnostic in diagnostics_flat:
            if not isinstance(diagnostic, dict):
                continue
            kind = str(diagnostic.get("kind") or "")
            lines.append(
                f"- {KIND_TITLES.get(kind, kind)} "
                f"(`{diagnostic.get('goal_id')}`): "
                f"{diagnostic.get('reason') or ''}"
            )

    goals = payload.get("goals")
    if isinstance(goals, list) and goals:
        lines.extend(["", "## Goals", ""])
        for goal in goals:
            if not isinstance(goal, dict):
                continue
            discrepancy_count = goal.get("discrepancy_count", 0)
            diagnostic_count = len(goal.get("diagnostics") or [])
            lines.append(
                f"- `{goal.get('goal_id')}`: {discrepancy_count} discrepancy(ies), "
                f"{diagnostic_count} diagnostic(s), window delta "
                f"`{goal.get('current_window_slot_delta', 0)}` slot(s)"
            )

    goals_clean = payload.get("goals_clean")
    if isinstance(goals_clean, list) and goals_clean:
        lines.extend(["", "## Goals without discrepancies", ""])
        lines.append(", ".join(f"`{goal_id}`" for goal_id in goals_clean))

    applied_batches = payload.get("applied_batches")
    if isinstance(applied_batches, list) and applied_batches:
        lines.extend(["", "## Applied batches", ""])
        for batch in applied_batches:
            if not isinstance(batch, dict):
                continue
            lines.append(
                f"- `{batch.get('goal_id')}`: status `{batch.get('status')}`, "
                f"written `{batch.get('written', 0)}`, replayed "
                f"`{batch.get('replayed', 0)}`, repaired "
                f"`{batch.get('repaired', 0)}`, already resolved "
                f"`{batch.get('already_resolved', 0)}`"
            )

    return "\n".join(lines) + "\n"
