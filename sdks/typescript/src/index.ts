import { createHash } from "node:crypto";
import { closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

export const CLOUD_EVENTS_SPECVERSION = "1.0";
export const USAGE_CLOUD_EVENT_TYPE = "org.contextualwisdomlab.metering.usage.v1";

export class ProducerContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ProducerContractError";
  }
}

export interface Measurement {
  meter_code: string;
  meter_version?: number;
  quantity: string;
  unit_code: string;
  quality_code: string;
}

export interface CorrectionLineage {
  prior_event_id: string;
  relationship_code: "corrects" | "reverses" | "supersedes";
  reason_code?: string;
}

export interface UsageEventInput {
  event_id: string;
  event_contract_version: number;
  producer_contract_version: number;
  source_event_key: string;
  tenant_reference: string;
  billing_account_reference: string;
  billing_principal_reference: string;
  credential_reference?: string;
  cost_center_reference?: string;
  project_reference?: string;
  repository_reference?: string;
  trace_reference?: string;
  correlation_reference?: string;
  causation_reference?: string;
  available_at?: string;
  correction_lineage?: CorrectionLineage;
  product_code: string;
  operation_code?: string;
  dimensions?: Record<string, string>;
  occurred_at: string;
  measurements: Measurement[];
}

export interface UsageEvent extends UsageEventInput {
  source_payload_hash: string;
}

export interface UsageCloudEvent {
  specversion: string;
  id: string;
  source: string;
  type: string;
  subject: string;
  time: string;
  datacontenttype: string;
  data: UsageEvent;
}

export interface OutboxFlushResult {
  attemptedCount: number;
  acceptedCount: number;
  duplicateReplayCount: number;
  rejectedCount: number;
  retriedCount: number;
  deadLetteredCount: number;
  pendingCount: number;
}

export type UsageDeliveryResponse = { event_receipts: Array<Record<string, unknown>> };
export type UsageDeliverySender = (
  events: readonly UsageEvent[],
) => UsageDeliveryResponse | Promise<UsageDeliveryResponse>;

export class TransientDeliveryError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "TransientDeliveryError";
  }
}

export class PermanentDeliveryError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PermanentDeliveryError";
  }
}

type OutboxRecord = {
  event: UsageEvent;
  attempts: number;
  state: "pending" | "dead_letter";
  lastErrorCode?: string;
};

/** A process-local durable queue; the file is atomically replaced per change. */
export class FileUsageOutbox {
  private readonly filePath: string;

  constructor(filePath: string) {
    this.filePath = filePath;
    mkdirSync(dirname(filePath), { recursive: true });
    if (!existsSync(filePath)) this.write([]);
  }

  enqueue(event: UsageEvent): void {
    buildUsageCloudEvent(event, "urn:cwl:producer:outbox-validation");
    const records = this.read();
    const existing = records.find((record) => record.event.event_id === event.event_id);
    if (existing !== undefined) {
      if (stableJson(existing.event) !== stableJson(event)) {
        throw new ProducerContractError("event_id already has different event bytes");
      }
      return;
    }
    records.push({ event: structuredClone(event), attempts: 0, state: "pending" });
    this.write(records);
  }

  replayDeadLetter(eventId: string): void {
    const records = this.read();
    const record = records.find((candidate) => candidate.event.event_id === eventId);
    if (record === undefined || record.state !== "dead_letter") throw new Error(`unknown dead letter: ${eventId}`);
    record.attempts = 0;
    record.state = "pending";
    delete record.lastErrorCode;
    this.write(records);
  }

  pendingCount(): number {
    return this.read().filter((record) => record.state === "pending").length;
  }

  deadLetterCount(): number {
    return this.read().filter((record) => record.state === "dead_letter").length;
  }

  async flush(sender: UsageDeliverySender, batchSize = 100, maxAttempts = 5): Promise<OutboxFlushResult> {
    if (!Number.isInteger(batchSize) || batchSize < 1 || !Number.isInteger(maxAttempts) || maxAttempts < 1) {
      throw new RangeError("batchSize and maxAttempts must be positive integers");
    }
    const records = this.read();
    const batch = records.filter((record) => record.state === "pending").slice(0, batchSize);
    if (batch.length === 0) return this.result(0, 0, 0, 0, 0, 0);
    let accepted = 0;
    let duplicateReplay = 0;
    let rejected = 0;
    let retried = 0;
    let deadLettered = 0;
    const fail = (record: OutboxRecord, code: string, forceDead = false): void => {
      record.attempts += 1;
      if (forceDead || record.attempts >= maxAttempts) {
        record.state = "dead_letter";
        deadLettered += 1;
      } else {
        retried += 1;
      }
      record.lastErrorCode = code;
    };
    try {
      const response = await sender(batch.map((record) => record.event));
      if (!response || !Array.isArray(response.event_receipts)) {
        for (const record of batch) fail(record, "invalid_delivery_response");
      } else {
        for (const record of batch) {
          const receipt = findReceipt(response.event_receipts, record.event);
          if (receipt?.ingestion_outcome_code === "accepted" && receiptMatches(receipt, record.event)) {
            records.splice(records.indexOf(record), 1);
            accepted += 1;
          } else if (receipt?.ingestion_outcome_code === "duplicate_replay" && receiptMatches(receipt, record.event)) {
            records.splice(records.indexOf(record), 1);
            duplicateReplay += 1;
          } else if (receipt?.ingestion_outcome_code === "rejected" && receiptMatches(receipt, record.event)) {
            fail(record, typeof receipt.rejection_reason_code === "string" ? receipt.rejection_reason_code : "rejected", true);
            rejected += 1;
          } else {
            fail(record, "invalid_delivery_receipt");
          }
        }
      }
    } catch (error) {
      const permanent = error instanceof PermanentDeliveryError;
      for (const record of batch) fail(record, permanent ? "transport_permanent" : "transport_transient", permanent);
    }
    this.write(records);
    return this.result(batch.length, accepted, duplicateReplay, rejected, retried, deadLettered);
  }

  private result(attemptedCount: number, acceptedCount: number, duplicateReplayCount: number, rejectedCount: number, retriedCount: number, deadLetteredCount: number): OutboxFlushResult {
    return { attemptedCount, acceptedCount, duplicateReplayCount, rejectedCount, retriedCount, deadLetteredCount, pendingCount: this.pendingCount() };
  }

  private read(): OutboxRecord[] {
    return JSON.parse(readFileSync(this.filePath, "utf8")) as OutboxRecord[];
  }

  private write(records: OutboxRecord[]): void {
    const temporaryPath = `${this.filePath}.tmp`;
    writeFileSync(temporaryPath, JSON.stringify(records), { encoding: "utf8", mode: 0o600 });
    const descriptor = openSync(temporaryPath, "r");
    fsyncSync(descriptor);
    closeSync(descriptor);
    renameSync(temporaryPath, this.filePath);
    const directory = openSync(dirname(this.filePath), "r");
    fsyncSync(directory);
    closeSync(directory);
  }
}

/** Fetch-based sender for the platform's existing POST /v1/usage-events route. */
export function httpUsageIngestionTransport(
  endpoint: string,
  headers: Record<string, string> = {},
  timeoutMilliseconds = 10_000,
): UsageDeliverySender {
  let parsedEndpoint: URL;
  try {
    parsedEndpoint = new URL(endpoint);
  } catch {
    throw new ProducerContractError("endpoint must be an absolute HTTPS URL");
  }
  if (parsedEndpoint.protocol !== "https:" || !parsedEndpoint.host) {
    throw new ProducerContractError("endpoint must be an absolute HTTPS URL");
  }
  return async (events) => {
    const controller = new AbortController();
    const timeoutHandle = setTimeout(() => controller.abort(), timeoutMilliseconds);
    try {
      let response: Response;
      try {
        response = await fetch(endpoint, {
          method: "POST",
          headers: { "content-type": "application/json", ...headers },
          body: JSON.stringify({ events }),
          redirect: "error",
          signal: controller.signal,
        });
      } catch (error) {
        throw new TransientDeliveryError("network");
      }
      if (response.status >= 500 || response.status === 408 || response.status === 429) {
        throw new TransientDeliveryError(response.status >= 500 ? "http_5xx" : `http_${response.status}`);
      }
      if (!response.ok && response.status !== 422) throw new PermanentDeliveryError(`http_${response.status}`);
      let body: unknown;
      try {
        body = await response.json();
      } catch (error) {
        throw new TransientDeliveryError("invalid_json");
      }
      if (!body || typeof body !== "object" || !Array.isArray((body as { event_receipts?: unknown }).event_receipts)) {
        throw new TransientDeliveryError("invalid_delivery_response");
      }
      return body as UsageDeliveryResponse;
    } finally {
      clearTimeout(timeoutHandle);
    }
  };
}

export function buildUsageEvent(input: UsageEventInput): UsageEvent {
  validateInput(input);
  const event: UsageEvent = {
    event_id: input.event_id,
    event_contract_version: input.event_contract_version,
    producer_contract_version: input.producer_contract_version,
    source_event_key: input.source_event_key,
    tenant_reference: input.tenant_reference,
    billing_account_reference: input.billing_account_reference,
    billing_principal_reference: input.billing_principal_reference,
    ...(input.credential_reference === undefined
      ? {}
      : { credential_reference: input.credential_reference }),
    ...(input.cost_center_reference === undefined
      ? {}
      : { cost_center_reference: input.cost_center_reference }),
    ...(input.project_reference === undefined ? {} : { project_reference: input.project_reference }),
    ...(input.repository_reference === undefined ? {} : { repository_reference: input.repository_reference }),
    ...(input.trace_reference === undefined ? {} : { trace_reference: input.trace_reference }),
    ...(input.correlation_reference === undefined ? {} : { correlation_reference: input.correlation_reference }),
    ...(input.causation_reference === undefined ? {} : { causation_reference: input.causation_reference }),
    ...(input.available_at === undefined ? {} : { available_at: input.available_at }),
    ...(input.correction_lineage === undefined ? {} : { correction_lineage: { ...input.correction_lineage } }),
    product_code: input.product_code,
    ...(input.operation_code === undefined ? {} : { operation_code: input.operation_code }),
    ...(input.dimensions === undefined ? {} : { dimensions: { ...input.dimensions } }),
    occurred_at: input.occurred_at,
    measurements: input.measurements.map((measurement) => ({ ...measurement })),
    source_payload_hash: "",
  };
  event.source_payload_hash = computeSourcePayloadHash(event);
  return event;
}

export function buildUsageCloudEvent(event: UsageEvent, source: string): UsageCloudEvent {
  if (typeof source !== "string" || source.length === 0) {
    throw new ProducerContractError("CloudEvents source must be a non-empty string");
  }
  validateEvent(event);
  const expectedHash = computeSourcePayloadHash(event);
  if (event.source_payload_hash !== expectedHash) {
    throw new ProducerContractError("source_payload_hash must equal " + expectedHash);
  }
  return {
    specversion: CLOUD_EVENTS_SPECVERSION,
    id: event.event_id,
    source,
    type: USAGE_CLOUD_EVENT_TYPE,
    subject: event.source_event_key,
    time: event.occurred_at,
    datacontenttype: "application/json",
    data: { ...event, measurements: event.measurements.map((measurement) => ({ ...measurement })) },
  };
}

export function canonicalSourcePayloadJson(event: UsageEvent): string {
  const payload: Record<string, unknown> = {};
  if (event.available_at !== undefined) {
    payload.available_at = canonicalTimestamp(event.available_at);
  }
  payload.billing_account_reference = event.billing_account_reference;
  payload.billing_principal_reference = event.billing_principal_reference;
  if (event.causation_reference !== undefined) {
    payload.causation_reference = event.causation_reference;
  }
  if (event.correction_lineage !== undefined) {
    payload.correction_lineage = {
      prior_event_id: event.correction_lineage.prior_event_id,
      ...(event.correction_lineage.reason_code === undefined
        ? {}
        : { reason_code: event.correction_lineage.reason_code }),
      relationship_code: event.correction_lineage.relationship_code,
    };
  }
  if (event.correlation_reference !== undefined) {
    payload.correlation_reference = event.correlation_reference;
  }
  if (event.cost_center_reference !== undefined) {
    payload.cost_center_reference = event.cost_center_reference;
  }
  if (event.credential_reference !== undefined) {
    payload.credential_reference = event.credential_reference;
  }
  if (event.dimensions !== undefined) {
    payload.dimensions = Object.fromEntries(
      Object.entries(event.dimensions).sort(([left], [right]) => (left > right ? 1 : -1)),
    );
  }
  payload.event_contract_version = event.event_contract_version;
  payload.measurements = event.measurements.map((measurement) => ({
    meter_code: measurement.meter_code,
    ...(measurement.meter_version === undefined ? {} : { meter_version: measurement.meter_version }),
    quality_code: measurement.quality_code,
    quantity: canonicalQuantity(measurement.quantity),
    unit_code: measurement.unit_code,
  }));
  payload.occurred_at = canonicalTimestamp(event.occurred_at);
  if (event.operation_code !== undefined) {
    payload.operation_code = event.operation_code;
  }
  payload.producer_contract_version = event.producer_contract_version;
  payload.product_code = event.product_code;
  if (event.project_reference !== undefined) {
    payload.project_reference = event.project_reference;
  }
  if (event.repository_reference !== undefined) {
    payload.repository_reference = event.repository_reference;
  }
  payload.tenant_reference = event.tenant_reference;
  if (event.trace_reference !== undefined) {
    payload.trace_reference = event.trace_reference;
  }
  return JSON.stringify(payload);
}

function computeSourcePayloadHash(event: UsageEvent): string {
  return "sha256:" + createHash("sha256").update(canonicalSourcePayloadJson(event), "utf8").digest("hex");
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, entryValue]) => entryValue !== undefined)
      .sort(([left], [right]) => (left > right ? 1 : -1));
    return `{${entries.map(([key, entryValue]) => `${JSON.stringify(key)}:${stableJson(entryValue)}`).join(",")}}`;
  }
  return JSON.stringify(value) as string;
}

function canonicalQuantity(quantity: string): string {
  validateQuantity(quantity);
  const [integer, fraction = ""] = quantity.split(".");
  const trimmedFraction = fraction.replace(/0+$/, "");
  return trimmedFraction.length === 0 ? integer : integer + "." + trimmedFraction;
}

function canonicalTimestamp(timestamp: string): string {
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$/.exec(
    timestamp,
  );
  if (match === null) {
    throw new ProducerContractError("occurred_at must be an RFC3339 date-time");
  }
  const [, dateTime, fraction = "", offset] = match;
  if (fraction.length > 6) {
    throw new ProducerContractError("timestamps cannot contain sub-microsecond precision");
  }
  const parsed = Date.parse(dateTime + offset);
  if (Number.isNaN(parsed)) {
    throw new ProducerContractError("occurred_at must be an RFC3339 date-time");
  }
  const utcDateTime = new Date(parsed).toISOString().slice(0, 19);
  const microseconds = fraction.padEnd(6, "0").slice(0, 6);
  return microseconds === "000000"
    ? utcDateTime + "Z"
    : utcDateTime + "." + microseconds + "Z";
}

function validateInput(input: UsageEventInput): void {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(input.event_id)) {
    throw new ProducerContractError("event_id must be a UUID");
  }
  if (!Number.isInteger(input.event_contract_version) || input.event_contract_version < 1) {
    throw new ProducerContractError("event_contract_version must be at least 1");
  }
  if (!Number.isInteger(input.producer_contract_version) || input.producer_contract_version < 1) {
    throw new ProducerContractError("producer_contract_version must be at least 1");
  }
  validateBoundedText("source_event_key", input.source_event_key, 256);
  for (const [name, value] of [
    ["tenant_reference", input.tenant_reference],
    ["billing_account_reference", input.billing_account_reference],
    ["billing_principal_reference", input.billing_principal_reference],
  ] as const) {
    validateReference(name, value);
  }
  for (const [name, value] of [
    ["credential_reference", input.credential_reference],
    ["cost_center_reference", input.cost_center_reference],
    ["project_reference", input.project_reference],
    ["repository_reference", input.repository_reference],
    ["correlation_reference", input.correlation_reference],
    ["causation_reference", input.causation_reference],
  ] as const) {
    if (value !== undefined) {
      validateReference(name, value);
    }
  }
  if (input.trace_reference !== undefined) {
    validateBoundedText("trace_reference", input.trace_reference, 256);
  }
  validateCode("product_code", input.product_code, 64);
  if (input.operation_code !== undefined) {
    validateCode("operation_code", input.operation_code, 64);
  }
  validateDimensions(input.dimensions);
  canonicalTimestamp(input.occurred_at);
  if (input.available_at !== undefined) {
    canonicalTimestamp(input.available_at);
  }
  if (input.correction_lineage !== undefined) {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(input.correction_lineage.prior_event_id)) {
      throw new ProducerContractError("correction_lineage.prior_event_id must be a UUID");
    }
    if (!(["corrects", "reverses", "supersedes"] as const).includes(input.correction_lineage.relationship_code)) {
      throw new ProducerContractError("correction_lineage relationship_code is not in the published enum");
    }
    if (input.correction_lineage.reason_code !== undefined) {
      validateCode("correction_lineage reason_code", input.correction_lineage.reason_code, 64);
    }
  }
  if (!Array.isArray(input.measurements) || input.measurements.length < 1 || input.measurements.length > 64) {
    throw new ProducerContractError("measurements must contain between 1 and 64 objects");
  }
  for (const measurement of input.measurements) {
    if (measurement === null || typeof measurement !== "object") {
      throw new ProducerContractError("measurements must contain objects");
    }
    const keys = Object.keys(measurement);
    if (keys.some((key) => !["meter_code", "meter_version", "quantity", "unit_code", "quality_code"].includes(key))) {
      throw new ProducerContractError("measurements cannot contain arbitrary fields");
    }
    validateCode("meter_code", measurement.meter_code, 96);
    if (measurement.meter_version !== undefined && (!Number.isInteger(measurement.meter_version) || measurement.meter_version < 1)) {
      throw new ProducerContractError("meter_version must be at least 1");
    }
    validateQuantity(measurement.quantity);
    validateCode("unit_code", measurement.unit_code, 32);
    if (
      ![
        "provider_reported",
        "locally_measured",
        "deterministically_derived",
        "estimated",
        "reconstructed",
        "corrected",
      ].includes(measurement.quality_code)
    ) {
      throw new ProducerContractError("quality_code is not in the published enum");
    }
  }
}

function validateEvent(event: UsageEvent): void {
  validateInput(event);
  const allowedKeys = new Set([
    "event_id",
    "event_contract_version",
    "producer_contract_version",
    "source_event_key",
    "tenant_reference",
    "billing_account_reference",
    "billing_principal_reference",
    "credential_reference",
    "cost_center_reference",
    "project_reference",
    "repository_reference",
    "trace_reference",
    "correlation_reference",
    "causation_reference",
    "available_at",
    "correction_lineage",
    "product_code",
    "operation_code",
    "dimensions",
    "occurred_at",
    "measurements",
    "source_payload_hash",
  ]);
  if (Object.keys(event).some((key) => !allowedKeys.has(key))) {
    throw new ProducerContractError("usage event cannot contain arbitrary fields");
  }
  if (!/^sha256:[0-9a-f]{64}$/.test(event.source_payload_hash)) {
    throw new ProducerContractError("source_payload_hash must be a sha256 digest");
  }
}

function findReceipt(receipts: Array<Record<string, unknown>>, event: UsageEvent): Record<string, unknown> | undefined {
  const matches = receipts.filter((receipt) => receipt.source_event_key === event.source_event_key && receipt.tenant_reference === event.tenant_reference);
  return matches.length === 1 ? matches[0] : undefined;
}

function receiptMatches(receipt: Record<string, unknown>, event: UsageEvent): boolean {
  return receipt.tenant_reference === event.tenant_reference && receipt.source_payload_hash === event.source_payload_hash && receipt.event_contract_version === event.event_contract_version;
}

function validateDimensions(dimensions: Record<string, string> | undefined): void {
  if (dimensions === undefined) {
    return;
  }
  if (typeof dimensions !== "object" || dimensions === null || Array.isArray(dimensions)) {
    throw new ProducerContractError("dimensions must be an object");
  }
  const allowed = new Set([
    "provider_code",
    "model_code",
    "workflow_code",
    "role_code",
    "orchestration_mode_code",
    "backend_code",
    "document_job_reference",
    "shard_reference",
    "run_reference",
    "artifact_reference",
    "configuration_reference",
    "seed_reference",
  ]);
  const names = Object.keys(dimensions);
  if (names.length > 10 || names.some((name) => !allowed.has(name))) {
    throw new ProducerContractError("dimensions contain an unknown or excessive field");
  }
  for (const name of names) {
    const value = dimensions[name];
    if (name === "model_code") {
      if (typeof value !== "string" || value.length === 0 || value.length > 128 || !/^[A-Za-z0-9][A-Za-z0-9._:/-]*$/.test(value)) {
        throw new ProducerContractError("model_code must be a bounded provider model identifier");
      }
    } else if (name.endsWith("_reference")) {
      validateReference(name, value);
    } else {
      validateCode(name, value, 64);
    }
  }
}

function validateReference(name: string, value: string): void {
  if (typeof value !== "string" || value.length === 0 || !value.startsWith("urn:cwl:")) {
    throw new ProducerContractError(name + " must be a non-empty urn:cwl reference");
  }
}

function validateBoundedText(name: string, value: string, maximum: number): void {
  if (typeof value !== "string" || value.length === 0 || [...value].length > maximum) {
    throw new ProducerContractError(name + " must be between 1 and " + maximum + " characters");
  }
}

function validateCode(name: string, value: string, maximum: number): void {
  if (typeof value !== "string" || value.length < 2 || value.length > maximum || !/^[a-z][a-z0-9_]*$/.test(value)) {
    throw new ProducerContractError(name + " must be lower snake_case");
  }
}

function validateQuantity(quantity: string): void {
  if (
    typeof quantity !== "string" ||
    quantity.length === 0 ||
    quantity.length > 39 ||
    !/^(0|[1-9][0-9]*)(\.[0-9]+)?$/.test(quantity)
  ) {
    throw new ProducerContractError("quantity must be a non-negative exact decimal");
  }
}
