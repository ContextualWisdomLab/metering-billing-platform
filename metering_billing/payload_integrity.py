"""Source-payload hashing and contract-version integrity.

Idempotent delivery is identified by the producer ``source_event_key`` inside a
tenant.  The SHA-256 source-payload hash plus ``event_contract_version`` then
prove that a replay carries the same commercial fact.  A changed hash or
contract version for an existing key is a conflict, not an update.

The hash excludes envelope identifiers (``event_id``, ``source_event_key``),
``source_payload_hash`` (circular), and ``recorded_at`` (assigned at ingest).
Canonical JSON uses sorted keys and compact separators so two semantically
identical commercial facts produce one digest.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

SOURCE_PAYLOAD_FIELDS = (
    "event_contract_version",
    "tenant_reference",
    "billing_account_reference",
    "billing_principal_reference",
    "credential_reference",
    "cost_center_reference",
    "project_reference",
    "product_code",
    "operation_code",
    "occurred_at",
    "measurements",
)


def canonical_source_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    """Return the producer-controlled fields that participate in the digest."""
    return {field_name: event[field_name] for field_name in SOURCE_PAYLOAD_FIELDS if field_name in event}


def compute_source_payload_hash(event: Mapping[str, Any]) -> str:
    """Return the ``sha256:<hex>`` digest of the canonical source payload."""
    canonical_text = json.dumps(
        canonical_source_payload(event),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def source_payload_hash_errors(event: Mapping[str, Any]) -> tuple[str, ...]:
    """Return a diagnostic when the supplied hash does not match the payload."""
    expected_hash = compute_source_payload_hash(event)
    supplied_hash = event.get("source_payload_hash")
    if supplied_hash != expected_hash:
        return (f"source_payload_hash must equal {expected_hash}",)
    return ()
