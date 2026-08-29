"""Verify and minimally normalize Lemon Squeezy webhook requests."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Mapping


LEMON_SQUEEZY_WEBHOOK_CONTRACT_VERSION = 1
LEMON_SQUEEZY_PROVIDER_CODE = "lemon_squeezy"
MAXIMUM_WEBHOOK_BODY_BYTES = 1_048_576


class LemonSqueezyWebhookError(ValueError):
    """Raised when a webhook cannot be authenticated or safely normalized."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class LemonSqueezyWebhook:
    """Signature-verified provider reference without raw payload or PII."""

    event_name: str
    provider_object_type: str
    provider_object_reference: str

    def as_contract_dict(self) -> dict[str, object]:
        """Render only the normalized provider reference for async processing."""
        return {
            "lemon_squeezy_webhook_contract_version": LEMON_SQUEEZY_WEBHOOK_CONTRACT_VERSION,
            "provider_code": LEMON_SQUEEZY_PROVIDER_CODE,
            "event_name": self.event_name,
            "provider_object_type": self.provider_object_type,
            "provider_object_reference": self.provider_object_reference,
        }


def verify_lemon_squeezy_webhook(
    raw_body: bytes,
    signature_header: str,
    signing_secret: str,
) -> LemonSqueezyWebhook:
    """Verify raw bytes before parsing and return a PII-free event reference."""
    if not isinstance(raw_body, bytes) or not raw_body:
        raise LemonSqueezyWebhookError("body_invalid")
    if len(raw_body) > MAXIMUM_WEBHOOK_BODY_BYTES:
        raise LemonSqueezyWebhookError("body_too_large")
    if not isinstance(signing_secret, str) or not signing_secret:
        raise LemonSqueezyWebhookError("secret_invalid")
    if not isinstance(signature_header, str):
        raise LemonSqueezyWebhookError("signature_invalid")
    signature = signature_header.strip().lower()
    if len(signature) != hashlib.sha256().digest_size * 2 or any(
        character not in "0123456789abcdef" for character in signature
    ):
        raise LemonSqueezyWebhookError("signature_invalid")
    expected = hmac.new(
        signing_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise LemonSqueezyWebhookError("signature_invalid")
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LemonSqueezyWebhookError("payload_invalid") from error
    if not isinstance(payload, Mapping):
        raise LemonSqueezyWebhookError("payload_invalid")
    meta = payload.get("meta")
    if not isinstance(meta, Mapping):
        raise LemonSqueezyWebhookError("event_name_invalid")
    event_name = _reference(meta.get("event_name"), "event_name_invalid", 100)
    resource: Any = payload.get("data", payload)
    if not isinstance(resource, Mapping):
        raise LemonSqueezyWebhookError("resource_invalid")
    provider_object_type = _reference(resource.get("type"), "resource_type_invalid", 100)
    provider_object_reference = _reference(resource.get("id"), "resource_reference_invalid", 200)
    return LemonSqueezyWebhook(
        event_name,
        provider_object_type,
        provider_object_reference,
    )


def _reference(value: object, reason_code: str, maximum: int) -> str:
    """Require bounded provider references without returning provider payloads."""
    if not isinstance(value, str) or not 0 < len(value) <= maximum:
        raise LemonSqueezyWebhookError(reason_code)
    return value
