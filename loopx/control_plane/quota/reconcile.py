"""Python bridge for the TypeScript-owned quota reconciliation effects.

The reconciliation domain rules and durable transactions live in the typed
control plane (``quota/reconcile.ts``). This module only assembles the typed
requests and validates the response envelopes; it never mutates the run
ledger directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...file_lock import exclusive_file_lock
from ..effect_runtime import EffectRuntimeRejected, effect_runtime_result

QUOTA_RECONCILE_SCAN_REQUEST_SCHEMA = "loopx_quota_reconcile_scan_request_v0"
QUOTA_RECONCILE_SCAN_RESULT_SCHEMA = "loopx_quota_reconcile_scan_result_v0"
QUOTA_RECONCILE_COMMIT_REQUEST_SCHEMA = "loopx_quota_reconcile_commit_request_v0"
QUOTA_RECONCILE_COMMIT_RESULT_SCHEMA = "loopx_quota_reconcile_commit_result_v0"
QUOTA_RECONCILIATION_REPORT_SCHEMA = "quota_reconciliation_report_v0"

DEFAULT_TIMESTAMP_TOLERANCE_SECONDS = 60

__all__ = [
    "QUOTA_RECONCILE_SCAN_REQUEST_SCHEMA",
    "QUOTA_RECONCILE_SCAN_RESULT_SCHEMA",
    "QUOTA_RECONCILE_COMMIT_REQUEST_SCHEMA",
    "QUOTA_RECONCILE_COMMIT_RESULT_SCHEMA",
    "QUOTA_RECONCILIATION_REPORT_SCHEMA",
    "DEFAULT_TIMESTAMP_TOLERANCE_SECONDS",
    "scan_quota_reconciliation",
    "commit_quota_reconciliation",
]


def _runtime_root_value(runtime_root: Path | str) -> str:
    return str(Path(runtime_root).expanduser().resolve())


def scan_quota_reconciliation(
    runtime_root: Path | str,
    *,
    goal_ids: list[str] | None = None,
    tolerance_seconds: int = DEFAULT_TIMESTAMP_TOLERANCE_SECONDS,
    window_hours_by_goal: dict[str, int] | None = None,
    now: str | None = None,
    generated_at: str,
) -> dict[str, Any]:
    """Run the read-only reconciliation scan via the typed runtime.

    No lock is taken and nothing is written.
    """

    params: dict[str, Any] = {
        "schema_version": QUOTA_RECONCILE_SCAN_REQUEST_SCHEMA,
        "runtime_root": _runtime_root_value(runtime_root),
        "goal_ids": goal_ids,
        "timestamp_tolerance_seconds": int(tolerance_seconds),
        "window_hours_by_goal": window_hours_by_goal or None,
        "generated_at": generated_at,
    }
    if now:
        params["now"] = now
    try:
        result = effect_runtime_result("quota.reconcile.scan", params)
    except EffectRuntimeRejected as exc:
        raise ValueError(str(exc)) from None
    if not isinstance(result, Mapping):
        raise RuntimeError("TypeScript quota reconciliation scan result shape mismatch")
    if result.get("schema_version") != QUOTA_RECONCILE_SCAN_RESULT_SCHEMA:
        raise RuntimeError("TypeScript quota reconciliation scan result shape mismatch")
    payload = result.get("payload")
    if not isinstance(payload, dict):
        raise RuntimeError("TypeScript quota reconciliation scan result shape mismatch")
    if payload.get("schema_version") != QUOTA_RECONCILIATION_REPORT_SCHEMA:
        raise RuntimeError("TypeScript quota reconciliation report shape mismatch")
    return payload


def commit_quota_reconciliation(
    runtime_root: Path | str,
    goal_id: str,
    discrepancy_ids: list[str],
    *,
    execute: bool,
    expected_index_digest: str | None,
    generated_at: str,
    tolerance_seconds: int = DEFAULT_TIMESTAMP_TOLERANCE_SECONDS,
) -> dict[str, Any]:
    """Commit one per-goal reconciliation batch.

    Execution holds the kernel index lock so legacy Python run writers and
    other reconciliation batches serialize against the same ``index.jsonl``.
    """

    runtime_root_path = Path(runtime_root).expanduser().resolve()
    index_path = runtime_root_path / "goals" / goal_id / "runs" / "index.jsonl"
    params: dict[str, Any] = {
        "schema_version": QUOTA_RECONCILE_COMMIT_REQUEST_SCHEMA,
        "runtime_root": str(runtime_root_path),
        "goal_id": goal_id,
        "generated_at": generated_at,
        "execute": bool(execute),
        "expected_index_digest": expected_index_digest,
        "timestamp_tolerance_seconds": int(tolerance_seconds),
        "items": [
            {"discrepancy_id": discrepancy_id} for discrepancy_id in discrepancy_ids
        ],
    }

    def _request() -> dict[str, Any]:
        try:
            result = effect_runtime_result("quota.reconcile.commit", params)
        except EffectRuntimeRejected as exc:
            raise ValueError(str(exc)) from None
        if not isinstance(result, dict):
            raise RuntimeError(
                "TypeScript quota reconciliation commit result shape mismatch"
            )
        if result.get("schema_version") != QUOTA_RECONCILE_COMMIT_RESULT_SCHEMA:
            raise RuntimeError(
                "TypeScript quota reconciliation commit result shape mismatch"
            )
        return result

    if execute:
        with exclusive_file_lock(index_path, operation="quota_reconcile_commit"):
            return _request()
    return _request()
