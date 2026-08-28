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
    for (const status of [408, 429]) {
      globalThis.fetch = async () => new Response(null, { status });
      await assert.rejects(
        () => httpUsageIngestionTransport("https://metering.invalid")([]),
        { name: "TransientDeliveryError" },
      );
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("HTTP transport rejects non-HTTP endpoints", () => {
  assert.throws(
    () => httpUsageIngestionTransport("file:///etc/passwd"),
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
