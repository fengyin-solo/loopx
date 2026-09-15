"""End-to-end tests for the quota reconciliation feature.

These tests exercise the full Python facade and the managed TypeScript
effect runtime: real run indexes, real rollout receipt logs, durable
correction artifacts, and crash recovery.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from loopx.control_plane.quota.slot_accounting import (
    load_quota_event_from_run,
)
from loopx.control_plane.quota.reconcile import (
    QUOTA_RECONCILE_COMMIT_RESULT_SCHEMA,
    QUOTA_RECONCILE_SCAN_RESULT_SCHEMA,
    commit_quota_reconciliation,
    scan_quota_reconciliation,
)
from loopx.quota import goal_quota_with_spend_ledger, reconcile_quota

REPO_ROOT = Path(__file__).resolve().parents[2]

DUP_GOAL = "goal-duplicate"
MISSING_VOID_GOAL = "goal-missing-void"
REIMB_GOAL = "goal-reimbursement"
DRIFT_GOAL = "goal-drift"
CLEAN_GOAL = "goal-clean"
ALL_GOALS = [
    DUP_GOAL,
    MISSING_VOID_GOAL,
    REIMB_GOAL,
    DRIFT_GOAL,
    CLEAN_GOAL,
]


def _iso(minutes_ago: float = 0.0, *, seconds: float = 0.0) -> str:
    moment = datetime.now(timezone.utc) - timedelta(
        minutes=minutes_ago, seconds=seconds
    )
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


@pytest.fixture()
def runtime_root(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    for goal_id in ALL_GOALS:
        (root / "goals" / goal_id / "runs").mkdir(parents=True, exist_ok=True)
    return root


def _runs_dir(runtime_root: Path, goal_id: str) -> Path:
    return runtime_root / "goals" / goal_id / "runs"


def _append_index(runtime_root: Path, goal_id: str, record: dict[str, Any]) -> None:
    path = _runs_dir(runtime_root, goal_id) / "index.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _write_spend(
    runtime_root: Path,
    goal_id: str,
    generated_at: str,
    slots: int,
    *,
    effect_id: str | None = None,
    turn_instance_id: str | None = None,
    todo_id: str | None = None,
) -> None:
    event: dict[str, Any] = {
        "event_type": "quota_slot_spent",
        "source": "heartbeat",
        "slots": slots,
        "turn_instance_id": turn_instance_id,
        "todo_id": todo_id,
        "settlement_identity": {"effect_id": effect_id} if effect_id else None,
    }
    record: dict[str, Any] = {
        "generated_at": generated_at,
        "goal_id": goal_id,
        "classification": "quota_slot_spent",
        "quota_event": event,
    }
    if turn_instance_id:
        record["turn_instance_id"] = turn_instance_id
    _append_index(runtime_root, goal_id, record)


def _write_void(
    runtime_root: Path,
    goal_id: str,
    generated_at: str,
    target_generated_at: str,
    slots: int,
) -> None:
    _append_index(
        runtime_root,
        goal_id,
        {
            "generated_at": generated_at,
            "goal_id": goal_id,
            "classification": "quota_slot_voided",
            "quota_event": {
                "event_type": "quota_slot_voided",
                "source": "heartbeat",
                "slots": slots,
                "voided_run_generated_at": target_generated_at,
            },
        },
    )


def _append_rollout(
    runtime_root: Path,
    goal_id: str,
    event: dict[str, Any],
) -> None:
    path = runtime_root / "goals" / goal_id / "rollout-event-log.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")


def _write_spend_receipt(
    runtime_root: Path,
    goal_id: str,
    event_id: str,
    effect_id: str,
    slots: int,
    recorded_at: str,
) -> None:
    _append_rollout(
        runtime_root,
        goal_id,
        {
            "schema_version": "loopx_rollout_event_v0",
            "goal_id": goal_id,
            "event_kind": "quota_spend",
            "event_id": event_id,
            "recorded_at": recorded_at,
            "run_id": f"turn-{event_id}",
            "details": {
                "ok": True,
                "appended": True,
                "slots": slots,
                "settlement_effect_id": effect_id,
            },
        },
    )


def _write_void_receipt(
    runtime_root: Path,
    goal_id: str,
    event_id: str,
    target_generated_at: str,
    slots: int,
    recorded_at: str,
) -> None:
    _append_rollout(
        runtime_root,
        goal_id,
        {
            "schema_version": "loopx_rollout_event_v0",
            "goal_id": goal_id,
            "event_kind": "quota_void",
            "event_id": event_id,
            "recorded_at": recorded_at,
            "details": {
                "ok": True,
                "appended": True,
                "slots": slots,
                "voided_run_generated_at": target_generated_at,
            },
        },
    )


def _seed_discrepancies(runtime_root: Path) -> None:
    # Duplicate billing: one settlement effect id with two spend events.
    dup_t1, dup_t2 = _iso(20), _iso(19)
    _write_spend(runtime_root, DUP_GOAL, dup_t1, 1, effect_id="effect-duplicate")
    _write_spend(runtime_root, DUP_GOAL, dup_t2, 1, effect_id="effect-duplicate")

    # Missing void: receipt says the spend was voided, but no void event.
    missing_void_t = _iso(18)
    _write_spend(runtime_root, MISSING_VOID_GOAL, missing_void_t, 1)
    _write_void_receipt(
        runtime_root,
        MISSING_VOID_GOAL,
        "receipt-missing-void",
        missing_void_t,
        1,
        _iso(17),
    )

    # Reimbursement without consumption: receipt exists, spend does not.
    _write_spend_receipt(
        runtime_root,
        REIMB_GOAL,
        "receipt-backfill",
        "effect-backfill",
        2,
        _iso(16),
    )

    # Timestamp drift: void points 30 seconds away from its spend run.
    drift_spend_t = _iso(15)
    drift_target = (
        (
            datetime.fromisoformat(drift_spend_t.replace("Z", "+00:00"))
            + timedelta(seconds=30)
        )
        .isoformat()
        .replace("+00:00", "Z")
    )
    _write_spend(runtime_root, DRIFT_GOAL, drift_spend_t, 1)
    _write_void(runtime_root, DRIFT_GOAL, _iso(14), drift_target, 1)

    # Clean goal: one spend with a matching receipt.
    clean_t = _iso(10)
    _write_spend(runtime_root, CLEAN_GOAL, clean_t, 1, effect_id="effect-clean")
    _write_spend_receipt(
        runtime_root,
        CLEAN_GOAL,
        "receipt-clean",
        "effect-clean",
        1,
        clean_t,
    )


def _scan(runtime_root: Path, **kwargs: Any) -> dict[str, Any]:
    return scan_quota_reconciliation(
        runtime_root,
        goal_ids=kwargs.pop("goal_ids", ALL_GOALS),
        generated_at=_iso(),
        **kwargs,
    )


def _index_lines(runtime_root: Path, goal_id: str) -> list[str]:
    path = _runs_dir(runtime_root, goal_id) / "index.jsonl"
    if not path.exists():
        return []
    return [
        line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _load_runs(runtime_root: Path, goal_id: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in _index_lines(runtime_root, goal_id)]


def _kind_counts(report: dict[str, Any]) -> dict[str, int]:
    return dict(report["summary"]["by_kind"])


def test_scan_classifies_all_four_discrepancy_kinds(runtime_root: Path) -> None:
    _seed_discrepancies(runtime_root)

    result_envelope = {
        "schema_version": QUOTA_RECONCILE_SCAN_RESULT_SCHEMA,
    }
    assert result_envelope["schema_version"]
    report = _scan(runtime_root)
    assert report["schema_version"] == "quota_reconciliation_report_v0"
    assert report["dry_run"] is True
    assert report["appended"] is False
    assert _kind_counts(report) == {
        "duplicate_billing": 1,
        "missing_void": 1,
        "reimbursement_without_consumption": 1,
        "timestamp_drift": 1,
    }
    assert report["summary"]["goals_scanned"] == 5
    assert report["summary"]["fixable"] == 4
    assert report["summary"]["affected_quota_entry_count"] == 4
    assert report["summary"]["would_deduct_slots"] == 3
    assert report["summary"]["would_backfill_slots"] == 2
    assert report["goals_clean"] == [CLEAN_GOAL]

    by_kind = {item["kind"]: item for item in report["discrepancies"]}
    assert by_kind["duplicate_billing"]["correction"]["action"] == "append_void"
    assert by_kind["missing_void"]["correction"]["action"] == "append_void"
    assert (
        by_kind["reimbursement_without_consumption"]["correction"]["action"]
        == "append_spend"
    )
    drift = by_kind["timestamp_drift"]
    assert drift["evidence"]["delta_seconds"] in {30, -30}
    assert drift["correction"]["target_run_generated_at"]


def test_tight_tolerance_turns_drift_into_diagnostic(
    runtime_root: Path,
) -> None:
    _seed_discrepancies(runtime_root)

    report = _scan(runtime_root, tolerance_seconds=10)
    assert _kind_counts(report)["timestamp_drift"] == 0
    diagnostic_kinds = [item["kind"] for item in report["diagnostics"]]
    assert "orphan_void_event" in diagnostic_kinds


def test_scan_is_read_only(runtime_root: Path) -> None:
    _seed_discrepancies(runtime_root)
    snapshot = {
        goal_id: {
            "lines": list(_index_lines(runtime_root, goal_id)),
        }
        for goal_id in ALL_GOALS
    }

    for _ in range(2):
        report = _scan(runtime_root)
        assert report["appended"] is False

    for goal_id in ALL_GOALS:
        assert _index_lines(runtime_root, goal_id) == snapshot[goal_id]["lines"]
    # No reconciliation artifacts were created in preview mode.
    transactions_root = runtime_root / "goals" / DUP_GOAL / "runs" / ".transactions"
    if transactions_root.exists():
        assert "quota-reconcile" not in {
            path.name for path in transactions_root.iterdir()
        }


def test_apply_fixes_all_goals_and_is_idempotent(runtime_root: Path) -> None:
    _seed_discrepancies(runtime_root)

    before = {
        goal_id: len(_index_lines(runtime_root, goal_id)) for goal_id in ALL_GOALS
    }

    applied = reconcile_quota({"runtime_root": str(runtime_root)}, execute=True)
    assert applied["dry_run"] is False
    assert applied["executed"] is True
    assert applied["appended"] is True
    assert applied["summary"]["total_discrepancies"] == 0
    assert {batch["status"] for batch in applied["applied_batches"]} == {"applied"}

    # Each discrepancy goal received exactly one correction event.
    assert len(_index_lines(runtime_root, DUP_GOAL)) == before[DUP_GOAL] + 1
    assert (
        len(_index_lines(runtime_root, MISSING_VOID_GOAL))
        == before[MISSING_VOID_GOAL] + 1
    )
    assert len(_index_lines(runtime_root, REIMB_GOAL)) == before[REIMB_GOAL] + 1
    assert len(_index_lines(runtime_root, DRIFT_GOAL)) == before[DRIFT_GOAL] + 1
    assert len(_index_lines(runtime_root, CLEAN_GOAL)) == before[CLEAN_GOAL]

    # A second execution appends nothing and reports no discrepancies.
    second = reconcile_quota({"runtime_root": str(runtime_root)}, execute=True)
    assert second["appended"] is False
    assert second["applied_batches"] == []
    assert second["summary"]["total_discrepancies"] == 0
    for goal_id in ALL_GOALS:
        assert len(_index_lines(runtime_root, goal_id)) == (
            before[goal_id] + (0 if goal_id == CLEAN_GOAL else 1)
        )


def test_corrections_repair_the_window_ledger_net(runtime_root: Path) -> None:
    _seed_discrepancies(runtime_root)

    def net_spent(goal_id: str) -> int:
        return int(
            goal_quota_with_spend_ledger(
                {"id": goal_id, "quota": {"window_hours": 24}},
                _load_runs(runtime_root, goal_id),
            )["spent_slots"]
        )

    assert net_spent(DUP_GOAL) == 2
    assert net_spent(REIMB_GOAL) == 0
    assert net_spent(DRIFT_GOAL) == 1
    assert net_spent(CLEAN_GOAL) == 1

    reconcile_quota({"runtime_root": str(runtime_root)}, execute=True)

    assert net_spent(DUP_GOAL) == 1
    assert net_spent(REIMB_GOAL) == 2
    assert net_spent(DRIFT_GOAL) == 0
    assert net_spent(CLEAN_GOAL) == 1


def test_correction_events_carry_typed_provenance(runtime_root: Path) -> None:
    _seed_discrepancies(runtime_root)

    reconcile_quota({"runtime_root": str(runtime_root)}, execute=True)

    corrected = 0
    for goal_id in ALL_GOALS:
        for run in _load_runs(runtime_root, goal_id):
            event = load_quota_event_from_run(run) or {}
            block = event.get("reconciliation")
            if not block:
                continue
            assert block["schema_version"] == "quota_reconciliation_correction_v0"
            assert block["discrepancy_id"]
            assert str(block["effect_id"]).startswith("quota-reconcile:")
            corrected += 1
    assert corrected == 4


def test_interrupted_prepared_batch_repairs_without_duplicates(
    runtime_root: Path,
) -> None:
    _seed_discrepancies(runtime_root)
    first_scan = _scan(runtime_root, goal_ids=[DUP_GOAL])
    discrepancy_ids = [
        item["discrepancy_id"]
        for item in first_scan["discrepancies"]
        if item["fixable"]
    ]
    first = reconcile_quota(
        {"runtime_root": str(runtime_root)}, execute=True, goal_id=DUP_GOAL
    )
    assert first["appended"] is True
    lines_after_first = _index_lines(runtime_root, DUP_GOAL)

    # Simulate a crash with a prepared receipt, missing artifacts, and a
    # half-appended final index line.
    tx_dir = _runs_dir(runtime_root, DUP_GOAL) / ".transactions"
    receipt_paths = list((tx_dir / "quota-reconcile-void").glob("*.json"))
    assert len(receipt_paths) == 1
    receipt_path = receipt_paths[0]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "committed"
    receipt["status"] = "prepared"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    Path(receipt["json_path"]).unlink()
    Path(receipt["markdown_path"]).unlink()
    index_path = _runs_dir(runtime_root, DUP_GOAL) / "index.jsonl"
    raw = index_path.read_text(encoding="utf-8")
    last_line = raw.splitlines(keepends=True)[-1]
    prefix = raw[: -len(last_line)]
    index_path.write_text(
        prefix + last_line[: len(last_line) // 2],
        encoding="utf-8",
    )

    # Recovery goes through the commit effect directly: the scan cannot parse
    # a half-written index line, but the stored receipt can repair it.
    retried = commit_quota_reconciliation(
        runtime_root,
        DUP_GOAL,
        discrepancy_ids,
        execute=True,
        expected_index_digest=None,
        generated_at=_iso(),
    )
    assert retried["status"] == "applied"
    assert any(item["status"] == "repaired" for item in retried["items"])

    # Exactly one correction void exists; no duplicate deduction.
    repaired_lines = _index_lines(runtime_root, DUP_GOAL)
    assert len(repaired_lines) == len(lines_after_first)
    void_lines = [
        line
        for line in repaired_lines
        if json.loads(line)["classification"] == "quota_slot_voided"
    ]
    assert len(void_lines) == 1
    assert (
        _scan(runtime_root, goal_ids=[DUP_GOAL])["summary"]["total_discrepancies"] == 0
    )


def test_stale_index_digest_conflicts(runtime_root: Path) -> None:
    _seed_discrepancies(runtime_root)
    report = _scan(runtime_root, goal_ids=[DUP_GOAL])
    discrepancy_ids = [
        item["discrepancy_id"] for item in report["discrepancies"] if item["fixable"]
    ]
    result = commit_quota_reconciliation(
        runtime_root,
        DUP_GOAL,
        discrepancy_ids,
        execute=True,
        expected_index_digest="sha256:0000000000000000",
        generated_at=_iso(),
    )
    assert result["schema_version"] == QUOTA_RECONCILE_COMMIT_RESULT_SCHEMA
    assert result["status"] == "conflict"
    assert result["reason_code"] == "index_digest_conflict"
    # Nothing was appended.
    assert len(_index_lines(runtime_root, DUP_GOAL)) == 2


def _write_cli_registry(root: Path, runtime_root: Path, goal_id: str) -> Path:
    project = root / "project"
    registry_path = project / ".loopx" / "registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "common_runtime_root": str(runtime_root),
                "goals": [
                    {
                        "id": goal_id,
                        "domain": "reconcile-cli-fixture",
                        "status": "active",
                        "repo": str(project),
                        "adapter": {
                            "kind": "read_only_project_map_v0",
                            "status": "connected-read-only",
                        },
                        "quota": {"compute": 1.0, "window_hours": 24},
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return registry_path


def _run_cli(
    registry_path: Path, runtime_root: Path, *args: str
) -> tuple[int, str, str]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "loopx.cli",
            "--registry",
            str(registry_path),
            "--runtime-root",
            str(runtime_root),
            *args,
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    return result.returncode, result.stdout, result.stderr


def test_quota_reconcile_cli_end_to_end(runtime_root: Path, tmp_path: Path) -> None:
    _seed_discrepancies(runtime_root)
    registry_path = _write_cli_registry(tmp_path, runtime_root, DUP_GOAL)

    code, stdout, stderr = _run_cli(
        registry_path,
        runtime_root,
        "--format",
        "json",
        "quota",
        "reconcile",
        "--goal-id",
        DUP_GOAL,
    )
    assert code == 0, stderr
    dry_report = json.loads(stdout)
    assert dry_report["dry_run"] is True
    assert dry_report["summary"]["by_kind"]["duplicate_billing"] == 1

    code, stdout, stderr = _run_cli(
        registry_path,
        runtime_root,
        "--format",
        "json",
        "quota",
        "reconcile",
        "--goal-id",
        DUP_GOAL,
        "--execute",
    )
    assert code == 0, stderr
    applied = json.loads(stdout)
    assert applied["executed"] is True
    assert applied["appended"] is True
    assert applied["summary"]["total_discrepancies"] == 0

    code, stdout, stderr = _run_cli(
        registry_path,
        runtime_root,
        "quota",
        "reconcile",
        "--goal-id",
        DUP_GOAL,
    )
    assert code == 0, stderr
    assert "LoopX Quota Reconciliation" in stdout
    assert "Duplicate billing=0" in stdout
