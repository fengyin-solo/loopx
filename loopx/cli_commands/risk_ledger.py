from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import risk_ledger as ledger_module
from ..risk_ledger import (
	LEDGER_SCHEMA_VERSION,
	RiskLedgerError,
	render_ledger_action_markdown,
	render_ledger_view_markdown,
)


PrintPayload = Callable[
	[dict[str, object], str, Callable[[dict[str, object]], str]],
	None,
]
FormatSelector = Callable[..., str]
AddFormat = Callable[[argparse.ArgumentParser], None]

_LIFECYCLE_CHOICES = sorted(
	status.value for status in ledger_module.RiskLifecycleStatus
)
_SEVERITY_CHOICES = sorted(ledger_module.SEVERITY_RANK)
_MUTATION_COMMANDS = {"ack", "assign", "resolve", "suppress"}
_ACTION_VERBS = {
	"ack": "acknowledged",
	"assign": "assigned",
	"resolve": "resolved",
	"suppress": "suppressed",
}


def register_risk_ledger_commands(
	subparsers: argparse._SubParsersAction,
	add_subcommand_format: AddFormat,
) -> None:
	parser = subparsers.add_parser(
		"risk-ledger",
		help=(
			"Maintain the persistent global risk ledger: init, acknowledge, "
			"assign, resolve, suppress, and show lifecycle records."
		),
	)
	commands = parser.add_subparsers(dest="risk_ledger_command", required=True)

	init = commands.add_parser(
		"init", help="Create the ledger beside the current registry once."
	)
	add_subcommand_format(init)

	show = commands.add_parser(
		"show", help="Show ledger records with status/severity/assignee filters."
	)
	add_subcommand_format(show)
	show.add_argument(
		"--status",
		action="append",
		dest="ledger_status",
		choices=_LIFECYCLE_CHOICES,
		help="Lifecycle status to include; repeatable.",
	)
	show.add_argument(
		"--severity",
		action="append",
		dest="ledger_severity",
		choices=_SEVERITY_CHOICES,
		help="Severity to include (current or maximum); repeatable.",
	)
	show.add_argument(
		"--assignee",
		dest="ledger_assignee",
		help="Registered agent id, or `unassigned` for records with no owner.",
	)
	show.add_argument(
		"--include-absent",
		action="store_true",
		help="Include records absent from the latest scan (hidden by default).",
	)
	show.add_argument("--risk-id", dest="ledger_risk_id")
	show.add_argument("--limit", type=int, default=100)

	ack = commands.add_parser(
		"ack", help="Acknowledge one risk record (confirmed received)."
	)
	add_subcommand_format(ack)
	_add_mutation_args(ack)

	assign = commands.add_parser(
		"assign", help="Assign one risk record to a registered agent."
	)
	add_subcommand_format(assign)
	_add_mutation_args(assign)
	assign.add_argument("agent", help="Registered agent id to own the risk.")

	resolve = commands.add_parser(
		"resolve", help="Mark one risk record as handled."
	)
	add_subcommand_format(resolve)
	_add_mutation_args(resolve)

	suppress = commands.add_parser(
		"suppress",
		help="Suppress one risk record until an ISO8601 instant or Nh/Nd deadline.",
	)
	add_subcommand_format(suppress)
	suppress.add_argument(
		"--until",
		required=True,
		help="Suppression deadline: ISO8601 instant or positive Nh/Nd duration.",
	)
	_add_mutation_args(suppress)


def _add_mutation_args(parser: argparse.ArgumentParser) -> None:
	parser.add_argument("risk_id", help="Stable 16-character ledger risk id.")
	parser.add_argument(
		"--by",
		default=None,
		help="Actor recording the disposition; defaults to $LOOPX_AGENT_ID.",
	)
	parser.add_argument("--note", default=None, help="Public-safe disposition note.")


def _error_payload(command: str, exc: RiskLedgerError | Exception) -> dict[str, Any]:
	code = getattr(exc, "code", "risk_ledger_error")
	return {
		"ok": False,
		"schema_version": LEDGER_SCHEMA_VERSION,
		"command": f"risk-ledger {command}",
		"error_code": code,
		"error": str(exc)[:300],
	}


def _action_payload(action: str, record: dict[str, Any]) -> dict[str, Any]:
	return {
		"ok": True,
		"schema_version": LEDGER_SCHEMA_VERSION,
		"command": f"risk-ledger {action}",
		"action": _ACTION_VERBS.get(action, action),
		"record": record,
	}


def _load_registry_payload(registry_path: Path) -> dict[str, Any]:
	try:
		raw = registry_path.read_text(encoding="utf-8")
	except OSError as exc:
		raise RiskLedgerError(
			f"registry is required for agent validation: {exc.strerror or exc}"
		) from exc
	try:
		payload = json.loads(raw)
	except json.JSONDecodeError as exc:
		raise RiskLedgerError(
			f"registry is malformed for agent validation at line {exc.lineno}"
		) from exc
	if not isinstance(payload, dict):
		raise RiskLedgerError("registry payload is not an object")
	return payload


def handle_risk_ledger_command(
	args: argparse.Namespace,
	*,
	registry_path: Path,
	output_format: FormatSelector,
	print_payload: PrintPayload,
) -> int | None:
	if args.command != "risk-ledger":
		return None
	command = args.risk_ledger_command
	ledger_path = ledger_module.risk_ledger_path_for_registry(registry_path)

	try:
		if command == "init":
			ledger = ledger_module.initialize_ledger_file(ledger_path)
			payload: dict[str, Any] = {
				"ok": True,
				"schema_version": LEDGER_SCHEMA_VERSION,
				"command": "risk-ledger init",
				"action": "initialized",
				"summary": {
					"record_count": len(ledger.get("records", {})),
					"created_at": ledger.get("created_at"),
				},
			}
			print_payload(
				payload,
				output_format(args),
				render_ledger_action_markdown,
			)
			return 0

		if command == "show":
			ledger_module.apply_due_expirations_file(ledger_path)
			ledger = ledger_module.load_ledger(ledger_path)
			if ledger is None:
				raise ledger_module.RiskLedgerNotFound(
					"no risk ledger exists at this registry; run `loopx risk-ledger init`"
				)
			statuses = set(args.ledger_status or [])
			severities = set(args.ledger_severity or [])
			assignee = args.ledger_assignee
			if assignee and assignee != "unassigned":
				normalized = ledger_module.normalize_assignee_token(assignee)
				if normalized is None:
					raise RiskLedgerError(f"invalid assignee token {assignee}")
				assignee = normalized
			view = ledger_module.ledger_view(
				ledger,
				statuses=statuses or None,
				severities=severities or None,
				assignee=assignee,
				include_absent=bool(args.include_absent),
				risk_id=args.ledger_risk_id,
				limit=max(1, args.limit),
			)
			view["command"] = "risk-ledger show"
			print_payload(
				view,
				output_format(args),
				render_ledger_view_markdown,
			)
			return 0

		# Mutation commands
		actor = args.by or os.environ.get("LOOPX_AGENT_ID") or None
		until = getattr(args, "until", None)
		agent: str | None = None
		if command == "assign":
			agent = ledger_module.normalize_assignee_token(args.agent)
			if agent is None:
				raise RiskLedgerError(f"invalid agent id {args.agent}")
			existing = ledger_module.load_ledger(ledger_path)
			if existing is None:
				raise ledger_module.RiskLedgerNotFound(
					"no risk ledger exists at this registry; run `loopx risk-ledger init`"
				)
			target = existing["records"].get(args.risk_id)
			if not isinstance(target, dict):
				raise ledger_module.RiskRecordNotFound(
					f"unknown risk id {args.risk_id}"
				)
			registry_payload = _load_registry_payload(registry_path)
			ledger_module.validate_registered_assignee(
				registry_payload,
				goal_id=target.get("goal_id"),
				agent=agent,
			)
		record = ledger_module.apply_lifecycle_file(
			ledger_path,
			args.risk_id,
			_ACTION_VERBS[command],
			actor=actor,
			note=getattr(args, "note", None),
			agent=agent,
			until=until,
		)
		payload = _action_payload(command, record)
	except RiskLedgerError as exc:
		payload = _error_payload(command, exc)
	print_payload(payload, output_format(args), render_ledger_action_markdown)
	return 0 if payload.get("ok") else 1
