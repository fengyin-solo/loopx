from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path

import pytest

import loopx.global_risks as global_risks
import loopx.risk_ledger as rl


FROZEN_TIME = "2026-09-15T12:00:00Z"
LATER_TIME = "2026-09-16T12:00:00Z"
LATEST_TIME = "2026-09-17T12:00:00Z"


def observed_row(
	*,
	source_surface: str = "status.contract.error_diagnostics",
	kind: str = "public_boundary_violation",
	scope: str = "global",
	goal_id: str | None = None,
	severity: str = "warning",
	summary: str = "A redacted boundary finding.",
	occurrence_count: int = 1,
	evidence_refs: list[str] | None = None,
) -> dict[str, object]:
	return {
		"source_surface": source_surface,
		"kind": kind,
		"scope": scope,
		"goal_id": goal_id,
		"category": "boundary_warning"
		if kind == "public_boundary_violation"
		else "failing_check",
		"severity": severity,
		"summary": summary,
		"next_safe_action": "Inspect the finding.",
		"requires_user_approval": False,
		"occurrence_count": occurrence_count,
		"evidence_refs": evidence_refs if evidence_refs is not None else [f"{source_surface}:{kind}"],
	}


def registry_with_agents(*, goals: list[dict[str, object]]) -> dict[str, object]:
	return {"goals": goals}


# --- Task 1: storage foundation ------------------------------------------


def test_severity_rank_matches_global_risks_contract() -> None:
	assert rl.SEVERITY_RANK == global_risks.SEVERITY_RANK


def test_lifecycle_statuses_are_explicit() -> None:
	assert {status.value for status in rl.RiskLifecycleStatus} == {
		"open",
		"acknowledged",
		"assigned",
		"resolved",
		"suppressed",
	}


@pytest.mark.parametrize(
	("registry_path", "expected"),
	[
		(Path("/work/.loopx/registry.json"), Path("/work/.loopx/global-risk-ledger.json")),
		(
			Path.home() / ".codex" / "loopx" / "registry.global.json",
			Path.home() / ".codex" / "loopx" / "global-risk-ledger.json",
		),
	],
)
def test_ledger_path_sits_beside_its_registry(
	registry_path: Path, expected: Path
) -> None:
	assert rl.risk_ledger_path_for_registry(registry_path) == expected


def test_load_missing_ledger_returns_none_without_creating(tmp_path: Path) -> None:
	path = tmp_path / "global-risk-ledger.json"
	assert rl.load_ledger(path) is None
	assert not path.exists()


def test_initialize_creates_valid_document_and_refuses_reset(tmp_path: Path) -> None:
	path = tmp_path / "global-risk-ledger.json"
	ledger = rl.initialize_ledger_file(path)
	assert ledger["schema_version"] == rl.LEDGER_SCHEMA_VERSION
	assert ledger["records"] == {}
	assert ledger["last_scan"] is None

	roundtrip = rl.load_ledger(path)
	assert roundtrip is not None
	assert roundtrip["created_at"] == ledger["created_at"]

	with pytest.raises(rl.RiskLedgerExists):
		rl.initialize_ledger_file(path)


def test_corrupt_ledger_loads_as_unreadable() -> None:
	assert rl.RiskLedgerUnreadable.code == "risk_ledger_unreadable"


def test_open_ledger_on_corrupt_file_never_rewrites(tmp_path: Path) -> None:
	path = tmp_path / "global-risk-ledger.json"
	path.write_text("{not-json", encoding="utf-8")
	before = path.read_bytes()
	st_mtime = path.stat().st_mtime_ns

	with pytest.raises(rl.RiskLedgerUnreadable):
		with rl.open_risk_ledger(path):
			pytest.fail("corrupt ledger must not open a session")

	assert path.read_bytes() == before
	assert path.stat().st_mtime_ns == st_mtime


def test_open_ledger_missing_raises_not_found(tmp_path: Path) -> None:
	with pytest.raises(rl.RiskLedgerNotFound):
		with rl.open_risk_ledger(tmp_path / "global-risk-ledger.json"):
			pytest.fail("missing ledger must not open a session")


def test_session_writes_only_when_marked_dirty(tmp_path: Path) -> None:
	path = tmp_path / "global-risk-ledger.json"
	rl.initialize_ledger_file(path)
	before = path.read_bytes()
	with rl.open_risk_ledger(path) as session:
		assert session.exists is True
		# read-only session must not rewrite
	assert path.read_bytes() == before


# --- Task 2: stable identity and counting --------------------------------


def test_stable_risk_id_excludes_source_position() -> None:
	identity = dict(
		source_surface="status.contract.error_diagnostics",
		kind="public_boundary_violation",
		scope="global",
		goal_id=None,
	)
	assert rl.stable_risk_id(**identity) == rl.stable_risk_id(**identity)
	assert (
		rl.stable_risk_id(**identity)
		!= rl.stable_risk_id(**{**identity, "kind": "registry_boundary_risk"})
	)


def test_merge_dedupes_across_scans_and_counts_once_per_scan(
	tmp_path: Path,
) -> None:
	path = tmp_path / "global-risk-ledger.json"
	rl.initialize_ledger_file(path)
	row_a = observed_row(summary="first scan", occurrence_count=2)
	row_b = observed_row(summary="second scan", occurrence_count=3)

	_, stats_a = rl.merge_scan_file(
		path, [row_a], scan_id="scan-1", scanned_at=FROZEN_TIME, now=FROZEN_TIME
	)
	assert stats_a["new_count"] == 1
	assert stats_a["dirty"] is True

	# Different source position on the next scan is still the same identity.
	_, stats_b = rl.merge_scan_file(
		path, [row_b], scan_id="scan-2", scanned_at=LATER_TIME, now=LATER_TIME
	)
	assert stats_b["new_count"] == 0
	ledger = rl.load_ledger(path)
	assert ledger is not None
	assert len(ledger["records"]) == 1
	record = next(iter(ledger["records"].values()))
	assert record["first_seen_at"] == FROZEN_TIME
	assert record["last_seen_at"] == LATER_TIME
	assert record["scan_count"] == 2
	assert record["occurrence_count"] == 5
	assert record["present_in_latest_scan"] is True
	assert ledger["last_scan"] == {"scan_id": "scan-2", "scanned_at": LATER_TIME}

	# Replaying the same scan batch is fully idempotent and needs no rewrite.
	before = path.read_bytes()
	_, replay = rl.merge_scan_file(
		path, [row_b], scan_id="scan-2", scanned_at=LATER_TIME, now=LATER_TIME
	)
	assert replay["dirty"] is False
	assert replay["new_count"] == 0
	assert path.read_bytes() == before
	ledger = rl.load_ledger(path)
	record = next(iter(ledger["records"].values()))
	assert record["scan_count"] == 2
	assert record["occurrence_count"] == 5


def test_merge_without_ledger_is_absent_and_writes_nothing(tmp_path: Path) -> None:
	path = tmp_path / "global-risk-ledger.json"
	state, stats = rl.merge_scan_file(
		path, [observed_row()], scan_id="scan-1", scanned_at=FROZEN_TIME
	)
	assert state == "absent"
	assert stats is None
	assert not path.exists()


def test_absent_records_keep_presence_flag_without_deletion(tmp_path: Path) -> None:
	path = tmp_path / "global-risk-ledger.json"
	rl.initialize_ledger_file(path)
	rl.merge_scan_file(
		path, [observed_row(kind="a")], scan_id="scan-1", scanned_at=FROZEN_TIME
	)
	rl.merge_scan_file(
		path, [observed_row(kind="b")], scan_id="scan-2", scanned_at=LATER_TIME
	)
	ledger = rl.load_ledger(path)
	assert ledger is not None
	assert len(ledger["records"]) == 2
	records = list(ledger["records"].values())
	presence = {record["kind"]: record["present_in_latest_scan"] for record in records}
	assert presence == {"a": False, "b": True}


# --- lifecycle transitions -----------------------------------------------


def _seed_record(tmp_path: Path, *, severity: str = "warning") -> tuple[Path, str]:
	path = tmp_path / "global-risk-ledger.json"
	rl.initialize_ledger_file(path)
	rl.merge_scan_file(
		path,
		[observed_row(severity=severity)],
		scan_id="scan-1",
		scanned_at=FROZEN_TIME,
	)
	ledger = rl.load_ledger(path)
	assert ledger is not None
	risk_id = next(iter(ledger["records"]))
	return path, risk_id


def test_full_manual_lifecycle_writes_history(tmp_path: Path) -> None:
	path, risk_id = _seed_record(tmp_path)

	rl.apply_lifecycle_file(
		path, risk_id, "acknowledged", now=LATER_TIME, actor="Agent A", note="seen"
	)
	record = rl.apply_lifecycle_file(
		path,
		risk_id,
		"assigned",
		now=LATER_TIME,
		actor="boss",
		agent="agent-a",
	)
	assert record["status"] == "assigned"
	assert record["assignee"] == "agent-a"
	assert record["acknowledged"]["by"] == "Agent A"

	rl.apply_lifecycle_file(path, risk_id, "resolved", now=LATEST_TIME, actor="agent-a")
	ledger = rl.load_ledger(path)
	assert ledger is not None
	record = ledger["records"][risk_id]
	assert record["status"] == "resolved"
	assert record["resolved"]["at"] == LATEST_TIME
	actions = [event["action"] for event in record["history"]]
	assert actions == ["opened", "acknowledged", "assigned", "resolved"]
	assert all("from_status" in event and "to_status" in event for event in record["history"])


def test_unknown_risk_and_invalid_transitions_fail_closed(tmp_path: Path) -> None:
	path, risk_id = _seed_record(tmp_path)

	with pytest.raises(rl.InvalidSuppressionDeadline):
		rl.apply_lifecycle_file(
			path, risk_id, "suppressed", now=FROZEN_TIME, until="bad"
		)

	rl.apply_lifecycle_file(path, risk_id, "resolved", now=LATER_TIME)

	with pytest.raises(rl.RiskRecordNotFound):
		rl.apply_lifecycle_file(path, "missing00000000", "acknowledged")

	with pytest.raises(rl.RiskLifecycleTransitionError):
		# resolving an already resolved record fails visibly
		rl.apply_lifecycle_file(path, risk_id, "resolved", now=LATER_TIME)

	ledger = rl.load_ledger(path)
	assert ledger is not None
	record = ledger["records"][risk_id]
	assert record["status"] == "resolved"
	# the failed suppression must not have mutated disposition state
	assert all(event["action"] != "suppressed" for event in record["history"])


def test_active_suppression_blocks_bypassing_actions(tmp_path: Path) -> None:
	path, risk_id = _seed_record(tmp_path)
	rl.apply_lifecycle_file(
		path, risk_id, "suppressed", now=FROZEN_TIME, until="7d"
	)
	with pytest.raises(rl.RiskLifecycleTransitionError):
		rl.apply_lifecycle_file(path, risk_id, "acknowledged", now=FROZEN_TIME)
	with pytest.raises(rl.RiskLifecycleTransitionError):
		rl.apply_lifecycle_file(path, risk_id, "resolved", now=FROZEN_TIME)
	# extending/re-suppressing is allowed
	record = rl.apply_lifecycle_file(
		path, risk_id, "suppressed", now=FROZEN_TIME, until="14d"
	)
	assert record["suppress_until"] == "2026-09-29T12:00:00Z"


def test_suppression_deadline_validation() -> None:
	with pytest.raises(rl.InvalidSuppressionDeadline):
		rl.parse_suppression_deadline("not-a-time", now=FROZEN_TIME)
	with pytest.raises(rl.InvalidSuppressionDeadline):
		rl.parse_suppression_deadline("2020-01-01T00:00:00Z", now=FROZEN_TIME)
	assert (
		rl.parse_suppression_deadline("2026-09-16T12:00:00Z", now=FROZEN_TIME)
		== "2026-09-16T12:00:00Z"
	)


def test_assign_requires_registered_agent() -> None:
	payload = registry_with_agents(
		goals=[
			{"id": "goal-1", "coordination": {"registered_agents": ["agent-a"]}}
		]
	)
	assert (
		rl.validate_registered_assignee(
			payload, goal_id="goal-1", agent="agent-a"
		)
		== "agent-a"
	)
	# normalization accepts display-style input
	assert (
		rl.validate_registered_assignee(
			payload, goal_id="goal-1", agent=rl.normalize_assignee_token("Agent A")
		)
		== "agent-a"
	)
	with pytest.raises(rl.UnregisteredAssignee):
		rl.validate_registered_assignee(payload, goal_id="goal-1", agent="agent-b")
	with pytest.raises(rl.UnregisteredAssignee):
		rl.validate_registered_assignee(payload, goal_id="other-goal", agent="agent-a")
	# global scope accepts registration on any goal
	assert (
		rl.validate_registered_assignee(payload, goal_id=None, agent="agent-a")
		== "agent-a"
	)
	with pytest.raises(rl.UnregisteredAssignee):
		rl.validate_registered_assignee({"goals": []}, goal_id=None, agent="agent-a")
	with pytest.raises(rl.UnregisteredAssignee):
		rl.validate_registered_assignee(None, goal_id=None, agent="agent-a")


# --- suppression expiry --------------------------------------------------


def test_suppression_expiry_lands_on_merge_even_without_observation(
	tmp_path: Path,
) -> None:
	path, risk_id = _seed_record(tmp_path)
	rl.apply_lifecycle_file(
		path, risk_id, "suppressed", now=FROZEN_TIME, until="24h"
	)
	# empty later scan: expiry still lands for unobserved records
	_, stats = rl.merge_scan_file(
		path, [], scan_id="scan-2", scanned_at=LATER_TIME, now=LATER_TIME
	)
	assert stats["suppressed_expired_count"] == 1
	ledger = rl.load_ledger(path)
	assert ledger is not None
	record = ledger["records"][risk_id]
	assert record["status"] == "open"
	assert record["suppress_until"] is None
	assert record["history"][-1]["reason"] == "suppression_expired"


def test_suppression_within_window_stays_suppressed(tmp_path: Path) -> None:
	path, risk_id = _seed_record(tmp_path)
	rl.apply_lifecycle_file(
		path, risk_id, "suppressed", now=FROZEN_TIME, until="7d"
	)
	_, stats = rl.merge_scan_file(
		path,
		[observed_row(severity="warning")],
		scan_id="scan-2",
		scanned_at=LATER_TIME,
		now=LATER_TIME,
	)
	assert stats["suppressed_expired_count"] == 0
	ledger = rl.load_ledger(path)
	assert ledger is not None
	record = ledger["records"][risk_id]
	assert record["status"] == "suppressed"
	assert record["scan_count"] == 2


def test_show_path_lands_due_expirations(tmp_path: Path) -> None:
	path, risk_id = _seed_record(tmp_path)
	rl.apply_lifecycle_file(
		path, risk_id, "suppressed", now=FROZEN_TIME, until="24h"
	)
	assert rl.apply_due_expirations_file(path, now=FROZEN_TIME) == 0
	assert rl.apply_due_expirations_file(path, now=LATER_TIME) == 1
	ledger = rl.load_ledger(path)
	assert ledger is not None
	assert ledger["records"][risk_id]["status"] == "open"


# --- reopen matrix --------------------------------------------------------


@pytest.mark.parametrize(
	("initial_action", "observed_severity", "expected_status", "expect_reopen", "expect_event"),
	[
		("resolved", "warning", "open", 1, "reappeared"),
		("suppressed", "warning", "suppressed", 0, None),
		("suppressed", "high", "open", 1, "severity_escalated"),
		("acknowledged", "high", "acknowledged", 0, "severity_escalated"),
		("assigned", "high", "assigned", 0, "severity_escalated"),
	],
)
def test_reopen_and_escalation_matrix(
	tmp_path: Path,
	initial_action: str,
	observed_severity: str,
	expected_status: str,
	expect_reopen: int,
	expect_event: str | None,
) -> None:
	path, risk_id = _seed_record(tmp_path, severity="warning")
	kwargs: dict[str, object] = {"now": LATER_TIME}
	if initial_action == "assigned":
		kwargs["agent"] = "agent-a"
	if initial_action == "suppressed":
		kwargs["until"] = "7d"
	rl.apply_lifecycle_file(path, risk_id, initial_action, **kwargs)

	_, stats = rl.merge_scan_file(
		path,
		[observed_row(severity=observed_severity)],
		scan_id="scan-2",
		scanned_at=LATEST_TIME,
		now=LATEST_TIME,
	)
	assert stats["reopened_count"] == expect_reopen
	ledger = rl.load_ledger(path)
	assert ledger is not None
	record = ledger["records"][risk_id]
	assert record["status"] == expected_status
	assert record["reopen_count"] == expect_reopen
	assert record["first_seen_at"] == FROZEN_TIME
	assert record["occurrence_count"] == 2
	if expect_event:
		assert record["history"][-1]["reason"] == expect_event
	if observed_severity == "high":
		assert record["severity"] == "high"
		assert record["max_severity"] == "high"


def test_reopen_preserves_full_history_and_counts(tmp_path: Path) -> None:
	path, risk_id = _seed_record(tmp_path, severity="warning")
	rl.apply_lifecycle_file(path, risk_id, "acknowledged", now=FROZEN_TIME)
	rl.apply_lifecycle_file(path, risk_id, "resolved", now=FROZEN_TIME)
	rl.merge_scan_file(
		path, [observed_row(severity="warning")], scan_id="scan-2",
		scanned_at=LATER_TIME, now=LATER_TIME,
	)
	rl.apply_lifecycle_file(path, risk_id, "resolved", now=LATEST_TIME)
	rl.merge_scan_file(
		path, [observed_row(severity="warning")], scan_id="scan-3",
		scanned_at=LATEST_TIME, now=LATEST_TIME,
	)
	ledger = rl.load_ledger(path)
	assert ledger is not None
	record = ledger["records"][risk_id]
	assert record["reopen_count"] == 2
	assert record["scan_count"] == 3
	assert record["occurrence_count"] == 3
	assert [event["action"] for event in record["history"]] == [
		"opened",
		"acknowledged",
		"resolved",
		"reopened",
		"resolved",
		"reopened",
	]


def test_history_is_bounded_but_counts_survive(tmp_path: Path) -> None:
	path, risk_id = _seed_record(tmp_path)
	for round_index in range(60):
		at = f"2026-09-{15 + round_index % 5:02d}T{12 + round_index % 8:02d}:00:00Z"
		# suppress far future then reopen through expiry requires monotonic time;
		# instead alternate resolve + reappearance, each appending events.
		rl.apply_lifecycle_file(path, risk_id, "resolved", now=at)
		rl.merge_scan_file(
			path,
			[observed_row()],
			scan_id=f"scan-{round_index + 2}",
			scanned_at=at,
			now=at,
		)
	ledger = rl.load_ledger(path)
	assert ledger is not None
	record = ledger["records"][risk_id]
	assert len(record["history"]) <= rl.LEDGER_HISTORY_LIMIT
	assert record["reopen_count"] == 60
	assert record["status"] == "open"


# --- corruption safety at file boundary ----------------------------------


def test_merge_corrupt_ledger_preserves_bytes(tmp_path: Path) -> None:
	path = tmp_path / "global-risk-ledger.json"
	path.write_text("{broken", encoding="utf-8")
	before = path.read_bytes()
	with pytest.raises(rl.RiskLedgerUnreadable):
		rl.merge_scan_file(
			path, [observed_row()], scan_id="scan-x", scanned_at=FROZEN_TIME
		)
	assert path.read_bytes() == before


def test_lifecycle_on_missing_ledger_fails_without_creating(tmp_path: Path) -> None:
	path = tmp_path / "global-risk-ledger.json"
	with pytest.raises(rl.RiskLedgerNotFound):
		rl.apply_lifecycle_file(path, "anyid000000000", "acknowledged")
	assert not path.exists()


# --- concurrency ----------------------------------------------------------


def _mp_merge(
	queue_item: tuple[str, list[dict[str, object]], str, str],
) -> tuple[str, object]:
	path_text, rows, scan_id, at = queue_item
	try:
		state, stats = rl.merge_scan_file(
			path_text, rows, scan_id=scan_id, scanned_at=at, now=at
		)
		return ("ok", state if stats is None else stats.get("new_count"))
	except Exception as exc:  # multiprocessing surfaces the string
		return ("error", repr(exc))


def test_concurrent_scan_merges_lose_no_updates(tmp_path: Path) -> None:
	path = tmp_path / "global-risk-ledger.json"
	rl.initialize_ledger_file(path)
	worker_count = 6
	rounds_per_worker = 20
	jobs: list[tuple[str, list[dict[str, object]], str, str]] = []
	# Three scan ids are attempted by every worker concurrently and must count
	# only once each; the rest are unique per worker.
	scan_index = 0
	for shared_index in range(3):
		for worker in range(worker_count):
			jobs.append(
				(
					str(path),
					[observed_row(kind="shared-kind")],
					f"shared-{shared_index}",
					FROZEN_TIME,
				)
			)
	for worker in range(worker_count):
		for round_index in range(rounds_per_worker):
			scan_index += 1
			jobs.append(
				(
					str(path),
					[observed_row(kind="shared-kind")],
					f"unique-{worker}-{round_index}",
					f"2026-09-15T12:{scan_index % 60:02d}:00Z",
				)
			)

	with mp.get_context("fork").Pool(worker_count) as pool:
		results = pool.map(_mp_merge, jobs)
	assert all(result[0] == "ok" for result in results), results

	ledger = json.loads(path.read_text(encoding="utf-8"))
	record = next(iter(ledger["records"].values()))
	distinct_scans = 3 + worker_count * rounds_per_worker
	assert record["scan_count"] == distinct_scans
	assert record["occurrence_count"] == distinct_scans
	assert record["risk_id"] == rl.stable_risk_id(
		source_surface="status.contract.error_diagnostics",
		kind="shared-kind",
		scope="global",
		goal_id=None,
	)


# --- ledger view filters --------------------------------------------------


def test_ledger_view_filters_and_bounds(tmp_path: Path) -> None:
	path = tmp_path / "global-risk-ledger.json"
	rl.initialize_ledger_file(path)
	rl.merge_scan_file(
		path,
		[
			observed_row(kind="a", severity="high"),
			observed_row(
				kind="b", severity="warning", scope="goal", goal_id="goal-1"
			),
		],
		scan_id="scan-1",
		scanned_at=FROZEN_TIME,
	)
	rl.apply_lifecycle_file(
		path,
		rl.stable_risk_id(
			source_surface="status.contract.error_diagnostics",
			kind="b",
			scope="goal",
			goal_id="goal-1",
		),
		"assigned",
		now=LATER_TIME,
		agent="agent-a",
	)
	ledger = rl.load_ledger(path)
	assert ledger is not None

	open_only = rl.ledger_view(ledger, statuses={"open"})
	assert {record["kind"] for record in open_only["records"]} == {"a"}

	assigned_view = rl.ledger_view(ledger, assignee="agent-a")
	assert [record["kind"] for record in assigned_view["records"]] == ["b"]

	unassigned = rl.ledger_view(ledger, assignee="unassigned")
	assert {record["kind"] for record in unassigned["records"]} == {"a"}

	high_view = rl.ledger_view(ledger, severities={"high"})
	assert [record["kind"] for record in high_view["records"]] == ["a"]

	# after a scan where kind-a disappears, default view hides it
	rl.merge_scan_file(
		path,
		[
			observed_row(
				kind="b", severity="warning", scope="goal", goal_id="goal-1"
			)
		],
		scan_id="scan-2",
		scanned_at=LATEST_TIME,
	)
	ledger = rl.load_ledger(path)
	assert ledger is not None
	assert {record["kind"] for record in rl.ledger_view(ledger)["records"]} == {"b"}
	with_absent = rl.ledger_view(ledger, include_absent=True)
	assert {record["kind"] for record in with_absent["records"]} == {"a", "b"}
