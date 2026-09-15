#!/usr/bin/env python3
"""Smoke-test dreaming proposal ledger de-duplication, conflicts, and lifecycle.

The CLI flow under test:

* consolidate one mixed signal batch -> equivalent proposals merge, opposing
  proposals are pairwise conflict-annotated and ordered by sidecar policy;
* replaying the same batch is byte-for-byte idempotent and never overwrites;
* defer records a reason and an expiry, and active defers suppress the same
  window; expired defers reopen into adjudication;
* reject suppresses the same evidence window and terminal decisions cannot be
  overwritten; decision history stays append-only across reopen.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
GOAL_ID = "dreaming-ledger-fixture"


def run_cli(
    *args: str,
    registry_path: Path,
    runtime: Path,
    fmt: str = "json",
    check: bool = True,
) -> dict:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "loopx.cli",
            "--registry",
            str(registry_path),
            "--runtime-root",
            str(runtime),
            "--format",
            fmt,
            *args,
        ],
        cwd=REPO_ROOT,
        check=check,
        capture_output=True,
        text=True,
    )
    if fmt == "markdown":
        return {"markdown": result.stdout}
    return json.loads(result.stdout)


def append_run(runs_dir: Path, *, generated_at: str, classification: str, action: str) -> None:
    runs_dir.mkdir(parents=True, exist_ok=True)
    stem = generated_at.replace(":", "-")
    json_path = runs_dir / f"{stem}.json"
    markdown_path = runs_dir / f"{stem}.md"
    record = {
        "generated_at": generated_at,
        "goal_id": GOAL_ID,
        "classification": classification,
        "recommended_action": action,
        "delivery_outcome": "outcome_progress",
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
    }
    json_path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text("# Fixture Run\n", encoding="utf-8")
    with (runs_dir / "index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def write_fixture(root: Path) -> tuple[Path, Path, Path, Path, Path]:
    project = root / "project"
    runtime = root / "runtime"
    state_file = f".codex/goals/{GOAL_ID}/ACTIVE_GOAL_STATE.md"
    state_path = project / state_file
    registry_path = project / ".loopx" / "registry.json"
    policy_path = project / ".loopx" / "dreaming-policy.json"
    runs_dir = runtime / "goals" / GOAL_ID / "runs"

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        "---\n"
        "status: active\n"
        "updated_at: 2026-01-01T00:00:00+00:00\n"
        "---\n\n"
        "# Dreaming Ledger Fixture\n\n"
        "## Next Action\n\n"
        "- Wait for explicit proposal adjudication.\n\n"
        "## Agent Todo\n\n"
        "- [ ] [P1] Keep normal delivery separate from advisory dreaming.\n",
        encoding="utf-8",
    )
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "common_runtime_root": str(runtime),
                "goals": [
                    {
                        "id": GOAL_ID,
                        "domain": "dreaming-ledger-fixture",
                        "status": "active",
                        "repo": str(project),
                        "state_file": state_file,
                        "adapter": {
                            "kind": "harness_self_improvement",
                            "status": "connected-read-only",
                        },
                        "quota": {
                            "compute": 1.0,
                            "window_hours": 24,
                            "allowed_slots": 5,
                        },
                    }
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    # Sidecar policy auto-discovered next to the registry: prefer the
    # convergent side whenever scope-direction proposals conflict.
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": "dreaming_policy_v0",
                "conflict_priority": {"scope_direction": "converge"},
                "prefer_stronger_evidence": True,
                "default_defer_ttl_hours": 168,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    append_run(
        runs_dir,
        generated_at="2026-01-01T00:03:00+00:00",
        classification="docs_governance_refactor_warning_merged",
        action="Repeated refactor warning about state drift and a duplicate seam.",
    )
    append_run(
        runs_dir,
        generated_at="2026-01-01T00:02:30+00:00",
        classification="delivery_lane_option",
        action="Explore an alternative lane and investigate new options.",
    )
    append_run(
        runs_dir,
        generated_at="2026-01-01T00:02:00+00:00",
        classification="team_retro_lesson",
        action="Consolidate the repeated lesson into a playbook skill.",
    )
    append_run(
        runs_dir,
        generated_at="2026-01-01T00:01:00+00:00",
        classification="workflow_handoff_note",
        action="Handoff completed with a generic delivery signal to review.",
    )
    return registry_path, runtime, state_path, runs_dir / "index.jsonl", policy_path


def assert_no_quota_spend(runtime: Path) -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in runtime.rglob("*.json*"))
    assert "quota_slot_spent" not in text, text


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="loopx-dreaming-ledger-") as tmp:
        registry_path, runtime, state_path, index_path, policy_path = write_fixture(
            Path(tmp)
        )
        before_state = state_path.read_text(encoding="utf-8")

        first = run_cli(
            "dreaming",
            "consolidate",
            "--goal-id",
            GOAL_ID,
            "--limit",
            "10",
            registry_path=registry_path,
            runtime=runtime,
        )
        assert first["ok"] is True, first
        assert first["schema_version"] == "dreaming_consolidation_v0", first
        assert first["dry_run"] is False, first
        assert first["counts"]["new"] == 3, first
        assert first["counts"]["conflicts"] == 2, first
        assert first["counts"]["pending_human"] == 0, first
        assert first["ledger_written"] is True, first
        proposals = {item["proposal_type"]: item for item in first["proposals"]}
        assert set(proposals) == {
            "refactor_warning",
            "memory_consolidation",
            "exploration",
        }, proposals
        refactor_id = proposals["refactor_warning"]["proposal_id"]
        memory_id = proposals["memory_consolidation"]["proposal_id"]
        explore_id = proposals["exploration"]["proposal_id"]
        assert proposals["refactor_warning"]["merged_source_count"] == 1, proposals
        for conflict in first["conflicts"]:
            assert conflict["conflict_type"] == "scope_direction", conflict
            assert conflict["resolution"] == "priority", conflict
            assert conflict["priority_rule"] == "configured_priority:scope_direction=converge", conflict
            preferred = conflict["preferred_proposal_id"]
            assert preferred in {refactor_id, memory_id}, conflict
            assert explore_id in conflict["proposal_ids"], conflict
            assert all(item["evidence_count"] >= 1 for item in conflict["basis"]), conflict
        ledger_path = Path(first["ledger_path"])
        assert ledger_path.exists(), first

        # Markdown surface renders the same conflict and merge information.
        markdown = run_cli(
            "dreaming",
            "consolidate",
            "--goal-id",
            GOAL_ID,
            "--limit",
            "10",
            registry_path=registry_path,
            runtime=runtime,
            fmt="markdown",
        )["markdown"]
        assert markdown.startswith("# Dreaming Proposal Consolidation"), markdown
        assert "configured_priority:scope_direction=converge" in markdown, markdown

        # Replaying the identical batch is idempotent: dry-run never writes, a
        # real replay changes nothing and leaves identical ledger bytes.
        preview = run_cli(
            "dreaming",
            "consolidate",
            "--goal-id",
            GOAL_ID,
            "--limit",
            "10",
            "--dry-run",
            registry_path=registry_path,
            runtime=runtime,
        )
        assert preview["ok"] is True, preview
        assert preview["ledger_written"] is False, preview
        ledger_bytes = ledger_path.read_text(encoding="utf-8")
        replay = run_cli(
            "dreaming",
            "consolidate",
            "--goal-id",
            GOAL_ID,
            "--limit",
            "10",
            registry_path=registry_path,
            runtime=runtime,
        )
        assert replay["counts"] == {
            "new": 0,
            "merged": 0,
            "suppressed_decided": 0,
            "suppressed_deferred": 0,
            "reopened": 0,
            "conflicts": 2,
            "pending_human": 0,
        }, replay
        assert replay["ledger_written"] is False, replay
        assert ledger_path.read_text(encoding="utf-8") == ledger_bytes

        queue = run_cli(
            "dreaming", "proposals", "--goal-id", GOAL_ID,
            registry_path=registry_path, runtime=runtime,
        )
        assert queue["ok"] is True, queue
        assert queue["read_only"] is True, queue
        assert queue["status_counts"]["pending"] == 3, queue
        pending_only = run_cli(
            "dreaming",
            "proposals",
            "--goal-id",
            GOAL_ID,
            "--status",
            "pending",
            registry_path=registry_path,
            runtime=runtime,
        )
        assert len(pending_only["proposals"]) == 3, pending_only

        # Defer with an explicit TTL: reason and expiry are recorded, the same
        # evidence window is suppressed while the defer is active.
        before_defer_state = state_path.read_text(encoding="utf-8")
        defer = run_cli(
            "dreaming",
            "decide",
            "--goal-id",
            GOAL_ID,
            "--proposal-id",
            explore_id,
            "--decision",
            "defer",
            "--reason-summary",
            "Wait for a stronger delivery boundary before exploring.",
            "--defer-ttl-hours",
            "24",
            "--no-global-sync",
            registry_path=registry_path,
            runtime=runtime,
        )
        assert defer["ok"] is True, defer
        assert defer["classification"] == "dreaming_proposal_deferred", defer
        assert defer["proposal_ledger"]["ledger_status"] == "deferred", defer
        assert defer["proposal_ledger"]["expires_at"], defer
        assert defer["proposal_ledger"]["decision_history_count"] == 1, defer
        assert defer["side_effects"]["active_state_mutated"] is False, defer
        assert state_path.read_text(encoding="utf-8") == before_defer_state

        suppressed = run_cli(
            "dreaming",
            "consolidate",
            "--goal-id",
            GOAL_ID,
            "--limit",
            "10",
            registry_path=registry_path,
            runtime=runtime,
        )
        assert suppressed["counts"]["suppressed_deferred"] == 1, suppressed
        assert suppressed["counts"]["new"] == 0, suppressed

        # Reject another proposal: its window must never re-produce.
        reject = run_cli(
            "dreaming",
            "decide",
            "--goal-id",
            GOAL_ID,
            "--proposal-id",
            refactor_id,
            "--decision",
            "reject",
            "--reason-summary",
            "The refactor warning is not useful for this boundary.",
            "--no-global-sync",
            registry_path=registry_path,
            runtime=runtime,
        )
        assert reject["ok"] is True, reject
        assert reject["classification"] == "dreaming_proposal_rejected", reject
        re_decide = run_cli(
            "dreaming",
            "decide",
            "--goal-id",
            GOAL_ID,
            "--proposal-id",
            refactor_id,
            "--decision",
            "approve",
            "--reason-summary",
            "Attempt to overwrite the terminal decision.",
            "--todo-text",
            "[P1] must not be promoted",
            "--no-global-sync",
            registry_path=registry_path,
            runtime=runtime,
            check=False,
        )
        assert re_decide["ok"] is False, re_decide
        assert "terminal" in (re_decide.get("error") or ""), re_decide
        assert "[P1] must not be promoted" not in state_path.read_text(encoding="utf-8")

        after_reject = run_cli(
            "dreaming",
            "consolidate",
            "--goal-id",
            GOAL_ID,
            "--limit",
            "10",
            registry_path=registry_path,
            runtime=runtime,
        )
        assert after_reject["counts"]["suppressed_decided"] == 1, after_reject
        assert after_reject["counts"]["suppressed_deferred"] == 1, after_reject
        assert after_reject["counts"]["new"] == 0, after_reject
        ledger_now = json.loads(ledger_path.read_text(encoding="utf-8"))
        rejected_entry = next(
            item for item in ledger_now["proposals"] if item["proposal_id"] == refactor_id
        )
        assert rejected_entry["status"] == "rejected", rejected_entry
        assert [event["event"] for event in rejected_entry["decision_history"]] == [
            "operator_decision"
        ], rejected_entry

        # An expired defer re-enters adjudication on the next ingest, and a
        # later approve appends to the same history instead of replacing it.
        past_expiry = (
            (datetime.now(timezone.utc) - timedelta(hours=1))
            .replace(microsecond=0)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        expire_memory = run_cli(
            "dreaming",
            "decide",
            "--goal-id",
            GOAL_ID,
            "--proposal-id",
            memory_id,
            "--decision",
            "defer",
            "--reason-summary",
            "Shelve the consolidation briefly.",
            "--defer-until",
            past_expiry,
            "--no-global-sync",
            registry_path=registry_path,
            runtime=runtime,
        )
        assert expire_memory["ok"] is True, expire_memory
        reopened = run_cli(
            "dreaming",
            "consolidate",
            "--goal-id",
            GOAL_ID,
            "--limit",
            "10",
            registry_path=registry_path,
            runtime=runtime,
        )
        assert reopened["counts"]["reopened"] == 1, reopened
        assert memory_id in reopened["reopened_proposal_ids"], reopened
        ledger_reopened = json.loads(ledger_path.read_text(encoding="utf-8"))
        memory_entry = next(
            item for item in ledger_reopened["proposals"] if item["proposal_id"] == memory_id
        )
        assert memory_entry["status"] == "pending", memory_entry
        events = [event["event"] for event in memory_entry["decision_history"]]
        assert events == ["operator_decision", "defer_expired_reopen"], memory_entry

        approve = run_cli(
            "dreaming",
            "decide",
            "--goal-id",
            GOAL_ID,
            "--proposal-id",
            memory_id,
            "--decision",
            "approve",
            "--reason-summary",
            "Consolidate the lesson as bounded normal delivery.",
            "--todo-text",
            "[P1] Consolidate the approved dreaming lesson into the playbook.",
            "--no-global-sync",
            registry_path=registry_path,
            runtime=runtime,
        )
        assert approve["ok"] is True, approve
        assert approve["classification"] == "dreaming_proposal_approved", approve
        assert approve["dreaming_decision"]["promoted_to_delivery"] is True, approve
        assert approve["proposal_ledger"]["ledger_status"] == "approved", approve
        assert approve["proposal_ledger"]["decision_history_count"] == 3, approve
        assert "[P1] Consolidate the approved dreaming lesson" in state_path.read_text(
            encoding="utf-8"
        )
        ledger_final = json.loads(ledger_path.read_text(encoding="utf-8"))
        memory_final = next(
            item for item in ledger_final["proposals"] if item["proposal_id"] == memory_id
        )
        assert [event["event"] for event in memory_final["decision_history"]] == [
            "operator_decision",
            "defer_expired_reopen",
            "operator_decision",
        ], memory_final
        assert memory_final["decision"]["decision"] == "approve", memory_final

        # Advisory boundaries hold throughout.
        assert_no_quota_spend(runtime)
        assert state_path.read_text(encoding="utf-8") != before_state
        assert index_path.exists()

    print("dreaming-proposal-ledger-smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
