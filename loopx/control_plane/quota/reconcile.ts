import { createHash } from "node:crypto";
import { readdir, readFile } from "node:fs/promises";
import { basename, isAbsolute, join } from "node:path";

import type { JsonObject } from "../effect_program.ts";
import { EffectRuntimeRequestError } from "../effect_runtime_errors.ts";
import { atomicWriteJson } from "../effect_runtime_io.ts";
import {
  jsonObject,
  optionalNonEmptyString as optionalString,
  requireBoolean,
  requireInteger,
  requireJsonObject as requiredObject,
  requireNonEmptyString as requiredString,
} from "../runtime_decode.ts";
import {
  commitQuotaAccountingArtifactTransaction,
  parseQuotaAccountingIndex,
  quotaAccountingIndexDigest,
  readIndexedQuotaEvent,
  renderQuotaSlotMarkdown,
  resolveQuotaAccountingEffect,
  type QuotaAccountingArtifactKind,
} from "./accounting_artifact_transaction.ts";

export const QUOTA_RECONCILE_SCAN_REQUEST_SCHEMA =
  "loopx_quota_reconcile_scan_request_v0";
export const QUOTA_RECONCILE_SCAN_RESULT_SCHEMA =
  "loopx_quota_reconcile_scan_result_v0";
export const QUOTA_RECONCILE_COMMIT_REQUEST_SCHEMA =
  "loopx_quota_reconcile_commit_request_v0";
export const QUOTA_RECONCILE_COMMIT_RESULT_SCHEMA =
  "loopx_quota_reconcile_commit_result_v0";
export const QUOTA_RECONCILIATION_REPORT_SCHEMA =
  "quota_reconciliation_report_v0";
export const QUOTA_RECONCILIATION_CORRECTION_SCHEMA =
  "quota_reconciliation_correction_v0";
export const QUOTA_RECONCILE_BATCH_RECEIPT_SCHEMA =
  "quota_reconcile_batch_receipt_v0";
export const QUOTA_RECONCILE_SPEND_RECEIPT_SCHEMA =
  "quota_reconcile_spend_receipt_v0";
export const QUOTA_RECONCILE_VOID_RECEIPT_SCHEMA =
  "quota_reconcile_void_receipt_v0";
export const QUOTA_SLOT_SPENT_CLASSIFICATION = "quota_slot_spent";
export const QUOTA_SLOT_VOIDED_CLASSIFICATION = "quota_slot_voided";
export const QUOTA_SLOT_ACCOUNTING_PROJECTION_SCHEMA =
  "quota_slot_accounting_projection_v0";

const ROLLOUT_EVENT_SCHEMA = "loopx_rollout_event_v0";
const DEFAULT_WINDOW_HOURS = 24;
const DEFAULT_TOLERANCE_SECONDS = 60;

const DISCREPANCY_KINDS = [
  "duplicate_billing",
  "missing_void",
  "reimbursement_without_consumption",
  "timestamp_drift",
] as const;
const DIAGNOSTIC_KINDS = [
  "ambiguous_timestamp_drift",
  "orphan_void_receipt",
  "orphan_void_event",
  "amount_mismatch",
  "receipt_without_target_key",
] as const;

export type ReconcileDiscrepancyKind = (typeof DISCREPANCY_KINDS)[number];
export type ReconcileDiagnosticKind = (typeof DIAGNOSTIC_KINDS)[number];

const CORRECTION_REASONS: Record<ReconcileDiscrepancyKind, string> = {
  duplicate_billing:
    "reconciliation correction: void duplicate quota slot spend beyond the canonical run",
  missing_void:
    "reconciliation correction: append the quota slot void required by its settlement receipt",
  reimbursement_without_consumption:
    "reconciliation correction: backfill the quota slot spend attested by its settlement receipt",
  timestamp_drift:
    "reconciliation correction: repoint the timestamp-drifted void at the exact spend run",
};

interface ScanRequest {
  schema_version: typeof QUOTA_RECONCILE_SCAN_REQUEST_SCHEMA;
  runtimeRoot: string;
  goalIds: string[] | null;
  toleranceSeconds: number;
  windowHoursByGoal: Record<string, number>;
  now: string | null;
  generatedAt: string;
}

interface CommitItemRequest {
  discrepancy_id: string;
}

interface CommitRequest {
  schema_version: typeof QUOTA_RECONCILE_COMMIT_REQUEST_SCHEMA;
  runtimeRoot: string;
  goalId: string;
  generatedAt: string;
  execute: boolean;
  expectedIndexDigest: string | null;
  toleranceSeconds: number;
  items: CommitItemRequest[];
}

interface ResolvedCorrection {
  discrepancy_id: string;
  kind: ReconcileDiscrepancyKind;
  action: "append_void" | "append_spend";
  slots: number;
  target_run_generated_at: string | null;
  run_generated_at: string | null;
  settlement_effect_id: string | null;
  turn_instance_id: string | null;
  receipt_event_id: string | null;
  original_void_run_generated_at: string | null;
  resolved_void_run_generated_at: string | null;
}

interface SpendObservation {
  index: number;
  generatedAt: string;
  generatedMs: number;
  slots: number;
  settlementEffectId: string | null;
  turnInstanceId: string | null;
  binding: string | null;
  reconciliation: JsonObject | null;
}

interface VoidObservation {
  index: number;
  generatedAt: string;
  generatedMs: number;
  slots: number;
  targetGeneratedAt: string;
  targetMs: number | null;
  reconciliation: JsonObject | null;
}

interface SpendReceipt {
  eventId: string;
  recordedAt: string;
  runId: string | null;
  effectId: string;
  slots: number;
}

interface VoidReceipt {
  eventId: string;
  recordedAt: string;
  target: string;
  slots: number;
}

interface ReconciliationModel {
  goalId: string;
  runsDir: string;
  indexDigest: string | null;
  spends: SpendObservation[];
  voids: VoidObservation[];
  spendReceipts: SpendReceipt[];
  voidReceipts: VoidReceipt[];
  receiptWithoutTargetKey: number;
}

interface CorrectionPlan {
  action: "append_void" | "append_spend";
  slots: number;
  targetRunGeneratedAt: string | null;
  runGeneratedAt: string | null;
  settlementEffectId: string | null;
  turnInstanceId: string | null;
  receiptEventId: string | null;
  originalVoidRunGeneratedAt: string | null;
  resolvedVoidRunGeneratedAt: string | null;
}

interface ReconcileFinding {
  goalId: string;
  kind: ReconcileDiscrepancyKind | ReconcileDiagnosticKind;
  fixable: boolean;
  runIdentity: JsonObject;
  evidence: JsonObject;
  correction: CorrectionPlan | null;
  receiptEventId: string | null;
  fingerprintInputs: JsonObject;
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue);
  const object = jsonObject(value);
  if (!object) return value;
  return Object.fromEntries(
    Object.entries(object)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, child]) => [key, stableValue(child)]),
  );
}

function canonicalJson(value: unknown): string {
  return JSON.stringify(stableValue(value));
}

function sha256(value: string): string {
  return `sha256:${createHash("sha256").update(value, "utf8").digest("hex")}`;
}

function shortHash(value: unknown, length: number): string {
  return sha256(canonicalJson(value)).slice("sha256:".length, "sha256:".length + length);
}

function reconcileGoalId(value: unknown, label: string): string {
  const goalId = requiredString(value, label).trim();
  if (
    goalId === "." ||
    goalId === ".." ||
    goalId.includes("/") ||
    goalId.includes("\\")
  ) {
    throw new EffectRuntimeRequestError("goal_id must be a single path segment");
  }
  if (basename(goalId) !== goalId) {
    throw new EffectRuntimeRequestError("goal_id must not include path traversal");
  }
  return goalId;
}

/**
 * Parse an ISO 8601 timestamp. Runtime records may carry six fractional
 * digits, which V8's Date.parse rejects, so fractional seconds are truncated
 * to millisecond precision before parsing.
 */
function parseTimestampMs(value: string): number | null {
  const normalized = value.trim().replace(/\.\d{3}\d+/, (fraction) =>
    fraction.slice(0, 4),
  );
  const ms = Date.parse(normalized);
  return Number.isFinite(ms) ? ms : null;
}

function legacyInteger(value: unknown, fallback: number): number {
  if (typeof value === "boolean") return fallback;
  if (typeof value === "number" && Number.isFinite(value)) {
    return Math.trunc(value);
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value.trim());
    if (Number.isFinite(parsed)) return Math.trunc(parsed);
  }
  return fallback;
}

function nonEmptyStringField(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

async function readOptionalText(path: string): Promise<string | null> {
  try {
    return await readFile(path, "utf8");
  } catch (error) {
    if (error instanceof Error && "code" in error && error.code === "ENOENT") {
      return null;
    }
    throw error;
  }
}

async function enumerateGoalIds(runtimeRoot: string): Promise<string[]> {
  let entries: import("node:fs").Dirent[];
  try {
    entries = await readdir(join(runtimeRoot, "goals"), { withFileTypes: true });
  } catch (error) {
    if (error instanceof Error && "code" in error && error.code === "ENOENT") {
      return [];
    }
    throw error;
  }
  const goalIds: string[] = [];
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    if (entry.name === "." || entry.name === ".." || entry.name.includes("/")) {
      continue;
    }
    goalIds.push(entry.name);
  }
  return goalIds.sort((left, right) => left.localeCompare(right));
}

function decodeScanRequest(value: unknown): ScanRequest {
  const request = requiredObject(value, "quota.reconcile.scan params");
  if (request.schema_version !== QUOTA_RECONCILE_SCAN_REQUEST_SCHEMA) {
    throw new EffectRuntimeRequestError("Quota reconcile scan request schema mismatch");
  }
  const runtimeRoot = requiredString(request.runtime_root, "runtime_root").trim();
  if (!isAbsolute(runtimeRoot)) {
    throw new EffectRuntimeRequestError("runtime_root must be absolute");
  }
  let goalIds: string[] | null = null;
  if (request.goal_ids !== undefined && request.goal_ids !== null) {
    if (!Array.isArray(request.goal_ids)) {
      throw new EffectRuntimeRequestError("goal_ids must be an array of strings or null");
    }
    goalIds = request.goal_ids.map((entry, index) =>
      reconcileGoalId(entry, `goal_ids[${index}]`),
    );
  }
  const toleranceSeconds =
    request.timestamp_tolerance_seconds === undefined
      ? DEFAULT_TOLERANCE_SECONDS
      : requireInteger(
          request.timestamp_tolerance_seconds,
          "timestamp_tolerance_seconds",
        );
  if (toleranceSeconds < 0) {
    throw new EffectRuntimeRequestError(
      "timestamp_tolerance_seconds cannot be negative",
    );
  }
  const windowHoursByGoal: Record<string, number> = {};
  const rawWindows = request.window_hours_by_goal;
  if (rawWindows !== undefined && rawWindows !== null) {
    const windows = requiredObject(rawWindows, "window_hours_by_goal");
    for (const [goal, hours] of Object.entries(windows)) {
      const safeGoal = reconcileGoalId(goal, "window_hours_by_goal key");
      const parsedHours = requireInteger(hours, `window_hours_by_goal.${goal}`);
      if (parsedHours <= 0) {
        throw new EffectRuntimeRequestError("window_hours must be positive");
      }
      windowHoursByGoal[safeGoal] = parsedHours;
    }
  }
  const now = optionalString(request.now, "now")?.trim() ?? null;
  if (now !== null && parseTimestampMs(now) === null) {
    throw new EffectRuntimeRequestError("now must be an ISO 8601 timestamp");
  }
  return {
    schema_version: QUOTA_RECONCILE_SCAN_REQUEST_SCHEMA,
    runtimeRoot,
    goalIds,
    toleranceSeconds,
    windowHoursByGoal,
    now,
    generatedAt: requiredString(request.generated_at, "generated_at").trim(),
  };
}

function reconciliationBlock(event: JsonObject): JsonObject | null {
  const block = jsonObject(event.reconciliation);
  if (!block) return null;
  if (block.schema_version !== QUOTA_RECONCILIATION_CORRECTION_SCHEMA) return null;
  return block;
}

function identityFromObservation(
  event: JsonObject,
  record: JsonObject,
): {
  settlementEffectId: string | null;
  turnInstanceId: string | null;
  binding: string | null;
} {
  const eventIdentity = jsonObject(event.settlement_identity);
  const recordIdentity = jsonObject(record.settlement_identity);
  const settlementEffectId = nonEmptyStringField(
    eventIdentity?.effect_id,
  ) ?? nonEmptyStringField(recordIdentity?.effect_id);
  const turnInstanceId = nonEmptyStringField(event.turn_instance_id) ??
    nonEmptyStringField(record.turn_instance_id);
  const todoId = nonEmptyStringField(event.todo_id) ??
    nonEmptyStringField(record.todo_id);
  const replanId = nonEmptyStringField(event.replan_obligation_id) ??
    nonEmptyStringField(record.replan_obligation_id);
  const binding = todoId ? `todo:${todoId}` : replanId ? `replan:${replanId}` : null;
  return { settlementEffectId, turnInstanceId, binding };
}

async function loadReconciliationModel(
  runtimeRoot: string,
  goalId: string,
): Promise<ReconciliationModel> {
  const runsDir = join(runtimeRoot, "goals", goalId, "runs");
  const indexPath = join(runsDir, "index.jsonl");
  const content = await readOptionalText(indexPath);
  const records = parseQuotaAccountingIndex(content);
  const indexDigest = await quotaAccountingIndexDigest(indexPath);

  const spends: SpendObservation[] = [];
  const voids: VoidObservation[] = [];
  for (const [index, record] of records.entries()) {
    const recordGoal = nonEmptyStringField(record.goal_id);
    if (recordGoal && recordGoal !== goalId) continue;
    const generatedAt = nonEmptyStringField(record.generated_at);
    if (!generatedAt) continue;
    const generatedMs = parseTimestampMs(generatedAt);
    if (generatedMs === null) continue;
    if (record.classification === QUOTA_SLOT_SPENT_CLASSIFICATION) {
      const event = await readIndexedQuotaEvent(
        runsDir,
        record,
        goalId,
        QUOTA_SLOT_SPENT_CLASSIFICATION,
      );
      if (!event || event.event_type !== QUOTA_SLOT_SPENT_CLASSIFICATION) continue;
      const slots = legacyInteger(event.slots, 0);
      if (slots <= 0) continue;
      const identity = identityFromObservation(event, record);
      spends.push({
        index,
        generatedAt,
        generatedMs,
        slots,
        settlementEffectId: identity.settlementEffectId,
        turnInstanceId: identity.turnInstanceId,
        binding: identity.binding,
        reconciliation: reconciliationBlock(event),
      });
    } else if (record.classification === QUOTA_SLOT_VOIDED_CLASSIFICATION) {
      const event = await readIndexedQuotaEvent(
        runsDir,
        record,
        goalId,
        QUOTA_SLOT_VOIDED_CLASSIFICATION,
      );
      if (!event || event.event_type !== QUOTA_SLOT_VOIDED_CLASSIFICATION) continue;
      const slots = legacyInteger(event.slots, 0);
      if (slots <= 0) continue;
      const target = nonEmptyStringField(event.voided_run_generated_at) ?? "";
      voids.push({
        index,
        generatedAt,
        generatedMs,
        slots,
        targetGeneratedAt: target,
        targetMs: target ? parseTimestampMs(target) : null,
        reconciliation: reconciliationBlock(event),
      });
    }
  }

  const spendReceipts: SpendReceipt[] = [];
  const voidReceipts: VoidReceipt[] = [];
  let receiptWithoutTargetKey = 0;
  const logContent = await readOptionalText(
    join(runtimeRoot, "goals", goalId, "rollout-event-log.jsonl"),
  );
  if (logContent !== null) {
    for (const [lineIndex, line] of logContent.split(/\r?\n/).entries()) {
      if (!line.trim()) continue;
      let row: unknown;
      try {
        row = JSON.parse(line);
      } catch {
        throw new EffectRuntimeRequestError(
          `rollout event log line ${lineIndex + 1} is malformed`,
          "malformed_rollout_event_log",
        );
      }
      const event = requiredObject(row, `rollout event log line ${lineIndex + 1}`);
      if (event.schema_version !== ROLLOUT_EVENT_SCHEMA) continue;
      if (nonEmptyStringField(event.goal_id) !== goalId) continue;
      const eventKind = nonEmptyStringField(event.event_kind);
      if (eventKind !== "quota_spend" && eventKind !== "quota_void") continue;
      const details = jsonObject(event.details) ?? {};
      if (details.ok === false || details.appended === false) continue;
      const eventId = nonEmptyStringField(event.event_id);
      const recordedAt = nonEmptyStringField(event.recorded_at);
      if (!eventId || !recordedAt) continue;
      if (eventKind === "quota_spend") {
        const effectId = nonEmptyStringField(details.settlement_effect_id);
        if (!effectId) continue;
        const slots = legacyInteger(details.slots, 0);
        if (slots <= 0) continue;
        spendReceipts.push({
          eventId,
          recordedAt,
          runId: nonEmptyStringField(event.run_id),
          effectId,
          slots,
        });
      } else {
        const target = nonEmptyStringField(details.voided_run_generated_at);
        const slots = legacyInteger(details.slots, 0);
        if (!target) {
          receiptWithoutTargetKey += 1;
          continue;
        }
        if (slots <= 0) continue;
        voidReceipts.push({ eventId, recordedAt, target, slots });
      }
    }
  }

  return {
    goalId,
    runsDir,
    indexDigest,
    spends,
    voids,
    spendReceipts,
    voidReceipts,
    receiptWithoutTargetKey,
  };
}

function groupSpends(
  spends: readonly SpendObservation[],
  keyOf: (spend: SpendObservation) => string | null,
): Map<string, SpendObservation[]> {
  const groups = new Map<string, SpendObservation[]>();
  for (const spend of spends) {
    const key = keyOf(spend);
    if (!key) continue;
    const group = groups.get(key);
    if (group) group.push(spend);
    else groups.set(key, [spend]);
  }
  return groups;
}

interface LedgerState {
  spentByRun: Map<string, number>;
  voidedByRun: Map<string, number>;
}

function ledgerFromModel(model: ReconciliationModel): LedgerState {
  const spentByRun = new Map<string, number>();
  const voidedByRun = new Map<string, number>();
  for (const spend of model.spends) {
    spentByRun.set(
      spend.generatedAt,
      (spentByRun.get(spend.generatedAt) ?? 0) + spend.slots,
    );
  }
  for (const voidEvent of model.voids) {
    if (!voidEvent.targetGeneratedAt) continue;
    voidedByRun.set(
      voidEvent.targetGeneratedAt,
      (voidedByRun.get(voidEvent.targetGeneratedAt) ?? 0) + voidEvent.slots,
    );
  }
  return { spentByRun, voidedByRun };
}

function windowLedger(
  model: ReconciliationModel,
  nowMs: number,
  windowHours: number,
): LedgerState {
  const windowStart = nowMs - windowHours * 60 * 60 * 1000;
  const spentByRun = new Map<string, number>();
  const voidedByRun = new Map<string, number>();
  for (const spend of model.spends) {
    if (spend.generatedMs < windowStart || spend.generatedMs > nowMs) continue;
    spentByRun.set(
      spend.generatedAt,
      (spentByRun.get(spend.generatedAt) ?? 0) + spend.slots,
    );
  }
  for (const voidEvent of model.voids) {
    if (voidEvent.generatedMs < windowStart || voidEvent.generatedMs > nowMs) {
      continue;
    }
    if (!voidEvent.targetGeneratedAt) continue;
    voidedByRun.set(
      voidEvent.targetGeneratedAt,
      (voidedByRun.get(voidEvent.targetGeneratedAt) ?? 0) + voidEvent.slots,
    );
  }
  return { spentByRun, voidedByRun };
}

function netSpent(ledger: LedgerState): number {
  let total = 0;
  for (const [key, slots] of ledger.spentByRun) {
    total += Math.max(0, slots - (ledger.voidedByRun.get(key) ?? 0));
  }
  return total;
}

function correctionEffectId(
  kind: ReconcileDiscrepancyKind,
  fingerprintInputs: JsonObject,
): string {
  const prefix = {
    duplicate_billing: "dup",
    missing_void: "mvoid",
    reimbursement_without_consumption: "reimb",
    timestamp_drift: "drift",
  }[kind];
  return `quota-reconcile:${prefix}:${shortHash(fingerprintInputs, 24)}`;
}

function analyzeGoal(
  model: ReconciliationModel,
  toleranceMs: number,
  nowMs: number,
  windowHours: number,
): {
  findings: ReconcileFinding[];
  simulation: {
    affectedEntryCount: number;
    currentWindowSlotDelta: number;
    deductSlots: number;
    backfillSlots: number;
  };
} {
  const findings: ReconcileFinding[] = [];
  const spendsByGeneratedAt = groupSpends(model.spends, (spend) =>
    spend.generatedAt,
  );
  const spendsByEffect = groupSpends(model.spends, (spend) =>
    spend.settlementEffectId ? `effect:${spend.settlementEffectId}` : null,
  );
  const spendsByTurn = groupSpends(model.spends, (spend) =>
    spend.turnInstanceId
      ? `turn:${spend.turnInstanceId}\u0000${spend.binding ?? ""}`
      : null,
  );
  const voidsByTarget = new Map<string, VoidObservation[]>();
  for (const voidEvent of model.voids) {
    if (!voidEvent.targetGeneratedAt) continue;
    const group = voidsByTarget.get(voidEvent.targetGeneratedAt);
    if (group) group.push(voidEvent);
    else voidsByTarget.set(voidEvent.targetGeneratedAt, [voidEvent]);
  }

  const voidsCovering = (spendGeneratedAt: string): number => {
    let covered = 0;
    for (const voidEvent of voidsByTarget.get(spendGeneratedAt) ?? []) {
      covered += voidEvent.slots;
    }
    return covered;
  };

  // 1. duplicate_billing: one run identity with more than one spend event.
  const emittedDuplicateKeys = new Set<string>();
  const addDuplicateClusters = (
    groups: Map<string, SpendObservation[]>,
    identityField: "settlement_effect_id" | "turn_instance_id",
  ) => {
    for (const [identityKey, clusterRaw] of groups) {
      if (clusterRaw.length < 2) continue;
      const cluster = [...clusterRaw].sort((left, right) =>
        left.index === right.index
          ? left.generatedAt.localeCompare(right.generatedAt)
          : left.index - right.index,
      );
      const canonical = cluster[0];
      for (const duplicate of cluster.slice(1)) {
        if (emittedDuplicateKeys.has(duplicate.generatedAt)) continue;
        emittedDuplicateKeys.add(duplicate.generatedAt);
        const alreadyVoided = voidsCovering(duplicate.generatedAt);
        const netSlots = Math.max(0, duplicate.slots - alreadyVoided);
        if (netSlots <= 0) continue;
        const identityValue = identityField === "settlement_effect_id"
          ? (duplicate.settlementEffectId ?? "")
          : (duplicate.turnInstanceId ?? "");
        const fingerprintInputs: JsonObject = {
          goal_id: model.goalId,
          kind: "duplicate_billing",
          identity_field: identityField,
          identity_value: identityValue,
          duplicate_run_generated_at: duplicate.generatedAt,
        };
        findings.push({
          goalId: model.goalId,
          kind: "duplicate_billing",
          fixable: true,
          runIdentity: {
            settlement_effect_id: duplicate.settlementEffectId,
            turn_instance_id: duplicate.turnInstanceId,
            binding: duplicate.binding,
            run_generated_at: duplicate.generatedAt,
          },
          evidence: {
            identity_field: identityField,
            identity_value: identityValue,
            canonical_run_generated_at: canonical.generatedAt,
            duplicate_run_generated_at: duplicate.generatedAt,
            cluster_size: cluster.length,
            duplicate_slots: duplicate.slots,
            already_voided_slots: alreadyVoided,
          },
          correction: {
            action: "append_void",
            slots: netSlots,
            targetRunGeneratedAt: duplicate.generatedAt,
            runGeneratedAt: null,
            settlementEffectId: null,
            turnInstanceId: null,
            receiptEventId: null,
            originalVoidRunGeneratedAt: null,
            resolvedVoidRunGeneratedAt: null,
          },
          receiptEventId: null,
          fingerprintInputs,
        });
      }
    }
  };
  addDuplicateClusters(spendsByEffect, "settlement_effect_id");
  addDuplicateClusters(spendsByTurn, "turn_instance_id");

  // 2. reimbursement_without_consumption / amount_mismatch.
  for (const receipt of model.spendReceipts) {
    const linked = spendsByEffect.get(`effect:${receipt.effectId}`) ?? [];
    if (linked.length === 0) {
      const fingerprintInputs: JsonObject = {
        goal_id: model.goalId,
        kind: "reimbursement_without_consumption",
        settlement_effect_id: receipt.effectId,
        receipt_event_id: receipt.eventId,
      };
      findings.push({
        goalId: model.goalId,
        kind: "reimbursement_without_consumption",
        fixable: true,
        runIdentity: {
          settlement_effect_id: receipt.effectId,
          turn_instance_id: receipt.runId,
          binding: null,
          run_generated_at: receipt.recordedAt,
        },
        evidence: {
          receipt_event_id: receipt.eventId,
          receipt_recorded_at: receipt.recordedAt,
          receipt_slots: receipt.slots,
        },
        correction: {
          action: "append_spend",
          slots: receipt.slots,
          targetRunGeneratedAt: null,
          runGeneratedAt: receipt.recordedAt,
          settlementEffectId: receipt.effectId,
          turnInstanceId: receipt.runId,
          receiptEventId: receipt.eventId,
          originalVoidRunGeneratedAt: null,
          resolvedVoidRunGeneratedAt: null,
        },
        receiptEventId: receipt.eventId,
        fingerprintInputs,
      });
    } else if (linked.length === 1) {
      const ledgerSlots = linked[0].slots;
      if (ledgerSlots !== receipt.slots) {
        findings.push({
          goalId: model.goalId,
          kind: "amount_mismatch",
          fixable: false,
          runIdentity: {
            settlement_effect_id: receipt.effectId,
            turn_instance_id: receipt.runId,
            binding: linked[0].binding,
            run_generated_at: linked[0].generatedAt,
          },
          evidence: {
            receipt_event_id: receipt.eventId,
            receipt_slots: receipt.slots,
            ledger_slots: ledgerSlots,
          },
          correction: null,
          receiptEventId: receipt.eventId,
          fingerprintInputs: {
            goal_id: model.goalId,
            kind: "amount_mismatch",
            settlement_effect_id: receipt.effectId,
            receipt_event_id: receipt.eventId,
          },
        });
      }
    }
  }

  // 3. missing_void / orphan_void_receipt.
  for (const receipt of model.voidReceipts) {
    if (!spendsByGeneratedAt.has(receipt.target)) {
      findings.push({
        goalId: model.goalId,
        kind: "orphan_void_receipt",
        fixable: false,
        runIdentity: {
          settlement_effect_id: null,
          turn_instance_id: null,
          binding: null,
          run_generated_at: receipt.target,
        },
        evidence: {
          receipt_event_id: receipt.eventId,
          receipt_recorded_at: receipt.recordedAt,
          target_run_generated_at: receipt.target,
          receipt_slots: receipt.slots,
        },
        correction: null,
        receiptEventId: receipt.eventId,
        fingerprintInputs: {
          goal_id: model.goalId,
          kind: "orphan_void_receipt",
          receipt_event_id: receipt.eventId,
        },
      });
      continue;
    }
    if (voidsByTarget.has(receipt.target)) continue;
    const netSlots = Math.max(
      0,
      (spendsByGeneratedAt.get(receipt.target) ?? []).reduce(
        (sum, spend) => sum + spend.slots,
        0,
      ),
    );
    const correctionSlots = Math.min(receipt.slots, netSlots);
    if (correctionSlots <= 0) continue;
    const fingerprintInputs: JsonObject = {
      goal_id: model.goalId,
      kind: "missing_void",
      receipt_event_id: receipt.eventId,
      target_run_generated_at: receipt.target,
    };
    findings.push({
      goalId: model.goalId,
      kind: "missing_void",
      fixable: true,
      runIdentity: {
        settlement_effect_id: null,
        turn_instance_id: null,
        binding: null,
        run_generated_at: receipt.target,
      },
      evidence: {
        receipt_event_id: receipt.eventId,
        receipt_recorded_at: receipt.recordedAt,
        target_run_generated_at: receipt.target,
        receipt_slots: receipt.slots,
        net_spend_slots: netSlots,
      },
      correction: {
        action: "append_void",
        slots: correctionSlots,
        targetRunGeneratedAt: receipt.target,
        runGeneratedAt: null,
        settlementEffectId: null,
        turnInstanceId: null,
        receiptEventId: receipt.eventId,
        originalVoidRunGeneratedAt: null,
        resolvedVoidRunGeneratedAt: null,
      },
      receiptEventId: receipt.eventId,
      fingerprintInputs,
    });
  }

  // 4. timestamp_drift / ambiguous_timestamp_drift / orphan_void_event.
  for (const voidEvent of model.voids) {
    if (!voidEvent.targetGeneratedAt) continue;
    if (spendsByGeneratedAt.has(voidEvent.targetGeneratedAt)) continue;
    if (voidEvent.reconciliation) {
      // A correction event targets the exact spend key by construction; if it
      // ever points at a missing key it must not generate a second drift.
      continue;
    }
    const alreadyRepointed = model.voids.some(
      (other) =>
        other.reconciliation !== null &&
        other.reconciliation.discrepancy_kind === "timestamp_drift" &&
        nonEmptyStringField(
          other.reconciliation.original_void_run_generated_at,
        ) === voidEvent.generatedAt,
    );
    if (alreadyRepointed) continue;
    if (voidEvent.targetMs === null) continue;
    const driftTargetMs = voidEvent.targetMs;
    const candidates = model.spends
      .map((spend) => ({
        spend,
        distanceMs: Math.abs(spend.generatedMs - driftTargetMs),
      }))
      .filter((candidate) => candidate.distanceMs > 0 && candidate.distanceMs <= toleranceMs)
      .sort((left, right) =>
        left.distanceMs === right.distanceMs
          ? left.spend.generatedAt.localeCompare(right.spend.generatedAt)
          : left.distanceMs - right.distanceMs,
      );
    if (candidates.length === 0) {
      findings.push({
        goalId: model.goalId,
        kind: "orphan_void_event",
        fixable: false,
        runIdentity: {
          settlement_effect_id: null,
          turn_instance_id: null,
          binding: null,
          run_generated_at: voidEvent.generatedAt,
        },
        evidence: {
          void_run_generated_at: voidEvent.generatedAt,
          recorded_target: voidEvent.targetGeneratedAt,
          void_slots: voidEvent.slots,
        },
        correction: null,
        receiptEventId: null,
        fingerprintInputs: {
          goal_id: model.goalId,
          kind: "orphan_void_event",
          void_run_generated_at: voidEvent.generatedAt,
        },
      });
      continue;
    }
    const nearestDistance = candidates[0].distanceMs;
    const nearest = candidates.filter(
      (candidate) => candidate.distanceMs === nearestDistance,
    );
    if (nearest.length > 1) {
      findings.push({
        goalId: model.goalId,
        kind: "ambiguous_timestamp_drift",
        fixable: false,
        runIdentity: {
          settlement_effect_id: null,
          turn_instance_id: null,
          binding: null,
          run_generated_at: voidEvent.generatedAt,
        },
        evidence: {
          void_run_generated_at: voidEvent.generatedAt,
          recorded_target: voidEvent.targetGeneratedAt,
          void_slots: voidEvent.slots,
          tolerance_seconds: Math.round(toleranceMs / 1000),
          candidate_run_generated_at: nearest.map(
            (candidate) => candidate.spend.generatedAt,
          ),
        },
        correction: null,
        receiptEventId: null,
        fingerprintInputs: {
          goal_id: model.goalId,
          kind: "ambiguous_timestamp_drift",
          void_run_generated_at: voidEvent.generatedAt,
        },
      });
      continue;
    }
    const target = nearest[0].spend;
    const netSlotsAtTarget = Math.max(
      0,
      (spendsByGeneratedAt.get(target.generatedAt) ?? []).reduce(
        (sum, spend) => sum + spend.slots,
        0,
      ) - voidsCovering(target.generatedAt),
    );
    const correctionSlots = Math.min(voidEvent.slots, netSlotsAtTarget);
    if (correctionSlots <= 0) continue;
    const fingerprintInputs: JsonObject = {
      goal_id: model.goalId,
      kind: "timestamp_drift",
      void_run_generated_at: voidEvent.generatedAt,
    };
    findings.push({
      goalId: model.goalId,
      kind: "timestamp_drift",
      fixable: true,
      runIdentity: {
        settlement_effect_id: target.settlementEffectId,
        turn_instance_id: target.turnInstanceId,
        binding: target.binding,
        run_generated_at: target.generatedAt,
      },
      evidence: {
        void_run_generated_at: voidEvent.generatedAt,
        recorded_target: voidEvent.targetGeneratedAt,
        resolved_target: target.generatedAt,
        delta_seconds: Math.round(
          (target.generatedMs - driftTargetMs) / 1000,
        ),
        void_slots: voidEvent.slots,
      },
      correction: {
        action: "append_void",
        slots: correctionSlots,
        targetRunGeneratedAt: target.generatedAt,
        runGeneratedAt: null,
        settlementEffectId: null,
        turnInstanceId: null,
        receiptEventId: null,
        originalVoidRunGeneratedAt: voidEvent.generatedAt,
        resolvedVoidRunGeneratedAt: target.generatedAt,
      },
      receiptEventId: null,
      fingerprintInputs,
    });
  }

  // Simulate the fixable corrections against both the historical ledger and
  // the goal's current rolling window.
  const historical = ledgerFromModel(model);
  const window = windowLedger(model, nowMs, windowHours);
  const affectedEntries = new Set<string>();
  let deductSlots = 0;
  let backfillSlots = 0;
  for (const finding of findings) {
    if (!finding.fixable || !finding.correction) continue;
    const correction = finding.correction;
    if (correction.action === "append_void") {
      const key = correction.targetRunGeneratedAt ?? "";
      affectedEntries.add(key);
      deductSlots += correction.slots;
      historical.voidedByRun.set(
        key,
        (historical.voidedByRun.get(key) ?? 0) + correction.slots,
      );
      // Void corrections are appended at commit time, so they are inside the
      // current rolling window by construction.
      window.voidedByRun.set(
        key,
        (window.voidedByRun.get(key) ?? 0) + correction.slots,
      );
    } else {
      const key = correction.runGeneratedAt ?? "";
      const keyMs = parseTimestampMs(key);
      affectedEntries.add(key);
      backfillSlots += correction.slots;
      historical.spentByRun.set(
        key,
        (historical.spentByRun.get(key) ?? 0) + correction.slots,
      );
      if (keyMs !== null && keyMs >= nowMs - windowHours * 60 * 60 * 1000 && keyMs <= nowMs) {
        window.spentByRun.set(
          key,
          (window.spentByRun.get(key) ?? 0) + correction.slots,
        );
      }
    }
  }
  const baselineWindowNet = netSpent(windowLedger(model, nowMs, windowHours));
  const currentWindowSlotDelta = netSpent(window) - baselineWindowNet;

  return {
    findings,
    simulation: {
      affectedEntryCount: affectedEntries.size,
      currentWindowSlotDelta,
      deductSlots,
      backfillSlots,
    },
  };
}

function buildFindingPayload(
  finding: ReconcileFinding,
  effectId: string | null,
): JsonObject {
  const correction = finding.correction;
  return {
    discrepancy_id:
      `${finding.kind}-${shortHash({ ...finding.fingerprintInputs, goal: finding.goalId }, 12)}`,
    kind: finding.kind,
    goal_id: finding.goalId,
    fixable: finding.fixable,
    run_identity: finding.runIdentity,
    evidence: finding.evidence,
    correction: correction
      ? {
          action: correction.action,
          slots: correction.slots,
          target_run_generated_at: correction.targetRunGeneratedAt,
          run_generated_at: correction.runGeneratedAt,
          effect_id: effectId ?? correctionEffectId(
            finding.kind as ReconcileDiscrepancyKind,
            finding.fingerprintInputs,
          ),
        }
      : null,
    reason: CORRECTION_REASONS[finding.kind as ReconcileDiscrepancyKind] ??
      `diagnostic only: ${finding.kind}`,
  };
}

async function scanGoals(request: ScanRequest): Promise<JsonObject> {
  const nowMs = request.now
    ? (parseTimestampMs(request.now) as number)
    : Date.now();
  const goalIds = request.goalIds ?? (await enumerateGoalIds(request.runtimeRoot));
  const goalsPayload: JsonObject[] = [];
  const flatDiscrepancies: JsonObject[] = [];
  const flatDiagnostics: JsonObject[] = [];
  const goalsClean: string[] = [];
  const summary = {
    goals_scanned: 0,
    goals_with_discrepancies: 0,
    total_discrepancies: 0,
    fixable: 0,
    unfixable_discrepancies: 0,
    correction_count: 0,
    affected_quota_entry_count: 0,
    would_deduct_slots: 0,
    would_backfill_slots: 0,
    current_window_slot_delta: 0,
  };
  const byKind: Record<string, number> = {};
  const diagnosticsByKind: Record<string, number> = {};
  for (const kind of [...DISCREPANCY_KINDS, ...DIAGNOSTIC_KINDS]) {
    if ((DISCREPANCY_KINDS as readonly string[]).includes(kind)) byKind[kind] = 0;
    diagnosticsByKind[kind] = 0;
  }

  for (const goalId of goalIds) {
    const windowHours = request.windowHoursByGoal[goalId] ?? DEFAULT_WINDOW_HOURS;
    const model = await loadReconciliationModel(request.runtimeRoot, goalId);
    const { findings, simulation } = analyzeGoal(
      model,
      request.toleranceSeconds * 1000,
      nowMs,
      windowHours,
    );
    summary.goals_scanned += 1;
    const discrepancyPayloads: JsonObject[] = [];
    const diagnosticPayloads: JsonObject[] = [];
    let goalHasDiscrepancy = false;
    for (const finding of findings) {
      const isDiscrepancy = (DISCREPANCY_KINDS as readonly string[]).includes(
        finding.kind,
      );
      const payload = buildFindingPayload(
        finding,
        finding.fixable
          ? correctionEffectId(
              finding.kind as ReconcileDiscrepancyKind,
              finding.fingerprintInputs,
            )
          : null,
      );
      if (isDiscrepancy) {
        goalHasDiscrepancy = true;
        summary.total_discrepancies += 1;
        byKind[finding.kind] = (byKind[finding.kind] ?? 0) + 1;
        if (finding.fixable) {
          summary.fixable += 1;
          summary.correction_count += 1;
        } else {
          summary.unfixable_discrepancies += 1;
        }
        discrepancyPayloads.push(payload);
        flatDiscrepancies.push(payload);
      } else {
        diagnosticsByKind[finding.kind] =
          (diagnosticsByKind[finding.kind] ?? 0) + 1;
        diagnosticPayloads.push(payload);
        flatDiagnostics.push(payload);
      }
    }
    if (model.receiptWithoutTargetKey) {
      diagnosticsByKind.receipt_without_target_key =
        (diagnosticsByKind.receipt_without_target_key ?? 0) +
        model.receiptWithoutTargetKey;
    }
    if (goalHasDiscrepancy) summary.goals_with_discrepancies += 1;
    if (findings.length === 0 && model.receiptWithoutTargetKey === 0) {
      goalsClean.push(goalId);
    }
    summary.affected_quota_entry_count += simulation.affectedEntryCount;
    summary.would_deduct_slots += simulation.deductSlots;
    summary.would_backfill_slots += simulation.backfillSlots;
    summary.current_window_slot_delta += simulation.currentWindowSlotDelta;
    goalsPayload.push({
      goal_id: goalId,
      index_digest: model.indexDigest,
      current_window_hours: windowHours,
      observed: {
        spend_events: model.spends.length,
        void_events: model.voids.length,
        spend_receipts: model.spendReceipts.length,
        void_receipts: model.voidReceipts.length,
      },
      discrepancy_count: discrepancyPayloads.length,
      affected_quota_entry_count: simulation.affectedEntryCount,
      current_window_slot_delta: simulation.currentWindowSlotDelta,
      would_deduct_slots: simulation.deductSlots,
      would_backfill_slots: simulation.backfillSlots,
      discrepancies: discrepancyPayloads,
      diagnostics: diagnosticPayloads,
    });
  }

  return {
    schema_version: QUOTA_RECONCILIATION_REPORT_SCHEMA,
    mode: "reconcile",
    dry_run: true,
    executed: false,
    ok: true,
    appended: false,
    registry_mutated: false,
    generated_at: request.generatedAt,
    timestamp_tolerance_seconds: request.toleranceSeconds,
    summary: {
      ...summary,
      by_kind: byKind,
      diagnostics: {
        total: Object.values(diagnosticsByKind).reduce(
          (sum, count) => sum + count,
          0,
        ),
        by_kind: diagnosticsByKind,
      },
    },
    goals: goalsPayload,
    discrepancies: flatDiscrepancies,
    diagnostics: flatDiagnostics,
    goals_clean: goalsClean,
  };
}

export async function evaluateQuotaReconcileScan(
  value: unknown,
): Promise<JsonObject> {
  const request = decodeScanRequest(value);
  return {
    schema_version: QUOTA_RECONCILE_SCAN_RESULT_SCHEMA,
    payload: await scanGoals(request),
  };
}

function decodeCommitRequest(value: unknown): CommitRequest {
  const request = requiredObject(value, "quota.reconcile.commit params");
  if (request.schema_version !== QUOTA_RECONCILE_COMMIT_REQUEST_SCHEMA) {
    throw new EffectRuntimeRequestError("Quota reconcile commit request schema mismatch");
  }
  const runtimeRoot = requiredString(request.runtime_root, "runtime_root").trim();
  if (!isAbsolute(runtimeRoot)) {
    throw new EffectRuntimeRequestError("runtime_root must be absolute");
  }
  const goalId = reconcileGoalId(request.goal_id, "goal_id");
  const execute = requireBoolean(request.execute, "execute");
  const expectedDigest = optionalString(
    request.expected_index_digest,
    "expected_index_digest",
  ) ?? null;
  const toleranceSeconds =
    request.timestamp_tolerance_seconds === undefined
      ? DEFAULT_TOLERANCE_SECONDS
      : requireInteger(
          request.timestamp_tolerance_seconds,
          "timestamp_tolerance_seconds",
        );
  if (toleranceSeconds < 0) {
    throw new EffectRuntimeRequestError(
      "timestamp_tolerance_seconds cannot be negative",
    );
  }
  if (!Array.isArray(request.items)) {
    throw new EffectRuntimeRequestError("items must be an array");
  }
  const items: CommitItemRequest[] = request.items.map((raw, index) => {
    const item = requiredObject(raw, `items[${index}]`);
    return {
      discrepancy_id: requiredString(
        item.discrepancy_id,
        `items[${index}].discrepancy_id`,
      ).trim(),
    };
  });
  return {
    schema_version: QUOTA_RECONCILE_COMMIT_REQUEST_SCHEMA,
    runtimeRoot,
    goalId,
    generatedAt: requiredString(request.generated_at, "generated_at").trim(),
    execute,
    expectedIndexDigest: expectedDigest,
    toleranceSeconds,
    items,
  };
}

function resolvedCorrectionDigestible(item: ResolvedCorrection): JsonObject {
  return {
    discrepancy_id: item.discrepancy_id,
    kind: item.kind,
    action: item.action,
    slots: item.slots,
    target_run_generated_at: item.target_run_generated_at,
    run_generated_at: item.run_generated_at,
    settlement_effect_id: item.settlement_effect_id,
    turn_instance_id: item.turn_instance_id,
    receipt_event_id: item.receipt_event_id,
    original_void_run_generated_at: item.original_void_run_generated_at,
    resolved_void_run_generated_at: item.resolved_void_run_generated_at,
  };
}

function reconciliationBlockForItem(
  item: ResolvedCorrection,
  effectId: string,
): JsonObject {
  return {
    schema_version: QUOTA_RECONCILIATION_CORRECTION_SCHEMA,
    discrepancy_id: item.discrepancy_id,
    discrepancy_kind: item.kind,
    effect_id: effectId,
    ...(item.receipt_event_id ? { receipt_event_id: item.receipt_event_id } : {}),
    ...(item.original_void_run_generated_at
      ? { original_void_run_generated_at: item.original_void_run_generated_at }
      : {}),
    ...(item.resolved_void_run_generated_at
      ? { resolved_void_run_generated_at: item.resolved_void_run_generated_at }
      : {}),
  };
}

function correctionArtifactKind(
  action: ResolvedCorrection["action"],
): QuotaAccountingArtifactKind {
  return action === "append_void" ? "reconcile_void" : "reconcile_spend";
}

function correctionArtifactKindByDiscrepancyKind(
  kind: ReconcileDiscrepancyKind,
): QuotaAccountingArtifactKind {
  return kind === "reimbursement_without_consumption"
    ? "reconcile_spend"
    : "reconcile_void";
}

function buildCorrectionPreparation(
  goalId: string,
  item: ResolvedCorrection,
  effectId: string,
  requestDigest: string,
  generatedAt: string,
  context: {
    jsonPath: string;
    markdownPath: string;
    indexPath: string;
  },
): {
  record: JsonObject;
  indexRecord: JsonObject;
  markdown: string;
  payload: JsonObject;
} {
  const reason = CORRECTION_REASONS[item.kind];
  if (item.action === "append_void") {
    const target = item.target_run_generated_at ?? "";
    if (!target) {
      throw new EffectRuntimeRequestError(
        `correction ${item.discrepancy_id} requires target_run_generated_at`,
      );
    }
    const quotaEvent: JsonObject = {
      event_type: QUOTA_SLOT_VOIDED_CLASSIFICATION,
      source: "controller",
      slots: item.slots,
      reason_summary: reason,
      voided_run_generated_at: target,
      voided_run_classification: QUOTA_SLOT_SPENT_CLASSIFICATION,
      reconciliation: reconciliationBlockForItem(item, effectId),
      before: {},
      after: {},
    };
    const record: JsonObject = {
      generated_at: generatedAt,
      goal_id: goalId,
      classification: QUOTA_SLOT_VOIDED_CLASSIFICATION,
      recommended_action: reason,
      health_check:
        "quota reconciliation correction event public-safe; original spend preserved for audit",
      quota_event: quotaEvent,
      quota_void_commit: {
        schema_version: QUOTA_RECONCILE_VOID_RECEIPT_SCHEMA,
        effect_id: effectId,
        request_digest: requestDigest,
      },
    };
    const indexRecord: JsonObject = {
      generated_at: generatedAt,
      goal_id: goalId,
      classification: QUOTA_SLOT_VOIDED_CLASSIFICATION,
      recommended_action: reason,
      health_check: record.health_check,
      json_path: context.jsonPath,
      markdown_path: context.markdownPath,
      quota_void_commit: record.quota_void_commit,
    };
    const payload: JsonObject = {
      ok: true,
      mode: "reconcile",
      dry_run: false,
      goal_id: goalId,
      slots: item.slots,
      appended: true,
      registry_mutated: false,
      source: "controller",
      classification: QUOTA_SLOT_VOIDED_CLASSIFICATION,
      generated_at: generatedAt,
      quota_event: quotaEvent,
      json_path: context.jsonPath,
      markdown_path: context.markdownPath,
      index_path: context.indexPath,
      effect_id: effectId,
      reason: `appended quota reconciliation void event: ${item.slots} slot(s) targeted at ${target}`,
    };
    return {
      record,
      indexRecord,
      markdown: renderQuotaSlotMarkdown(payload, QUOTA_SLOT_VOIDED_CLASSIFICATION),
      payload,
    };
  }

  const runGeneratedAt = item.run_generated_at ?? "";
  if (!runGeneratedAt) {
    throw new EffectRuntimeRequestError(
      `correction ${item.discrepancy_id} requires run_generated_at`,
    );
  }
  const settlementEffectId = item.settlement_effect_id ?? "";
  if (!settlementEffectId) {
    throw new EffectRuntimeRequestError(
      `correction ${item.discrepancy_id} requires settlement_effect_id`,
    );
  }
  const quotaEvent: JsonObject = {
    event_type: QUOTA_SLOT_SPENT_CLASSIFICATION,
    source: "controller",
    todo_id: null,
    replan_obligation_id: null,
    turn_instance_id: item.turn_instance_id,
    settlement_identity: { effect_id: settlementEffectId },
    effect_ref: `${settlementEffectId}#quota_spend`,
    slots: item.slots,
    reason_summary: reason,
    delivery_run_generated_at: null,
    reconciliation: reconciliationBlockForItem(item, effectId),
    accounting_projection: {
      schema_version: QUOTA_SLOT_ACCOUNTING_PROJECTION_SCHEMA,
      settlement_event_semantics: "append_only",
      spent_slots_semantics: "rolling_window_aggregate",
      before_after_semantics: "reconciliation_correction",
      window_hours: null,
    },
  };
  const record: JsonObject = {
    generated_at: runGeneratedAt,
    goal_id: goalId,
    classification: QUOTA_SLOT_SPENT_CLASSIFICATION,
    recommended_action: reason,
    health_check:
      "quota reconciliation backfill event public-safe; derived from a committed settlement receipt",
    quota_event: quotaEvent,
    quota_spend_commit: {
      schema_version: QUOTA_RECONCILE_SPEND_RECEIPT_SCHEMA,
      effect_id: effectId,
      request_digest: requestDigest,
    },
  };
  if (item.turn_instance_id) {
    record.turn_instance_id = item.turn_instance_id;
    quotaEvent.turn_instance_id = item.turn_instance_id;
  }
  const indexRecord: JsonObject = {
    generated_at: runGeneratedAt,
    goal_id: goalId,
    classification: QUOTA_SLOT_SPENT_CLASSIFICATION,
    recommended_action: reason,
    health_check: record.health_check,
    json_path: context.jsonPath,
    markdown_path: context.markdownPath,
    quota_spend_commit: record.quota_spend_commit,
  };
  if (item.turn_instance_id) {
    indexRecord.turn_instance_id = item.turn_instance_id;
    indexRecord.settlement_identity = { effect_id: settlementEffectId };
  }
  const payload: JsonObject = {
    ok: true,
    mode: "reconcile",
    dry_run: false,
    goal_id: goalId,
    slots: item.slots,
    appended: true,
    registry_mutated: false,
    source: "controller",
    classification: QUOTA_SLOT_SPENT_CLASSIFICATION,
    generated_at: runGeneratedAt,
    quota_event: quotaEvent,
    json_path: context.jsonPath,
    markdown_path: context.markdownPath,
    index_path: context.indexPath,
    effect_id: effectId,
    reason:
      `appended quota reconciliation spend event: ${item.slots} slot(s) ` +
      `attested by settlement ${settlementEffectId}`,
  };
  return {
    record,
    indexRecord,
    markdown: renderQuotaSlotMarkdown(payload, QUOTA_SLOT_SPENT_CLASSIFICATION),
    payload,
  };
}

interface BatchReceipt extends JsonObject {
  schema_version: typeof QUOTA_RECONCILE_BATCH_RECEIPT_SCHEMA;
  batch_id: string;
  request_digest: string;
  goal_id: string;
  status: "prepared" | "committed";
  tolerance_seconds: number;
  items: Array<{
    discrepancy_id: string;
    kind: ReconcileDiscrepancyKind | null;
    effect_id: string;
  }>;
  corrections: JsonObject[];
}

function batchReceiptPath(runsDir: string, batchId: string): string {
  return join(
    runsDir,
    ".transactions",
    "quota-reconcile",
    `${batchId}.json`,
  );
}

async function readBatchReceipt(path: string): Promise<BatchReceipt | null> {
  const content = await readOptionalText(path);
  if (content === null) return null;
  let value: unknown;
  try {
    value = JSON.parse(content);
  } catch {
    throw new EffectRuntimeRequestError(
      "quota reconciliation batch receipt is malformed",
      "malformed_transaction_receipt",
    );
  }
  const receipt = requiredObject(value, "quota reconciliation batch receipt");
  if (receipt.schema_version !== QUOTA_RECONCILE_BATCH_RECEIPT_SCHEMA) {
    throw new EffectRuntimeRequestError(
      "quota reconciliation batch receipt schema mismatch",
      "malformed_transaction_receipt",
    );
  }
  return receipt as BatchReceipt;
}

interface PlannedItem {
  discrepancyId: string;
  kind: ReconcileDiscrepancyKind | null;
  effectId: string;
  state: "apply" | "recovered" | "normalize" | "already_resolved";
  correction: ResolvedCorrection | null;
  recoveredStatus: "written" | "replayed" | "repaired" | null;
}

function resolveCorrectionFromFinding(
  discrepancyId: string,
  finding: ReconcileFinding,
): ResolvedCorrection {
  const correction = finding.correction;
  if (!correction) {
    throw new EffectRuntimeRequestError(
      `discrepancy ${discrepancyId} has no correction plan`,
    );
  }
  return {
    discrepancy_id: discrepancyId,
    kind: finding.kind as ReconcileDiscrepancyKind,
    action: correction.action,
    slots: correction.slots,
    target_run_generated_at: correction.targetRunGeneratedAt,
    run_generated_at: correction.runGeneratedAt,
    settlement_effect_id: correction.settlementEffectId,
    turn_instance_id: correction.turnInstanceId,
    receipt_event_id: correction.receiptEventId,
    original_void_run_generated_at: correction.originalVoidRunGeneratedAt,
    resolved_void_run_generated_at: correction.resolvedVoidRunGeneratedAt,
  };
}

export async function evaluateQuotaReconcileCommit(
  value: unknown,
): Promise<JsonObject> {
  const request = decodeCommitRequest(value);
  const runsDir = join(request.runtimeRoot, "goals", request.goalId, "runs");
  const indexPath = join(runsDir, "index.jsonl");

  if (!request.execute) {
    return {
      schema_version: QUOTA_RECONCILE_COMMIT_RESULT_SCHEMA,
      status: "preview",
      goal_id: request.goalId,
      dry_run: true,
      appended: false,
      items: request.items.map((item) => ({
        discrepancy_id: item.discrepancy_id,
        status: "preview",
      })),
      reason: "quota reconciliation commit preview evaluated by TypeScript",
    };
  }

  // The batch identity is the ordered set of discrepancy ids plus the scan
  // tolerance. Corrections are derived from the live ledger under the lock,
  // so a retried commit after a lost response resolves to the same identity.
  // generated_at is a transport fact and never participates in the digest.
  const batchDigest = sha256(canonicalJson({
    schema_version: request.schema_version,
    goal_id: request.goalId,
    tolerance_seconds: request.toleranceSeconds,
    discrepancy_ids: request.items.map((item) => item.discrepancy_id),
  }));
  const batchId = batchDigest.slice(
    "sha256:".length,
    "sha256:".length + 16,
  );
  const receiptPath = batchReceiptPath(runsDir, batchId);

  // The batch itself takes no index lock: each correction rides the shared
  // accounting artifact transaction, which owns the file mutation lock per
  // append. The expected-digest chain between items makes any concurrent
  // spend/void/other reconciliation writer fail closed on the next item
  // instead of double-applying.
  return await (async () => {
    const currentDigest = await quotaAccountingIndexDigest(indexPath);
    const existing = await readBatchReceipt(receiptPath);
    if (existing && existing.request_digest !== batchDigest) {
      return {
        schema_version: QUOTA_RECONCILE_COMMIT_RESULT_SCHEMA,
        status: "conflict",
        goal_id: request.goalId,
        conflict: true,
        reason_code: "batch_request_conflict",
        index_digest: currentDigest,
        reason:
          "quota reconciliation batch identity is already bound to a different request",
        items: [],
      };
    }

    if (request.items.length === 0) {
      return {
        schema_version: QUOTA_RECONCILE_COMMIT_RESULT_SCHEMA,
        status: "applied",
        goal_id: request.goalId,
        dry_run: false,
        conflict: false,
        appended: false,
        written: 0,
        replayed: 0,
        repaired: 0,
        normalized: 0,
        already_resolved: 0,
        correction_count: 0,
        index_digest: currentDigest,
        items: [],
        reason: "no reconciliation corrections requested",
      };
    }

    // A first attempt honors the compare-and-swap precondition. A retried
    // batch skips it and relies on the stored per-item receipts: recovery
    // may need to repair a truncated index tail, whose live digest by
    // definition cannot match the pre-crash value.
    if (!existing && request.expectedIndexDigest !== currentDigest) {
      return {
        schema_version: QUOTA_RECONCILE_COMMIT_RESULT_SCHEMA,
        status: "conflict",
        goal_id: request.goalId,
        conflict: true,
        reason_code: "index_digest_conflict",
        index_digest: currentDigest,
        reason: "quota run index compare-and-swap precondition failed",
        items: [],
      };
    }

    // Recovery pass: an existing batch receipt means a previous attempt may
    // have been interrupted after preparing per-item receipts or with a
    // truncated index tail. Replay every stored item through its transaction
    // first; this also tolerates a half-written final index line, which the
    // subsequent scan cannot parse yet.
    const recoveredByDiscrepancy = new Map<
      string,
      {
        effectId: string;
        kind: ReconcileDiscrepancyKind;
        status: "written" | "replayed" | "repaired";
      }
    >();
    let chainDigest: string | null = currentDigest;
    if (existing) {
      for (const stored of existing.items) {
        if (!stored.effect_id || !stored.kind) continue;
        const outcome = await commitQuotaAccountingArtifactTransaction({
          kind: correctionArtifactKindByDiscrepancyKind(stored.kind),
          runsDir,
          generatedAt: request.generatedAt,
          effectId: stored.effect_id,
          requestDigest: batchDigest,
          expectedIndexDigest: chainDigest,
          prepare: () => {
            throw new EffectRuntimeRequestError(
              "quota reconciliation recovery cannot mint a new correction",
            );
          },
        });
        if (outcome.status === "conflict") {
          return {
            schema_version: QUOTA_RECONCILE_COMMIT_RESULT_SCHEMA,
            status: "conflict",
            goal_id: request.goalId,
            conflict: true,
            reason_code: outcome.reasonCode,
            index_digest: outcome.indexDigest,
            reason: outcome.reason,
            items: [],
          };
        }
        if (outcome.status === "not_found") {
          throw new EffectRuntimeRequestError(outcome.reason);
        }
        chainDigest = outcome.indexDigest;
        recoveredByDiscrepancy.set(stored.discrepancy_id, {
          effectId: stored.effect_id,
          kind: stored.kind,
          status: outcome.status,
        });
      }
    }

    // Re-scan after recovery so a stale plan can never double-apply a
    // correction whose discrepancy is already resolved.
    const nowMs = Date.now();
    const indexContent = await readOptionalText(indexPath);
    const currentRecords = parseQuotaAccountingIndex(indexContent);
    const model = await loadReconciliationModel(request.runtimeRoot, request.goalId);
    const { findings } = analyzeGoal(
      model,
      request.toleranceSeconds * 1000,
      nowMs,
      DEFAULT_WINDOW_HOURS,
    );
    const fixableById = new Map<string, { finding: ReconcileFinding; effectId: string }>();
    for (const finding of findings) {
      if (!finding.fixable) continue;
      const effectId = correctionEffectId(
        finding.kind as ReconcileDiscrepancyKind,
        finding.fingerprintInputs,
      );
      const payload = buildFindingPayload(finding, effectId);
      fixableById.set(String(payload.discrepancy_id), { finding, effectId });
    }
    const priorByDiscrepancy = new Map<
      string,
      { effectId: string; kind: ReconcileDiscrepancyKind | null }
    >();
    for (const item of existing?.items ?? []) {
      priorByDiscrepancy.set(item.discrepancy_id, {
        effectId: item.effect_id,
        kind: item.kind,
      });
    }

    const plannedItems: PlannedItem[] = [];
    for (const item of request.items) {
      const recovered = recoveredByDiscrepancy.get(item.discrepancy_id);
      if (recovered) {
        plannedItems.push({
          discrepancyId: item.discrepancy_id,
          kind: recovered.kind,
          effectId: recovered.effectId,
          state: "recovered",
          correction: null,
          recoveredStatus: recovered.status,
        });
        continue;
      }
      const match = fixableById.get(item.discrepancy_id);
      if (match) {
        plannedItems.push({
          discrepancyId: item.discrepancy_id,
          kind: match.finding.kind as ReconcileDiscrepancyKind,
          effectId: match.effectId,
          state: "apply",
          correction: resolveCorrectionFromFinding(
            item.discrepancy_id,
            match.finding,
          ),
          recoveredStatus: null,
        });
        continue;
      }
      // The discrepancy is gone on the live ledger. A batch receipt missing
      // the recovery mark but holding a prepared per-item receipt whose
      // effect is already present in the index is normalized without
      // appending a second event.
      const prior = priorByDiscrepancy.get(item.discrepancy_id);
      if (
        prior &&
        prior.effectId &&
        prior.kind &&
        resolveQuotaAccountingEffect(
          correctionArtifactKindByDiscrepancyKind(prior.kind),
          currentRecords,
          prior.effectId,
        ).kind === "matched"
      ) {
        plannedItems.push({
          discrepancyId: item.discrepancy_id,
          kind: prior.kind,
          effectId: prior.effectId,
          state: "normalize",
          correction: null,
          recoveredStatus: null,
        });
      } else {
        plannedItems.push({
          discrepancyId: item.discrepancy_id,
          kind: prior?.kind ?? null,
          effectId: "",
          state: "already_resolved",
          correction: null,
          recoveredStatus: null,
        });
      }
    }

    const batchReceipt: BatchReceipt = {
      schema_version: QUOTA_RECONCILE_BATCH_RECEIPT_SCHEMA,
      batch_id: batchId,
      request_digest: batchDigest,
      goal_id: request.goalId,
      status: "prepared",
      tolerance_seconds: request.toleranceSeconds,
      items: plannedItems.map((planned) => ({
        discrepancy_id: planned.discrepancyId,
        kind: planned.kind,
        effect_id: planned.effectId,
      })),
      corrections: plannedItems
        .filter((planned) => planned.correction !== null)
        .map((planned) => resolvedCorrectionDigestible(planned.correction as ResolvedCorrection)),
    };
    await atomicWriteJson(receiptPath, batchReceipt);

    const itemResults: JsonObject[] = [];
    let written = 0;
    let replayed = 0;
    let repaired = 0;
    let normalized = 0;
    let alreadyResolved = 0;

    for (const planned of plannedItems) {
      if (planned.state === "already_resolved") {
        alreadyResolved += 1;
        itemResults.push({
          discrepancy_id: planned.discrepancyId,
          kind: planned.kind,
          status: "already_resolved",
          appended: false,
        });
        continue;
      }
      if (planned.state === "recovered" && planned.recoveredStatus) {
        // Recovery already replayed/repaired this item before the rescan.
        const status = planned.recoveredStatus;
        if (status === "written") written += 1;
        else if (status === "replayed") replayed += 1;
        else repaired += 1;
        itemResults.push({
          discrepancy_id: planned.discrepancyId,
          kind: planned.kind,
          action: null,
          slots: null,
          status,
          appended: status !== "replayed",
          idempotent_replay: status === "replayed",
          transaction_repaired: status === "repaired",
          recovered: true,
          effect_id: planned.effectId,
        });
        continue;
      }
      const generatedAt = planned.correction
        ? (planned.correction.action === "append_spend"
          ? (planned.correction.run_generated_at ?? request.generatedAt)
          : request.generatedAt)
        : request.generatedAt;
      const outcome = await commitQuotaAccountingArtifactTransaction({
        kind: planned.kind === null
          ? "reconcile_void"
          : correctionArtifactKindByDiscrepancyKind(planned.kind),
        runsDir,
        generatedAt,
        effectId: planned.effectId,
        requestDigest: batchDigest,
        expectedIndexDigest: chainDigest,
        prepare: (context) => {
          if (!planned.correction) {
            throw new EffectRuntimeRequestError(
              "quota reconciliation normalization cannot mint a new correction",
            );
          }
          return {
            kind: "prepared" as const,
            ...buildCorrectionPreparation(
              request.goalId,
              planned.correction,
              planned.effectId,
              batchDigest,
              generatedAt,
              context,
            ),
          };
        },
      });
      if (outcome.status === "conflict") {
        return {
          schema_version: QUOTA_RECONCILE_COMMIT_RESULT_SCHEMA,
          status: "conflict",
          goal_id: request.goalId,
          conflict: true,
          reason_code: outcome.reasonCode,
          index_digest: outcome.indexDigest,
          reason: outcome.reason,
          items: itemResults,
        };
      }
      if (outcome.status === "not_found") {
        throw new EffectRuntimeRequestError(outcome.reason);
      }
      if (planned.state === "normalize" && outcome.status === "written") {
        throw new EffectRuntimeRequestError(
          "quota reconciliation normalization unexpectedly appended a new correction",
        );
      }
      chainDigest = outcome.indexDigest;
      if (planned.state === "normalize") {
        normalized += 1;
      } else if (outcome.status === "written") {
        written += 1;
      } else if (outcome.status === "replayed") {
        replayed += 1;
      } else {
        repaired += 1;
      }
      itemResults.push({
        discrepancy_id: planned.discrepancyId,
        kind: planned.kind,
        action: planned.correction?.action ?? null,
        slots: planned.correction?.slots ?? null,
        status: outcome.status,
        appended:
          planned.state === "apply" &&
          (outcome.status === "written" || outcome.status === "repaired"),
        idempotent_replay:
          outcome.status === "replayed" || planned.state === "normalize",
        transaction_repaired: outcome.status === "repaired",
        normalized: planned.state === "normalize",
        effect_id: planned.effectId,
        artifact_json_path:
          jsonObject(outcome.receipt.payload)?.json_path ?? null,
      });
    }

    await atomicWriteJson(receiptPath, {
      ...batchReceipt,
      status: "committed",
    } satisfies BatchReceipt);

    const appended = written + repaired > 0;
    return {
      schema_version: QUOTA_RECONCILE_COMMIT_RESULT_SCHEMA,
      status: "applied",
      goal_id: request.goalId,
      dry_run: false,
      conflict: false,
      appended,
      written,
      replayed,
      repaired,
      normalized,
      already_resolved: alreadyResolved,
      correction_count: written + repaired,
      index_digest: chainDigest,
      batch_id: batchId,
      items: itemResults,
      reason:
        `quota reconciliation applied ${written + repaired} correction(s), ` +
        `${replayed} replayed, ${normalized} normalized, ` +
        `${alreadyResolved} already resolved`,
    };
  })();
}
