import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import {
  buildUsageCloudEvent,
  buildUsageEvent,
  canonicalSourcePayloadJson,
  FileUsageOutbox,
  httpUsageIngestionTransport,
  PermanentDeliveryError,
} from "../src/index.ts";

const fixture = JSON.parse(
  readFileSync(new URL("../../../schemas/examples/usage-event-v1-conformance.json", import.meta.url), "utf8"),
);

test("matches the Python conformance vector", () => {
  const { source_payload_hash: _sourcePayloadHash, ...input } = fixture.event;
  const event = buildUsageEvent(input);
  assert.deepEqual(event, fixture.event);
  assert.equal(canonicalSourcePayloadJson(event), fixture.canonical_source_payload_json);
  assert.equal(event.source_payload_hash, fixture.source_payload_hash);

  const cloudEvent = buildUsageCloudEvent(event, "urn:cwl:producer:reference-typescript");
  assert.equal(cloudEvent.specversion, "1.0");
  assert.equal(cloudEvent.id, event.event_id);
  assert.equal(cloudEvent.subject, event.source_event_key);
  assert.deepEqual(cloudEvent.data, event);
});

test("rejects tampered hashes and sensitive measurement fields", () => {
  const { source_payload_hash: _sourcePayloadHash, ...input } = fixture.event;
  const invalidHash = {
    ...buildUsageEvent(input),
    source_payload_hash: "sha256:" + "0".repeat(64),
  };
  assert.throws(
    () => buildUsageCloudEvent(invalidHash, "urn:cwl:producer:test"),
    { name: "ProducerContractError" },
  );
  assert.throws(
    () =>
      buildUsageEvent({
        ...input,
        measurements: [{ ...input.measurements[0], prompt: "do not persist" }],
      }),
    { name: "ProducerContractError" },
  );
  assert.throws(
    () =>
      buildUsageEvent({
        ...input,
        measurements: [{ ...input.measurements[0], quantity: "1e3" }],
      }),
    { name: "ProducerContractError" },
  );
  assert.throws(
    () =>
      buildUsageEvent({
        ...input,
        occurred_at: "2026-08-16T10:27:42.1234567Z",
      }),
      { name: "ProducerContractError" },
  );
  const unicode = buildUsageEvent({ ...input, source_event_key: "😀".repeat(256) });
  assert.equal([...unicode.source_event_key].length, 256);
  assert.throws(
    () => buildUsageEvent({ ...input, source_event_key: "😀".repeat(257) }),
    { name: "ProducerContractError" },
  );
});

test("accepts UUID variants allowed by the published UUID format", () => {
  const { source_payload_hash: _sourcePayloadHash, ...input } = fixture.event;
  const eventId = "019d7b92-1aa0-7a7f-01c1-962c0f4bf61c";
  const event = buildUsageEvent({
    ...input,
    event_id: eventId,
    correction_lineage: { ...input.correction_lineage, prior_event_id: eventId },
  });
  assert.equal(event.event_id, eventId);
});

test("rejects invalid contract edges and preserves optional fields", () => {
  const { source_payload_hash: _sourcePayloadHash, ...input } = fixture.event;
  const invalid = (overrides) => assert.throws(
    () => buildUsageEvent({ ...input, ...overrides }),
    { name: "ProducerContractError" },
  );
  invalid({ event_id: "not-a-uuid" });
  invalid({ event_id: null });
  invalid({ event_contract_version: 0 });
  invalid({ event_contract_version: 2 });
  invalid({ event_contract_version: 1.5 });
  invalid({ producer_contract_version: 0 });
  invalid({ producer_contract_version: 1.5 });
  invalid({ source_event_key: "" });
  invalid({ source_event_key: null });
  invalid({ source_event_key: "x".repeat(257) });
  invalid({ tenant_reference: "not-a-reference" });
  invalid({ tenant_reference: null });
  invalid({ tenant_reference: "" });
  invalid({ dimensions: [] });
  invalid({ dimensions: null });
  invalid({ dimensions: Object.fromEntries(Array.from({ length: 11 }, (_, index) => [`provider_code_${index}`, "openai"])) });
  invalid({ dimensions: { model_code: "not a model" } });
  invalid({ dimensions: { model_code: null } });
  invalid({ dimensions: { model_code: "" } });
  invalid({ dimensions: { model_code: "a".repeat(129) } });
  invalid({ correction_lineage: { ...input.correction_lineage, prior_event_id: "bad" } });
  invalid({ correction_lineage: { ...input.correction_lineage, relationship_code: "other" } });
  invalid({ correction_lineage: { ...input.correction_lineage, secret: "do not persist" } });
  invalid({ correction_lineage: null });
  invalid({ correction_lineage: [] });
  invalid({ correction_lineage: "not-an-object" });
  invalid({ measurements: [] });
  invalid({ measurements: null });
  invalid({ measurements: Array.from({ length: 65 }, () => ({ ...input.measurements[0] })) });
  invalid({ measurements: [null] });
  invalid({ measurements: [{ ...input.measurements[0], meter_version: 0 }] });
  invalid({ measurements: [{ ...input.measurements[0], meter_version: 1.5 }] });
  invalid({ measurements: [{ ...input.measurements[0], quality_code: "other" }] });
  invalid({ measurements: [{ ...input.measurements[0], quantity: "" }] });
  invalid({ measurements: [{ ...input.measurements[0], quantity: "1".repeat(40) }] });
  invalid({ measurements: [{ ...input.measurements[0], quantity: null }] });
  invalid({ occurred_at: "not-a-timestamp" });
  invalid({ occurred_at: "2026-99-99T00:00:00Z" });
  invalid({ occurred_at: "2026-02-30T00:00:00Z" });
  invalid({ occurred_at: "2023-02-29T00:00:00Z" });
  invalid({ occurred_at: "2026-00-01T00:00:00Z" });
  invalid({ occurred_at: "2026-01-00T00:00:00Z" });
  invalid({ occurred_at: "2026-01-01T24:00:00Z" });
  invalid({ occurred_at: "2026-01-01T00:60:00Z" });
  invalid({ occurred_at: "2026-01-01T00:00:60Z" });
  invalid({ occurred_at: "2026-01-01T00:00:00+24:00" });
  invalid({ occurred_at: "1900-02-29T00:00:00Z" });
  invalid({ product_code: "Not_lower_snake_case" });
  invalid({ product_code: null });
  invalid({ product_code: "" });
  invalid({ product_code: "a".repeat(65) });

  const event = buildUsageEvent({
    ...input,
    cost_center_reference: "urn:cwl:tenant_001:cost_center:01",
    dimensions: {
      model_code: "gpt-4o-mini",
      provider_code: "openai",
      workflow_code: "verified_workflow",
      role_code: "assistant",
      orchestration_mode_code: "sync",
      backend_code: "remote",
      document_job_reference: "urn:cwl:tenant_001:job:01",
    },
    correction_lineage: { ...input.correction_lineage, reason_code: "corrected_event" },
  });
  assert.equal(event.cost_center_reference, "urn:cwl:tenant_001:cost_center:01");
  assert.equal(event.dimensions.backend_code, "remote");
  assert.equal(event.correction_lineage.reason_code, "corrected_event");
  const noReasonEvent = buildUsageEvent({
    ...input,
    correction_lineage: {
      prior_event_id: input.correction_lineage.prior_event_id,
      relationship_code: "corrects",
    },
  });
  assert.equal(noReasonEvent.correction_lineage.reason_code, undefined);
  assert.equal(
    buildUsageEvent({ ...input, occurred_at: "2024-02-29T00:00:00Z" }).occurred_at,
    "2024-02-29T00:00:00Z",
  );
  const offsetEvent = buildUsageEvent({ ...input, occurred_at: "2024-02-29T01:02:03+01:00" });
  assert.match(canonicalSourcePayloadJson(offsetEvent), /"occurred_at":"2024-02-29T00:02:03Z"/);
  assert.equal(
    buildUsageEvent({ ...input, occurred_at: "2000-02-29T00:00:00Z" }).occurred_at,
    "2000-02-29T00:00:00Z",
  );
  assert.throws(() => buildUsageCloudEvent(event, ""), { name: "ProducerContractError" });
  assert.throws(() => buildUsageCloudEvent(event, null), { name: "ProducerContractError" });
  assert.throws(() => buildUsageCloudEvent({ ...event, prompt: "do not persist" }, "urn:cwl:test"), { name: "ProducerContractError" });
  assert.throws(() => buildUsageCloudEvent({ ...event, source_payload_hash: "bad" }, "urn:cwl:test"), { name: "ProducerContractError" });

  const minimalInput = { ...input };
  for (const field of [
    "credential_reference",
    "cost_center_reference",
    "project_reference",
    "repository_reference",
    "trace_reference",
    "correlation_reference",
    "causation_reference",
    "available_at",
    "correction_lineage",
    "dimensions",
    "operation_code",
  ]) delete minimalInput[field];
  minimalInput.occurred_at = "2026-08-16T10:27:42Z";
  minimalInput.measurements = [{ ...input.measurements[0], meter_version: undefined, quantity: "42" }];
  assert.equal(buildUsageEvent(minimalInput).occurred_at, minimalInput.occurred_at);
});

test("matches Python for allowlisted provider dimensions", () => {
  const { source_payload_hash: _sourcePayloadHash, ...input } = fixture.event;
  const event = buildUsageEvent({
    ...input,
    dimensions: {
      model_code: "gpt-4o-mini",
      provider_code: "openai",
      workflow_code: "verified_workflow",
    },
  });
  assert.equal(
    event.source_payload_hash,
    "sha256:601172eebd1e5f5d840706bcf1b5833203d4b802898459c00176fd4600ebed35",
  );
  assert.throws(
    () => buildUsageEvent({ ...input, dimensions: { prompt: "must-not-persist" } }),
    { name: "ProducerContractError" },
  );
});

test("HTTP transport retries timeout and rate-limit responses", async () => {
  const originalFetch = globalThis.fetch;
  try {
    for (const status of [408, 429, 500]) {
      globalThis.fetch = async () => new Response(null, { status });
      await assert.rejects(
        () => httpUsageIngestionTransport("https://metering.invalid")([]),
        { name: "TransientDeliveryError" },
      );
    }
    globalThis.fetch = async () => new Response(null, { status: 400 });
    await assert.rejects(
      () => httpUsageIngestionTransport("https://metering.invalid")([]),
      { name: "PermanentDeliveryError" },
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("HTTP transport classifies malformed successful responses", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response("not-json", { status: 200 });
    await assert.rejects(
      () => httpUsageIngestionTransport("https://metering.invalid")([]),
      { name: "TransientDeliveryError" },
    );
    globalThis.fetch = async () => new Response(JSON.stringify({ nope: true }), { status: 200 });
    await assert.rejects(
      () => httpUsageIngestionTransport("https://metering.invalid")([]),
      { name: "TransientDeliveryError" },
    );
    globalThis.fetch = async () => new Response(null, { status: 200 });
    await assert.rejects(
      () => httpUsageIngestionTransport("https://metering.invalid")([]),
      { name: "TransientDeliveryError" },
    );
    globalThis.fetch = async () => new Response("text", { status: 200 });
    await assert.rejects(
      () => httpUsageIngestionTransport("https://metering.invalid")([]),
      { name: "TransientDeliveryError" },
    );
    globalThis.fetch = async () => new Response("42", { status: 200 });
    await assert.rejects(
      () => httpUsageIngestionTransport("https://metering.invalid")([]),
      { name: "TransientDeliveryError" },
    );
    const body = { event_receipts: [] };
    globalThis.fetch = async () => new Response(JSON.stringify(body), { status: 422 });
    assert.deepEqual(await httpUsageIngestionTransport("https://metering.invalid")([]), body);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("HTTP transport fails closed on redirects", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async (_input, init) => {
      assert.equal(init.redirect, "error");
      return new Response(JSON.stringify({ event_receipts: [] }), { status: 200 });
    };
    assert.deepEqual(await httpUsageIngestionTransport("https://metering.invalid")([]), {
      event_receipts: [],
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("HTTP transport timeout covers response body consumption", async () => {
  const originalFetch = globalThis.fetch;
  let aborted = false;
  let fallbackTimer;
  try {
    globalThis.fetch = async (_input, init) => {
      const body = new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode('{"event_receipts":['));
          init.signal.addEventListener("abort", () => {
            aborted = true;
            clearTimeout(fallbackTimer);
            controller.error(new Error("body timeout"));
          }, { once: true });
          fallbackTimer = setTimeout(() => controller.error(new Error("test timeout")), 100);
        },
      });
      return new Response(body, { status: 200, headers: { "content-type": "application/json" } });
    };
    await assert.rejects(
      () => httpUsageIngestionTransport("https://metering.invalid", {}, 10)([]),
      { name: "TransientDeliveryError" },
    );
    assert.equal(aborted, true);
  } finally {
    clearTimeout(fallbackTimer);
    globalThis.fetch = originalFetch;
  }
});

test("HTTP transport turns an abort into a transient delivery error", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async (_input, init) => await new Promise((_resolve, reject) => {
      init.signal.addEventListener("abort", () => reject(new Error("timeout")), { once: true });
    });
    await assert.rejects(
      () => httpUsageIngestionTransport("https://metering.invalid", {}, 1)([]),
      { name: "TransientDeliveryError" },
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("HTTP transport rejects non-HTTP endpoints", () => {
  assert.throws(
    () => httpUsageIngestionTransport("not a URL"),
    { name: "ProducerContractError" },
  );
  assert.throws(
    () => httpUsageIngestionTransport("file:///etc/passwd"),
    { name: "ProducerContractError" },
  );
  assert.throws(
    () => httpUsageIngestionTransport("http://metering.invalid"),
    { name: "ProducerContractError" },
  );
  assert.throws(
    () => httpUsageIngestionTransport("https://"),
    { name: "ProducerContractError" },
  );
});

test("durable outbox keeps failed events and accepts a matched replay receipt", async () => {
  const { source_payload_hash: _sourcePayloadHash, ...input } = fixture.event;
  const event = buildUsageEvent(input);
  const directory = mkdtempSync(join(tmpdir(), "cwl-outbox-"));
  try {
    const outbox = new FileUsageOutbox(join(directory, "outbox.json"));
    outbox.enqueue(event);
    outbox.enqueue(Object.fromEntries(Object.entries(event).reverse()) as typeof event);
    assert.equal(outbox.pendingCount(), 1);
    let calls = 0;
    const result = await outbox.flush(async (events) => {
      calls += 1;
      if (calls === 1) throw new Error("offline");
      return {
        event_receipts: [{
          source_event_key: events[0].source_event_key,
          tenant_reference: events[0].tenant_reference,
          event_contract_version: events[0].event_contract_version,
          source_payload_hash: events[0].source_payload_hash,
          ingestion_outcome_code: "duplicate_replay",
        }],
      };
    }, 1, 3);
    assert.equal(result.retriedCount, 1);
    assert.equal(outbox.pendingCount(), 1);
    const replay = await outbox.flush(async (events) => ({ event_receipts: [{
      source_event_key: events[0].source_event_key,
      tenant_reference: events[0].tenant_reference,
      event_contract_version: events[0].event_contract_version,
      source_payload_hash: events[0].source_payload_hash,
      ingestion_outcome_code: "duplicate_replay",
    }] }));
    assert.equal(replay.duplicateReplayCount, 1);
    assert.equal(outbox.pendingCount(), 0);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("durable outbox applies partial receipts per event", async () => {
  const { source_payload_hash: _sourcePayloadHash, ...input } = fixture.event;
  const first = buildUsageEvent(input);
  const second = buildUsageEvent({
    ...input,
    event_id: "019d7b92-1aa0-7a7f-b61c-962c0f4bf6ad",
    source_event_key: "producer-reference:workflow-381:step-05",
  });
  const directory = mkdtempSync(join(tmpdir(), "cwl-outbox-"));
  try {
    const outbox = new FileUsageOutbox(join(directory, "outbox.json"));
    outbox.enqueue(first);
    outbox.enqueue(second);
    const result = await outbox.flush(async (events) => ({ event_receipts: [{
      source_event_key: events[0].source_event_key,
      tenant_reference: events[0].tenant_reference,
      event_contract_version: events[0].event_contract_version,
      source_payload_hash: events[0].source_payload_hash,
      ingestion_outcome_code: "accepted",
    }] }), 2, 3);
    assert.equal(result.acceptedCount, 1);
    assert.equal(result.retriedCount, 1);
    assert.equal(outbox.pendingCount(), 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("durable outbox requires a tenant-matched receipt", async () => {
  const { source_payload_hash: _sourcePayloadHash, ...input } = fixture.event;
  const event = buildUsageEvent(input);
  const directory = mkdtempSync(join(tmpdir(), "cwl-outbox-"));
  try {
    const outbox = new FileUsageOutbox(join(directory, "outbox.json"));
    outbox.enqueue(event);
    const result = await outbox.flush(async (events) => ({ event_receipts: [{
      source_event_key: events[0].source_event_key,
      event_contract_version: events[0].event_contract_version,
      source_payload_hash: events[0].source_payload_hash,
      ingestion_outcome_code: "accepted",
    }] }));
    assert.equal(result.acceptedCount, 0);
    assert.equal(result.retriedCount, 1);
    assert.equal(outbox.pendingCount(), 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("durable outbox requires full receipt binding for rejection", async () => {
  const { source_payload_hash: _sourcePayloadHash, ...input } = fixture.event;
  const event = buildUsageEvent(input);
  const directory = mkdtempSync(join(tmpdir(), "cwl-outbox-"));
  try {
    const outbox = new FileUsageOutbox(join(directory, "outbox.json"));
    outbox.enqueue(event);
    const result = await outbox.flush(async (events) => ({ event_receipts: [{
      source_event_key: events[0].source_event_key,
      tenant_reference: events[0].tenant_reference,
      event_contract_version: events[0].event_contract_version + 1,
      source_payload_hash: events[0].source_payload_hash,
      ingestion_outcome_code: "rejected",
      rejection_reason_code: "meter_not_found",
    }] }));
    assert.equal(result.rejectedCount, 0);
    assert.equal(result.retriedCount, 1);
    assert.equal(outbox.pendingCount(), 1);
    assert.equal(outbox.deadLetterCount(), 0);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("durable outbox preserves events enqueued during an in-flight flush", async () => {
  const { source_payload_hash: _sourcePayloadHash, ...input } = fixture.event;
  const first = buildUsageEvent(input);
  const second = buildUsageEvent({
    ...input,
    event_id: "019d7b92-1aa0-7a7f-b61c-962c0f4bf6ad",
    source_event_key: "producer-reference:workflow-381:step-05",
  });
  const directory = mkdtempSync(join(tmpdir(), "cwl-outbox-"));
  try {
    const outbox = new FileUsageOutbox(join(directory, "outbox.json"));
    outbox.enqueue(first);
    let release!: () => void;
    let started!: () => void;
    const senderStarted = new Promise<void>((resolve) => {
      started = resolve;
    });
    const firstFlush = outbox.flush(async (events) => {
      started();
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      return { event_receipts: [{
        source_event_key: events[0].source_event_key,
        tenant_reference: events[0].tenant_reference,
        event_contract_version: events[0].event_contract_version,
        source_payload_hash: events[0].source_payload_hash,
        ingestion_outcome_code: "accepted",
      }] };
    }, 1);
    await senderStarted;
    outbox.enqueue(second);
    release();
    const firstResult = await firstFlush;
    assert.equal(firstResult.acceptedCount, 1);
    assert.equal(outbox.pendingCount(), 1);
    const secondResult = await outbox.flush(async (events) => ({ event_receipts: [{
      source_event_key: events[0].source_event_key,
      tenant_reference: events[0].tenant_reference,
      event_contract_version: events[0].event_contract_version,
      source_payload_hash: events[0].source_payload_hash,
      ingestion_outcome_code: "accepted",
    }] }));
    assert.equal(secondResult.acceptedCount, 1);
    assert.equal(outbox.pendingCount(), 0);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("durable outbox tolerates an event acknowledged by another writer", async () => {
  const { source_payload_hash: _sourcePayloadHash, ...input } = fixture.event;
  const event = buildUsageEvent(input);
  const directory = mkdtempSync(join(tmpdir(), "cwl-outbox-"));
  try {
    const outbox = new FileUsageOutbox(join(directory, "outbox.json"));
    const otherWriter = new FileUsageOutbox(join(directory, "outbox.json"));
    outbox.enqueue(event);
    const result = await outbox.flush(async (events) => {
      const otherResult = await otherWriter.flush(async (otherEvents) => ({ event_receipts: [{
        source_event_key: otherEvents[0].source_event_key,
        tenant_reference: otherEvents[0].tenant_reference,
        event_contract_version: otherEvents[0].event_contract_version,
        source_payload_hash: otherEvents[0].source_payload_hash,
        ingestion_outcome_code: "accepted",
      }] }));
      assert.equal(otherResult.acceptedCount, 1);
      return { event_receipts: [{
        source_event_key: events[0].source_event_key,
        tenant_reference: events[0].tenant_reference,
        event_contract_version: events[0].event_contract_version,
        source_payload_hash: events[0].source_payload_hash,
        ingestion_outcome_code: "accepted",
      }] };
    });
    assert.equal(result.attemptedCount, 1);
    assert.equal(result.acceptedCount, 0);
    assert.equal(outbox.pendingCount(), 0);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("durable outbox dead-letters matched rejection and handles empty flush", async () => {
  const { source_payload_hash: _sourcePayloadHash, ...input } = fixture.event;
  const event = buildUsageEvent(input);
  const directory = mkdtempSync(join(tmpdir(), "cwl-outbox-"));
  try {
    const outbox = new FileUsageOutbox(join(directory, "outbox.json"));
    outbox.enqueue(event);
    outbox.enqueue({ ...event });
    outbox.enqueue({ ...event, cost_center_reference: undefined });
    const differentEvent = buildUsageEvent({
      ...input,
      event_id: event.event_id,
      source_event_key: "producer-reference:workflow-381:other-step",
    });
    assert.throws(() => outbox.enqueue(differentEvent), { name: "ProducerContractError" });
    await assert.rejects(() => outbox.flush(async () => null, 0), { name: "RangeError" });
    await assert.rejects(() => outbox.flush(async () => null, 1, 0), { name: "RangeError" });
    const invalidResponse = await outbox.flush(async () => null);
    assert.equal(invalidResponse.retriedCount, 1);
    const invalidObjectResponse = new FileUsageOutbox(join(directory, "invalid-response.json"));
    invalidObjectResponse.enqueue(event);
    const invalidObjectResult = await invalidObjectResponse.flush(async () => ({}));
    assert.equal(invalidObjectResult.retriedCount, 1);
    const permanentOutbox = new FileUsageOutbox(join(directory, "permanent.json"));
    permanentOutbox.enqueue(event);
    const permanentResult = await permanentOutbox.flush(async () => {
      throw new PermanentDeliveryError("bad request");
    });
    assert.equal(permanentResult.deadLetteredCount, 1);
    const exhaustedOutbox = new FileUsageOutbox(join(directory, "exhausted.json"));
    exhaustedOutbox.enqueue(event);
    const exhaustedResult = await exhaustedOutbox.flush(async () => {
      throw new Error("offline");
    }, 1, 1);
    assert.equal(exhaustedResult.deadLetteredCount, 1);
    const rejected = await outbox.flush(async (events) => ({ event_receipts: [{
      source_event_key: events[0].source_event_key,
      tenant_reference: events[0].tenant_reference,
      event_contract_version: events[0].event_contract_version,
      source_payload_hash: events[0].source_payload_hash,
      ingestion_outcome_code: "rejected",
      rejection_reason_code: "meter_not_found",
    }] }));
    assert.equal(rejected.rejectedCount, 1);
    assert.equal(rejected.deadLetteredCount, 1);
    assert.equal(outbox.deadLetterCount(), 1);
    outbox.replayDeadLetter(event.event_id);
    assert.equal(outbox.pendingCount(), 1);
    assert.throws(() => outbox.replayDeadLetter("missing"), { message: "unknown dead letter: missing" });
    assert.throws(() => outbox.replayDeadLetter(event.event_id), { message: `unknown dead letter: ${event.event_id}` });
    const accepted = await outbox.flush(async (events) => ({ event_receipts: [{
      source_event_key: events[0].source_event_key,
      tenant_reference: events[0].tenant_reference,
      event_contract_version: events[0].event_contract_version,
      source_payload_hash: events[0].source_payload_hash,
      ingestion_outcome_code: "accepted",
    }] }));
    assert.equal(accepted.acceptedCount, 1);
    assert.equal((await outbox.flush(async () => ({ event_receipts: [] }))).attemptedCount, 0);

    const staleAccepted = new FileUsageOutbox(join(directory, "stale-accepted.json"));
    staleAccepted.enqueue(event);
    const staleResult = await staleAccepted.flush(async (events) => ({ event_receipts: [{
      source_event_key: events[0].source_event_key,
      tenant_reference: events[0].tenant_reference,
      ingestion_outcome_code: "accepted",
    }] }));
    assert.equal(staleResult.retriedCount, 1);

    const unknownReceipt = new FileUsageOutbox(join(directory, "unknown-receipt.json"));
    unknownReceipt.enqueue(event);
    const unknownResult = await unknownReceipt.flush(async (events) => ({ event_receipts: [{
      source_event_key: "other-event",
      tenant_reference: events[0].tenant_reference,
      ingestion_outcome_code: "accepted",
    }] }));
    assert.equal(unknownResult.retriedCount, 1);

    const rejectedWithoutReason = new FileUsageOutbox(join(directory, "rejected-without-reason.json"));
    rejectedWithoutReason.enqueue(event);
    const rejectedResult = await rejectedWithoutReason.flush(async (events) => ({ event_receipts: [{
      source_event_key: events[0].source_event_key,
      tenant_reference: events[0].tenant_reference,
      event_contract_version: events[0].event_contract_version,
      source_payload_hash: events[0].source_payload_hash,
      ingestion_outcome_code: "rejected",
    }] }));
    assert.equal(rejectedResult.deadLetteredCount, 1);

    const duplicateReceipts = new FileUsageOutbox(join(directory, "duplicate-receipts.json"));
    duplicateReceipts.enqueue(event);
    const duplicateResult = await duplicateReceipts.flush(async (events) => ({ event_receipts: [
      {
        source_event_key: events[0].source_event_key,
        tenant_reference: events[0].tenant_reference,
        ingestion_outcome_code: "accepted",
      },
      {
        source_event_key: events[0].source_event_key,
        tenant_reference: events[0].tenant_reference,
        ingestion_outcome_code: "accepted",
      },
    ] }));
    assert.equal(duplicateResult.retriedCount, 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
