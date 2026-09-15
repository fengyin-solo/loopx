import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import assert from "node:assert/strict";

import {
  evaluateQuotaReconcileCommit,
  evaluateQuotaReconcileScan,
  QUOTA_RECONCILE_COMMIT_REQUEST_SCHEMA,
  QUOTA_RECONCILE_SCAN_REQUEST_SCHEMA,
} from "../../loopx/control_plane/quota/reconcile.ts";

const GOAL = "goal-demo";
const NOW = "2026-09-15T10:00:00.000Z";
const T0800 = "2026-09-15T08:00:00.000Z";
const T0830 = "2026-09-15T08:30:00.000Z";
const T0900 = "2026-09-15T09:00:00.000Z";

interface Root {
  root: string;
  runsDir: string;
  indexPath: string;
  rolloutPath: string;
}

async function createRoot(): Promise<Root> {
  const root = await mkdtemp(join(tmpdir(), "loopx-quota-reconcile-"));
  const runsDir = join(root, "goals", GOAL, "runs");
  await mkdir(runsDir, { recursive: true });
  const indexPath = join(runsDir, "index.jsonl");
  const rolloutPath = join(root, "goals", GOAL, "rollout-event-log.jsonl");
  return { root, runsDir, indexPath, rolloutPath };
}

async function appendJson(path: string, record: Record<string, unknown>): Promise<void> {
  await writeFile(path, `${JSON.stringify(record)}\n`, { flag: "a" });
}

interface SpendOptions {
  generatedAt: string;
  slots?: number;
  effectId?: string | null;
  turnInstanceId?: string | null;
  todoId?: string | null;
}

async function writeSpend(
  root: Root,
  { generatedAt, slots = 1, effectId = null, turnInstanceId = null, todoId = null }:
    SpendOptions,
): Promise<void> {
  const event: Record<string, unknown> = {
    event_type: "quota_slot_spent",
    source: "heartbeat",
    slots,
    turn_instance_id: turnInstanceId,
    todo_id: todoId,
    settlement_identity: effectId ? { effect_id: effectId } : null,
  };
  const record: Record<string, unknown> = {
    generated_at: generatedAt,
    goal_id: GOAL,
    classification: "quota_slot_spent",
    quota_event: event,
  };
  if (turnInstanceId) record.turn_instance_id = turnInstanceId;
  if (effectId) record.settlement_identity = { effect_id: effectId };
  await appendJson(root.indexPath, record);
}

async function writeVoid(
  root: Root,
  generatedAt: string,
  targetGeneratedAt: string,
  slots = 1,
  reconciliation: Record<string, unknown> | null = null,
): Promise<void> {
  await appendJson(root.indexPath, {
    generated_at: generatedAt,
    goal_id: GOAL,
    classification: "quota_slot_voided",
    quota_event: {
      event_type: "quota_slot_voided",
      source: "heartbeat",
      slots,
      voided_run_generated_at: targetGeneratedAt,
      ...(reconciliation ? { reconciliation } : {}),
    },
  });
}

async function writeSpendReceipt(
  root: Root,
  eventId: string,
  effectId: string,
  slots: number,
  recordedAt = T0900,
  runId: string | null = null,
): Promise<void> {
  await appendJson(root.rolloutPath, {
    schema_version: "loopx_rollout_event_v0",
    goal_id: GOAL,
    event_kind: "quota_spend",
    event_id: eventId,
    recorded_at: recordedAt,
    ...(runId ? { run_id: runId } : {}),
    details: {
      ok: true,
      appended: true,
      slots,
      settlement_effect_id: effectId,
    },
  });
}

async function writeVoidReceipt(
  root: Root,
  eventId: string,
  targetGeneratedAt: string,
  slots: number,
  recordedAt = T0900,
): Promise<void> {
  await appendJson(root.rolloutPath, {
    schema_version: "loopx_rollout_event_v0",
    goal_id: GOAL,
    event_kind: "quota_void",
    event_id: eventId,
    recorded_at: recordedAt,
    details: {
      ok: true,
      appended: true,
      slots,
      voided_run_generated_at: targetGeneratedAt,
    },
  });
}

function scanRequest(root: string, overrides: Record<string, unknown> = {}) {
  return {
    schema_version: QUOTA_RECONCILE_SCAN_REQUEST_SCHEMA,
    runtime_root: root,
    goal_ids: "goal_ids" in overrides ? overrides.goal_ids : [GOAL],
    timestamp_tolerance_seconds:
      overrides.timestamp_tolerance_seconds ?? 60,
    window_hours_by_goal: overrides.window_hours_by_goal ?? null,
    now: overrides.now ?? NOW,
    generated_at: overrides.generated_at ?? NOW,
  };
}

async function scan(root: string, overrides: Record<string, unknown> = {}) {
  const result = await evaluateQuotaReconcileScan(scanRequest(root, overrides));
  assert.equal(result.schema_version, "loopx_quota_reconcile_scan_result_v0");
  return result.payload as Record<string, unknown>;
}

function discrepancies(report: Record<string, unknown>): Record<string, unknown>[] {
  return report.discrepancies as Record<string, unknown>[];
}

function diagnostics(report: Record<string, unknown>): Record<string, unknown>[] {
  return report.diagnostics as Record<string, unknown>[];
}

async function indexLines(root: Root): Promise<string[]> {
  let content: string;
  try {
    content = await readFile(root.indexPath, "utf8");
  } catch {
    return [];
  }
  return content.split(/\r?\n/).filter((line) => line.trim());
}

async function indexRecords(root: Root): Promise<Record<string, unknown>[]> {
  const lines = await indexLines(root);
  const records: Record<string, unknown>[] = [];
  for (const line of lines) {
    const record = JSON.parse(line) as Record<string, unknown>;
    if (!record.quota_event && typeof record.json_path === "string") {
      const artifact = JSON.parse(await readFile(record.json_path, "utf8"));
      record.quota_event = artifact.quota_event;
    }
    records.push(record);
  }
  return records;
}

function commitRequest(
  root: string,
  discrepancyIds: string[],
  digest: string | null,
  overrides: Record<string, unknown> = {},
) {
  return {
    schema_version: QUOTA_RECONCILE_COMMIT_REQUEST_SCHEMA,
    runtime_root: root,
    goal_id: GOAL,
    generated_at: (overrides.generated_at as string) ?? NOW,
    execute: true,
    expected_index_digest: digest,
    timestamp_tolerance_seconds: 60,
    items: discrepancyIds.map((discrepancyId) => ({
      discrepancy_id: discrepancyId,
    })),
  };
}

async function applyReport(root: Root, report: Record<string, unknown>) {
  const goal = (report.goals as Record<string, unknown>[])[0];
  const ids = discrepancies(report).map(
    (discrepancy) => discrepancy.discrepancy_id as string,
  );
  return await evaluateQuotaReconcileCommit(
    commitRequest(root.root, ids, goal.index_digest as string),
  ) as Record<string, unknown>;
}

test("clean goal scans without discrepancies or diagnostics", async (t) => {
  const root = await createRoot();
  t.after(() => rm(root.root, { recursive: true, force: true }));
  await writeSpend(root, { generatedAt: T0800, slots: 1, effectId: "effect-ok" });
  await writeSpendReceipt(root, "receipt-ok", "effect-ok", 1);

  const report = await scan(root.root);
  const summary = report.summary as Record<string, unknown>;
  assert.equal(summary.total_discrepancies, 0);
  assert.deepEqual(summary.by_kind, {
    duplicate_billing: 0,
    missing_void: 0,
    reimbursement_without_consumption: 0,
    timestamp_drift: 0,
  });
  assert.deepEqual(report.goals_clean, [GOAL]);
  assert.equal(report.dry_run, true);
  assert.equal(report.appended, false);
});

test("duplicate billing: two spends share a settlement effect id", async (t) => {
  const root = await createRoot();
  t.after(() => rm(root.root, { recursive: true, force: true }));
  await writeSpend(root, { generatedAt: T0800, slots: 1, effectId: "effect-dup" });
  await writeSpend(root, {
    generatedAt: T0830,
    slots: 2,
    effectId: "effect-dup",
  });

  const report = await scan(root.root);
  const found = discrepancies(report);
  assert.equal(found.length, 1);
  assert.equal(found[0].kind, "duplicate_billing");
  assert.equal(found[0].fixable, true);
  const evidence = found[0].evidence as Record<string, unknown>;
  assert.equal(evidence.cluster_size, 2);
  assert.equal(evidence.canonical_run_generated_at, T0800);
  assert.equal(evidence.duplicate_run_generated_at, T0830);
  const correction = found[0].correction as Record<string, unknown>;
  assert.equal(correction.action, "append_void");
  assert.equal(correction.slots, 2);
  assert.equal(correction.target_run_generated_at, T0830);
  assert.match(String(correction.effect_id), /^quota-reconcile:dup:/);
  const summary = report.summary as Record<string, unknown>;
  assert.equal(summary.would_deduct_slots, 2);
  assert.equal(summary.affected_quota_entry_count, 1);
  const goal = (report.goals as Record<string, unknown>[])[0] as Record<string, unknown>;
  assert.equal(goal.current_window_slot_delta, -2);
});

test("duplicate billing also clusters on the typed turn identity", async (t) => {
  const root = await createRoot();
  t.after(() => rm(root.root, { recursive: true, force: true }));
  await writeSpend(root, {
    generatedAt: T0800,
    turnInstanceId: "turn-1",
    todoId: "todo-1",
  });
  await writeSpend(root, {
    generatedAt: T0830,
    turnInstanceId: "turn-1",
    todoId: "todo-1",
  });

  const report = await scan(root.root);
  assert.equal(discrepancies(report).length, 1);
  assert.equal(discrepancies(report)[0].kind, "duplicate_billing");
});

test("duplicate already covered by a void is not re-reported", async (t) => {
  const root = await createRoot();
  t.after(() => rm(root.root, { recursive: true, force: true }));
  await writeSpend(root, { generatedAt: T0800, slots: 1, effectId: "effect-dup2" });
  await writeSpend(root, { generatedAt: T0830, slots: 1, effectId: "effect-dup2" });
  await writeVoid(root, T0900, T0830, 1);

  const report = await scan(root.root);
  assert.deepEqual(discrepancies(report), []);
});

test("missing void: void receipt without a ledger void event", async (t) => {
  const root = await createRoot();
  t.after(() => rm(root.root, { recursive: true, force: true }));
  await writeSpend(root, { generatedAt: T0800, slots: 1 });
  await writeVoidReceipt(root, "void-receipt-1", T0800, 1);

  const report = await scan(root.root);
  const found = discrepancies(report);
  assert.equal(found.length, 1);
  assert.equal(found[0].kind, "missing_void");
  const correction = found[0].correction as Record<string, unknown>;
  assert.equal(correction.action, "append_void");
  assert.equal(correction.target_run_generated_at, T0800);
  assert.equal(correction.slots, 1);
});

test("void receipt with an existing void event is consistent", async (t) => {
  const root = await createRoot();
  t.after(() => rm(root.root, { recursive: true, force: true }));
  await writeSpend(root, { generatedAt: T0800, slots: 1 });
  await writeVoid(root, T0900, T0800, 1);
  await writeVoidReceipt(root, "void-receipt-2", T0800, 1);

  const report = await scan(root.root);
  assert.deepEqual(discrepancies(report), []);
});

test("orphan void receipt pointing at no spend is diagnostic only", async (t) => {
  const root = await createRoot();
  t.after(() => rm(root.root, { recursive: true, force: true }));
  await writeVoidReceipt(root, "void-receipt-3", T0800, 1);

  const report = await scan(root.root);
  assert.deepEqual(discrepancies(report), []);
  const found = diagnostics(report);
  assert.equal(found.length, 1);
  assert.equal(found[0].kind, "orphan_void_receipt");
  assert.equal(found[0].fixable, false);
});

test("reimbursement without consumption backfills the spend", async (t) => {
  const root = await createRoot();
  t.after(() => rm(root.root, { recursive: true, force: true }));
  await writeSpendReceipt(
    root,
    "receipt-backfill",
    "effect-backfill",
    2,
    T0830,
    "turn-9",
  );

  const report = await scan(root.root);
  const found = discrepancies(report);
  assert.equal(found.length, 1);
  assert.equal(found[0].kind, "reimbursement_without_consumption");
  const correction = found[0].correction as Record<string, unknown>;
  assert.equal(correction.action, "append_spend");
  assert.equal(correction.slots, 2);
  assert.equal(correction.run_generated_at, T0830);
  const goal = (report.goals as Record<string, unknown>[])[0] as Record<string, unknown>;
  assert.equal(goal.current_window_slot_delta, 2);
  assert.equal((report.summary as Record<string, unknown>).would_backfill_slots, 2);
});

test("receipt and ledger slot mismatch is a non-fixable diagnostic", async (t) => {
  const root = await createRoot();
  t.after(() => rm(root.root, { recursive: true, force: true }));
  await writeSpend(root, { generatedAt: T0800, slots: 1, effectId: "effect-amount" });
  await writeSpendReceipt(root, "receipt-amount", "effect-amount", 3);

  const report = await scan(root.root);
  assert.deepEqual(discrepancies(report), []);
  const found = diagnostics(report);
  assert.equal(found.length, 1);
  assert.equal(found[0].kind, "amount_mismatch");
  assert.equal(found[0].fixable, false);
});

test("timestamp drift within tolerance is fixable and unique", async (t) => {
  const root = await createRoot();
  t.after(() => rm(root.root, { recursive: true, force: true }));
  await writeSpend(root, { generatedAt: T0800, slots: 1 });
  await writeVoid(root, T0900, "2026-09-15T08:00:30.000Z", 1);

  const report = await scan(root.root);
  const found = discrepancies(report);
  assert.equal(found.length, 1);
  assert.equal(found[0].kind, "timestamp_drift");
  const evidence = found[0].evidence as Record<string, unknown>;
  assert.equal(evidence.delta_seconds, -30);
  const correction = found[0].correction as Record<string, unknown>;
  assert.equal(correction.target_run_generated_at, T0800);
});

test("equidistant drift candidates are reported ambiguous and not fixable", async (t) => {
  const root = await createRoot();
  t.after(() => rm(root.root, { recursive: true, force: true }));
  await writeSpend(root, { generatedAt: "2026-09-15T08:00:00.000Z" });
  await writeSpend(root, { generatedAt: "2026-09-15T08:00:04.000Z" });
  await writeVoid(root, T0900, "2026-09-15T08:00:02.000Z", 1);

  const report = await scan(root.root, { timestamp_tolerance_seconds: 60 });
  assert.deepEqual(discrepancies(report), []);
  const found = diagnostics(report);
  assert.equal(found.length, 1);
  assert.equal(found[0].kind, "ambiguous_timestamp_drift");
  assert.equal(found[0].fixable, false);
});

test("void beyond tolerance with no target is an orphan event diagnostic", async (t) => {
  const root = await createRoot();
  t.after(() => rm(root.root, { recursive: true, force: true }));
  await writeSpend(root, { generatedAt: T0800, slots: 1 });
  await writeVoid(root, T0900, "2026-09-15T08:02:00.000Z", 1);

  const tight = await scan(root.root, { timestamp_tolerance_seconds: 10 });
  assert.deepEqual(discrepancies(tight), []);
  assert.equal(diagnostics(tight)[0].kind, "orphan_void_event");
});

test("scan is read-only: no files or digests change", async (t) => {
  const root = await createRoot();
  t.after(() => rm(root.root, { recursive: true, force: true }));
  await writeSpend(root, { generatedAt: T0800, slots: 1, effectId: "effect-ro" });
  await writeSpend(root, { generatedAt: T0830, slots: 1, effectId: "effect-ro" });

  const goalDir = root.runsDir;
  const before = (await readdir(goalDir)).sort();
  const linesBefore = await indexLines(root);
  await scan(root.root);
  await scan(root.root);
  const after = (await readdir(goalDir)).sort();
  const linesAfter = await indexLines(root);
  assert.deepEqual(after, before);
  assert.deepEqual(linesAfter, linesBefore);
});

test("apply appends corrections, re-scan is clean, and re-apply is idempotent", async (t) => {
  const root = await createRoot();
  t.after(() => rm(root.root, { recursive: true, force: true }));
  await writeSpend(root, { generatedAt: T0800, slots: 1, effectId: "effect-apply" });
  await writeSpend(root, { generatedAt: T0830, slots: 1, effectId: "effect-apply" });
  await writeVoid(root, T0900, "2026-09-15T08:00:30.000Z", 1);

  const before = await scan(root.root);
  assert.equal(discrepancies(before).length, 2);
  const linesBefore = await indexLines(root);

  const applied = await applyReport(root, before) as Record<string, unknown>;
  assert.equal(applied.status, "applied");
  assert.equal(applied.correction_count, 2);
  assert.equal(applied.appended, true);
  const items = applied.items as Record<string, unknown>[];
  assert.ok(items.every((item) => item.status === "written"));

  const linesAfterFirstApply = await indexLines(root);
  assert.equal(linesAfterFirstApply.length, linesBefore.length + 2);

  // Corrections carry the typed reconciliation provenance.
  const parsed = await indexRecords(root);
  const corrections: Record<string, unknown>[] = parsed
    .filter((record) => (record.quota_event as Record<string, unknown>)?.reconciliation)
    .map((record) =>
      (record.quota_event as Record<string, unknown>).reconciliation as Record<string, unknown>,
    );
  assert.equal(corrections.length, 2);
  for (const block of corrections) {
    assert.equal(block.schema_version, "quota_reconciliation_correction_v0");
    assert.ok(String(block.effect_id).startsWith("quota-reconcile:"));
  }

  const after = await scan(root.root);
  assert.deepEqual(discrepancies(after), []);
  assert.deepEqual(after.goals_clean, [GOAL]);

  // Re-applying the same discrepancy ids appends nothing.
  const reapplied = await applyReport(root, before) as Record<string, unknown>;
  assert.equal(reapplied.status, "applied");
  assert.equal(reapplied.correction_count, 0);
  assert.equal(reapplied.appended, false);
  for (const item of reapplied.items as Record<string, unknown>[]) {
    assert.equal(item.status, "replayed");
    assert.equal(item.appended, false);
  }
  const linesAfterSecondApply = await indexLines(root);
  assert.equal(linesAfterSecondApply.length, linesAfterFirstApply.length);
});

test("backfill spend correction links the receipt and lands in the ledger", async (t) => {
  const root = await createRoot();
  t.after(() => rm(root.root, { recursive: true, force: true }));
  await writeSpendReceipt(
    root,
    "receipt-bf",
    "effect-bf",
    3,
    T0830,
    "turn-bf",
  );

  const before = await scan(root.root);
  const applied = await applyReport(root, before) as Record<string, unknown>;
  assert.equal(applied.status, "applied");
  assert.equal((applied.items as Record<string, unknown>[])[0].kind, "reimbursement_without_consumption");

  const parsed = await indexRecords(root);
  const backfill = parsed.find(
    (record) => record.classification === "quota_slot_spent" &&
      (record.quota_event as Record<string, unknown>)?.reconciliation,
  );
  assert.ok(backfill);
  const backfillEvent = backfill.quota_event as Record<string, unknown>;
  assert.equal(backfill.generated_at, T0830);
  assert.equal(backfillEvent.slots, 3);
  assert.equal(
    (backfillEvent.settlement_identity as Record<string, unknown>).effect_id,
    "effect-bf",
  );
  assert.equal(backfillEvent.effect_ref, "effect-bf#quota_spend");
  assert.equal(
    (backfill.quota_spend_commit as Record<string, unknown>).schema_version,
    "quota_reconcile_spend_receipt_v0",
  );

  const after = await scan(root.root);
  assert.deepEqual(discrepancies(after), []);
});

test("interrupted prepared correction is repaired without duplication", async (t) => {
  const root = await createRoot();
  t.after(() => rm(root.root, { recursive: true, force: true }));
  await writeSpend(root, { generatedAt: T0800, slots: 1, effectId: "effect-crash" });
  await writeSpend(root, { generatedAt: T0830, slots: 1, effectId: "effect-crash" });

  const before = await scan(root.root);
  const goal = (before.goals as Record<string, unknown>[])[0];
  const ids = discrepancies(before).map((d) => d.discrepancy_id as string);
  const applied = await evaluateQuotaReconcileCommit(
    commitRequest(root.root, ids, goal.index_digest as string),
  ) as Record<string, unknown>;
  assert.equal(applied.correction_count, 1);
  const committedLines = await indexLines(root);

  // Simulate a crash after the index append but before the receipt/committed
  // finalization: prepared receipt, missing JSON/Markdown artifacts, and the
  // index line half-truncated.
  const txDir = join(root.runsDir, ".transactions", "quota-reconcile-void");
  const receipts = (await readdir(txDir)).filter((name) => name.endsWith(".json"));
  assert.equal(receipts.length, 1);
  const receiptPath = join(txDir, receipts[0]);
  const receipt = JSON.parse(await readFile(receiptPath, "utf8"));
  assert.equal(receipt.status, "committed");
  receipt.status = "prepared";
  await writeFile(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`);
  await rm(receipt.json_path, { force: true });
  await rm(receipt.markdown_path, { force: true });
  const repairedLine = `${JSON.stringify(receipt.index_record)}\n`;
  const indexContent = committedLines.join("\n") + "\n";
  const prefix = indexContent.slice(0, indexContent.length - repairedLine.length);
  await writeFile(
    root.indexPath,
    prefix + repairedLine.slice(0, Math.floor(repairedLine.length / 2)),
  );

  const retried = await evaluateQuotaReconcileCommit(
    commitRequest(root.root, ids, null),
  ) as Record<string, unknown>;
  assert.equal(retried.status, "applied");
  assert.ok(
    (retried.items as Record<string, unknown>[]).some(
      (item) => item.status === "repaired",
    ),
  );

  // Exactly one correction event remains; no duplicate deduction.
  const repairedLines = await indexLines(root);
  const voidLines = repairedLines.filter(
    (line) => JSON.parse(line).classification === "quota_slot_voided",
  );
  assert.equal(voidLines.length, 1);
  const clean = await scan(root.root);
  assert.deepEqual(discrepancies(clean), []);
});

test("stale index digest fails closed as a conflict", async (t) => {
  const root = await createRoot();
  t.after(() => rm(root.root, { recursive: true, force: true }));
  await writeSpend(root, { generatedAt: T0800, slots: 1, effectId: "effect-cas" });
  await writeSpend(root, { generatedAt: T0830, slots: 1, effectId: "effect-cas" });

  const report = await scan(root.root);
  const ids = discrepancies(report).map((d) => d.discrepancy_id as string);
  const result = await evaluateQuotaReconcileCommit(
    commitRequest(root.root, ids, "sha256:0000000000000000"),
  ) as Record<string, unknown>;
  assert.equal(result.status, "conflict");
  assert.equal(result.reason_code, "index_digest_conflict");
  assert.equal((await indexLines(root)).length, 2);
});

test("empty execute batch writes no batch receipt", async (t) => {
  const root = await createRoot();
  t.after(() => rm(root.root, { recursive: true, force: true }));
  const result = await evaluateQuotaReconcileCommit(
    commitRequest(root.root, [], null),
  ) as Record<string, unknown>;
  assert.equal(result.status, "applied");
  assert.equal(result.appended, false);
  let txDir: string[];
  try {
    txDir = await readdir(join(root.runsDir, ".transactions", "quota-reconcile"));
  } catch {
    txDir = [];
  }
  assert.deepEqual(txDir, []);
});

test("all-goal scan enumerates the runtime and isolates clean goals", async (t) => {
  const root = await createRoot();
  t.after(() => rm(root.root, { recursive: true, force: true }));
  const otherRuns = join(root.root, "goals", "goal-other", "runs");
  await mkdir(otherRuns, { recursive: true });
  await writeFile(join(otherRuns, "index.jsonl"), "");
  await writeSpend(root, { generatedAt: T0800, slots: 1, effectId: "effect-multi" });
  await writeSpend(root, { generatedAt: T0830, slots: 1, effectId: "effect-multi" });

  const report = await scan(root.root, { goal_ids: null });
  const goals = report.goals as Record<string, unknown>[];
  assert.deepEqual(goals.map((goal) => goal.goal_id).sort(), [
    GOAL,
    "goal-other",
  ]);
  assert.deepEqual(report.goals_clean, ["goal-other"]);
  const summary = report.summary as Record<string, unknown>;
  assert.equal(summary.goals_scanned, 2);
  assert.equal(summary.goals_with_discrepancies, 1);
});
