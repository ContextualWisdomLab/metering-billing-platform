"""Append-only webhook subscriptions, outbox events, and explicit deliveries.

The buyer-facing path is:

1. Register an https callback for a closed event-type set.
2. Accept a commercial fact (journal proposal, payment receipt, credit, issued invoice, issued-invoice void, issued credit note, credit-note application, collection-case settlement, collection write-off, leftover apply, leftover refund, collection-dispute hold, or collection-dispute release).
3. Run ``deliver_due_events`` so active subscriptions receive a signed POST.

Accepted facts include collection-dispute releases (``dispute.released``).
AIS may keep polling ``GET /v1/journal-proposals``.  This slice does not
require AIS to subscribe, does not flip ``proposal_status``, and does not
call AIS posting-receipt (Fielding et al., 2022; Krawczyk et al., 1997).
HTTP Message Signatures (RFC 9421) are not used; the signature is HMAC-SHA256
over the raw JSON body in ``X-CWL-Webhook-Signature`` (Backman et al., 2024).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import UUID

from metering_billing.errors import (
    WebhookDeliveryOutcomeCode,
    WebhookDeliveryRejectionReasonCode,
    WebhookSubscriptionOutcomeCode,
    WebhookSubscriptionQueryError,
    WebhookSubscriptionRejectionReasonCode,
)
from metering_billing.tenant_api_credential import (
    DEFAULT_CREDENTIAL_PEPPER,
    hash_api_credential_secret,
)
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredWebhookDeliveryAttempt,
    StoredWebhookOutboxEvent,
    StoredWebhookSubscription,
    generate_record_id,
)


Clock = Callable[[], datetime]
WebhookTransport = Callable[[str, bytes, Mapping[str, str]], tuple[int | None, str | None]]

WEBHOOK_SUBSCRIPTION_CONTRACT_VERSION = 1
WEBHOOK_DELIVERY_CONTRACT_VERSION = 1
WEBHOOK_SECRET_TOKEN = "cwlwh_"
WEBHOOK_SECRET_PREFIX_LENGTH = 12
WEBHOOK_SIGNATURE_HEADER = "X-CWL-Webhook-Signature"
EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED = "journal_proposal.validated"
EVENT_TYPE_PAYMENT_RECEIPT_APPLIED = "payment_receipt.applied"
EVENT_TYPE_CREDIT_ADJUSTMENT_RECORDED = "credit_adjustment.recorded"
EVENT_TYPE_INVOICE_ISSUED = "invoice.issued"
EVENT_TYPE_INVOICE_VOIDED = "invoice.voided"
EVENT_TYPE_CREDIT_NOTE_ISSUED = "credit_note.issued"
EVENT_TYPE_CREDIT_NOTE_APPLIED = "credit_note.applied"
EVENT_TYPE_COLLECTION_SETTLED = "collection.settled"
EVENT_TYPE_WRITE_OFF_RECORDED = "write_off.recorded"
EVENT_TYPE_UNAPPLIED_CASH_APPLIED = "unapplied_cash.applied"
EVENT_TYPE_REFUND_RECORDED = "refund.recorded"
EVENT_TYPE_DISPUTE_HELD = "dispute.held"
EVENT_TYPE_DISPUTE_RELEASED = "dispute.released"
KNOWN_EVENT_TYPE_CODES = frozenset(
    {
        EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
        EVENT_TYPE_PAYMENT_RECEIPT_APPLIED,
        EVENT_TYPE_CREDIT_ADJUSTMENT_RECORDED,
        EVENT_TYPE_INVOICE_ISSUED,
        EVENT_TYPE_INVOICE_VOIDED,
        EVENT_TYPE_CREDIT_NOTE_ISSUED,
        EVENT_TYPE_CREDIT_NOTE_APPLIED,
        EVENT_TYPE_COLLECTION_SETTLED,
        EVENT_TYPE_WRITE_OFF_RECORDED,
        EVENT_TYPE_UNAPPLIED_CASH_APPLIED,
        EVENT_TYPE_REFUND_RECORDED,
        EVENT_TYPE_DISPUTE_HELD,
        EVENT_TYPE_DISPUTE_RELEASED,
    }
)
LOCAL_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "api_credential_secret",
        "webhook_secret",
        "credential_secret_hash",
        "webhook_secret_hash",
        "card_pan",
        "primary_account_number",
    }
)


def mint_webhook_secret() -> tuple[str, str]:
    """Return ``(webhook_secret_prefix, webhook_secret)`` for one register."""
    secret = f"{WEBHOOK_SECRET_TOKEN}{secrets.token_urlsafe(32)}"
    return secret[:WEBHOOK_SECRET_PREFIX_LENGTH], secret


def hash_webhook_secret(secret: str, pepper: str) -> str:
    """Return ``hmac-sha256:<hex>`` for one minted webhook secret."""
    return hash_api_credential_secret(secret, pepper)


def sign_webhook_body(secret: str, raw_body: bytes) -> str:
    """Return ``sha256=<hex>`` HMAC-SHA256 over the raw POST body."""
    if not isinstance(secret, str) or not secret:
        raise ValueError("webhook secret must be a non-empty string")
    if not isinstance(raw_body, (bytes, bytearray)):
        raise ValueError("webhook body must be bytes")
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def callback_url_is_allowed(callback_url: str) -> bool:
    """Return whether *callback_url* is https, or http on a local test host."""
    if not isinstance(callback_url, str) or not callback_url:
        return False
    parsed = urlparse(callback_url)
    if parsed.scheme == "https" and parsed.netloc:
        return True
    if parsed.scheme == "http" and parsed.hostname in LOCAL_HTTP_HOSTS:
        return True
    return False


def canonical_event_type_set(event_type_codes: object) -> tuple[str, ...]:
    """Return a sorted unique closed event-type set, or fail closed."""
    if isinstance(event_type_codes, str) or not isinstance(event_type_codes, Sequence):
        raise WebhookSubscriptionQueryError("webhook_event_type_unknown")
    normalized: list[str] = []
    for event_type_code in event_type_codes:
        if not isinstance(event_type_code, str) or event_type_code not in KNOWN_EVENT_TYPE_CODES:
            raise WebhookSubscriptionQueryError("webhook_event_type_unknown")
        if event_type_code not in normalized:
            normalized.append(event_type_code)
    if not normalized:
        raise WebhookSubscriptionQueryError("webhook_event_type_unknown")
    return tuple(sorted(normalized))


def event_type_set_text(event_type_codes: tuple[str, ...]) -> str:
    """Return the stored identity text for one canonical event-type set."""
    return ",".join(event_type_codes)


def post_signed_webhook(
    callback_url: str, raw_body: bytes, headers: Mapping[str, str]
) -> tuple[int | None, str | None]:
    """POST *raw_body* to *callback_url* and return ``(http_status, failure)``."""
    request = Request(callback_url, data=raw_body, method="POST")
    for header_name, header_value in headers.items():
        request.add_header(header_name, header_value)
    try:
        with urlopen(request, timeout=5) as response:
            return int(response.getcode()), None
    except HTTPError as error:
        return int(error.code), "webhook_http_error"
    except (URLError, TimeoutError, OSError):
        return None, "webhook_transport_failure"


def enqueue_accepted_fact(
    ledger: MemoryUsageLedger,
    tenant_reference: str,
    event_type_code: str,
    source_id: UUID,
    contract_dict: Mapping[str, object],
    occurred_at: datetime,
) -> StoredWebhookOutboxEvent | None:
    """Append one outbox event for an accepted commercial fact.

    Replay of the same tenant, event type, source, and payload hash returns
    the stored row and does not grow the outbox.  API secrets and PANs are
    refused.  Missing tenants and unknown event types write zero rows.
    """
    if event_type_code not in KNOWN_EVENT_TYPE_CODES:
        return None
    tenant, tenant_error = ledger.resolve_tenant(tenant_reference)
    if tenant_error is not None or tenant is None:
        return None
    _reject_forbidden_payload(contract_dict)
    envelope = {
        "event_type_code": event_type_code,
        "occurred_at": _format_instant(occurred_at),
        "tenant_reference": tenant.tenant_reference,
        "data": dict(contract_dict),
    }
    payload_json = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload_hash = f"sha256:{hashlib.sha256(payload_json.encode('utf-8')).hexdigest()}"
    existing = ledger.find_webhook_outbox_event(
        tenant.tenant_account_id, event_type_code, source_id, payload_hash
    )
    if existing is not None:
        return existing
    return ledger.insert_webhook_outbox_event(
        StoredWebhookOutboxEvent(
            outbox_event_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            event_type_code=event_type_code,
            payload_hash=payload_hash,
            source_id=source_id,
            occurred_at=occurred_at,
            delivery_status="pending",
            payload_json=payload_json,
            enqueued_at=occurred_at,
        )
    )


@dataclass(frozen=True)
class WebhookSubscriptionResult:
    """Buyer-facing result of registering, listing, or revoking one subscription."""

    webhook_subscription_outcome_code: WebhookSubscriptionOutcomeCode
    webhook_subscription_contract_version: int
    webhook_subscription_id: UUID | None
    tenant_reference: str | None
    callback_url: str | None
    event_type_codes: tuple[str, ...]
    webhook_secret_prefix: str | None
    webhook_secret: str | None
    subscription_status: str | None
    issued_at: datetime | None
    rejection_reason_code: WebhookSubscriptionRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the register contract, including the secret only when minted."""
        outcome = self.webhook_subscription_outcome_code
        outcome_text = (
            outcome.value if isinstance(outcome, WebhookSubscriptionOutcomeCode) else str(outcome)
        )
        payload: dict[str, object] = {
            "webhook_subscription_contract_version": self.webhook_subscription_contract_version,
            "webhook_subscription_outcome_code": outcome_text,
        }
        if outcome_text == WebhookSubscriptionOutcomeCode.REJECTED:
            payload["rejection_reason_code"] = (
                self.rejection_reason_code.value
                if self.rejection_reason_code is not None
                else "tenant_not_found"
            )
            return payload
        if (
            outcome_text != WebhookSubscriptionOutcomeCode.ACCEPTED
            and outcome_text != WebhookSubscriptionOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported webhook subscription outcome: {outcome_text}")
        payload["webhook_subscription_id"] = str(self.webhook_subscription_id)
        payload["tenant_reference"] = self.tenant_reference
        payload["callback_url"] = self.callback_url
        payload["event_type_codes"] = list(self.event_type_codes)
        payload["webhook_secret_prefix"] = self.webhook_secret_prefix
        payload["subscription_status"] = self.subscription_status
        payload["issued_at"] = _format_instant(self.issued_at)
        if self.webhook_secret is not None:
            payload["webhook_secret"] = self.webhook_secret
        return payload

    def as_metadata_dict(self) -> dict[str, object]:
        """Return list/revoke metadata.  Never includes the secret or hash."""
        return {
            "webhook_subscription_id": str(self.webhook_subscription_id),
            "callback_url": self.callback_url,
            "event_type_codes": list(self.event_type_codes),
            "webhook_secret_prefix": self.webhook_secret_prefix,
            "subscription_status": self.subscription_status,
            "issued_at": _format_instant(self.issued_at),
        }


@dataclass(frozen=True)
class WebhookSubscriptionPage:
    """One tenant-scoped list of webhook subscription metadata rows."""

    webhook_subscriptions: tuple[WebhookSubscriptionResult, ...]

    def as_contract_dict(self) -> dict[str, object]:
        """Return metadata only.  Secrets and hashes are omitted."""
        return {
            "webhook_subscriptions": [
                item.as_metadata_dict() for item in self.webhook_subscriptions
            ]
        }


@dataclass(frozen=True)
class WebhookDeliveryResult:
    """Buyer-facing result of one explicit ``deliver_due_events`` run."""

    webhook_delivery_outcome_code: WebhookDeliveryOutcomeCode
    webhook_delivery_contract_version: int
    delivered_event_count: int
    attempted_delivery_count: int
    failed_delivery_count: int
    rejection_reason_code: WebhookDeliveryRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return delivery counts, or a sparse rejected operational result."""
        outcome = self.webhook_delivery_outcome_code
        outcome_text = (
            outcome.value if isinstance(outcome, WebhookDeliveryOutcomeCode) else str(outcome)
        )
        payload: dict[str, object] = {
            "webhook_delivery_contract_version": self.webhook_delivery_contract_version,
            "webhook_delivery_outcome_code": outcome_text,
        }
        if outcome_text == WebhookDeliveryOutcomeCode.REJECTED:
            payload["rejection_reason_code"] = (
                self.rejection_reason_code.value
                if self.rejection_reason_code is not None
                else "tenant_not_found"
            )
            return payload
        if outcome_text != WebhookDeliveryOutcomeCode.ACCEPTED:
            raise ValueError(f"unsupported webhook delivery outcome: {outcome_text}")
        payload["delivered_event_count"] = self.delivered_event_count
        payload["attempted_delivery_count"] = self.attempted_delivery_count
        payload["failed_delivery_count"] = self.failed_delivery_count
        return payload


class WebhookSubscriptionService:
    """Register, list, and revoke tenant-scoped webhook subscriptions."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
        credential_pepper: str | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))
        if credential_pepper is None:
            credential_pepper = os.environ.get("CWL_API_CREDENTIAL_PEPPER") or DEFAULT_CREDENTIAL_PEPPER
        if not credential_pepper:
            raise ValueError("credential pepper must be a non-empty string")
        self._pepper = credential_pepper

    def register_subscription(
        self,
        tenant_reference: str,
        callback_url: str,
        event_type_codes: object,
    ) -> WebhookSubscriptionResult:
        """Write one active subscription and return the secret once.

        Replay of the same tenant, callback URL, event set, and contract
        version returns the stored ``webhook_subscription_id`` as
        ``duplicate_replay`` and does not mint a second secret.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _subscription_rejected(WebhookSubscriptionRejectionReasonCode.TENANT_NOT_FOUND)
        assert tenant is not None
        if not callback_url_is_allowed(callback_url):
            return _subscription_rejected(
                WebhookSubscriptionRejectionReasonCode.WEBHOOK_CALLBACK_URL_INSECURE
            )
        try:
            codes = canonical_event_type_set(event_type_codes)
        except WebhookSubscriptionQueryError:
            return _subscription_rejected(
                WebhookSubscriptionRejectionReasonCode.WEBHOOK_EVENT_TYPE_UNKNOWN
            )
        event_set = event_type_set_text(codes)
        existing = self.ledger.find_webhook_subscription(
            tenant.tenant_account_id,
            callback_url,
            event_set,
            WEBHOOK_SUBSCRIPTION_CONTRACT_VERSION,
        )
        if existing is not None:
            return _subscription_from_stored(
                existing, tenant.tenant_reference, None, WebhookSubscriptionOutcomeCode.DUPLICATE_REPLAY
            )
        prefix, secret = mint_webhook_secret()
        stored = self.ledger.insert_webhook_subscription(
            StoredWebhookSubscription(
                webhook_subscription_id=generate_record_id(),
                tenant_account_id=tenant.tenant_account_id,
                webhook_subscription_contract_version=WEBHOOK_SUBSCRIPTION_CONTRACT_VERSION,
                callback_url=callback_url,
                event_type_set=event_set,
                webhook_secret_prefix=prefix,
                webhook_secret_hash=hash_webhook_secret(secret, self._pepper),
                subscription_status="active",
                issued_at=self._clock(),
                revoked_at=None,
            )
        )
        self.ledger.store_webhook_subscription_secret(stored.webhook_subscription_id, secret)
        return _subscription_from_stored(
            stored, tenant.tenant_reference, secret, WebhookSubscriptionOutcomeCode.ACCEPTED
        )

    def list_subscriptions(self, tenant_reference: str) -> WebhookSubscriptionPage:
        """Return metadata for one tenant.  Secrets and hashes are omitted."""
        tenant = self._require_tenant(tenant_reference)
        return WebhookSubscriptionPage(
            webhook_subscriptions=tuple(
                _subscription_from_stored(
                    stored, tenant.tenant_reference, None, WebhookSubscriptionOutcomeCode.ACCEPTED
                )
                for stored in self.ledger.list_webhook_subscriptions(tenant.tenant_account_id)
            )
        )

    def revoke_subscription(
        self, tenant_reference: str, webhook_subscription_id: UUID
    ) -> WebhookSubscriptionResult:
        """Revoke one same-tenant subscription.  A second revoke is a replay."""
        tenant = self._require_tenant(tenant_reference)
        stored = self.ledger.get_webhook_subscription(webhook_subscription_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise WebhookSubscriptionQueryError("webhook_subscription_not_found")
        if stored.subscription_status == "revoked":
            return _subscription_from_stored(
                stored, tenant.tenant_reference, None, WebhookSubscriptionOutcomeCode.DUPLICATE_REPLAY
            )
        updated = self.ledger.revoke_webhook_subscription(
            stored.webhook_subscription_id, self._clock()
        )
        return _subscription_from_stored(
            updated, tenant.tenant_reference, None, WebhookSubscriptionOutcomeCode.ACCEPTED
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise WebhookSubscriptionQueryError("tenant_not_found")
        assert tenant is not None
        return tenant


class WebhookDeliveryService:
    """POST due outbox events to active same-tenant subscriptions."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
        transport: WebhookTransport | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._transport: WebhookTransport = (
            transport if transport is not None else post_signed_webhook
        )

    def deliver_due_events(self, tenant_reference: str) -> WebhookDeliveryResult:
        """POST pending outbox events for one tenant to matching active callbacks.

        There is no scheduler.  Operators call this method or
        ``POST /v1/webhook-deliveries``.  A later call retries failed
        attempts and increments ``attempt_number``.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None or tenant is None:
            return WebhookDeliveryResult(
                webhook_delivery_outcome_code=WebhookDeliveryOutcomeCode.REJECTED,
                webhook_delivery_contract_version=WEBHOOK_DELIVERY_CONTRACT_VERSION,
                delivered_event_count=0,
                attempted_delivery_count=0,
                failed_delivery_count=0,
                rejection_reason_code=WebhookDeliveryRejectionReasonCode.TENANT_NOT_FOUND,
            )
        delivered_event_count = 0
        attempted_delivery_count = 0
        failed_delivery_count = 0
        for outbox_event in self.ledger.list_pending_webhook_outbox_events(tenant.tenant_account_id):
            subscriptions = self.ledger.list_active_webhook_subscriptions(
                tenant.tenant_account_id, outbox_event.event_type_code
            )
            if not subscriptions:
                continue
            event_failed = False
            for subscription in subscriptions:
                if _has_successful_attempt(self.ledger, outbox_event.outbox_event_id, subscription):
                    continue
                attempted_delivery_count += 1
                if self._deliver_one(outbox_event, subscription):
                    continue
                event_failed = True
                failed_delivery_count += 1
            if not event_failed and _all_active_succeeded(
                self.ledger, outbox_event, subscriptions
            ):
                self.ledger.mark_webhook_outbox_event_delivered(outbox_event.outbox_event_id)
                delivered_event_count += 1
        return WebhookDeliveryResult(
            webhook_delivery_outcome_code=WebhookDeliveryOutcomeCode.ACCEPTED,
            webhook_delivery_contract_version=WEBHOOK_DELIVERY_CONTRACT_VERSION,
            delivered_event_count=delivered_event_count,
            attempted_delivery_count=attempted_delivery_count,
            failed_delivery_count=failed_delivery_count,
            rejection_reason_code=None,
        )

    def _deliver_one(
        self,
        outbox_event: StoredWebhookOutboxEvent,
        subscription: StoredWebhookSubscription,
    ) -> bool:
        """POST one signed body and append the attempt.  Return success."""
        secret = self.ledger.get_webhook_subscription_secret(subscription.webhook_subscription_id)
        raw_body = outbox_event.payload_json.encode("utf-8")
        attempted_at = self._clock()
        prior = self.ledger.list_webhook_delivery_attempts(
            outbox_event.outbox_event_id, subscription.webhook_subscription_id
        )
        attempt_number = len(prior) + 1
        if secret is None:
            self.ledger.insert_webhook_delivery_attempt(
                StoredWebhookDeliveryAttempt(
                    delivery_attempt_id=generate_record_id(),
                    outbox_event_id=outbox_event.outbox_event_id,
                    webhook_subscription_id=subscription.webhook_subscription_id,
                    attempt_number=attempt_number,
                    http_status=None,
                    delivered_at=None,
                    failure_reason_code="webhook_secret_unavailable",
                    attempted_at=attempted_at,
                )
            )
            return False
        headers = {
            "Content-Type": "application/json",
            WEBHOOK_SIGNATURE_HEADER: sign_webhook_body(secret, raw_body),
        }
        http_status, failure_reason_code = self._transport(
            subscription.callback_url, raw_body, headers
        )
        succeeded = failure_reason_code is None and http_status is not None and 200 <= http_status < 300
        self.ledger.insert_webhook_delivery_attempt(
            StoredWebhookDeliveryAttempt(
                delivery_attempt_id=generate_record_id(),
                outbox_event_id=outbox_event.outbox_event_id,
                webhook_subscription_id=subscription.webhook_subscription_id,
                attempt_number=attempt_number,
                http_status=http_status,
                delivered_at=attempted_at if succeeded else None,
                failure_reason_code=None if succeeded else (failure_reason_code or "webhook_http_error"),
                attempted_at=attempted_at,
            )
        )
        return succeeded


def _has_successful_attempt(
    ledger: MemoryUsageLedger,
    outbox_event_id: UUID,
    subscription: StoredWebhookSubscription,
) -> bool:
    """Return whether this subscription already accepted the outbox event."""
    return any(
        attempt.delivered_at is not None
        for attempt in ledger.list_webhook_delivery_attempts(
            outbox_event_id, subscription.webhook_subscription_id
        )
    )


def _all_active_succeeded(
    ledger: MemoryUsageLedger,
    outbox_event: StoredWebhookOutboxEvent,
    subscriptions: tuple[StoredWebhookSubscription, ...],
) -> bool:
    """Return whether every currently active matching subscription succeeded."""
    if not subscriptions:
        return False
    return all(
        _has_successful_attempt(ledger, outbox_event.outbox_event_id, subscription)
        for subscription in subscriptions
    )


def _reject_forbidden_payload(payload: object) -> None:
    """Refuse API secrets and PANs in an outbox envelope."""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if key in FORBIDDEN_PAYLOAD_KEYS:
                raise ValueError("webhook payload must not include secrets or PANs")
            _reject_forbidden_payload(value)
        return
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        for item in payload:
            _reject_forbidden_payload(item)


def _subscription_rejected(
    reason_code: WebhookSubscriptionRejectionReasonCode,
) -> WebhookSubscriptionResult:
    """Build a rejected result without minting a secret."""
    return WebhookSubscriptionResult(
        webhook_subscription_outcome_code=WebhookSubscriptionOutcomeCode.REJECTED,
        webhook_subscription_contract_version=WEBHOOK_SUBSCRIPTION_CONTRACT_VERSION,
        webhook_subscription_id=None,
        tenant_reference=None,
        callback_url=None,
        event_type_codes=(),
        webhook_secret_prefix=None,
        webhook_secret=None,
        subscription_status=None,
        issued_at=None,
        rejection_reason_code=reason_code,
    )


def _subscription_from_stored(
    stored: StoredWebhookSubscription,
    tenant_reference: str,
    secret: str | None,
    outcome: WebhookSubscriptionOutcomeCode,
) -> WebhookSubscriptionResult:
    """Project a persisted subscription.  ``secret`` is set only on register."""
    return WebhookSubscriptionResult(
        webhook_subscription_outcome_code=outcome,
        webhook_subscription_contract_version=stored.webhook_subscription_contract_version,
        webhook_subscription_id=stored.webhook_subscription_id,
        tenant_reference=tenant_reference,
        callback_url=stored.callback_url,
        event_type_codes=tuple(
            code for code in stored.event_type_set.split(",") if code
        ),
        webhook_secret_prefix=stored.webhook_secret_prefix,
        webhook_secret=secret,
        subscription_status=stored.subscription_status,
        issued_at=stored.issued_at,
        rejection_reason_code=None,
    )


def _format_instant(instant: datetime | None) -> str:
    """Render an instant as a timezone-aware ISO 8601 value."""
    assert instant is not None
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")
