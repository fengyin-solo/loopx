from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

import loopx.cli_commands.risk_ledger as risk_cli
import loopx.cli_commands.summary_all as manager_cli
import loopx.global_risks as global_risks
from loopx.cli import build_parser
from loopx.cli_runtime import output_format as resolve_format

FROZEN_TIME = "2026-09-15T12:00:00Z"
LATER_TIME = "2026-09-16T12:00:00Z"


def _status_payload() -> dict[str, Any]:
	return {
		"ok": True,
		"contract": {
			"error_diagnostics": [
				{
					"code": "public_boundary_violation",
					"message": "A redacted boundary finding.",
					"severity": "error",
					"scope": "global",
				}
			]
		},
		"global_registry": {"available": True, "findings": []},
		"attention_queue": {"items": []},
	}


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
	path = tmp_path / "registry.json"
	path.write_text(
		json.dumps(
			{
				"goals": [
					{"id": "goal-1", "coordination": {"registered_agents": ["agent-a"]}}
				]
			}
		),
		encoding="utf-8",
	)
	return path


class Captured:
	def __init__(self) -> None:
		self.outputs: list[tuple[dict[str, Any], str, Any]] = []

	def print(self, value: dict[str, Any], format_name: str, renderer: Any) -> None:
		self.outputs.append((value, format_name, renderer))

	@property
	def payload(self) -> dict[str, Any]:
		return self.outputs[-1][0]


def _run_captured(
	argv: list[str], *, registry_path: Path, captured: Captured
) -> int:
	args = build_parser().parse_args(argv)
	fmt = resolve_format
	if args.command == "risk-ledger":
		code = risk_cli.handle_risk_ledger_command(
			args,
			registry_path=registry_path,
			output_format=fmt,
			print_payload=captured.print,
		)
	else:
		code = manager_cli.handle_summary_all_command(
			args,
			registry_path=registry_path,
			runtime_root_arg=None,
			output_format=fmt,
			print_payload=captured.print,
		)
	assert code is not None
	return code


def test_parser_registers_risk_ledger_actions() -> None:
	args = build_parser().parse_args(
		["risk-ledger", "suppress", "abc123", "--until", "7d"]
	)
	assert args.command == "risk-ledger"
	assert args.risk_ledger_command == "suppress"
	assert args.risk_id == "abc123"
	assert args.until == "7d"

	with pytest.raises(SystemExit) as exit_info:
		build_parser().parse_args(["risk-ledger", "suppress", "abc123"])
	assert exit_info.value.code == 2

	show_args = build_parser().parse_args(
		[
			"risk-ledger",
			"show",
			"--status",
			"open",
			"--status",
			"assigned",
			"--severity",
			"high",
			"--assignee",
			"unassigned",
			"--include-absent",
		]
	)
	assert show_args.ledger_status == ["open", "assigned"]
	assert show_args.ledger_severity == ["high"]
	assert show_args.ledger_assignee == "unassigned"
	assert show_args.include_absent is True

	risks_args = build_parser().parse_args(
		["global-risks", "--status", "open", "--severity", "high"]
	)
	assert risks_args.risk_status == ["open"]
	assert risks_args.risk_severity == ["high"]


def test_risk_ledger_full_cli_lifecycle(
	monkeypatch: pytest.MonkeyPatch, registry_path: Path
) -> None:
	captured = Captured()
	scan_times = iter([FROZEN_TIME, LATER_TIME])

	def fake_collect_status(**_kwargs: Any) -> dict[str, Any]:
		return deepcopy(_status_payload())

	monkeypatch.setattr(global_risks, "collect_status", fake_collect_status)
	monkeypatch.setattr(
		global_risks, "now_utc_iso", lambda: next(scan_times)
	)

	# init works, and refuses to reset an existing ledger.
	assert _run_captured(["risk-ledger", "init"], registry_path=registry_path, captured=captured) == 0
	assert captured.payload["action"] == "initialized"
	assert _run_captured(["risk-ledger", "init"], registry_path=registry_path, captured=captured) == 1
	assert captured.payload["error_code"] == "risk_ledger_already_initialized"

	# scan merges findings into the ledger and annotates the projection.
	assert _run_captured(["global-risks"], registry_path=registry_path, captured=captured) == 0
	scan_payload = captured.payload
	assert scan_payload["ok"] is True
	assert scan_payload["summary"]["ledger"]["enabled"] is True
	risk_id = scan_payload["risks"][0]["ledger"]["risk_id"]

	# show lists the new open record.
	assert _run_captured(["risk-ledger", "show"], registry_path=registry_path, captured=captured) == 0
	show_payload = captured.payload
	assert show_payload["summary"]["matched_count"] == 1
	assert show_payload["records"][0]["status"] == "open"

	# assignment validates registered agents.
	bad = _run_captured(
		["risk-ledger", "assign", risk_id, "agent-b"],
		registry_path=registry_path,
		captured=captured,
	)
	assert bad == 1
	assert captured.payload["error_code"] == "unregistered_assignee"

	assert _run_captured(
		["risk-ledger", "assign", risk_id, "agent-a", "--by", "operator"],
		registry_path=registry_path,
		captured=captured,
	) == 0
	assert captured.payload["record"]["status"] == "assigned"
	assert captured.payload["record"]["assignee"] == "agent-a"

	# unknown risk ids fail closed without writing.
	assert _run_captured(
		["risk-ledger", "ack", "0000000000000000"],
		registry_path=registry_path,
		captured=captured,
	) == 1
	assert captured.payload["error_code"] == "risk_record_not_found"

	assert _run_captured(
		["risk-ledger", "ack", risk_id, "--note", "looking at it"],
		registry_path=registry_path,
		captured=captured,
	) == 0
	assert captured.payload["record"]["status"] == "acknowledged"

	# invalid deadlines fail before mutating state.
	assert _run_captured(
		["risk-ledger", "suppress", risk_id, "--until", "nonsense"],
		registry_path=registry_path,
		captured=captured,
	) == 1
	assert captured.payload["error_code"] == "invalid_suppression_deadline"

	assert _run_captured(
		["risk-ledger", "suppress", risk_id, "--until", "7d"],
		registry_path=registry_path,
		captured=captured,
	) == 0
	assert captured.payload["record"]["status"] == "suppressed"
	suppressed_record = captured.payload["record"]
	assert suppressed_record["suppress_until"] is not None
	# The CLI uses wall-clock time; the deadline must be ~7 days out.
	from datetime import datetime, timezone

	from loopx.control_plane.runtime.time import parse_timestamp

	deadline = parse_timestamp(suppressed_record["suppress_until"])
	delta_seconds = (deadline - datetime.now(timezone.utc)).total_seconds()
	assert 6 * 86400 < delta_seconds <= 7 * 86400

	# second scan (same severity) keeps suppression; show filters find it.
	assert _run_captured(
		["global-risks", "--status", "suppressed"],
		registry_path=registry_path,
		captured=captured,
	) == 0
	assert [row["kind"] for row in captured.payload["risks"]] == [
		"public_boundary_violation"
	]

	markdown_args = build_parser().parse_args(["risk-ledger", "show", "--format", "markdown"])
	code = risk_cli.handle_risk_ledger_command(
		markdown_args,
		registry_path=registry_path,
		output_format=resolve_format,
		print_payload=captured.print,
	)
	assert code == 0
	assert captured.outputs[-1][1] == "markdown"
	rendered = captured.outputs[-1][2](captured.payload)
	assert f"`{risk_id}`" in rendered
	assert "status=`suppressed`" in rendered

	# no local paths or raw registry content leak into JSON output
	serialized = json.dumps(captured.payload, ensure_ascii=False)
	assert str(registry_path.parent) not in serialized
	assert "registered_agents" not in serialized


def test_commands_without_ledger_fail(registry_path: Path) -> None:
	captured = Captured()
	assert _run_captured(
		["risk-ledger", "show"], registry_path=registry_path, captured=captured
	) == 1
	assert captured.payload["error_code"] == "risk_ledger_not_initialized"
	assert _run_captured(
		["risk-ledger", "ack", "0000000000000000"],
		registry_path=registry_path,
		captured=captured,
	) == 1
	assert captured.payload["error_code"] == "risk_ledger_not_initialized"


def test_global_risks_filter_requires_initialized_ledger(
	monkeypatch: pytest.MonkeyPatch, registry_path: Path
) -> None:
	captured = Captured()
	monkeypatch.setattr(
		global_risks, "collect_status", lambda **_: deepcopy(_status_payload())
	)
	code = _run_captured(
		["global-risks", "--status", "open"],
		registry_path=registry_path,
		captured=captured,
	)
	assert code == 1
	assert captured.payload["ok"] is False
	assert captured.payload["error_code"] == "risk_ledger_not_initialized"


def test_global_risks_without_ledger_remains_legacy(
	monkeypatch: pytest.MonkeyPatch, registry_path: Path
) -> None:
	captured = Captured()
	monkeypatch.setattr(
		global_risks, "collect_status", lambda **_: deepcopy(_status_payload())
	)
	code = _run_captured(["global-risks"], registry_path=registry_path, captured=captured)
	assert code == 0
	payload = captured.payload
	assert payload["ok"] is True
	assert "ledger" not in payload["summary"]
	assert "ledger" not in payload["risks"][0]
	assert not (registry_path.parent / "global-risk-ledger.json").exists()
