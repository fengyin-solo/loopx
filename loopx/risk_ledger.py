"""Persistent lifecycle ledger for global risk findings.

``loopx.global_risks`` rebuilds a read-only projection on every invocation.
This module gives each finding a stable cross-scan identity, durable
occurrence tracking, and an explicit lifecycle (acknowledge / assign /
resolve / suppress / reopen) without changing that projection's contract:

* the ledger file is created only by the explicit ``risk-ledger init`` CLI;
* scans ignore a missing ledger entirely (zero writes, legacy behavior);
* all read-modify-write sequences are serialized with
  :func:`loopx.file_lock.exclusive_file_lock` and persisted atomically;
* an unreadable ledger is never overwritten, so disposition state can not be
  erased by a failed scan.

The merge core (:func:`merge_scan`) is pure and clock-injectable; only the
``*_file`` wrappers touch the filesystem.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import timedelta
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .control_plane.runtime.time import now_utc_iso, parse_timestamp, utc_isoformat
from .file_lock import exclusive_file_lock
from .registry import atomic_write_json

RISK_LEDGER_FILENAME = "global-risk-ledger.json"
LEDGER_SCHEMA_VERSION = "global_risk_ledger_v1"
LEDGER_HISTORY_LIMIT = 100
LEDGER_EVIDENCE_LIMIT = 20
# Per-record bounded memory of applied scan batches. Concurrent merges from
# different scans interleave, so "latest scan id" alone can not make retries
# idempotent; membership in the recent window can. A replay after this many
# newer scans is outside any realistic CLI retry window.
LEDGER_SCAN_ID_LIMIT = 256

# Mirrors loopx.global_risks.SEVERITY_RANK; the smaller the rank, the higher
# the severity. risk_ledger must not import global_risks (global_risks imports
# this module), so the ordering contract is duplicated and parity-checked by
# tests.
SEVERITY_RANK = {"high": 0, "action": 1, "warning": 2, "info": 3}
_DEFAULT_SEVERITY_RANK = 4

_DURATION_PATTERN = re.compile(r"^([1-9][0-9]*)([hd])$")


class RiskLifecycleStatus(str, Enum):
	OPEN = "open"
	ACKNOWLEDGED = "acknowledged"
	ASSIGNED = "assigned"
	RESOLVED = "resolved"
	SUPPRESSED = "suppressed"


LIFECYCLE_VALUES = {status.value for status in RiskLifecycleStatus}

# Manual lifecycle transitions. Suppression expiry is an automatic transition
# applied before manual actions, so an active suppression can not be bypassed
# by acknowledging/ resolving through the CLI.
_MANUAL_TRANSITIONS: dict[str, frozenset[RiskLifecycleStatus]] = {
	"acknowledged": frozenset(
		{
			RiskLifecycleStatus.OPEN,
			RiskLifecycleStatus.ACKNOWLEDGED,
			RiskLifecycleStatus.ASSIGNED,
		}
	),
	"assigned": frozenset(
		{
			RiskLifecycleStatus.OPEN,
			RiskLifecycleStatus.ACKNOWLEDGED,
			RiskLifecycleStatus.ASSIGNED,
			RiskLifecycleStatus.RESOLVED,
		}
	),
	"resolved": frozenset(
		{
			RiskLifecycleStatus.OPEN,
			RiskLifecycleStatus.ACKNOWLEDGED,
			RiskLifecycleStatus.ASSIGNED,
		}
	),
	"suppressed": frozenset(
		{
			RiskLifecycleStatus.OPEN,
			RiskLifecycleStatus.ACKNOWLEDGED,
			RiskLifecycleStatus.ASSIGNED,
			RiskLifecycleStatus.SUPPRESSED,
		}
	),
}


class RiskLedgerError(Exception):
	code = "risk_ledger_error"


class RiskLedgerUnreadable(RiskLedgerError):
	code = "risk_ledger_unreadable"


class RiskLedgerNotFound(RiskLedgerError):
	code = "risk_ledger_not_initialized"


class RiskLedgerExists(RiskLedgerError):
	code = "risk_ledger_already_initialized"


class RiskRecordNotFound(RiskLedgerError):
	code = "risk_record_not_found"


class RiskLifecycleTransitionError(RiskLedgerError):
	code = "invalid_lifecycle_transition"


class InvalidSuppressionDeadline(RiskLedgerError):
	code = "invalid_suppression_deadline"


class UnregisteredAssignee(RiskLedgerError):
	code = "unregistered_assignee"


def _redact_text(value: object, *, limit: int = 260) -> str:
	redact_public_text = getattr(
		import_module("loopx.presentation.public_safety"),
		"redact_public_text",
	)
	return redact_public_text(value, limit=limit, truncation_marker="…")


def risk_ledger_path_for_registry(registry_path: str | Path) -> Path:
	"""Return the ledger path that owns one registry scan scope.

	Project registries live at ``<project>/.loopx/registry.json`` and global
	registries directly under the runtime root; in both cases the ledger sits
	beside the registry so multiple processes scanning the same scope share it.
	"""

	return Path(registry_path).expanduser().parent / RISK_LEDGER_FILENAME


def stable_risk_id(
	*,
	source_surface: str,
	kind: str,
	scope: str,
	goal_id: str | None,
) -> str:
	"""Stable cross-scan identity (deliberately excludes source list index)."""

	identity = {
		"goal_id": goal_id or None,
		"kind": kind,
		"scope": scope,
		"source_surface": source_surface,
	}
	encoded = json.dumps(
		identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False
	).encode("utf-8")
	return hashlib.sha256(encoded).hexdigest()[:16]


def make_scan_id(*, now: str | None = None) -> str:
	return f"{now or now_utc_iso()}-{uuid4().hex[:8]}"


def normalize_severity(value: object) -> str:
	normalized = str(value or "").strip()
	return normalized if normalized in SEVERITY_RANK else "warning"


def _is_higher_severity(incoming: str, current: str) -> bool:
	return SEVERITY_RANK.get(incoming, _DEFAULT_SEVERITY_RANK) < SEVERITY_RANK.get(
		current, _DEFAULT_SEVERITY_RANK
	)


def parse_suppression_deadline(value: object, *, now: str | None = None) -> str:
	"""Accept an ISO8601 future instant or a positive Nh/Nd duration."""

	text = str(value or "").strip()
	if not text:
		raise InvalidSuppressionDeadline("suppression deadline is required")
	reference = parse_timestamp(now or now_utc_iso())
	parsed = parse_timestamp(text)
	if parsed is not None:
		if reference is not None and parsed <= reference:
			raise InvalidSuppressionDeadline(
				"suppression deadline must be in the future"
			)
		return utc_isoformat(parsed)
	match = _DURATION_PATTERN.match(text.lower())
	if match is None:
		raise InvalidSuppressionDeadline(
			"suppression deadline must be an ISO8601 instant or Nh/Nd duration"
		)
	amount = int(match.group(1))
	delta_kwargs = {"hours": amount} if match.group(2) == "h" else {"days": amount}
	if reference is None:  # pragma: no cover - parse_timestamp accepts UTC Z
		raise InvalidSuppressionDeadline("suppression deadline reference is invalid")
	return utc_isoformat(reference + timedelta(**delta_kwargs))


def normalize_assignee_token(value: object) -> str | None:
	normalize_todo_claimed_by = getattr(
		import_module("loopx.control_plane.todos.contract"),
		"normalize_todo_claimed_by",
	)
	return normalize_todo_claimed_by(value)


def validate_registered_assignee(
	registry_payload: object,
	*,
	goal_id: str | None,
	agent: str,
) -> str:
	"""Validate that ``agent`` is registered for the risk scope.

	Goal-scoped risks require registration on that exact goal; global risks
	require registration on at least one registered goal.
	"""

	if isinstance(registry_payload, dict) and isinstance(
		registry_payload.get("goals"), list
	):
		normalize_registered_agents = getattr(
			import_module("loopx.agent_registry"),
			"normalize_registered_agents",
		)
		scope_match = False
		registered_anywhere = False
		for raw_goal in registry_payload["goals"]:
			if not isinstance(raw_goal, dict):
				continue
			coordination = raw_goal.get("coordination")
			if not isinstance(coordination, dict):
				continue
			registered_agents = coordination.get("registered_agents")
			if not isinstance(registered_agents, list):
				continue
			agents = normalize_registered_agents(registered_agents)
			if agent in agents:
				registered_anywhere = True
				if goal_id and str(raw_goal.get("id") or "") == goal_id:
					scope_match = True
		if scope_match or (not goal_id and registered_anywhere):
			return agent
	raise UnregisteredAssignee(
		f"agent {agent} is not registered for the risk scope"
	)


def new_empty_ledger(*, now: str | None = None) -> dict[str, Any]:
	stamp = now or now_utc_iso()
	return {
		"schema_version": LEDGER_SCHEMA_VERSION,
		"created_at": stamp,
		"updated_at": stamp,
		"last_scan": None,
		"applied_scan_ids": [],
		"records": {},
	}


def load_ledger(path: str | Path) -> dict[str, Any] | None:
	"""Return the ledger document, or None when the file is absent.

	Missing files never create one; malformed content raises
	:class:`RiskLedgerUnreadable` so callers fail closed.
	"""

	ledger_path = Path(path).expanduser()
	try:
		raw = ledger_path.read_text(encoding="utf-8")
	except FileNotFoundError:
		return None
	except OSError as exc:
		raise RiskLedgerUnreadable(f"ledger is unreadable: {exc.strerror or exc}") from exc
	try:
		payload = json.loads(raw)
	except json.JSONDecodeError as exc:
		raise RiskLedgerUnreadable(
			f"ledger content is malformed at line {exc.lineno}"
		) from exc
	if (
		not isinstance(payload, dict)
		or payload.get("schema_version") != LEDGER_SCHEMA_VERSION
		or not isinstance(payload.get("records"), dict)
	):
		raise RiskLedgerUnreadable("ledger document is missing required fields")
	return payload


def write_ledger_atomic(path: str | Path, ledger: dict[str, Any]) -> None:
	atomic_write_json(Path(path).expanduser(), ledger)


def initialize_ledger_file(path: str | Path) -> dict[str, Any]:
	"""Create an empty ledger, refusing to overwrite an existing one."""

	ledger_path = Path(path).expanduser()
	with exclusive_file_lock(
		ledger_path, operation="global_risk_ledger_init"
	):
		if ledger_path.exists():
			# Even malformed content must never be reset by init.
			raise RiskLedgerExists("a risk ledger already exists at this scope")
		ledger = new_empty_ledger()
		write_ledger_atomic(ledger_path, ledger)
		return ledger


class _LedgerSession:
	__slots__ = ("ledger", "exists", "dirty")

	def __init__(self, ledger: dict[str, Any], *, exists: bool) -> None:
		self.ledger = ledger
		self.exists = exists
		self.dirty = False


@contextmanager
def open_risk_ledger(path: str | Path) -> Iterator[_LedgerSession]:
	"""Lock a ledger for read-modify-write.

	Raises :class:`RiskLedgerNotFound` when absent and
	:class:`RiskLedgerUnreadable` when malformed; in both cases the context
	never writes.
	"""

	ledger_path = Path(path).expanduser()
	with exclusive_file_lock(
		ledger_path, operation="global_risk_ledger"
	):
		ledger = load_ledger(ledger_path)
		if ledger is None:
			raise RiskLedgerNotFound("no risk ledger exists at this registry scope")
		session = _LedgerSession(ledger, exists=True)
		try:
			yield session
		finally:
			if session.dirty:
				session.ledger["updated_at"] = now_utc_iso()
				write_ledger_atomic(ledger_path, session.ledger)


def _append_history(record: dict[str, Any], event: dict[str, Any]) -> None:
	history = record.setdefault("history", [])
	history.append(event)
	if len(history) > LEDGER_HISTORY_LIMIT:
		del history[: len(history) - LEDGER_HISTORY_LIMIT]


def _history_event(
	*,
	at: str,
	action: str,
	from_status: str | None,
	to_status: str,
	actor: str | None = None,
	note: str | None = None,
	reason: str | None = None,
	severity: str | None = None,
	scan_id: str | None = None,
) -> dict[str, Any]:
	event: dict[str, Any] = {
		"at": at,
		"action": action,
		"from_status": from_status,
		"to_status": to_status,
	}
	if actor:
		event["actor"] = actor
	if note:
		event["note"] = note
	if reason:
		event["reason"] = reason
	if severity:
		event["severity"] = severity
	if scan_id:
		event["scan_id"] = scan_id
	return event


def _change_status(
	record: dict[str, Any],
	target: RiskLifecycleStatus,
	*,
	at: str,
	action: str,
	actor: str | None = None,
	note: str | None = None,
	reason: str | None = None,
	severity: str | None = None,
	scan_id: str | None = None,
) -> None:
	previous = str(record.get("status") or RiskLifecycleStatus.OPEN.value)
	record["status"] = target.value
	record["status_changed_at"] = at
	_append_history(
		record,
		_history_event(
			at=at,
			action=action,
			from_status=previous,
			to_status=target.value,
			actor=actor,
			note=note,
			reason=reason,
			severity=severity,
			scan_id=scan_id,
		),
	)


def _new_record(
	*,
	identity: dict[str, Any],
	row: dict[str, Any],
	scan_id: str,
	at: str,
) -> dict[str, Any]:
	severity = normalize_severity(row.get("severity"))
	evidence_refs: list[str] = []
	for item in row.get("evidence_refs") or []:
		text = str(item)
		if text and text not in evidence_refs:
			evidence_refs.append(text)
	evidence_refs = evidence_refs[:LEDGER_EVIDENCE_LIMIT]
	record: dict[str, Any] = {
		"risk_id": identity["risk_id"],
		"source_surface": identity["source_surface"],
		"kind": identity["kind"],
		"scope": identity["scope"],
		"goal_id": identity["goal_id"],
		"category": str(row.get("category") or ""),
		"severity": severity,
		"max_severity": severity,
		"summary": str(row.get("summary") or ""),
		"next_safe_action": str(row.get("next_safe_action") or ""),
		"requires_user_approval": bool(row.get("requires_user_approval")),
		"evidence_refs": evidence_refs,
		"first_seen_at": at,
		"last_seen_at": at,
		"occurrence_count": _positive_count(row.get("occurrence_count")),
		"scan_count": 1,
		"last_scan_id": scan_id,
		"applied_scan_ids": [scan_id],
		"present_in_latest_scan": True,
		"status": RiskLifecycleStatus.OPEN.value,
		"status_changed_at": at,
		"acknowledged": None,
		"assignee": None,
		"resolved": None,
		"suppress_until": None,
		"suppressed": None,
		"reopen_count": 0,
		"history": [],
	}
	_append_history(
		record,
		_history_event(
			at=at,
			action="opened",
			from_status=None,
			to_status=RiskLifecycleStatus.OPEN.value,
			scan_id=scan_id,
			severity=severity,
		),
	)
	return record


def _positive_count(value: object) -> int:
	if isinstance(value, int) and not isinstance(value, bool) and value > 0:
		return value
	return 1


def _risk_identity(row: dict[str, Any]) -> dict[str, Any]:
	source_surface = str(row.get("source_surface") or "").strip()
	kind = str(row.get("kind") or "").strip()
	scope = str(row.get("scope") or "").strip() or "global"
	if not source_surface or not kind:
		raise ValueError("observed risk is missing stable identity fields")
	goal_id = str(row.get("goal_id") or "").strip() or None
	risk_id = stable_risk_id(
		source_surface=source_surface, kind=kind, scope=scope, goal_id=goal_id
	)
	declared = str(row.get("risk_id") or "").strip()
	if declared and declared != risk_id:
		raise ValueError("observed risk_id does not match its stable identity")
	return {
		"risk_id": risk_id,
		"source_surface": source_surface,
		"kind": kind,
		"scope": scope,
		"goal_id": goal_id,
	}


def _refresh_snapshot(
	record: dict[str, Any],
	row: dict[str, Any],
	*,
	escalated: bool,
) -> None:
	incoming = normalize_severity(row.get("severity"))
	if escalated:
		record["severity"] = incoming
		if _is_higher_severity(incoming, str(record.get("max_severity") or "")):
			record["max_severity"] = incoming
	for field in ("category", "summary", "next_safe_action"):
		value = str(row.get(field) or "").strip()
		if value:
			record[field] = value
	if "requires_user_approval" in row:
		record["requires_user_approval"] = bool(row.get("requires_user_approval"))
	evidence_refs = list(record.get("evidence_refs") or [])
	for item in row.get("evidence_refs") or []:
		text = str(item)
		if text and text not in evidence_refs:
			evidence_refs.append(text)
	record["evidence_refs"] = evidence_refs[:LEDGER_EVIDENCE_LIMIT]


def _apply_suppression_expiry(
	record: dict[str, Any],
	*,
	at: str,
	scan_id: str | None,
) -> bool:
	if record.get("status") != RiskLifecycleStatus.SUPPRESSED.value:
		return False
	deadline_text = record.get("suppress_until")
	deadline = parse_timestamp(deadline_text)
	reference = parse_timestamp(at)
	if deadline is None or reference is None or deadline > reference:
		return False
	record["suppress_until"] = None
	_change_status(
		record,
		RiskLifecycleStatus.OPEN,
		at=at,
		action="suppression_expired",
		reason="suppression_expired",
		scan_id=scan_id,
	)
	return True


def _reopen(
	record: dict[str, Any],
	*,
	at: str,
	reason: str,
	scan_id: str,
	severity: str | None = None,
) -> None:
	record["reopen_count"] = int(record.get("reopen_count") or 0) + 1
	record["resolved"] = None
	_change_status(
		record,
		RiskLifecycleStatus.OPEN,
		at=at,
		action="reopened",
		reason=reason,
		scan_id=scan_id,
		severity=severity,
	)


def _upsert_observed(
	record: dict[str, Any],
	row: dict[str, Any],
	*,
	scan_id: str,
	at: str,
	stats: dict[str, Any],
) -> None:
	incoming = normalize_severity(row.get("severity"))
	escalated = _is_higher_severity(incoming, str(record.get("severity") or ""))
	record["last_seen_at"] = at
	record["last_scan_id"] = scan_id
	applied_scan_ids = record.get("applied_scan_ids")
	if not isinstance(applied_scan_ids, list):
		applied_scan_ids = []
	applied_scan_ids.append(scan_id)
	if len(applied_scan_ids) > LEDGER_SCAN_ID_LIMIT:
		del applied_scan_ids[: len(applied_scan_ids) - LEDGER_SCAN_ID_LIMIT]
	record["applied_scan_ids"] = applied_scan_ids
	record["present_in_latest_scan"] = True
	record["occurrence_count"] = int(record.get("occurrence_count") or 0) + (
		_positive_count(row.get("occurrence_count"))
	)
	record["scan_count"] = int(record.get("scan_count") or 0) + 1
	_refresh_snapshot(record, row, escalated=escalated)

	status = record.get("status")
	if status == RiskLifecycleStatus.RESOLVED.value:
		_reopen(
			record,
			at=at,
			reason="reappeared",
			scan_id=scan_id,
			severity=incoming if escalated else None,
		)
		stats["reopened_count"] += 1
	elif status == RiskLifecycleStatus.SUPPRESSED.value:
		if escalated:
			record["suppress_until"] = None
			_reopen(
				record,
				at=at,
				reason="severity_escalated",
				scan_id=scan_id,
				severity=incoming,
			)
			stats["reopened_count"] += 1
	elif status in {
		RiskLifecycleStatus.ACKNOWLEDGED.value,
		RiskLifecycleStatus.ASSIGNED.value,
	}:
		if escalated:
			_append_history(
				record,
				_history_event(
					at=at,
					action="severity_escalated",
					from_status=status,
					to_status=status,
					reason="severity_escalated",
					scan_id=scan_id,
					severity=incoming,
				),
			)


def merge_scan(
	ledger: dict[str, Any],
	observed_rows: list[dict[str, Any]],
	*,
	scan_id: str,
	scanned_at: str | None = None,
	now: str | None = None,
) -> dict[str, Any]:
	"""Merge one scan batch into the ledger (pure, in-place).

	Replaying the same ``scan_id`` is fully idempotent and reports
	``dirty=False`` when nothing else changed.
	"""

	at = scanned_at or now or now_utc_iso()
	records = ledger.setdefault("records", {})
	stats: dict[str, Any] = {
		"dirty": False,
		"reopened_count": 0,
		"suppressed_expired_count": 0,
		"observed_count": 0,
		"new_count": 0,
	}

	# Automatic suppression expiry applies to every record, including records
	# absent from this scan.
	for record in records.values():
		if _apply_suppression_expiry(record, at=at, scan_id=scan_id):
			stats["suppressed_expired_count"] += 1
			stats["dirty"] = True

	for record in records.values():
		record["present_in_latest_scan"] = False

	observed_identities: set[str] = set()
	for raw_row in observed_rows:
		if not isinstance(raw_row, dict):
			continue
		identity = _risk_identity(raw_row)
		risk_id = identity["risk_id"]
		if risk_id in observed_identities:
			continue
		observed_identities.add(risk_id)
		stats["observed_count"] += 1
		existing = records.get(risk_id)
		if existing is None:
			records[risk_id] = _new_record(
				identity=identity, row=raw_row, scan_id=scan_id, at=at
			)
			stats["new_count"] += 1
			stats["dirty"] = True
			continue
		applied_scan_ids = existing.get("applied_scan_ids")
		if not isinstance(applied_scan_ids, list):
			applied_scan_ids = []
		if existing.get("last_scan_id") == scan_id or scan_id in applied_scan_ids:
			# Idempotent replay of an already applied scan batch: presence
			# only, even when newer scans have interleaved in between.
			existing["present_in_latest_scan"] = True
			continue
		_upsert_observed(
			existing,
			raw_row,
			scan_id=scan_id,
			at=at,
			stats=stats,
		)
		# Observation always updates counts/timestamps for a new scan batch.
		stats["dirty"] = True

	last_scan = ledger.get("last_scan")
	applied_scan_ids = ledger.get("applied_scan_ids")
	if not isinstance(applied_scan_ids, list):
		applied_scan_ids = []
	already_applied = (
		isinstance(last_scan, dict) and last_scan.get("scan_id") == scan_id
	) or scan_id in applied_scan_ids
	if not already_applied:
		ledger["last_scan"] = {"scan_id": scan_id, "scanned_at": at}
		applied_scan_ids.append(scan_id)
		if len(applied_scan_ids) > LEDGER_SCAN_ID_LIMIT:
			del applied_scan_ids[: len(applied_scan_ids) - LEDGER_SCAN_ID_LIMIT]
		ledger["applied_scan_ids"] = applied_scan_ids
		stats["dirty"] = True

	stats["record_count"] = len(records)
	stats["by_status"] = by_status(records)
	return stats


def _snapshot_signature(record: dict[str, Any]) -> tuple[Any, ...]:
	return (
		record.get("severity"),
		record.get("summary"),
		record.get("next_safe_action"),
		record.get("requires_user_approval"),
		tuple(record.get("evidence_refs") or []),
	)


def by_status(records: dict[str, Any]) -> dict[str, int]:
	counts = {status.value: 0 for status in RiskLifecycleStatus}
	for record in records.values():
		status = str(record.get("status") or "")
		if status in counts:
			counts[status] += 1
	return counts


def apply_due_expirations(
	ledger: dict[str, Any],
	*,
	now: str | None = None,
) -> int:
	"""Land due suppression expirations without a fresh scan (show path)."""

	at = now or now_utc_iso()
	expired = 0
	for record in ledger.setdefault("records", {}).values():
		if _apply_suppression_expiry(record, at=at, scan_id=None):
			expired += 1
	return expired


def apply_lifecycle(
	ledger: dict[str, Any],
	risk_id: str,
	action: str,
	*,
	now: str | None = None,
	actor: str | None = None,
	note: str | None = None,
	agent: str | None = None,
	until: str | None = None,
) -> dict[str, Any]:
	"""Apply one manual lifecycle action to a record (pure, in-place)."""

	at = now or now_utc_iso()
	records = ledger.setdefault("records", {})
	record = records.get(str(risk_id or ""))
	if not isinstance(record, dict):
		raise RiskRecordNotFound(f"unknown risk id {risk_id}")

	_apply_suppression_expiry(record, at=at, scan_id=None)
	status = RiskLifecycleStatus(str(record.get("status") or ""))
	allowed = _MANUAL_TRANSITIONS.get(action)
	if allowed is None:
		raise RiskLifecycleTransitionError(f"unknown lifecycle action {action}")
	if status not in allowed:
		raise RiskLifecycleTransitionError(
			f"action {action} is not valid for status {status.value}"
		)

	safe_actor = _redact_text(actor, limit=120) if actor else None
	safe_note = _redact_text(note, limit=260) if note else None

	if action == "acknowledged":
		record["acknowledged"] = {"at": at, "by": safe_actor}
		_change_status(
			record,
			RiskLifecycleStatus.ACKNOWLEDGED,
			at=at,
			action="acknowledged",
			actor=safe_actor,
			note=safe_note,
		)
	elif action == "assigned":
		if not agent:
			raise RiskLifecycleTransitionError("assign requires an agent id")
		record["assignee"] = agent
		_change_status(
			record,
			RiskLifecycleStatus.ASSIGNED,
			at=at,
			action="assigned",
			actor=safe_actor,
			note=safe_note,
		)
	elif action == "resolved":
		record["resolved"] = {"at": at, "by": safe_actor}
		record["resolved"]["note"] = safe_note
		_change_status(
			record,
			RiskLifecycleStatus.RESOLVED,
			at=at,
			action="resolved",
			actor=safe_actor,
			note=safe_note,
		)
	else:  # suppressed
		deadline = parse_suppression_deadline(until, now=at)
		record["suppress_until"] = deadline
		record["suppressed"] = {"at": at, "by": safe_actor}
		_change_status(
			record,
			RiskLifecycleStatus.SUPPRESSED,
			at=at,
			action="suppressed",
			actor=safe_actor,
			note=safe_note,
		)
	return record


def record_matches_filters(
	record: dict[str, Any],
	*,
	statuses: set[str] | None = None,
	severities: set[str] | None = None,
	assignee: str | None = None,
) -> bool:
	if statuses is not None and str(record.get("status") or "") not in statuses:
		return False
	if severities is not None:
		severity = str(record.get("severity") or "")
		max_severity = str(record.get("max_severity") or severity)
		if severity not in severities and max_severity not in severities:
			return False
	if assignee is not None:
		if assignee == "unassigned":
			if record.get("assignee"):
				return False
		elif record.get("assignee") != assignee:
			return False
	return True


def ledger_view(
	ledger: dict[str, Any],
	*,
	statuses: set[str] | None = None,
	severities: set[str] | None = None,
	assignee: str | None = None,
	include_absent: bool = False,
	risk_id: str | None = None,
	limit: int = 100,
) -> dict[str, Any]:
	"""Return a bounded, filtered projection of persisted ledger records."""

	bounded_limit = min(max(1, int(limit)), 400)
	records = ledger.get("records")
	if not isinstance(records, dict):
		records = {}
	matched: list[dict[str, Any]] = []
	for record in records.values():
		if not isinstance(record, dict):
			continue
		if risk_id and record.get("risk_id") != risk_id:
			continue
		if not include_absent and record.get("present_in_latest_scan") is not True:
			continue
		if not record_matches_filters(
			record,
			statuses=statuses,
			severities=severities,
			assignee=assignee,
		):
			continue
		matched.append(record)
	matched.sort(key=_ledger_record_sort_key)
	retained = matched[:bounded_limit]
	return {
		"schema_version": LEDGER_SCHEMA_VERSION,
		"generated_at": now_utc_iso(),
		"summary": {
			"record_count": len(records),
			"matched_count": len(matched),
			"returned_count": len(retained),
			"truncated": len(matched) > len(retained),
			"by_status": by_status(records),
			"last_scan": ledger.get("last_scan"),
		},
		"records": retained,
	}


def _ledger_record_sort_key(record: dict[str, Any]) -> tuple[int, str, str]:
	return (
		SEVERITY_RANK.get(str(record.get("severity") or ""), _DEFAULT_SEVERITY_RANK),
		str(record.get("status") or ""),
		str(record.get("risk_id") or ""),
	)


def merge_scan_file(
	path: str | Path,
	observed_rows: list[dict[str, Any]],
	*,
	scan_id: str,
	scanned_at: str | None = None,
	now: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
	"""Merge one scan into the on-disk ledger.

	Returns ``("absent", None)`` when no ledger has been established (no file
	is created) and ``("merged", stats)`` after a locked merge. Malformed
	ledgers raise :class:`RiskLedgerUnreadable` without writing.
	"""

	ledger_path = Path(path).expanduser()
	if not ledger_path.exists():
		return "absent", None
	with open_risk_ledger(ledger_path) as session:
		stats = merge_scan(
			session.ledger,
			observed_rows,
			scan_id=scan_id,
			scanned_at=scanned_at,
			now=now,
		)
		session.dirty = bool(stats.get("dirty"))
	return "merged", stats


def apply_due_expirations_file(path: str | Path, *, now: str | None = None) -> int:
	with open_risk_ledger(path) as session:
		expired = apply_due_expirations(session.ledger, now=now)
		session.dirty = expired > 0
		return expired


def apply_lifecycle_file(
	path: str | Path,
	risk_id: str,
	action: str,
	*,
	now: str | None = None,
	actor: str | None = None,
	note: str | None = None,
	agent: str | None = None,
	until: str | None = None,
) -> dict[str, Any]:
	with open_risk_ledger(path) as session:
		record = apply_lifecycle(
			session.ledger,
			risk_id,
			action,
			now=now,
			actor=actor,
			note=note,
			agent=agent,
			until=until,
		)
		session.dirty = True
		return record


# --- markdown rendering (colocated with the ledger seam, as in global_risks) --


def render_ledger_view_markdown(view: dict[str, Any]) -> str:
	summary = view.get("summary") if isinstance(view, dict) else None
	if not isinstance(summary, dict):
		summary = {}
	lines = [
		"# LoopX Global Risk Ledger",
		"",
		f"- matched: `{summary.get('matched_count', 0)}`",
		f"- returned: `{summary.get('returned_count', 0)}`",
		f"- truncated: `{bool(summary.get('truncated'))}`",
		f"- last_scan: `{str((summary.get('last_scan') or {}).get('scan_id') or 'none')}`",
		"",
	]
	records = view.get("records") if isinstance(view, dict) else None
	if not records:
		lines.append("- No matching ledger records.")
		return "\n".join(lines)
	for record in records:
		if not isinstance(record, dict):
			continue
		goal = str(record.get("goal_id") or "") or "global"
		line = (
			f"- `{record.get('risk_id')}` status=`{record.get('status')}` "
			f"severity=`{record.get('severity')}` goal=`{goal}` "
			f"kind=`{record.get('kind')}` total=`{record.get('occurrence_count')}` "
			f"scans=`{record.get('scan_count')}` reopens=`{record.get('reopen_count', 0)}`"
		)
		if record.get("assignee"):
			line += f" assignee=`{record['assignee']}`"
		if record.get("suppress_until"):
			line += f" suppressed_until=`{record['suppress_until']}`"
		line += f": {record.get('summary') or 'Structured risk requires attention.'}"
		line += (
			f" first=`{record.get('first_seen_at')}`"
			f" last=`{record.get('last_seen_at')}`"
		)
		lines.append(line)
	return "\n".join(lines)


def render_ledger_action_markdown(payload: dict[str, Any]) -> str:
	if not payload.get("ok"):
		return (
			"# LoopX Risk Ledger Error\n\n"
			f"- error_code: `{payload.get('error_code')}`\n"
			f"- error: {payload.get('error')}\n"
		)
	record = payload.get("record")
	lines = [
		"# LoopX Risk Ledger Updated",
		"",
		f"- action: `{payload.get('action')}`",
	]
	if isinstance(record, dict):
		lines.append(f"- risk_id: `{record.get('risk_id')}`")
		lines.append(f"- status: `{record.get('status')}`")
		if record.get("assignee"):
			lines.append(f"- assignee: `{record['assignee']}`")
		if record.get("suppress_until"):
			lines.append(f"- suppressed_until: `{record['suppress_until']}`")
	return "\n".join(lines)
