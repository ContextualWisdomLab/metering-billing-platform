"""Pre-execution monetary exposure control backed by the commercial ledger.

This module owns the first #85 vertical slice: one published spend budget is
reserved atomically, then actual use and unused exposure are recorded through
append-only receipts.  Hierarchical policies, quotas, credits, and
entitlements remain separate policy slices.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from metering_billing.errors import (
    SpendAuthorizationOutcomeCode,
    SpendAuthorizationQueryError,
)
from metering_billing.exact_decimal import (
    format_exact_decimal,
    require_decimal_quantity,
)
from metering_billing.usage_ledger import (
    StoredSpendAuthorization,
    StoredSpendCommitment,
    StoredSpendRelease,
    StoredSpendReservation,
    generate_record_id,
)


SPEND_AUTHORIZATION_CONTRACT_VERSION = 1
SPEND_AUTHORIZATION_OPERATIONS = frozenset({"request", "commitment", "release", "expire"})
_TERMINAL_STATUSES = frozenset({"committed", "released", "expired", "denied"})


@dataclass(frozen=True)
class SpendAuthorizationResult:
    """Stable command envelope for authorization lifecycle writes."""

    operation_code: str
    outcome_code: SpendAuthorizationOutcomeCode
    tenant_reference: str | None
    authorization: StoredSpendAuthorization | None
    mutation_id: UUID | None
    rejection_reason_code: str | None

    @property
    def spend_authorization_outcome_code(self) -> SpendAuthorizationOutcomeCode:
        """Expose the repository naming convention alongside ``outcome_code``."""
        return self.outcome_code

    def as_contract_dict(self) -> dict[str, object]:
        """Render exact money and lifecycle state without exposing internals."""
        body: dict[str, object] = {
            "spend_authorization_contract_version": SPEND_AUTHORIZATION_CONTRACT_VERSION,
            "spend_authorization_operation_code": self.operation_code,
            "spend_authorization_outcome_code": self.outcome_code.value,
        }
        if self.tenant_reference is not None:
            body["tenant_reference"] = self.tenant_reference
        if self.mutation_id is not None:
            body["mutation_id"] = str(self.mutation_id)
        if self.authorization is not None:
            body.update(_authorization_contract_fields(self.authorization))
        if self.rejection_reason_code is not None:
            body["rejection_reason_code"] = self.rejection_reason_code
        body["next_operator_action"] = _next_operator_action(
            self.authorization, self.rejection_reason_code
        )
        return body


class SpendAuthorizationService:
    """Authorize, commit, release, and expire exact pre-execution exposure."""

    def __init__(self, ledger: Any, clock: Any | None = None) -> None:
        self.ledger = ledger
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    def request_authorization(
        self,
        tenant_reference: str,
        spend_budget_id: UUID,
        requested_amount: Any,
        idempotency_key: str,
        actor_reference: str,
        purpose_code: str,
        policy_version: str,
        valid_until: datetime,
    ) -> SpendAuthorizationResult:
        """Reserve exact exposure against one immutable published budget."""
        boundary = getattr(self.ledger, "transaction", nullcontext)
        with boundary():
            tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
            if tenant_error is not None or tenant is None:
                return _rejected("request", tenant_reference, "tenant_not_found")
            now = self._now()
            reason = _validate_request(
                requested_amount,
                idempotency_key,
                actor_reference,
                purpose_code,
                policy_version,
                valid_until,
                now,
            )
            if reason is not None:
                return _rejected("request", tenant_reference, reason)
            amount = require_decimal_quantity(requested_amount)
            budget = self.ledger.get_spend_budget(spend_budget_id)
            if budget is None:
                return _rejected("request", tenant_reference, "spend_budget_not_found")
            if budget.tenant_account_id != tenant.tenant_account_id:
                return _rejected("request", tenant_reference, "spend_budget_not_found")
            account = self.ledger.get_billing_account(budget.billing_account_id)
            if account is None:
                return _rejected("request", tenant_reference, "billing_account_not_found")
            if account.tenant_account_id != tenant.tenant_account_id:
                return _rejected("request", tenant_reference, "billing_account_forbidden")
            if now < budget.window_started_at or now >= budget.window_ended_at:
                return _rejected("request", tenant_reference, "validity_window_invalid")
            if valid_until > budget.window_ended_at:
                return _rejected("request", tenant_reference, "validity_window_invalid")
            candidate = StoredSpendAuthorization(
                generate_record_id(),
                tenant.tenant_account_id,
                budget.billing_account_id,
                budget.spend_budget_id,
                SPEND_AUTHORIZATION_CONTRACT_VERSION,
                idempotency_key,
                actor_reference,
                purpose_code,
                policy_version,
                budget.currency_code,
                amount,
                amount,
                Decimal("0"),
                Decimal("0"),
                now,
                valid_until,
                "requested",
                None,
                now,
                now,
            )
            reservation = StoredSpendReservation(
                generate_record_id(),
                tenant.tenant_account_id,
                candidate.spend_authorization_id,
                amount,
                idempotency_key,
                now,
                valid_until,
            )
            try:
                stored, outcome = self.ledger.create_spend_authorization(
                    candidate, reservation
                )
            except KeyError:
                return _rejected("request", tenant_reference, "spend_budget_not_found")
            except ValueError:
                return _rejected("request", tenant_reference, "idempotency_key_conflict")
            if outcome == "duplicate_replay":
                if stored.authorization_status == "denied":
                    return _result(
                        "request",
                        SpendAuthorizationOutcomeCode.REJECTED,
                        tenant_reference,
                        stored,
                        stored.spend_authorization_id,
                        stored.rejection_reason_code,
                    )
                return _result(
                    "request",
                    SpendAuthorizationOutcomeCode.DUPLICATE_REPLAY,
                    tenant_reference,
                    stored,
                    stored.spend_authorization_id,
                    None,
                )
            if outcome == "authorization_exposure_exceeded":
                return _result(
                    "request",
                    SpendAuthorizationOutcomeCode.REJECTED,
                    tenant_reference,
                    stored,
                    stored.spend_authorization_id,
                    outcome,
                )
            return _result(
                "request",
                SpendAuthorizationOutcomeCode.ACCEPTED,
                tenant_reference,
                stored,
                stored.spend_authorization_id,
                None,
            )

    def commit_authorization(
        self,
        tenant_reference: str,
        spend_authorization_id: UUID,
        committed_amount: Any,
        idempotency_key: str,
        actual_usage_reference: str,
    ) -> SpendAuthorizationResult:
        """Append actual usage and never commit beyond the reserved remainder."""
        return self._apply_commitment(
            tenant_reference,
            spend_authorization_id,
            committed_amount,
            idempotency_key,
            actual_usage_reference,
        )

    def release_authorization(
        self,
        tenant_reference: str,
        spend_authorization_id: UUID,
        released_amount: Any,
        idempotency_key: str,
        release_reason_code: str,
    ) -> SpendAuthorizationResult:
        """Append unused-exposure release without changing the budget row."""
        boundary = getattr(self.ledger, "transaction", nullcontext)
        with boundary():
            tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
            if tenant_error is not None or tenant is None:
                return _rejected("release", tenant_reference, "tenant_not_found")
            reason = _validate_amount_key(
                released_amount, idempotency_key, "release_amount_invalid"
            )
            if reason is not None or not _valid_text(release_reason_code, 100):
                return _rejected("release", tenant_reference, reason or "request_invalid")
            authorization = self.ledger.get_spend_authorization(
                tenant.tenant_account_id, spend_authorization_id
            )
            if authorization is None:
                return _rejected("release", tenant_reference, "spend_authorization_not_found")
            if authorization.authorization_status == "denied":
                return _rejected("release", tenant_reference, "authorization_status_invalid")
            release = StoredSpendRelease(
                generate_record_id(),
                tenant.tenant_account_id,
                spend_authorization_id,
                idempotency_key,
                require_decimal_quantity(released_amount),
                release_reason_code,
                self._now(),
            )
            return self._apply_release(
                tenant_reference,
                tenant.tenant_account_id,
                release,
                "release",
            )

    def expire_authorization(
        self,
        tenant_reference: str,
        spend_authorization_id: UUID,
        idempotency_key: str,
    ) -> SpendAuthorizationResult:
        """Release remaining exposure as expired after the validity window closes."""
        boundary = getattr(self.ledger, "transaction", nullcontext)
        with boundary():
            tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
            if tenant_error is not None or tenant is None:
                return _rejected("expire", tenant_reference, "tenant_not_found")
            if not _valid_text(idempotency_key, 200):
                return _rejected("expire", tenant_reference, "idempotency_key_invalid")
            authorization = self.ledger.get_spend_authorization(
                tenant.tenant_account_id, spend_authorization_id
            )
            if authorization is None:
                return _rejected("expire", tenant_reference, "spend_authorization_not_found")
            now = self._now()
            if now < authorization.valid_until:
                return _rejected("expire", tenant_reference, "validity_window_invalid")
            remaining = _remaining_amount(authorization)
            if remaining <= 0:
                if authorization.authorization_status == "expired":
                    return _result(
                        "expire",
                        SpendAuthorizationOutcomeCode.DUPLICATE_REPLAY,
                        tenant_reference,
                        authorization,
                        authorization.spend_authorization_id,
                        None,
                    )
                return _rejected("expire", tenant_reference, "authorization_status_invalid")
            release = StoredSpendRelease(
                generate_record_id(),
                tenant.tenant_account_id,
                spend_authorization_id,
                idempotency_key,
                remaining,
                "expired",
                now,
            )
            return self._apply_release(
                tenant_reference,
                tenant.tenant_account_id,
                release,
                "expire",
                target_status="expired",
            )

    def present_authorization(
        self, tenant_reference: str, spend_authorization_id: UUID
    ) -> dict[str, object]:
        """Present one authorization after enforcing tenant ownership."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None or tenant is None:
            raise SpendAuthorizationQueryError("tenant_not_found")
        authorization = self.ledger.get_spend_authorization(
            tenant.tenant_account_id, spend_authorization_id
        )
        if authorization is None:
            raise SpendAuthorizationQueryError("spend_authorization_not_found")
        body = _authorization_contract_fields(authorization)
        body["spend_authorization_presentment_contract_version"] = (
            SPEND_AUTHORIZATION_CONTRACT_VERSION
        )
        body["tenant_reference"] = tenant_reference
        body["next_operator_action"] = _next_operator_action(authorization, None)
        return body

    def _apply_commitment(
        self,
        tenant_reference: str,
        spend_authorization_id: UUID,
        committed_amount: Any,
        idempotency_key: str,
        actual_usage_reference: str,
    ) -> SpendAuthorizationResult:
        """Run the commitment command inside the ledger transaction boundary."""
        boundary = getattr(self.ledger, "transaction", nullcontext)
        with boundary():
            tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
            if tenant_error is not None or tenant is None:
                return _rejected("commitment", tenant_reference, "tenant_not_found")
            reason = _validate_amount_key(
                committed_amount, idempotency_key, "commitment_amount_invalid"
            )
            if reason is not None or not _valid_text(actual_usage_reference, 200):
                return _rejected("commitment", tenant_reference, reason or "request_invalid")
            authorization = self.ledger.get_spend_authorization(
                tenant.tenant_account_id, spend_authorization_id
            )
            if authorization is None:
                return _rejected("commitment", tenant_reference, "spend_authorization_not_found")
            commitment = StoredSpendCommitment(
                generate_record_id(),
                tenant.tenant_account_id,
                spend_authorization_id,
                idempotency_key,
                require_decimal_quantity(committed_amount),
                actual_usage_reference,
                self._now(),
            )
            try:
                stored, outcome = self.ledger.apply_spend_commitment(
                    tenant.tenant_account_id, commitment, self._now()
                )
            except ValueError:
                return _rejected("commitment", tenant_reference, "idempotency_key_conflict")
            reason_code = {
                "authorization_expired": "authorization_expired",
                "authorization_status_invalid": "authorization_status_invalid",
                "commitment_amount_exceeded": "commitment_amount_exceeded",
            }.get(outcome)
            if reason_code is not None:
                return _result(
                    "commitment",
                    SpendAuthorizationOutcomeCode.REJECTED,
                    tenant_reference,
                    stored,
                    stored.spend_authorization_id,
                    reason_code,
                )
            return _result(
                "commitment",
                SpendAuthorizationOutcomeCode.DUPLICATE_REPLAY
                if outcome == "duplicate_replay"
                else SpendAuthorizationOutcomeCode.ACCEPTED,
                tenant_reference,
                stored,
                commitment.spend_commitment_id,
                None,
            )

    def _apply_release(
        self,
        tenant_reference: str,
        tenant_account_id: UUID,
        release: StoredSpendRelease,
        operation_code: str,
        target_status: str = "released",
    ) -> SpendAuthorizationResult:
        """Run one release receipt through the atomic ledger method."""
        try:
            stored, outcome = self.ledger.apply_spend_release(
                tenant_account_id, release, self._now(), target_status
            )
        except ValueError:
            return _rejected(operation_code, tenant_reference, "idempotency_key_conflict")
        if outcome == "release_amount_exceeded":
            return _result(
                operation_code,
                SpendAuthorizationOutcomeCode.REJECTED,
                tenant_reference,
                stored,
                stored.spend_authorization_id,
                "release_amount_exceeded",
            )
        return _result(
            operation_code,
            SpendAuthorizationOutcomeCode.DUPLICATE_REPLAY
            if outcome == "duplicate_replay"
            else SpendAuthorizationOutcomeCode.ACCEPTED,
            tenant_reference,
            stored,
            release.spend_release_id,
            None,
        )

    def _now(self) -> datetime:
        """Return a timezone-aware command timestamp."""
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("authorization clock must return an aware datetime")
        return value.astimezone(UTC)


def _authorization_contract_fields(
    authorization: StoredSpendAuthorization,
) -> dict[str, object]:
    """Render a stored authorization with exact decimal strings."""
    return {
        "spend_authorization_id": str(authorization.spend_authorization_id),
        "tenant_account_id": str(authorization.tenant_account_id),
        "billing_account_id": str(authorization.billing_account_id),
        "spend_budget_id": str(authorization.spend_budget_id),
        "idempotency_key": authorization.idempotency_key,
        "actor_reference": authorization.actor_reference,
        "purpose_code": authorization.purpose_code,
        "policy_version": authorization.policy_version,
        "currency_code": authorization.currency_code,
        "requested_amount": format_exact_decimal(authorization.requested_amount),
        "reserved_amount": format_exact_decimal(authorization.reserved_amount),
        "committed_amount": format_exact_decimal(authorization.committed_amount),
        "released_amount": format_exact_decimal(authorization.released_amount),
        "remaining_amount": format_exact_decimal(_remaining_amount(authorization)),
        "valid_from": authorization.valid_from.isoformat(),
        "valid_until": authorization.valid_until.isoformat(),
        "authorization_status": authorization.authorization_status,
        "created_at": authorization.created_at.isoformat(),
        "updated_at": authorization.updated_at.isoformat(),
    }


def _remaining_amount(authorization: StoredSpendAuthorization) -> Decimal:
    """Return conserved uncommitted and unreleased exposure."""
    return (
        authorization.requested_amount
        - authorization.committed_amount
        - authorization.released_amount
    )


def _next_operator_action(
    authorization: StoredSpendAuthorization | None, reason: str | None
) -> str:
    """Return a safe action hint without revealing another tenant's limits."""
    if reason is not None:
        return "reduce_requested_amount_or_review_policy"
    if authorization is None:
        return "correct_request"
    if authorization.authorization_status in _TERMINAL_STATUSES:
        return "stop"
    return "execute_within_validity_window"


def _validate_request(
    requested_amount: Any,
    idempotency_key: str,
    actor_reference: str,
    purpose_code: str,
    policy_version: str,
    valid_until: datetime,
    now: datetime,
) -> str | None:
    """Validate trust-boundary metadata before creating any row."""
    try:
        amount = require_decimal_quantity(requested_amount)
    except ValueError:
        return "requested_amount_invalid"
    if amount <= 0:
        return "requested_amount_invalid"
    if not _valid_text(idempotency_key, 200):
        return "idempotency_key_invalid"
    if not _valid_text(actor_reference, 200):
        return "actor_reference_invalid"
    if not _valid_text(purpose_code, 100):
        return "purpose_invalid"
    if not _valid_text(policy_version, 100):
        return "policy_version_invalid"
    if not isinstance(valid_until, datetime) or valid_until.tzinfo is None:
        return "validity_window_invalid"
    if valid_until.astimezone(UTC) <= now:
        return "validity_window_invalid"
    return None


def _validate_amount_key(value: Any, idempotency_key: str, amount_reason: str) -> str | None:
    """Validate exact positive mutation amounts and idempotency keys."""
    try:
        amount = require_decimal_quantity(value)
    except ValueError:
        return amount_reason
    if amount <= 0:
        return amount_reason
    if not _valid_text(idempotency_key, 200):
        return "idempotency_key_invalid"
    return None


def _valid_text(value: object, maximum: int) -> bool:
    """Accept one bounded non-empty string."""
    return isinstance(value, str) and 0 < len(value) <= maximum


def _result(
    operation_code: str,
    outcome_code: SpendAuthorizationOutcomeCode,
    tenant_reference: str | None,
    authorization: StoredSpendAuthorization | None,
    mutation_id: UUID | None,
    rejection_reason_code: str | None,
) -> SpendAuthorizationResult:
    """Build one command envelope."""
    return SpendAuthorizationResult(
        operation_code,
        outcome_code,
        tenant_reference,
        authorization,
        mutation_id,
        rejection_reason_code,
    )


def _rejected(
    operation_code: str, tenant_reference: str | None, reason: str
) -> SpendAuthorizationResult:
    """Build a non-mutating rejected command envelope."""
    return _result(
        operation_code,
        SpendAuthorizationOutcomeCode.REJECTED,
        tenant_reference,
        None,
        None,
        reason,
    )
