import { createHash } from "node:crypto";

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
  quantity: string;
  unit_code: string;
  quality_code: string;
}

export interface UsageEventInput {
  event_id: string;
  event_contract_version: number;
  source_event_key: string;
  tenant_reference: string;
  billing_account_reference: string;
  billing_principal_reference: string;
  credential_reference?: string;
  cost_center_reference?: string;
  project_reference?: string;
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

export function buildUsageEvent(input: UsageEventInput): UsageEvent {
  validateInput(input);
  const event: UsageEvent = {
    event_id: input.event_id,
    event_contract_version: input.event_contract_version,
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
  const payload: Record<string, unknown> = {
    billing_account_reference: event.billing_account_reference,
    billing_principal_reference: event.billing_principal_reference,
  };
  if (event.cost_center_reference !== undefined) {
    payload.cost_center_reference = event.cost_center_reference;
  }
  if (event.credential_reference !== undefined) {
    payload.credential_reference = event.credential_reference;
  }
  if (event.dimensions !== undefined) {
    payload.dimensions = Object.fromEntries(
      Object.entries(event.dimensions).sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0)),
    );
  }
  payload.event_contract_version = event.event_contract_version;
  payload.measurements = event.measurements.map((measurement) => ({
    meter_code: measurement.meter_code,
    quality_code: measurement.quality_code,
    quantity: canonicalQuantity(measurement.quantity),
    unit_code: measurement.unit_code,
  }));
  payload.occurred_at = canonicalTimestamp(event.occurred_at);
  if (event.operation_code !== undefined) {
    payload.operation_code = event.operation_code;
  }
  payload.product_code = event.product_code;
  if (event.project_reference !== undefined) {
    payload.project_reference = event.project_reference;
  }
  payload.tenant_reference = event.tenant_reference;
  return JSON.stringify(payload);
}

function computeSourcePayloadHash(event: UsageEvent): string {
  return "sha256:" + createHash("sha256").update(canonicalSourcePayloadJson(event), "utf8").digest("hex");
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
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(input.event_id)) {
    throw new ProducerContractError("event_id must be a UUID");
  }
  if (!Number.isInteger(input.event_contract_version) || input.event_contract_version < 1) {
    throw new ProducerContractError("event_contract_version must be at least 1");
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
  ] as const) {
    if (value !== undefined) {
      validateReference(name, value);
    }
  }
  validateCode("product_code", input.product_code, 64);
  if (input.operation_code !== undefined) {
    validateCode("operation_code", input.operation_code, 64);
  }
  validateDimensions(input.dimensions);
  canonicalTimestamp(input.occurred_at);
  if (!Array.isArray(input.measurements) || input.measurements.length < 1 || input.measurements.length > 64) {
    throw new ProducerContractError("measurements must contain between 1 and 64 objects");
  }
  for (const measurement of input.measurements) {
    if (measurement === null || typeof measurement !== "object") {
      throw new ProducerContractError("measurements must contain objects");
    }
    const keys = Object.keys(measurement);
    if (keys.some((key) => !["meter_code", "quantity", "unit_code", "quality_code"].includes(key))) {
      throw new ProducerContractError("measurements cannot contain arbitrary fields");
    }
    validateCode("meter_code", measurement.meter_code, 96);
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
    "source_event_key",
    "tenant_reference",
    "billing_account_reference",
    "billing_principal_reference",
    "credential_reference",
    "cost_center_reference",
    "project_reference",
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
  if (typeof value !== "string" || value.length === 0 || value.length > maximum) {
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
