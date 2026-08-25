"""Commercial spend budgets published against one billing-account window.

The service is the buyer-facing spend-budget write:

1. Resolve the tenant and that tenant's stored ``billing_account``.
2. Accept an exact ``budget_amount`` greater than zero and an ISO 4217 code.
3. Persist one append-only ``spend_budget`` for the half-open window.
4. Replay the same tenant, account, window, currency, payload hash, and version.

A published budget is never edited.  A later distinct amount or hash is a new
row.  First successful publish enqueues one ``spend_budget.published``
outbox event.  The budget is a commercial control fact, not a rated-spend
comparison and not a posted journal (IFRS Foundation, 2024).
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping
from uuid import UUID

from metering_billing.errors import (
    ExactDecimalError,
    SpendBudgetOutcomeCode,
    SpendBudgetRejectionReasonCode,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.invoice_draft import parse_invoice_amount
from metering_billing.time_window import TimeWindow
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredSpendBudget,
    generate_record_id,
)
from metering_billing.webhook_outbox import (
    EVENT_TYPE_SPEND_BUDGET_PUBLISHED,
    enqueue_accepted_fact,
)


Clock = Callable[[], datetime]
SPEND_BUDGET_CONTRACT_VERSION = 1
SPEND_BUDGET_STATUS = "published"
NEXT_OPERATOR_ACTION = "wait"
CURRENCY_CODE_PATTERN = re.compile(r"^[A-Z]{3}$")
SOURCE_PAYLOAD_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def parse_budget_amount(value: Any) -> Decimal:
    """Parse a spend-budget amount as an exact decimal greater than zero.

    Binary floating-point values are rejected at this boundary so a budget
    cannot smuggle IEEE inexact money into later commercial control reads.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise ExactDecimalError("budget amount must be an exact decimal")
    parsed = parse_invoice_amount(value)
    if parsed <= 0:
        raise ExactDecimalError("budget amount must be greater than zero")
    return parsed


def compute_spend_budget_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return the ``sha256:<hex>`` digest of the canonical published budget."""
    canonical_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class SpendBudgetResult:
    """Buyer-facing result of publishing one commercial spend budget."""

    spend_budget_outcome_code: SpendBudgetOutcomeCode
    spend_budget_contract_version: int
    spend_budget_id: UUID | None
    tenant_reference: str | None
    billing_account_id: UUID | None
    currency_code: str | None
    budget_amount: Decimal | None
    window_started_at: datetime | None
    window_ended_at: datetime | None
    spend_budget_status: str | None
    source_payload_hash: str | None
    published_at: datetime | None
    next_operator_action: str
    rejection_reason_code: SpendBudgetRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published budget, or a sparse rejected operational result."""
        outcome = self.spend_budget_outcome_code
        outcome_text = (
            outcome.value if isinstance(outcome, SpendBudgetOutcomeCode) else str(outcome)
        )
        if outcome_text == SpendBudgetOutcomeCode.REJECTED:
            return {
                "spend_budget_contract_version": self.spend_budget_contract_version,
                "spend_budget_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else SpendBudgetRejectionReasonCode.SPEND_BUDGET_NOT_FOUND.value
                ),
            }
        if (
            outcome_text != SpendBudgetOutcomeCode.ACCEPTED
            and outcome_text != SpendBudgetOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported spend budget outcome: {outcome_text}")
        if (
            self.spend_budget_id is None
            or self.published_at is None
            or self.budget_amount is None
            or self.window_started_at is None
            or self.window_ended_at is None
        ):
            raise ValueError("accepted spend budgets must include identity and amount")
        return {
            "spend_budget_contract_version": self.spend_budget_contract_version,
            "spend_budget_outcome_code": outcome_text,
            "spend_budget_id": str(self.spend_budget_id),
            "tenant_reference": self.tenant_reference,
            "billing_account_id": str(self.billing_account_id),
            "currency_code": self.currency_code,
            "budget_amount": format_exact_decimal(self.budget_amount),
            "window_started_at": _format_instant(self.window_started_at),
            "window_ended_at": _format_instant(self.window_ended_at),
            "spend_budget_status": SPEND_BUDGET_STATUS,
            "source_payload_hash": self.source_payload_hash,
            "published_at": _format_instant(self.published_at),
            "next_operator_action": self.next_operator_action,
        }

    def as_webhook_event_data(self) -> dict[str, object]:
        """Return the thin ``spend_budget.published`` facts for the #24 envelope.

        The payload is a reference plus hash, not a rated-spend comparison.
        Remaining, over, utilization, lines, PII, PAN, secrets, raw
        documents, and statutory identifiers are omitted.
        """
        if self.spend_budget_id is None or self.billing_account_id is None:
            raise ValueError("rejected spend budget has no webhook event data")
        if self.published_at is None:
            raise ValueError("accepted spend budgets must include published_at")
        return {
            "spend_budget_id": str(self.spend_budget_id),
            "billing_account_id": str(self.billing_account_id),
            "source_payload_hash": self.source_payload_hash,
            "spend_budget_contract_version": self.spend_budget_contract_version,
            "currency_code": self.currency_code,
            "budget_amount": format_exact_decimal(self.budget_amount),
            "window_started_at": _format_instant(self.window_started_at),
            "window_ended_at": _format_instant(self.window_ended_at),
            "published_at": _format_instant(self.published_at),
            "spend_budget_status": SPEND_BUDGET_STATUS,
        }


class SpendBudgetService:
    """Append-only commercial spend-budget publisher backed by a normalized ledger."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def publish_spend_budget(
        self,
        tenant_reference: str,
        billing_account_id: UUID,
        currency_code: str,
        budget_amount: Any,
        time_window: TimeWindow,
        source_payload_hash: str | None = None,
    ) -> SpendBudgetResult:
        """Publish one append-only commercial spend budget for a billing account.

        A replay of the same tenant, account, window, currency, exact amount,
        and contract version returns the stored ``spend_budget_id``.  A later
        distinct amount or hash appends a new row.  First successful publish
        enqueues one ``spend_budget.published`` outbox event.  Replay of
        that publish does not enqueue a second row.  Rated-spend, rating,
        and invoice-draft rows are unchanged.
        """
        boundary = getattr(self.ledger, "transaction", nullcontext)
        with boundary():
            return self._publish_spend_budget_in_boundary(
                tenant_reference,
                billing_account_id,
                currency_code,
                budget_amount,
                time_window,
                source_payload_hash,
            )

    def _publish_spend_budget_in_boundary(
        self,
        tenant_reference: str,
        billing_account_id: UUID,
        currency_code: str,
        budget_amount: Any,
        time_window: TimeWindow,
        source_payload_hash: str | None,
    ) -> SpendBudgetResult:
        """Execute one publish and outbox enqueue inside the ledger boundary."""
        if not isinstance(tenant_reference, str) or not tenant_reference:
            return _rejected(SpendBudgetRejectionReasonCode.TENANT_NOT_FOUND)
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(SpendBudgetRejectionReasonCode.TENANT_NOT_FOUND)
        tenant = require_resolved(tenant, "tenant")
        account = _billing_account_for(self.ledger, billing_account_id)
        if account is None:
            return _rejected(SpendBudgetRejectionReasonCode.BILLING_ACCOUNT_NOT_FOUND)
        if account.tenant_account_id != tenant.tenant_account_id:
            return _rejected(SpendBudgetRejectionReasonCode.BILLING_ACCOUNT_FORBIDDEN)
        if not isinstance(currency_code, str) or CURRENCY_CODE_PATTERN.fullmatch(currency_code) is None:
            return _rejected(SpendBudgetRejectionReasonCode.CURRENCY_INVALID)
        try:
            parsed_amount = parse_budget_amount(budget_amount)
        except ExactDecimalError:
            return _rejected(SpendBudgetRejectionReasonCode.BUDGET_AMOUNT_INVALID)
        started = time_window.window_started_at.astimezone(UTC)
        ended = time_window.window_ended_at.astimezone(UTC)
        computed_hash = compute_spend_budget_payload_hash(
            {
                "billing_account_id": str(account.billing_account_id),
                "currency_code": currency_code,
                "budget_amount": format_exact_decimal(parsed_amount),
                "window_started_at": _format_instant(started),
                "window_ended_at": _format_instant(ended),
                "spend_budget_contract_version": SPEND_BUDGET_CONTRACT_VERSION,
            }
        )
        if source_payload_hash is not None:
            if (
                not isinstance(source_payload_hash, str)
                or SOURCE_PAYLOAD_HASH_PATTERN.fullmatch(source_payload_hash) is None
                or source_payload_hash != computed_hash
            ):
                return _rejected(SpendBudgetRejectionReasonCode.REQUEST_INVALID)
        existing = self.ledger.find_spend_budget(
            tenant.tenant_account_id,
            account.billing_account_id,
            started,
            ended,
            currency_code,
            computed_hash,
            SPEND_BUDGET_CONTRACT_VERSION,
        )
        if existing is not None:
            result = _from_stored(
                existing, tenant.tenant_reference, SpendBudgetOutcomeCode.DUPLICATE_REPLAY
            )
            _enqueue_spend_budget_published(self.ledger, tenant.tenant_reference, result)
            return result
        candidate = StoredSpendBudget(
            spend_budget_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            billing_account_id=account.billing_account_id,
            spend_budget_contract_version=SPEND_BUDGET_CONTRACT_VERSION,
            currency_code=currency_code,
            budget_amount=parsed_amount,
            window_started_at=started,
            window_ended_at=ended,
            source_payload_hash=computed_hash,
            published_at=self._clock(),
        )
        stored = self.ledger.insert_spend_budget(candidate)
        outcome = (
            SpendBudgetOutcomeCode.ACCEPTED
            if stored.spend_budget_id == candidate.spend_budget_id
            else SpendBudgetOutcomeCode.DUPLICATE_REPLAY
        )
        result = _from_stored(stored, tenant.tenant_reference, outcome)
        _enqueue_spend_budget_published(self.ledger, tenant.tenant_reference, result)
        return result


def _enqueue_spend_budget_published(
    ledger: MemoryUsageLedger,
    tenant_reference: str,
    result: SpendBudgetResult,
) -> None:
    """Append one ``spend_budget.published`` outbox row for a stored budget.

    Replay of the same tenant, event type, ``spend_budget_id``, and payload
    hash returns the stored row.  A crash after insert and before enqueue
    is healed by the next publish replay.
    """
    if result.spend_budget_id is None or result.published_at is None:
        raise ValueError("accepted spend budgets must include identity and published_at")
    enqueue_accepted_fact(
        ledger,
        tenant_reference,
        EVENT_TYPE_SPEND_BUDGET_PUBLISHED,
        result.spend_budget_id,
        result.as_webhook_event_data(),
        result.published_at,
    )


def _billing_account_for(ledger: MemoryUsageLedger, billing_account_id: UUID):
    """Return the stored billing account for one internal identifier, if any."""
    return ledger.get_billing_account(billing_account_id)


def _rejected(reason: SpendBudgetRejectionReasonCode) -> SpendBudgetResult:
    """Return a sparse rejected spend-budget result."""
    return SpendBudgetResult(
        spend_budget_outcome_code=SpendBudgetOutcomeCode.REJECTED,
        spend_budget_contract_version=SPEND_BUDGET_CONTRACT_VERSION,
        spend_budget_id=None,
        tenant_reference=None,
        billing_account_id=None,
        currency_code=None,
        budget_amount=None,
        window_started_at=None,
        window_ended_at=None,
        spend_budget_status=None,
        source_payload_hash=None,
        published_at=None,
        next_operator_action=NEXT_OPERATOR_ACTION,
        rejection_reason_code=reason,
    )


def _from_stored(
    stored: StoredSpendBudget,
    tenant_reference: str,
    outcome: SpendBudgetOutcomeCode,
) -> SpendBudgetResult:
    """Project a persisted spend budget into the buyer-facing result."""
    return SpendBudgetResult(
        spend_budget_outcome_code=outcome,
        spend_budget_contract_version=stored.spend_budget_contract_version,
        spend_budget_id=stored.spend_budget_id,
        tenant_reference=tenant_reference,
        billing_account_id=stored.billing_account_id,
        currency_code=stored.currency_code,
        budget_amount=stored.budget_amount,
        window_started_at=stored.window_started_at,
        window_ended_at=stored.window_ended_at,
        spend_budget_status=stored.spend_budget_status,
        source_payload_hash=stored.source_payload_hash,
        published_at=stored.published_at,
        next_operator_action=NEXT_OPERATOR_ACTION,
        rejection_reason_code=None,
    )


def _format_instant(instant: datetime) -> str:
    """Return an RFC 3339 UTC timestamp for one stored instant."""
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")
