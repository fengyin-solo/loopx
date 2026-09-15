"""Unit tests for dreaming proposal de-duplication, conflicts, and lifecycle.

These tests pin the durable semantics of the local dreaming proposal ledger:
equivalent proposals merge with retained sources, sliding evidence windows
merge, conflicting proposals are pairwise annotated, configurable priority
orders clear sides while ties stay with a human, deferred proposals reopen on
expiry, decided windows suppress re-production, decisions are append-only, and
replaying a batch is idempotent.
"""

from __future__ import annotations

import pytest

from loopx.dreaming_proposals import (
    CONFLICT_RESOLUTION_PENDING_HUMAN,
    CONFLICT_RESOLUTION_PRIORITY,
    PROPOSAL_STATUS_APPROVED,
    PROPOSAL_STATUS_DEFERRED,
    PROPOSAL_STATUS_PENDING,
    PROPOSAL_STATUS_REJECTED,
    apply_proposal_lifecycle,
    candidate_proposals,
    canonical_proposal_id,
    ingest_candidates,
    legacy_proposal_type,
    load_dreaming_policy,
    new_ledger,
    record_entry_decision,
    resolve_defer_expires_at,
)
from loopx.dreaming import _proposal_id, _proposal_type

GOAL_ID = "dreaming-ledger-unit-fixture"
T0 = "2026-03-01T00:00:00Z"
T1 = "2026-03-01T01:00:00Z"
T2 = "2026-03-02T00:00:00Z"


def run(ts: str, classification: str, action: str) -> dict:
    return {
        "generated_at": ts,
        "classification": classification,
        "recommended_action": action,
        "delivery_outcome": "outcome_progress",
    }


REFACTOR_RUNS = [
    run("2026-03-01T00:03:00Z", "docs_refactor_warning", "Repeated refactor and drift warning."),
    run("2026-03-01T00:02:00Z", "state_drift_warning", "Duplicate handling bloats the state seam."),
    run("2026-03-01T00:01:00Z", "docs_refactor_warning", "Another refactor warning about the monolith."),
]
MEMORY_RUNS = [
    run("2026-03-01T00:04:00Z", "retro_lesson", "Capture the lesson in the project playbook and skill docs."),
]
ARCHIVE_RUNS = [
    run("2026-03-01T00:05:00Z", "stale_work_review", "This obsolete work is stale and should be archived."),
]
EXPLORE_RUNS = [
    run("2026-03-01T00:06:00Z", "delivery_option", "Explore an alternative lane and investigate new options."),
]
GENERIC_RUNS = [
    run("2026-03-01T00:07:00Z", "non_neutral_signal", "A delivery signal without any typed token."),
]


def candidates_for(runs, *, window="last_batch_non_neutral_runs"):
    return candidate_proposals(GOAL_ID, runs, evidence_window=window)


def by_type(candidates):
    return {candidate["proposal_type"]: candidate for candidate in candidates}


def ingest(ledger, candidates, *, policy=None, now_iso=T1):
    return ingest_candidates(
        ledger,
        GOAL_ID,
        candidates,
        policy=policy or default_policy(),
        now_iso=now_iso,
    )


def default_policy():
    return {
        "schema_version": "dreaming_policy_v0",
        "conflict_priority": {},
        "prefer_stronger_evidence": True,
        "default_defer_ttl_hours": 168.0,
    }


def entries_by_type(ledger):
    return {entry["proposal_type"]: entry for entry in ledger["proposals"]}


# ---------------------------------------------------------------------------
# Legacy classification and stable identity
# ---------------------------------------------------------------------------


def test_legacy_proposal_type_precedence_unchanged():
    assert _proposal_type(REFACTOR_RUNS) == legacy_proposal_type(REFACTOR_RUNS) == "refactor_warning"
    assert _proposal_type(MEMORY_RUNS) == "memory_consolidation"
    assert _proposal_type(ARCHIVE_RUNS) == "archive_suggestion"
    assert _proposal_type(GENERIC_RUNS) == "exploration"
    # First-match precedence beats later token groups.
    mixed = [run("2026-03-01T00:00:00Z", "x", "refactor drift then an archive lesson")]
    assert _proposal_type(mixed) == "refactor_warning"


def test_canonical_id_stable_for_same_evidence():
    candidates_a = candidates_for(REFACTOR_RUNS, window="last_3_non_neutral_runs")
    candidates_b = candidates_for(REFACTOR_RUNS, window="last_3_non_neutral_runs")
    assert candidates_a[0]["proposal_id"] == candidates_b[0]["proposal_id"]
    assert candidates_a[0]["proposal_id"] == canonical_proposal_id(
        goal_id=GOAL_ID,
        proposal_type="refactor_warning",
        evidence_run_keys=candidates_a[0]["evidence_run_keys"],
    )
    # The legacy dry-run hash is intentionally a different surface; both stay
    # dreaming-prefixed stable ids.
    assert _proposal_id(
        goal_id=GOAL_ID,
        proposal_type="refactor_warning",
        evidence_window="last_3_non_neutral_runs",
        runs=REFACTOR_RUNS,
    ).startswith("dreaming_")


# ---------------------------------------------------------------------------
# 1. Equivalent proposals merge with retained sources
# ---------------------------------------------------------------------------


def test_equivalent_batch_replay_is_idempotent_and_retains_sources():
    candidates = candidates_for(MEMORY_RUNS)
    ledger, report, changed = ingest(None, candidates, now_iso=T0)
    assert changed is True
    assert report["new"] == 1
    entry = ledger["proposals"][0]
    assert entry["status"] == PROPOSAL_STATUS_PENDING
    assert len(entry["merged_from"]) == 1

    ledger_two, report_two, changed_two = ingest(ledger, candidates, now_iso=T1)
    assert changed_two is False
    assert report_two["new"] == 0
    assert report_two["merged"] == 0
    assert ledger_two == ledger
    assert len(ledger_two["proposals"][0]["merged_from"]) == 1


# ---------------------------------------------------------------------------
# 2. Covering evidence windows merge into the surviving proposal
# ---------------------------------------------------------------------------


def test_covering_window_growth_merges_and_keeps_surviving_id():
    first = candidates_for(MEMORY_RUNS)
    ledger, _, _ = ingest(None, first, now_iso=T0)
    surviving_id = ledger["proposals"][0]["proposal_id"]

    grown_memory = MEMORY_RUNS + [
        run("2026-03-01T00:08:00Z", "retro_lesson", "Another lesson worth keeping in the skill docs.")
    ]
    grown = candidates_for(grown_memory)
    memory_candidate = by_type(grown)["memory_consolidation"]
    # Sliding-window growth: same-type evidence union merges into the
    # already-open proposal instead of creating a duplicate.
    ledger, report, changed = ingest(ledger, [memory_candidate], now_iso=T1)
    assert changed is True
    assert report["merged"] == 1
    assert len(ledger["proposals"]) == 1
    entry = ledger["proposals"][0]
    assert entry["proposal_id"] == surviving_id
    assert len(entry["merged_from"]) == 2
    assert {source["proposal_id"] for source in entry["merged_from"]} == {
        memory_candidate["proposal_id"],
        surviving_id,
    }


def test_subset_window_after_decision_is_suppressed_but_broader_is_new():
    candidates = candidates_for(REFACTOR_RUNS)
    ledger, _, _ = ingest(None, candidates, now_iso=T0)
    entry = ledger["proposals"][0]
    record_entry_decision(
        entry,
        "reject",
        reason_summary="not useful for this boundary",
        decided_at=T1,
    )
    assert entry["status"] == PROPOSAL_STATUS_REJECTED

    subset = candidates_for(REFACTOR_RUNS[:1])
    ledger, report, changed = ingest(ledger, subset, now_iso=T1)
    assert changed is False
    assert report["suppressed_decided"] == 1
    assert report["new"] == 0
    assert len(ledger["proposals"]) == 1

    broader_runs = REFACTOR_RUNS + [
        run("2026-03-03T00:00:00Z", "new_drift_warning", "Fresh refactor drift in a new seam.")
    ]
    broader = candidates_for(broader_runs)
    ledger, report, changed = ingest(ledger, broader, now_iso=T2)
    assert report["new"] == 1
    assert changed is True
    assert len(ledger["proposals"]) == 2


# ---------------------------------------------------------------------------
# 3. Conflicting proposals are pairwise annotated
# ---------------------------------------------------------------------------


def test_scope_conflict_pair_annotation_with_basis():
    mixed = REFACTOR_RUNS + EXPLORE_RUNS
    candidates = candidates_for(mixed)
    ledger, report, _ = ingest(None, candidates, now_iso=T0)
    assert len(report["conflicts"]) == 1
    conflict = report["conflicts"][0]
    assert conflict["conflict_type"] == "scope_direction"
    assert conflict["resolution"] == CONFLICT_RESOLUTION_PRIORITY
    # Three refactor runs outrank one exploration run on evidence strength.
    refactor = by_type(candidates)["refactor_warning"]
    assert conflict["preferred_proposal_id"] == refactor["proposal_id"]
    assert conflict["priority_rule"] == (
        "evidence_strength:more_supporting_runs_with_no_lower_confidence"
    )
    basis = {item["proposal_id"]: item for item in conflict["basis"]}
    assert basis[refactor["proposal_id"]]["evidence_count"] == 3
    sides = {side["side"]: side["proposal_id"] for side in conflict["sides"]}
    assert set(sides) == {"converge", "expand"}
    for entry in ledger["proposals"]:
        assert len(entry["conflicts"]) == 1


def test_retention_axis_conflicts_between_consolidation_and_archive():
    candidates = candidates_for(MEMORY_RUNS + ARCHIVE_RUNS)
    types_ = by_type(candidates)
    assert set(types_) == {"memory_consolidation", "archive_suggestion"}
    ledger, report, _ = ingest(None, candidates, now_iso=T0)
    assert len(report["conflicts"]) == 1
    conflict = report["conflicts"][0]
    assert conflict["conflict_type"] == "retention"
    # One run each, equal confidence: strength cannot decide, human must.
    assert conflict["resolution"] == CONFLICT_RESOLUTION_PENDING_HUMAN
    assert conflict["preferred_proposal_id"] is None


def test_disjoint_evidence_windows_do_not_conflict():
    first = candidates_for(REFACTOR_RUNS)
    ledger, _, _ = ingest(None, first, now_iso=T0)
    later_explore = candidates_for(
        [run("2026-04-01T00:00:00Z", "delivery_option", "Explore a fresh alternative lane.")],
        window="last_1_non_neutral_runs",
    )
    _, report, _ = ingest(ledger, later_explore, now_iso=T2)
    assert report["conflicts"] == []


# ---------------------------------------------------------------------------
# 4. Configured priority and pending-human fallback
# ---------------------------------------------------------------------------


def test_configured_priority_preference_wins_over_tie():
    candidates = candidates_for(MEMORY_RUNS + ARCHIVE_RUNS)
    policy = default_policy()
    policy["conflict_priority"] = {"retention": "consolidate"}
    ledger, report, _ = ingest(None, candidates, policy=policy, now_iso=T0)
    conflict = report["conflicts"][0]
    assert conflict["resolution"] == CONFLICT_RESOLUTION_PRIORITY
    memory = by_type(candidates)["memory_consolidation"]
    assert conflict["preferred_proposal_id"] == memory["proposal_id"]
    assert conflict["priority_rule"] == "configured_priority:retention=consolidate"


def test_equal_strength_without_configuration_stays_pending_human():
    candidates = candidates_for(
        [
            run("2026-03-01T00:01:00Z", "drift", "refactor warning"),
            run("2026-03-01T00:02:00Z", "option", "explore alternative"),
        ]
    )
    policy = default_policy()
    _, report, _ = ingest(None, candidates, policy=policy, now_iso=T0)
    assert report["pending_human"] == 1
    assert report["conflicts"][0]["resolution"] == CONFLICT_RESOLUTION_PENDING_HUMAN


def test_policy_validation_rejects_unknown_axis_and_side(tmp_path):
    policy_file = tmp_path / "dreaming-policy.json"
    import json

    policy_file.write_text(
        json.dumps({"conflict_priority": {"unknown_axis": "converge"}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown conflict type"):
        load_dreaming_policy(tmp_path / "registry.json", policy_file)


# ---------------------------------------------------------------------------
# 5. Deferred proposals reopen on expiry
# ---------------------------------------------------------------------------


def test_defer_expiry_reopens_and_appends_history():
    candidates = candidates_for(MEMORY_RUNS)
    ledger, _, _ = ingest(None, candidates, now_iso=T0)
    entry = ledger["proposals"][0]
    expires_at = resolve_defer_expires_at(
        default_policy(),
        defer_until=None,
        defer_ttl_hours=24,
        now_iso=T0,
    )
    record_entry_decision(
        entry,
        "defer",
        reason_summary="wait for a stronger boundary",
        decided_at=T0,
        expires_at=expires_at,
    )
    assert entry["status"] == PROPOSAL_STATUS_DEFERRED
    assert entry["decision"]["expires_at"].endswith("Z")

    # Still deferred before expiry.
    assert apply_proposal_lifecycle(ledger, now_iso=T1.replace("01:00:00Z", "00:30:00Z")) == []
    assert ledger["proposals"][0]["status"] == PROPOSAL_STATUS_DEFERRED

    reopened = apply_proposal_lifecycle(ledger, now_iso=T2)
    assert reopened == [entry["proposal_id"]]
    assert entry["status"] == PROPOSAL_STATUS_PENDING
    assert entry["decision"] is None
    events = entry["decision_history"]
    assert [item["event"] for item in events] == ["operator_decision", "defer_expired_reopen"]
    assert events[-1]["reopened_at"] == T2


def test_active_defer_suppresses_same_window_candidate():
    candidates = candidates_for(MEMORY_RUNS)
    ledger, _, _ = ingest(None, candidates, now_iso=T0)
    entry = ledger["proposals"][0]
    record_entry_decision(
        entry,
        "defer",
        reason_summary="waiting",
        decided_at=T0,
        expires_at=resolve_defer_expires_at(
            default_policy(), defer_until=None, defer_ttl_hours=48, now_iso=T0
        ),
    )
    ledger, report, changed = ingest(ledger, candidates, now_iso=T1)
    assert changed is False
    assert report["suppressed_deferred"] == 1
    assert report["new"] == 0


# ---------------------------------------------------------------------------
# 6/7. Decision history is append-only and terminal decisions are protected
# ---------------------------------------------------------------------------


def test_decision_history_is_append_only_and_terminal_is_protected():
    candidates = candidates_for(MEMORY_RUNS)
    ledger, _, _ = ingest(None, candidates, now_iso=T0)
    entry = ledger["proposals"][0]

    record_entry_decision(
        entry,
        "defer",
        reason_summary="first hold",
        decided_at=T0,
        expires_at=resolve_defer_expires_at(
            default_policy(), defer_until=None, defer_ttl_hours=1, now_iso=T0
        ),
    )
    apply_proposal_lifecycle(ledger, now_iso=T2)
    record_entry_decision(
        entry,
        "approve",
        reason_summary="boundary arrived",
        decided_at=T2,
    )
    assert entry["status"] == PROPOSAL_STATUS_APPROVED
    events = [item["event"] for item in entry["decision_history"]]
    assert events == ["operator_decision", "defer_expired_reopen", "operator_decision"]

    with pytest.raises(ValueError, match="terminal"):
        record_entry_decision(
            entry,
            "reject",
            reason_summary="must not overwrite",
            decided_at=T2,
        )
    assert entry["status"] == PROPOSAL_STATUS_APPROVED
    assert len(entry["decision_history"]) == 3


def test_defer_without_expiry_is_rejected():
    entry = new_ledger(GOAL_ID)["proposals"]
    candidates = candidates_for(MEMORY_RUNS)
    ledger, _, _ = ingest(None, candidates, now_iso=T0)
    with pytest.raises(ValueError, match="expires_at"):
        record_entry_decision(
            ledger["proposals"][0],
            "defer",
            reason_summary="no expiry supplied",
            decided_at=T0,
        )
    assert entry == []
