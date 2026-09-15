"""Persistent ledger for dreaming proposal de-duplication and conflicts.

This module owns the local, per-goal dreaming proposal ledger:

* candidate proposal generation from a batch of signal runs;
* de-duplication / merge of equivalent proposals and overlapping evidence
  windows (merged sources are retained on the surviving entry);
* typed, pairwise conflict annotation with configurable priority and a
  pending-human fallback;
* the operator decision lifecycle (approve / defer with an expiry / reject),
  append-only decision history, and deferred-proposal reopening;
* idempotent ingestion: replaying the same batch never creates a duplicate
  proposal and never overwrites a recorded decision.

The ledger is advisory local runtime state. It never appends delivery
history, mutates active goal truth, grants an agent command, or spends quota.
"""

from __future__ import annotations

import copy
import hashlib
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

from .control_plane.runtime.public_safety import public_safe_compact_text
from .control_plane.runtime.time import (
    now_local_iso,
    parse_timestamp,
    utc_isoformat,
)
from .registry import atomic_write_json, read_json

DREAMING_PROPOSAL_LEDGER_SCHEMA_VERSION = "dreaming_proposal_ledger_v0"
DREAMING_POLICY_SCHEMA_VERSION = "dreaming_policy_v0"
DREAMING_CONSOLIDATION_SCHEMA_VERSION = "dreaming_consolidation_v0"
DREAMING_PROPOSAL_QUEUE_SCHEMA_VERSION = "dreaming_proposal_queue_v0"

MAX_DREAMING_EVIDENCE_ITEMS = 5
MAX_LEDGER_EVIDENCE_ITEMS = 10
MAX_SHARED_EVIDENCE_KEYS = 10

PROPOSAL_STATUS_PENDING = "pending"
PROPOSAL_STATUS_APPROVED = "approved"
PROPOSAL_STATUS_DEFERRED = "deferred"
PROPOSAL_STATUS_REJECTED = "rejected"
TERMINAL_PROPOSAL_STATUSES = frozenset(
    {PROPOSAL_STATUS_APPROVED, PROPOSAL_STATUS_REJECTED}
)
LEDGER_PROPOSAL_STATUSES = frozenset(
    {
        PROPOSAL_STATUS_PENDING,
        PROPOSAL_STATUS_APPROVED,
        PROPOSAL_STATUS_DEFERRED,
        PROPOSAL_STATUS_REJECTED,
    }
)
DECISION_STATUS_MAP = {
    "approve": PROPOSAL_STATUS_APPROVED,
    "defer": PROPOSAL_STATUS_DEFERRED,
    "reject": PROPOSAL_STATUS_REJECTED,
}

# Order doubles as the legacy dry-run precedence: first matching token group
# wins, exploration remains the fallback when nothing matches.
PROPOSAL_TYPE_TOKENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "refactor_warning",
        ("refactor", "duplicate", "bloat", "large", "monolith", "drift"),
    ),
    (
        "memory_consolidation",
        ("lesson", "memory", "playbook", "skill", "docs", "documentation"),
    ),
    (
        "archive_suggestion",
        ("archive", "obsolete", "stale"),
    ),
    (
        "exploration",
        (
            "explore",
            "exploration",
            "exploratory",
            "option",
            "alternative",
            "expand",
            "widen",
            "investigate",
        ),
    ),
)
PROPOSAL_TYPE_ORDER = tuple(name for name, _tokens in PROPOSAL_TYPE_TOKENS)

# Conflict axes. Two active proposals conflict when their types sit on
# different sides of the same axis and their evidence windows overlap.
CONFLICT_AXES: dict[str, dict[str, frozenset[str]]] = {
    "scope_direction": {
        "converge": frozenset(
            {"refactor_warning", "memory_consolidation", "archive_suggestion"}
        ),
        "expand": frozenset({"exploration"}),
    },
    "retention": {
        "consolidate": frozenset({"memory_consolidation"}),
        "archive": frozenset({"archive_suggestion"}),
    },
}
CONFLICT_RESOLUTION_PRIORITY = "priority"
CONFLICT_RESOLUTION_PENDING_HUMAN = "pending_human"

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

POLICY_FILENAME = "dreaming-policy.json"
DEFAULT_DEFERRAL_TTL_HOURS = 168.0
MAX_DEFERRAL_TTL_HOURS = 24.0 * 366


# ---------------------------------------------------------------------------
# Type metadata and run signals
# ---------------------------------------------------------------------------


def classification_for_proposal_type(proposal_type: str) -> str:
    return {
        "refactor_warning": "dreaming_refactor_warning",
        "memory_consolidation": "dreaming_memory_consolidation",
        "archive_suggestion": "dreaming_archive_suggestion",
    }.get(proposal_type, "dreaming_exploration_proposal")


def operator_question_for_proposal_type(goal_id: str, proposal_type: str) -> str:
    if proposal_type == "refactor_warning":
        return (
            f"Should {goal_id} open a reviewed delivery todo for the repeated "
            "refactor or state-drift warning found in recent run history?"
        )
    if proposal_type == "memory_consolidation":
        return (
            f"Should {goal_id} consolidate these repeated lessons into a "
            "project-local playbook, skill, or active-state update?"
        )
    if proposal_type == "archive_suggestion":
        return (
            f"Should {goal_id} review whether stale or obsolete work should be "
            "archived before the next delivery slice?"
        )
    return (
        f"Should {goal_id} promote this exploration proposal into a concrete "
        "delivery todo, defer it, or reject it?"
    )


def proposal_summary_text(runs: list[dict[str, Any]], proposal_type: str) -> str:
    classifications = Counter(
        str(run.get("classification") or "unknown") for run in runs
    )
    top = ", ".join(f"{name} x{count}" for name, count in classifications.most_common(3))
    if not top:
        top = "no recent non-neutral run history"
    if proposal_type == "refactor_warning":
        return f"Recent run history suggests a possible refactor/state-drift warning: {top}."
    if proposal_type == "memory_consolidation":
        return f"Recent run history has repeated lessons worth consolidating: {top}."
    if proposal_type == "archive_suggestion":
        return f"Recent run history may contain stale work that needs archive review: {top}."
    return f"Recent run history suggests an exploration option for operator review: {top}."


def legacy_proposal_type(runs: list[dict[str, Any]]) -> str:
    """Classify one proposal type with the original first-match precedence."""

    compact_signals: list[str] = []
    for run in runs:
        compact = public_safe_compact_text(
            " ".join(
                str(run.get(field) or "")
                for field in (
                    "classification",
                    "recommended_action",
                    "health_check",
                    "delivery_outcome",
                )
            ),
            limit=500,
        )
        if compact:
            compact_signals.append(compact)
    combined = " ".join(compact_signals).lower()
    for proposal_type, tokens in PROPOSAL_TYPE_TOKENS:
        if any(token in combined for token in tokens):
            return proposal_type
    return "exploration"


def compact_run(run: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for field in ("generated_at", "classification"):
        value = public_safe_compact_text(run.get(field), limit=120)
        if value:
            compact[field] = value
    action = public_safe_compact_text(
        run.get("recommended_action") or run.get("summary") or run.get("health_check"),
        limit=220,
    )
    if action:
        compact["recommended_action"] = action
    outcome = public_safe_compact_text(run.get("delivery_outcome"), limit=80)
    if outcome:
        compact["delivery_outcome"] = outcome
    return compact


def _run_signal_text(run: dict[str, Any]) -> str:
    return (
        " ".join(
            str(run.get(field) or "")
            for field in (
                "classification",
                "recommended_action",
                "health_check",
                "delivery_outcome",
            )
        ).lower()
    )


def run_evidence_key(run: dict[str, Any]) -> str:
    generated_at = public_safe_compact_text(run.get("generated_at"), limit=120) or ""
    classification = (
        public_safe_compact_text(run.get("classification"), limit=120) or "unknown"
    )
    return f"{generated_at}|{classification}"


def canonical_proposal_id(
    *,
    goal_id: str,
    proposal_type: str,
    evidence_run_keys: list[str] | set[str] | tuple[str, ...],
) -> str:
    seed = "\n".join([goal_id, proposal_type, *sorted(set(evidence_run_keys))])
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    return f"dreaming_{digest}"


# ---------------------------------------------------------------------------
# Candidate generation (one batch -> zero, one, or several candidates)
# ---------------------------------------------------------------------------


def _build_candidate(
    *,
    goal_id: str,
    proposal_type: str,
    evidence_runs: list[dict[str, Any]],
    window_run_keys: list[str],
    evidence_window: str,
) -> dict[str, Any]:
    evidence_run_keys = sorted({run_evidence_key(run) for run in evidence_runs})
    proposal_id = canonical_proposal_id(
        goal_id=goal_id,
        proposal_type=proposal_type,
        evidence_run_keys=evidence_run_keys,
    )
    return {
        "proposal_id": proposal_id,
        "proposal_type": proposal_type,
        "classification": classification_for_proposal_type(proposal_type),
        "summary": public_safe_compact_text(
            proposal_summary_text(evidence_runs, proposal_type),
            limit=260,
        )
        or "",
        "operator_question": operator_question_for_proposal_type(
            goal_id, proposal_type
        ),
        "evidence_window": evidence_window,
        "evidence_run_keys": evidence_run_keys,
        "window_run_keys": sorted(set(window_run_keys)),
        "recent_evidence": [
            compact_run(run) for run in evidence_runs[:MAX_DREAMING_EVIDENCE_ITEMS]
        ],
        "confidence": "medium" if len(evidence_runs) >= 3 else "low",
    }


def candidate_proposals(
    goal_id: str,
    runs: list[dict[str, Any]],
    *,
    evidence_window: str,
) -> list[dict[str, Any]]:
    """Build de-duplicated candidate proposals for one signal-run batch.

    Each run is assigned to the first matching proposal type in the same
    precedence order as the legacy single-proposal classifier, so a run never
    fans out into several types on overlapping token substrings. Runs without
    any typed signal behave like the legacy exploration fallback. When no type
    matches, a single exploration candidate covers the whole batch.
    """

    if not runs:
        return []

    window_run_keys = sorted({run_evidence_key(run) for run in runs})
    matched_by_type: dict[str, list[dict[str, Any]]] = {}
    unmatched: list[dict[str, Any]] = []
    for run in runs:
        text = _run_signal_text(run)
        first_match = next(
            (
                proposal_type
                for proposal_type, tokens in PROPOSAL_TYPE_TOKENS
                if any(token in text for token in tokens)
            ),
            None,
        )
        if first_match is None:
            unmatched.append(run)
        else:
            matched_by_type.setdefault(first_match, []).append(run)

    candidates: list[dict[str, Any]] = []
    for proposal_type in PROPOSAL_TYPE_ORDER:
        evidence_runs = matched_by_type.get(proposal_type)
        if not evidence_runs:
            continue
        candidates.append(
            _build_candidate(
                goal_id=goal_id,
                proposal_type=proposal_type,
                evidence_runs=evidence_runs,
                window_run_keys=window_run_keys,
                evidence_window=evidence_window,
            )
        )

    if not candidates:
        candidates.append(
            _build_candidate(
                goal_id=goal_id,
                proposal_type="exploration",
                evidence_runs=runs,
                window_run_keys=window_run_keys,
                evidence_window=evidence_window,
            )
        )
    elif unmatched:
        # Generic, non-neutral runs remain unexplored options; surface them as
        # the exploration side of the same batch.
        exploration_runs = matched_by_type.get("exploration")
        if exploration_runs is None:
            candidates.append(
                _build_candidate(
                    goal_id=goal_id,
                    proposal_type="exploration",
                    evidence_runs=unmatched,
                    window_run_keys=window_run_keys,
                    evidence_window=evidence_window,
                )
            )
        else:
            candidates = [
                candidate
                for candidate in candidates
                if candidate["proposal_type"] != "exploration"
            ]
            candidates.append(
                _build_candidate(
                    goal_id=goal_id,
                    proposal_type="exploration",
                    evidence_runs=list(exploration_runs) + unmatched,
                    window_run_keys=window_run_keys,
                    evidence_window=evidence_window,
                )
            )
    return candidates


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def default_policy() -> dict[str, Any]:
    return {
        "schema_version": DREAMING_POLICY_SCHEMA_VERSION,
        "conflict_priority": {},
        "prefer_stronger_evidence": True,
        "default_defer_ttl_hours": DEFAULT_DEFERRAL_TTL_HOURS,
    }


def policy_path_for_registry(registry_path: Path) -> Path:
    return Path(registry_path).parent / POLICY_FILENAME


def _validated_policy(raw: Any, *, source: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"dreaming policy at {source} must be a JSON object")
    allowed = {
        "schema_version",
        "conflict_priority",
        "prefer_stronger_evidence",
        "default_defer_ttl_hours",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(
            f"dreaming policy at {source} has unknown fields: {sorted(unknown)}"
        )
    policy = default_policy()
    version = raw.get("schema_version", DREAMING_POLICY_SCHEMA_VERSION)
    if version != DREAMING_POLICY_SCHEMA_VERSION:
        raise ValueError(
            f"dreaming policy at {source} has unsupported schema_version {version!r}"
        )
    priority = raw.get("conflict_priority", {})
    if not isinstance(priority, dict):
        raise ValueError("dreaming policy conflict_priority must be an object")
    for axis, side in priority.items():
        sides = CONFLICT_AXES.get(str(axis))
        if sides is None:
            raise ValueError(
                f"dreaming policy conflict_priority has unknown conflict type {axis!r}"
            )
        if str(side) not in sides:
            raise ValueError(
                f"dreaming policy conflict_priority[{axis!r}] must name one side: "
                f"{sorted(sides)}"
            )
        policy["conflict_priority"][str(axis)] = str(side)
    prefer = raw.get("prefer_stronger_evidence", True)
    if not isinstance(prefer, bool):
        raise ValueError("dreaming policy prefer_stronger_evidence must be a boolean")
    policy["prefer_stronger_evidence"] = prefer
    ttl = raw.get("default_defer_ttl_hours", DEFAULT_DEFERRAL_TTL_HOURS)
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
        raise ValueError("dreaming policy default_defer_ttl_hours must be a number")
    if not 0 < float(ttl) <= MAX_DEFERRAL_TTL_HOURS:
        raise ValueError(
            "dreaming policy default_defer_ttl_hours must be greater than 0 and "
            f"at most {MAX_DEFERRAL_TTL_HOURS}"
        )
    policy["default_defer_ttl_hours"] = float(ttl)
    return policy


def load_dreaming_policy(
    registry_path: Path,
    policy_file: str | Path | None = None,
) -> dict[str, Any]:
    """Load the optional sidecar policy; fall back to safe defaults."""

    if policy_file:
        path = Path(policy_file)
        if not path.exists():
            raise ValueError(f"dreaming policy file not found: {path}")
    else:
        path = policy_path_for_registry(Path(registry_path))
        if not path.exists():
            return default_policy()
    return _validated_policy(read_json(path), source=path)


def resolve_defer_expires_at(
    policy: dict[str, Any],
    *,
    defer_until: str | None,
    defer_ttl_hours: float | None,
    now_iso: str,
) -> str:
    if defer_until and defer_ttl_hours is not None:
        raise ValueError("--defer-until and --defer-ttl-hours are mutually exclusive")
    base = parse_timestamp(now_iso)
    if base is None:
        raise ValueError("internal clock produced an invalid timestamp")
    if defer_until:
        explicit = parse_timestamp(defer_until)
        if explicit is None:
            raise ValueError("--defer-until must be an ISO 8601 timestamp")
        return utc_isoformat(explicit)
    if defer_ttl_hours is not None:
        if isinstance(defer_ttl_hours, bool) or not isinstance(
            defer_ttl_hours, (int, float)
        ):
            raise ValueError("--defer-ttl-hours must be a number")
        ttl = float(defer_ttl_hours)
    else:
        ttl = float(policy.get("default_defer_ttl_hours", DEFAULT_DEFERRAL_TTL_HOURS))
    if not 0 < ttl <= MAX_DEFERRAL_TTL_HOURS:
        raise ValueError(
            f"defer TTL hours must be greater than 0 and at most {MAX_DEFERRAL_TTL_HOURS}"
        )
    return utc_isoformat(base + timedelta(hours=ttl))


# ---------------------------------------------------------------------------
# Ledger storage
# ---------------------------------------------------------------------------


def new_ledger(goal_id: str, *, now_iso: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": DREAMING_PROPOSAL_LEDGER_SCHEMA_VERSION,
        "goal_id": goal_id,
        "updated_at": now_iso or now_local_iso(),
        "proposals": [],
    }


def ledger_dir(runtime_root: Path, goal_id: str) -> Path:
    return Path(runtime_root) / "goals" / goal_id / "dreaming"


def proposal_ledger_path(runtime_root: Path, goal_id: str) -> Path:
    return ledger_dir(runtime_root, goal_id) / "proposals.json"


def load_proposal_ledger(path: Path) -> dict[str, Any] | None:
    path = Path(path)
    if not path.exists():
        return None
    payload = read_json(path)
    version = payload.get("schema_version")
    if version != DREAMING_PROPOSAL_LEDGER_SCHEMA_VERSION:
        raise ValueError(
            f"dreaming proposal ledger at {path} has unsupported schema_version "
            f"{version!r}; expected {DREAMING_PROPOSAL_LEDGER_SCHEMA_VERSION}"
        )
    if not isinstance(payload.get("proposals"), list):
        raise ValueError(f"dreaming proposal ledger at {path} must list proposals")
    return payload


def save_proposal_ledger(path: Path, ledger: dict[str, Any]) -> None:
    atomic_write_json(Path(path), ledger)


# ---------------------------------------------------------------------------
# Lifecycle (defer expiry) and operator decisions
# ---------------------------------------------------------------------------


def apply_proposal_lifecycle(
    ledger: dict[str, Any],
    *,
    now_iso: str,
) -> list[str]:
    """Reopen expired deferred proposals in-place; return reopened proposal ids."""

    now = parse_timestamp(now_iso)
    if now is None:
        raise ValueError("internal clock produced an invalid timestamp")
    reopened: list[str] = []
    for entry in ledger.get("proposals", []):
        if entry.get("status") != PROPOSAL_STATUS_DEFERRED:
            continue
        decision = entry.get("decision") if isinstance(entry.get("decision"), dict) else {}
        expires_at = decision.get("expires_at")
        expiry = parse_timestamp(expires_at)
        if expiry is None or now < expiry:
            continue
        entry["status"] = PROPOSAL_STATUS_PENDING
        entry["decision"] = None
        entry.setdefault("decision_history", []).append(
            {
                "event": "defer_expired_reopen",
                "decision": "defer",
                "reason_summary": decision.get("reason_summary"),
                "decided_at": decision.get("decided_at"),
                "expires_at": expires_at,
                "reopened_at": now_iso,
            }
        )
        entry["updated_at"] = now_iso
        reopened.append(str(entry.get("proposal_id") or ""))
    return reopened


def record_entry_decision(
    entry: dict[str, Any],
    decision: str,
    *,
    reason_summary: str,
    decided_at: str,
    expires_at: str | None = None,
) -> None:
    """Record an operator decision on one ledger entry.

    Terminal entries cannot be overwritten. History is append-only; reopening
    a deferred proposal is the only way a decision gets superseded, and it is
    itself recorded as a history event.
    """

    current = str(entry.get("status") or "")
    if current in TERMINAL_PROPOSAL_STATUSES:
        raise ValueError(
            f"dreaming proposal {entry.get('proposal_id')} already has a terminal "
            f"decision ({current}); recorded decisions cannot be overwritten"
        )
    if decision not in DECISION_STATUS_MAP:
        raise ValueError(
            "decision must be one of: " + ", ".join(sorted(DECISION_STATUS_MAP))
        )
    if decision == "defer":
        if not expires_at or parse_timestamp(expires_at) is None:
            raise ValueError("defer decision requires a valid expires_at timestamp")

    history_item: dict[str, Any] = {
        "event": "operator_decision",
        "decision": decision,
        "reason_summary": reason_summary,
        "decided_at": decided_at,
    }
    if expires_at:
        history_item["expires_at"] = expires_at
    entry.setdefault("decision_history", []).append(history_item)
    entry["decision"] = {
        "decision": decision,
        "reason_summary": reason_summary,
        "decided_at": decided_at,
        "expires_at": expires_at,
    }
    entry["status"] = DECISION_STATUS_MAP[decision]
    entry["updated_at"] = decided_at


# ---------------------------------------------------------------------------
# Merge / suppression / conflict annotation
# ---------------------------------------------------------------------------


def _source_identity(candidate: dict[str, Any]) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    return (
        str(candidate.get("evidence_window") or ""),
        tuple(sorted(candidate.get("evidence_run_keys") or [])),
        tuple(sorted(candidate.get("window_run_keys") or [])),
    )


def _candidate_source_record(
    candidate: dict[str, Any],
    *,
    generated_at: str,
) -> dict[str, Any]:
    return {
        "proposal_id": candidate["proposal_id"],
        "evidence_window": candidate["evidence_window"],
        "evidence_run_keys": list(candidate.get("evidence_run_keys") or []),
        "window_run_keys": list(candidate.get("window_run_keys") or []),
        "generated_at": generated_at,
    }


def _entry_from_candidate(
    candidate: dict[str, Any],
    *,
    now_iso: str,
) -> dict[str, Any]:
    return {
        "proposal_id": candidate["proposal_id"],
        "proposal_type": candidate["proposal_type"],
        "classification": candidate["classification"],
        "status": PROPOSAL_STATUS_PENDING,
        "summary": candidate["summary"],
        "operator_question": candidate["operator_question"],
        "evidence_window": candidate["evidence_window"],
        "evidence_run_keys": list(candidate["evidence_run_keys"]),
        "window_run_keys": list(candidate["window_run_keys"]),
        "recent_evidence": list(candidate.get("recent_evidence") or []),
        "confidence": candidate.get("confidence", "low"),
        "merged_from": [_candidate_source_record(candidate, generated_at=now_iso)],
        "conflicts": [],
        "decision": None,
        "decision_history": [],
        "created_at": now_iso,
        "updated_at": now_iso,
    }


def _merge_candidate_into_entry(
    entry: dict[str, Any],
    candidate: dict[str, Any],
    *,
    now_iso: str,
) -> bool:
    """Merge an overlapping same-type candidate into an existing entry.

    Returns True when a new source was attached. The surviving proposal id and
    any recorded decision stay untouched.
    """

    merged_from = entry.setdefault("merged_from", [])
    identity = _source_identity(candidate)
    if any(
        (
            str(source.get("evidence_window") or ""),
            tuple(sorted(source.get("evidence_run_keys") or [])),
            tuple(sorted(source.get("window_run_keys") or [])),
        )
        == identity
        for source in merged_from
        if isinstance(source, dict)
    ):
        return False

    evidence_by_key: dict[str, dict[str, Any]] = {}
    for compact in entry.get("recent_evidence") or []:
        if isinstance(compact, dict):
            evidence_by_key.setdefault(run_evidence_key(compact), compact)
    for compact in candidate.get("recent_evidence") or []:
        if isinstance(compact, dict):
            evidence_by_key.setdefault(run_evidence_key(compact), compact)
    evidence_keys = set(entry.get("evidence_run_keys") or []) | set(
        candidate.get("evidence_run_keys") or []
    )
    window_keys = set(entry.get("window_run_keys") or []) | set(
        candidate.get("window_run_keys") or []
    )
    if len(candidate.get("window_run_keys") or []) > len(
        entry.get("window_run_keys") or []
    ):
        entry["evidence_window"] = candidate["evidence_window"]
    entry["evidence_run_keys"] = sorted(evidence_keys)
    entry["window_run_keys"] = sorted(window_keys)
    entry["recent_evidence"] = [
        evidence_by_key[key]
        for key in sorted(evidence_by_key)
        if key in evidence_keys
    ][:MAX_LEDGER_EVIDENCE_ITEMS]
    entry["confidence"] = (
        "medium" if len(entry["evidence_run_keys"]) >= 3 else entry.get("confidence", "low")
    )
    merged_from.append(_candidate_source_record(candidate, generated_at=now_iso))
    entry["updated_at"] = now_iso
    return True


def _axis_sides(
    proposal_type_a: str,
    proposal_type_b: str,
) -> list[tuple[str, str, str]]:
    """Return (axis, side_a, side_b) for every axis the two types oppose on."""

    pairs: list[tuple[str, str, str]] = []
    for axis, sides in CONFLICT_AXES.items():
        side_a = next(
            (side for side, types in sides.items() if proposal_type_a in types),
            None,
        )
        side_b = next(
            (side for side, types in sides.items() if proposal_type_b in types),
            None,
        )
        if side_a and side_b and side_a != side_b:
            pairs.append((axis, side_a, side_b))
    return pairs


def _conflict_identity(
    *,
    axis: str,
    proposal_ids: tuple[str, str],
    shared_keys: set[str],
) -> str:
    seed = "\n".join([axis, *sorted(proposal_ids), *sorted(shared_keys)])
    return f"dreaming_conflict_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:12]}"


def _resolve_conflict(
    *,
    axis: str,
    side_by_proposal: dict[str, str],
    basis_by_proposal: dict[str, dict[str, Any]],
    policy: dict[str, Any],
) -> tuple[str, str | None, str | None]:
    """Return (resolution, preferred_proposal_id, priority_rule)."""

    configured_side = policy.get("conflict_priority", {}).get(axis)
    if configured_side:
        preferred = next(
            (
                proposal_id
                for proposal_id, side in side_by_proposal.items()
                if side == configured_side
            ),
            None,
        )
        if preferred:
            return (
                CONFLICT_RESOLUTION_PRIORITY,
                preferred,
                f"configured_priority:{axis}={configured_side}",
            )
    if policy.get("prefer_stronger_evidence", True):
        ranked = sorted(
            side_by_proposal,
            key=lambda proposal_id: (
                int(basis_by_proposal[proposal_id]["evidence_count"]),
                CONFIDENCE_RANK.get(
                    str(basis_by_proposal[proposal_id].get("confidence") or "low"),
                    0,
                ),
            ),
            reverse=True,
        )
        stronger, weaker = ranked[0], ranked[1]
        stronger_basis = basis_by_proposal[stronger]
        weaker_basis = basis_by_proposal[weaker]
        if int(stronger_basis["evidence_count"]) > int(
            weaker_basis["evidence_count"]
        ) and CONFIDENCE_RANK.get(
            str(stronger_basis.get("confidence") or "low"), 0
        ) >= CONFIDENCE_RANK.get(
            str(weaker_basis.get("confidence") or "low"), 0
        ):
            return (
                CONFLICT_RESOLUTION_PRIORITY,
                stronger,
                "evidence_strength:more_supporting_runs_with_no_lower_confidence",
            )
    return CONFLICT_RESOLUTION_PENDING_HUMAN, None, None


def _build_conflict_annotation(
    *,
    axis: str,
    side_a: str,
    side_b: str,
    entry_a: dict[str, Any],
    entry_b: dict[str, Any],
    shared_keys: set[str],
    policy: dict[str, Any],
    now_iso: str,
) -> dict[str, Any]:
    ids = sorted([str(entry_a["proposal_id"]), str(entry_b["proposal_id"])])
    side_by_proposal = {
        str(entry_a["proposal_id"]): side_a,
        str(entry_b["proposal_id"]): side_b,
    }
    basis_by_proposal = {
        str(entry["proposal_id"]): {
            "proposal_id": str(entry["proposal_id"]),
            "proposal_type": str(entry.get("proposal_type") or ""),
            "evidence_count": len(entry.get("evidence_run_keys") or []),
            "confidence": str(entry.get("confidence") or "low"),
            "evidence_window": str(entry.get("evidence_window") or ""),
        }
        for entry in (entry_a, entry_b)
    }
    resolution, preferred, rule = _resolve_conflict(
        axis=axis,
        side_by_proposal=side_by_proposal,
        basis_by_proposal=basis_by_proposal,
        policy=policy,
    )
    return {
        "conflict_id": _conflict_identity(
            axis=axis,
            proposal_ids=(ids[0], ids[1]),
            shared_keys=shared_keys,
        ),
        "conflict_type": axis,
        "proposal_ids": ids,
        "sides": [
            {
                "side": side_by_proposal[proposal_id],
                "proposal_id": proposal_id,
                "proposal_type": basis_by_proposal[proposal_id]["proposal_type"],
            }
            for proposal_id in ids
        ],
        "shared_evidence_run_keys": sorted(shared_keys)[:MAX_SHARED_EVIDENCE_KEYS],
        "basis": [basis_by_proposal[proposal_id] for proposal_id in ids],
        "resolution": resolution,
        "preferred_proposal_id": preferred,
        "priority_rule": rule,
        "annotated_at": now_iso,
    }


def _annotate_conflicts(
    ledger: dict[str, Any],
    *,
    policy: dict[str, Any],
    now_iso: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Recompute pairwise conflict annotations on active proposals.

    Returns the current active conflict list and whether annotations changed.
    """

    entries = [
        entry
        for entry in ledger.get("proposals", [])
        if isinstance(entry, dict)
        and entry.get("status") not in TERMINAL_PROPOSAL_STATUSES
    ]
    annotations: dict[str, dict[str, Any]] = {}
    changed = False
    for index, entry_a in enumerate(entries):
        for entry_b in entries[index + 1 :]:
            window_a = set(entry_a.get("window_run_keys") or [])
            window_b = set(entry_b.get("window_run_keys") or [])
            shared = window_a & window_b
            if not shared:
                continue
            for axis, side_a, side_b in _axis_sides(
                str(entry_a.get("proposal_type") or ""),
                str(entry_b.get("proposal_type") or ""),
            ):
                annotation = _build_conflict_annotation(
                    axis=axis,
                    side_a=side_a,
                    side_b=side_b,
                    entry_a=entry_a,
                    entry_b=entry_b,
                    shared_keys=shared,
                    policy=policy,
                    now_iso=now_iso,
                )
                annotations[annotation["conflict_id"]] = annotation
                for entry in (entry_a, entry_b):
                    conflicts = entry.setdefault("conflicts", [])
                    existing = next(
                        (
                            item
                            for item in conflicts
                            if isinstance(item, dict)
                            and item.get("conflict_id") == annotation["conflict_id"]
                        ),
                        None,
                    )
                    if existing is None:
                        conflicts.append(
                            {
                                "conflict_id": annotation["conflict_id"],
                                "conflict_type": annotation["conflict_type"],
                                "proposal_ids": list(annotation["proposal_ids"]),
                                "resolution": annotation["resolution"],
                                "preferred_proposal_id": annotation[
                                    "preferred_proposal_id"
                                ],
                                "priority_rule": annotation["priority_rule"],
                            }
                        )
                        changed = True
                    elif (
                        existing.get("resolution") != annotation["resolution"]
                        or existing.get("preferred_proposal_id")
                        != annotation["preferred_proposal_id"]
                        or existing.get("priority_rule") != annotation["priority_rule"]
                    ):
                        existing.update(
                            {
                                "resolution": annotation["resolution"],
                                "preferred_proposal_id": annotation["preferred_proposal_id"],
                                "priority_rule": annotation["priority_rule"],
                            }
                        )
                        changed = True

    # Drop annotations whose pair is no longer active (decided proposals do
    # not keep pending conflict markers alive).
    active_ids = {str(entry.get("proposal_id")) for entry in entries}
    for entry in entries:
        conflicts = entry.get("conflicts") or []
        kept = [
            item
            for item in conflicts
            if isinstance(item, dict)
            and item.get("conflict_id") in annotations
            and all(
                proposal_id in active_ids
                for proposal_id in (item.get("proposal_ids") or [])
            )
        ]
        if len(kept) != len(conflicts):
            entry["conflicts"] = kept
            entry["updated_at"] = now_iso
            changed = True
    return list(annotations.values()), changed


def ingest_candidates(
    ledger: dict[str, Any] | None,
    goal_id: str,
    candidates: list[dict[str, Any]],
    *,
    policy: dict[str, Any],
    now_iso: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Merge a batch of candidates into the ledger.

    Pure function: inputs are never mutated. Returns ``(ledger, report,
    changed)``. Replaying an identical batch leaves the ledger untouched.
    """

    now_iso = now_iso or now_local_iso()
    result = (
        copy.deepcopy(ledger)
        if ledger is not None
        else new_ledger(goal_id, now_iso=now_iso)
    )
    reopened = apply_proposal_lifecycle(result, now_iso=now_iso)
    entries: list[dict[str, Any]] = result.setdefault("proposals", [])

    report: dict[str, Any] = {
        "batch_evidence_window": None,
        "candidates": len(candidates),
        "new": 0,
        "merged": 0,
        "suppressed_decided": 0,
        "suppressed_deferred": 0,
        "reopened": reopened,
        "new_proposal_ids": [],
        "merged_proposal_ids": [],
        "suppressed": [],
        "conflicts": [],
        "pending_human": 0,
    }
    changed = bool(reopened)

    for candidate in candidates:
        report["batch_evidence_window"] = candidate.get("evidence_window")
        proposal_type = str(candidate["proposal_type"])
        evidence = set(candidate.get("evidence_run_keys") or [])
        same_type = [
            entry
            for entry in entries
            if entry.get("proposal_type") == proposal_type
        ]
        exact = next(
            (
                entry
                for entry in same_type
                if set(entry.get("evidence_run_keys") or []) == evidence
            ),
            None,
        )
        if exact is not None:
            status = str(exact.get("status") or "")
            if status in TERMINAL_PROPOSAL_STATUSES:
                report["suppressed_decided"] += 1
                report["suppressed"].append(
                    {
                        "proposal_id": exact["proposal_id"],
                        "proposal_type": proposal_type,
                        "reason": f"already_{status}",
                    }
                )
                continue
            if status == PROPOSAL_STATUS_DEFERRED:
                report["suppressed_deferred"] += 1
                report["suppressed"].append(
                    {
                        "proposal_id": exact["proposal_id"],
                        "proposal_type": proposal_type,
                        "reason": "deferred_until_expiry",
                    }
                )
                continue
            if _merge_candidate_into_entry(exact, candidate, now_iso=now_iso):
                report["merged"] += 1
                report["merged_proposal_ids"].append(exact["proposal_id"])
                changed = True
            continue

        # Covering windows. A proposal already decided keeps suppressing only
        # candidates whose evidence stays inside its decided window; a broader
        # window is genuinely new evidence and must surface again.
        decided_cover = next(
            (
                entry
                for entry in same_type
                if entry.get("status") in TERMINAL_PROPOSAL_STATUSES
                and evidence <= set(entry.get("evidence_run_keys") or [])
            ),
            None,
        )
        if decided_cover is not None:
            report["suppressed_decided"] += 1
            report["suppressed"].append(
                {
                    "proposal_id": decided_cover["proposal_id"],
                    "proposal_type": proposal_type,
                    "reason": f"covered_by_{decided_cover['status']}",
                }
            )
            continue
        deferred_cover = next(
            (
                entry
                for entry in same_type
                if entry.get("status") == PROPOSAL_STATUS_DEFERRED
                and evidence <= set(entry.get("evidence_run_keys") or [])
            ),
            None,
        )
        if deferred_cover is not None:
            report["suppressed_deferred"] += 1
            report["suppressed"].append(
                {
                    "proposal_id": deferred_cover["proposal_id"],
                    "proposal_type": proposal_type,
                    "reason": "covered_by_deferred",
                }
            )
            continue

        covering = [
            entry
            for entry in same_type
            if entry.get("status") == PROPOSAL_STATUS_PENDING
            and (
                evidence <= set(entry.get("evidence_run_keys") or [])
                or set(entry.get("evidence_run_keys") or []) <= evidence
            )
        ]
        if covering:
            survivor = min(
                covering,
                key=lambda entry: (
                    -len(entry.get("evidence_run_keys") or []),
                    str(entry.get("proposal_id") or ""),
                ),
            )
            if _merge_candidate_into_entry(survivor, candidate, now_iso=now_iso):
                report["merged"] += 1
                report["merged_proposal_ids"].append(survivor["proposal_id"])
                changed = True
            continue

        entry = _entry_from_candidate(candidate, now_iso=now_iso)
        entries.append(entry)
        report["new"] += 1
        report["new_proposal_ids"].append(entry["proposal_id"])
        changed = True

    conflicts, conflicts_changed = _annotate_conflicts(
        result,
        policy=policy,
        now_iso=now_iso,
    )
    changed = changed or conflicts_changed
    report["conflicts"] = conflicts
    report["pending_human"] = sum(
        1
        for conflict in conflicts
        if conflict["resolution"] == CONFLICT_RESOLUTION_PENDING_HUMAN
    )
    if changed:
        result["updated_at"] = now_iso
    return result, report, changed


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------


def compact_conflict(annotation: dict[str, Any]) -> dict[str, Any]:
    return {
        key: annotation.get(key)
        for key in (
            "conflict_id",
            "conflict_type",
            "proposal_ids",
            "sides",
            "shared_evidence_run_keys",
            "basis",
            "resolution",
            "preferred_proposal_id",
            "priority_rule",
        )
        if key in annotation
    }


def compact_ledger_entry(entry: dict[str, Any]) -> dict[str, Any]:
    compact = {
        key: entry.get(key)
        for key in (
            "proposal_id",
            "proposal_type",
            "classification",
            "status",
            "summary",
            "operator_question",
            "evidence_window",
            "evidence_run_keys",
            "window_run_keys",
            "confidence",
            "decision",
            "created_at",
            "updated_at",
        )
        if key in entry
    }
    compact["recent_evidence"] = list(entry.get("recent_evidence") or [])[
        :MAX_DREAMING_EVIDENCE_ITEMS
    ]
    merged_from = entry.get("merged_from") or []
    compact["merged_from"] = merged_from[:MAX_LEDGER_EVIDENCE_ITEMS]
    compact["merged_source_count"] = len(merged_from)
    conflicts = entry.get("conflicts") or []
    compact["conflicts"] = [
        {
            key: item.get(key)
            for key in (
                "conflict_id",
                "conflict_type",
                "proposal_ids",
                "resolution",
                "preferred_proposal_id",
                "priority_rule",
            )
            if key in item
        }
        for item in conflicts
        if isinstance(item, dict)
    ]
    history = entry.get("decision_history") or []
    compact["decision_history"] = history[:MAX_LEDGER_EVIDENCE_ITEMS]
    compact["decision_history_count"] = len(history)
    return compact
