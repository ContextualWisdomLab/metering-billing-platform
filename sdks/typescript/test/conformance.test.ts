import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  buildUsageCloudEvent,
  buildUsageEvent,
  canonicalSourcePayloadJson,
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
