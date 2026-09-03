"""Typed outcomes and rejection reasons for usage, rating, drafts, exports, collections, intents, settlement, and observations.

Reason codes are stable operational vocabulary.  They are safe to persist in
audit receipts and do not require masking: they describe control failures, not
customer content.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TypeVar

ResolvedValue = TypeVar("ResolvedValue")


class IngestionOutcomeCode(StrEnum):
    """Terminal result of attempting to persist one usage event."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class RejectionReasonCode(StrEnum):
    """Why an event was refused without mutating previously stored usage."""

    SCHEMA_INVALID = "schema_invalid"
    PAYLOAD_HASH_MISMATCH = "payload_hash_mismatch"
    TENANT_NOT_FOUND = "tenant_not_found"
    ATTRIBUTION_TENANT_MISMATCH = "attribution_tenant_mismatch"
    BILLING_ACCOUNT_NOT_FOUND = "billing_account_not_found"
    BILLING_ACCOUNT_NOT_ACTIVE = "billing_account_not_active"
    BILLING_PRINCIPAL_NOT_FOUND = "billing_principal_not_found"
    PRINCIPAL_NOT_EFFECTIVE = "principal_not_effective"
    CREDENTIAL_NOT_FOUND = "credential_not_found"
    CREDENTIAL_NOT_ASSIGNED = "credential_not_assigned"
    METER_NOT_FOUND = "meter_not_found"
    METER_UNIT_MISMATCH = "meter_unit_mismatch"
    METER_QUALITY_NOT_ALLOWED = "meter_quality_not_allowed"
    MEASUREMENT_QUANTITY_INVALID = "measurement_quantity_invalid"
    MEASUREMENT_METER_DUPLICATE = "measurement_meter_duplicate"
    EVENT_OUTSIDE_TIME_WINDOW = "event_outside_time_window"
    SOURCE_EVENT_CONFLICT = "source_event_conflict"
    PAYLOAD_HASH_CONFLICT = "payload_hash_conflict"
    PRODUCER_EVENT_CONFLICT = "producer_event_conflict"


class UsageEventConflict(ValueError):
    """Describe a PostgreSQL unique-identity race for one usage event."""

    def __init__(
        self,
        existing: object,
        *,
        duplicate_replay: bool,
        rejection_reason_code: RejectionReasonCode | None = None,
    ) -> None:
        super().__init__("usage event identity already exists")
        self.existing = existing
        self.duplicate_replay = duplicate_replay
        self.rejection_reason_code = rejection_reason_code


class RatingOutcomeCode(StrEnum):
    """Terminal result of attempting to persist one windowed rating run."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class RatingRejectionReasonCode(StrEnum):
    """Why a rating request was refused without writing invoice-intent money."""

    TENANT_NOT_FOUND = "tenant_not_found"
    RATE_CARD_NOT_FOUND = "rate_card_not_found"
    RATE_CARD_NOT_EFFECTIVE = "rate_card_not_effective"
    METER_PRICE_MISSING = "meter_price_missing"
    BILLING_DISPOSITION_UNKNOWN = "billing_disposition_unknown"


class RateCardOutcomeCode(StrEnum):
    """Terminal result of publishing one tenant-scoped rate-card version."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class RateCardRejectionReasonCode(StrEnum):
    """Why a rate-card publish or read was refused without writing prices."""

    TENANT_NOT_FOUND = "tenant_not_found"
    RATE_CARD_NOT_FOUND = "rate_card_not_found"
    RATE_CARD_LINES_INVALID = "rate_card_lines_invalid"
    UNIT_AMOUNT_INVALID = "unit_amount_invalid"
    CURRENCY_MISMATCH = "currency_mismatch"
    METRIC_CODE_INVALID = "metric_code_invalid"
    RATE_CARD_NAME_INVALID = "rate_card_name_invalid"
    CURRENCY_CODE_INVALID = "currency_code_invalid"


class RateCardQueryError(ValueError):
    """Raised when a stored rate card or version cannot be authorized or decoded."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class RateCardPresentmentQueryError(ValueError):
    """Raised when a stored rate card cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class UsageEventPresentmentQueryError(ValueError):
    """Raised when a stored usage event cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class RatingRunPresentmentQueryError(ValueError):
    """Raised when a stored rating run cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class TaxAssessmentPresentmentQueryError(ValueError):
    """Raised when a stored tax assessment cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class TaxRateOutcomeCode(StrEnum):
    """Terminal result of publishing one tenant-scoped tax-rate version."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class TaxRateRejectionReasonCode(StrEnum):
    """Why a tax-rate publish or read was refused without writing a rate."""

    TENANT_NOT_FOUND = "tenant_not_found"
    TAX_RATE_NOT_FOUND = "tax_rate_not_found"
    TAX_CODE_INVALID = "tax_code_invalid"
    TAX_RATE_INVALID = "tax_rate_invalid"


class TaxRateQueryError(ValueError):
    """Raised when a stored tax-rate version cannot be authorized or decoded."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class TaxAssessmentOutcomeCode(StrEnum):
    """Terminal result of assessing tax on one invoice draft."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class TaxAssessmentRejectionReasonCode(StrEnum):
    """Why a tax assessment was refused without writing tax amounts."""

    TENANT_NOT_FOUND = "tenant_not_found"
    INVOICE_DRAFT_NOT_FOUND = "invoice_draft_not_found"
    TAX_RATE_NOT_FOUND = "tax_rate_not_found"
    TAX_AFTER_COLLECTION_OPENED = "tax_after_collection_opened"
    DRAFT_TOTAL_INVALID = "draft_total_invalid"
    CURRENCY_EXPONENT_UNKNOWN = "currency_exponent_unknown"
    TAX_ASSESSMENT_NOT_FOUND = "tax_assessment_not_found"


class TaxAssessmentQueryError(ValueError):
    """Raised when a stored tax assessment cannot be authorized or decoded."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class InvoiceDraftOutcomeCode(StrEnum):
    """Terminal result of attempting to persist one invoice-intent draft."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class InvoiceDraftRejectionReasonCode(StrEnum):
    """Why a draft request was refused without writing invoice-intent money."""

    TENANT_NOT_FOUND = "tenant_not_found"
    RATING_RUN_NOT_FOUND = "rating_run_not_found"


class InvoicePresentmentQueryError(ValueError):
    """Raised when a stored invoice draft cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class IssuedInvoiceOutcomeCode(StrEnum):
    """Terminal result of attempting to persist one commercial issued invoice."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class IssuedInvoiceRejectionReasonCode(StrEnum):
    """Why an issue request was refused without writing a commercial snapshot."""

    TENANT_NOT_FOUND = "tenant_not_found"
    INVOICE_DRAFT_NOT_FOUND = "invoice_draft_not_found"
    REQUEST_INVALID = "request_invalid"


class IssuedInvoicePresentmentQueryError(ValueError):
    """Raised when a stored issued invoice cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class IssuedInvoiceVoidOutcomeCode(StrEnum):
    """Terminal result of attempting to persist one commercial issued-invoice void."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class IssuedInvoiceVoidRejectionReasonCode(StrEnum):
    """Why a void request was refused without writing a commercial void."""

    TENANT_NOT_FOUND = "tenant_not_found"
    ISSUED_INVOICE_NOT_FOUND = "issued_invoice_not_found"
    PAYMENT_RECEIPT_EXISTS = "payment_receipt_exists"
    CREDIT_NOTE_ALREADY_APPLIED = "credit_note_already_applied"
    COLLECTION_WRITE_OFF_EXISTS = "collection_write_off_exists"
    UNAPPLIED_CASH_ALREADY_APPLIED = "unapplied_cash_already_applied"
    COLLECTION_CASE_SETTLED = "collection_case_settled"
    COLLECTION_CASE_DISPUTED = "collection_case_disputed"
    OUTSTANDING_MISMATCH = "outstanding_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    REQUEST_INVALID = "request_invalid"


class IssuedInvoiceVoidPresentmentQueryError(ValueError):
    """Raised when a stored issued-invoice void cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class IssuedCreditNoteOutcomeCode(StrEnum):
    """Terminal result of attempting to persist one commercial issued credit note."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class IssuedCreditNoteRejectionReasonCode(StrEnum):
    """Why an issue request was refused without writing a commercial snapshot."""

    TENANT_NOT_FOUND = "tenant_not_found"
    CREDIT_ADJUSTMENT_NOT_FOUND = "credit_adjustment_not_found"
    REQUEST_INVALID = "request_invalid"


class IssuedCreditNotePresentmentQueryError(ValueError):
    """Raised when a stored issued credit note cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class IssuedCreditNoteVoidOutcomeCode(StrEnum):
    """Terminal result of attempting to persist one commercial credit-note void."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class IssuedCreditNoteVoidRejectionReasonCode(StrEnum):
    """Why a void request was refused without writing a commercial void."""

    TENANT_NOT_FOUND = "tenant_not_found"
    ISSUED_CREDIT_NOTE_NOT_FOUND = "issued_credit_note_not_found"
    CREDIT_NOTE_ALREADY_APPLIED = "credit_note_already_applied"
    CURRENCY_MISMATCH = "currency_mismatch"
    REQUEST_INVALID = "request_invalid"


class IssuedCreditNoteVoidPresentmentQueryError(ValueError):
    """Raised when a stored issued-credit-note void cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class CreditNoteApplicationOutcomeCode(StrEnum):
    """Terminal result of applying one issued credit note to a collection case."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class CreditNoteApplicationRejectionReasonCode(StrEnum):
    """Why an apply request was refused without reducing outstanding."""

    TENANT_NOT_FOUND = "tenant_not_found"
    ISSUED_CREDIT_NOTE_NOT_FOUND = "issued_credit_note_not_found"
    COLLECTION_CASE_NOT_FOUND = "collection_case_not_found"
    CURRENCY_MISMATCH = "currency_mismatch"
    COLLECTION_CASE_SETTLED = "collection_case_settled"
    COLLECTION_CASE_DISPUTED = "collection_case_disputed"
    CREDIT_EXCEEDS_OUTSTANDING = "credit_exceeds_outstanding"
    INVOICE_MISMATCH = "invoice_mismatch"
    ISSUED_CREDIT_NOTE_VOIDED = "issued_credit_note_voided"
    REQUEST_INVALID = "request_invalid"


class CollectionCaseSettlementOutcomeCode(StrEnum):
    """Terminal result of settling one collection case at exact-zero outstanding."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class CollectionCaseSettlementRejectionReasonCode(StrEnum):
    """Why a settle request was refused without flipping case status."""

    TENANT_NOT_FOUND = "tenant_not_found"
    COLLECTION_CASE_NOT_FOUND = "collection_case_not_found"
    OUTSTANDING_NOT_ZERO = "outstanding_not_zero"
    COLLECTION_CASE_SETTLED = "collection_case_settled"
    COLLECTION_CASE_VOIDED = "collection_case_voided"
    COLLECTION_CASE_DISPUTED = "collection_case_disputed"
    REQUEST_INVALID = "request_invalid"


class CollectionCaseSettlementPresentmentQueryError(ValueError):
    """Raised when a stored collection-case settlement cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class CollectionWriteOffOutcomeCode(StrEnum):
    """Terminal result of writing off leftover collection remaining."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class CollectionWriteOffRejectionReasonCode(StrEnum):
    """Why a write-off request was refused without changing outstanding."""

    TENANT_NOT_FOUND = "tenant_not_found"
    COLLECTION_CASE_NOT_FOUND = "collection_case_not_found"
    COLLECTION_CASE_SETTLED = "collection_case_settled"
    COLLECTION_CASE_DISPUTED = "collection_case_disputed"
    OUTSTANDING_ALREADY_ZERO = "outstanding_already_zero"
    OUTSTANDING_NEGATIVE = "outstanding_negative"
    CURRENCY_MISMATCH = "currency_mismatch"
    WRITE_OFF_AMOUNT_MISMATCH = "write_off_amount_mismatch"
    REQUEST_INVALID = "request_invalid"


class CollectionWriteOffPresentmentQueryError(ValueError):
    """Raised when a stored collection write-off cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class CollectionDisputeOutcomeCode(StrEnum):
    """Terminal result of holding one collection case as disputed."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class CollectionDisputeRejectionReasonCode(StrEnum):
    """Why a dispute hold was refused without changing case status."""

    TENANT_NOT_FOUND = "tenant_not_found"
    COLLECTION_CASE_NOT_FOUND = "collection_case_not_found"
    COLLECTION_CASE_DISPUTED = "collection_case_disputed"
    COLLECTION_CASE_SETTLED = "collection_case_settled"
    COLLECTION_CASE_VOIDED = "collection_case_voided"
    CURRENCY_MISMATCH = "currency_mismatch"
    COLLECTION_DISPUTE_RELEASED = "collection_dispute_released"
    REQUEST_INVALID = "request_invalid"


class CollectionDisputePresentmentQueryError(ValueError):
    """Raised when a stored collection dispute cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class CollectionDisputeReleaseOutcomeCode(StrEnum):
    """Terminal result of releasing one held collection dispute."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class CollectionDisputeReleaseRejectionReasonCode(StrEnum):
    """Why a dispute release was refused without changing case status."""

    TENANT_NOT_FOUND = "tenant_not_found"
    COLLECTION_DISPUTE_NOT_FOUND = "collection_dispute_not_found"
    COLLECTION_DISPUTE_NOT_HELD = "collection_dispute_not_held"
    COLLECTION_CASE_NOT_FOUND = "collection_case_not_found"
    COLLECTION_CASE_SETTLED = "collection_case_settled"
    COLLECTION_CASE_VOIDED = "collection_case_voided"
    CURRENCY_MISMATCH = "currency_mismatch"
    REQUEST_INVALID = "request_invalid"


class CollectionDisputeReleasePresentmentQueryError(ValueError):
    """Raised when a stored collection-dispute release cannot be presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class UnappliedCashOutcomeCode(StrEnum):
    """Terminal result of parking leftover remittance against a receipt."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class UnappliedCashRejectionReasonCode(StrEnum):
    """Why leftover parking was refused without writing a new money fact."""

    TENANT_NOT_FOUND = "tenant_not_found"
    PAYMENT_RECEIPT_NOT_FOUND = "payment_receipt_not_found"
    PAYMENT_RECEIPT_ALREADY_CONSUMED = "payment_receipt_already_consumed"
    UNAPPLIED_AMOUNT_ZERO = "unapplied_amount_zero"
    UNAPPLIED_AMOUNT_NEGATIVE = "unapplied_amount_negative"
    UNAPPLIED_AMOUNT_EXCEEDS_RECEIPT = "unapplied_amount_exceeds_receipt"
    CURRENCY_MISMATCH = "currency_mismatch"
    REQUEST_INVALID = "request_invalid"


class UnappliedCashPresentmentQueryError(ValueError):
    """Raised when stored unapplied cash cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class UnappliedCashApplicationOutcomeCode(StrEnum):
    """Terminal result of applying parked leftover to one collection case."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class UnappliedCashApplicationRejectionReasonCode(StrEnum):
    """Why leftover apply was refused without reducing outstanding."""

    TENANT_NOT_FOUND = "tenant_not_found"
    UNAPPLIED_CASH_NOT_FOUND = "unapplied_cash_not_found"
    COLLECTION_CASE_NOT_FOUND = "collection_case_not_found"
    COLLECTION_CASE_SETTLED = "collection_case_settled"
    COLLECTION_CASE_DISPUTED = "collection_case_disputed"
    UNAPPLIED_CASH_ALREADY_REFUNDED = "unapplied_cash_already_refunded"
    CURRENCY_MISMATCH = "currency_mismatch"
    APPLIED_AMOUNT_ZERO = "applied_amount_zero"
    APPLIED_AMOUNT_NEGATIVE = "applied_amount_negative"
    APPLIED_AMOUNT_MISMATCH = "applied_amount_mismatch"
    APPLIED_AMOUNT_EXCEEDS_PARKED = "applied_amount_exceeds_parked"
    APPLIED_AMOUNT_EXCEEDS_OUTSTANDING = "applied_amount_exceeds_outstanding"
    OUTSTANDING_NEGATIVE = "outstanding_negative"
    REQUEST_INVALID = "request_invalid"


class UnappliedCashApplicationPresentmentQueryError(ValueError):
    """Raised when a stored leftover application cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class UnappliedCashRefundOutcomeCode(StrEnum):
    """Terminal result of refunding parked leftover remittance."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class UnappliedCashRefundRejectionReasonCode(StrEnum):
    """Why leftover refund was refused without writing a new money fact."""

    TENANT_NOT_FOUND = "tenant_not_found"
    UNAPPLIED_CASH_NOT_FOUND = "unapplied_cash_not_found"
    UNAPPLIED_CASH_ALREADY_APPLIED = "unapplied_cash_already_applied"
    UNAPPLIED_CASH_NOT_PARKED = "unapplied_cash_not_parked"
    CURRENCY_MISMATCH = "currency_mismatch"
    REFUND_AMOUNT_ZERO = "refund_amount_zero"
    REFUND_AMOUNT_NEGATIVE = "refund_amount_negative"
    REFUND_AMOUNT_MISMATCH = "refund_amount_mismatch"
    REQUEST_INVALID = "request_invalid"


class UnappliedCashRefundPresentmentQueryError(ValueError):
    """Raised when a stored leftover refund cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class CreditNoteApplicationPresentmentQueryError(ValueError):
    """Raised when a stored credit-note application cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


def require_resolved(value: ResolvedValue | None, name: str) -> ResolvedValue:
    """Return a resolved row or raise when a hollow success leaked through.

    Production paths must not use ``assert`` for this check.  ``-O`` would
    strip that guard and allow a None tenant or fact to continue.
    """
    if value is None:
        raise ValueError(f"{name} resolution succeeded without a stored {name}")
    return value


class DunningEventPresentmentQueryError(ValueError):
    """Raised when a stored collection dunning event cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class CollectionCasePresentmentQueryError(ValueError):
    """Raised when a stored collection case cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class CollectionAgingPresentmentQueryError(ValueError):
    """Raised when collection aging cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class AccountStatementPresentmentQueryError(ValueError):
    """Raised when a billing-account statement cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class RatedSpendPresentmentQueryError(ValueError):
    """Raised when already-rated spend cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class PaymentIntentPresentmentQueryError(ValueError):
    """Raised when a stored payment intent cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class PaymentReceiptPresentmentQueryError(ValueError):
    """Raised when a stored payment receipt cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class CreditAdjustmentPresentmentQueryError(ValueError):
    """Raised when a stored credit adjustment cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class TenantApiCredentialOutcomeCode(StrEnum):
    """Terminal result of issuing or revoking one tenant API credential."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class TenantApiCredentialRejectionReasonCode(StrEnum):
    """Why a credential command was refused without minting a secret."""

    TENANT_NOT_FOUND = "tenant_not_found"
    CREDENTIAL_LABEL_INVALID = "credential_label_invalid"
    API_CREDENTIAL_NOT_FOUND = "api_credential_not_found"
    API_CREDENTIAL_MISSING = "api_credential_missing"
    API_CREDENTIAL_INVALID = "api_credential_invalid"


class TenantApiCredentialPresentmentQueryError(ValueError):
    """Raised when a stored API credential cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class TenantApiCredentialQueryError(ValueError):
    """Raised when a stored API credential cannot be authorized or decoded."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class WebhookSubscriptionOutcomeCode(StrEnum):
    """Terminal result of registering or revoking one webhook subscription."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class WebhookSubscriptionRejectionReasonCode(StrEnum):
    """Why a webhook command was refused without writing a subscription."""

    TENANT_NOT_FOUND = "tenant_not_found"
    WEBHOOK_CALLBACK_URL_INSECURE = "webhook_callback_url_insecure"
    WEBHOOK_EVENT_TYPE_UNKNOWN = "webhook_event_type_unknown"
    WEBHOOK_SUBSCRIPTION_NOT_FOUND = "webhook_subscription_not_found"


class WebhookSubscriptionQueryError(ValueError):
    """Raised when a stored webhook subscription cannot be authorized or decoded."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class WebhookSubscriptionPresentmentQueryError(ValueError):
    """Raised when a stored webhook subscription cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class WebhookDeliveryOutcomeCode(StrEnum):
    """Terminal result of one explicit webhook delivery run."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


class WebhookDeliveryRejectionReasonCode(StrEnum):
    """Why an explicit delivery run was refused without posting callbacks."""

    TENANT_NOT_FOUND = "tenant_not_found"


class WebhookDeliveryPresentmentQueryError(ValueError):
    """Raised when a stored webhook delivery cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class WebhookOutboxEventPresentmentQueryError(ValueError):
    """Raised when a stored webhook outbox event cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class JournalProposalOutcomeCode(StrEnum):
    """Terminal result of attempting to persist one accounting journal proposal."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class JournalProposalRejectionReasonCode(StrEnum):
    """Why a proposal request was refused without writing a journal export."""

    TENANT_NOT_FOUND = "tenant_not_found"
    INVOICE_DRAFT_NOT_FOUND = "invoice_draft_not_found"
    DRAFT_TOTAL_INVALID = "draft_total_invalid"
    PAYMENT_RECEIPT_NOT_FOUND = "payment_receipt_not_found"
    RECEIPT_AMOUNT_INVALID = "receipt_amount_invalid"
    COLLECTION_WRITE_OFF_NOT_FOUND = "collection_write_off_not_found"
    WRITE_OFF_AMOUNT_INVALID = "write_off_amount_invalid"
    UNAPPLIED_CASH_REFUND_NOT_FOUND = "unapplied_cash_refund_not_found"
    REFUND_AMOUNT_INVALID = "refund_amount_invalid"
    UNAPPLIED_CASH_NOT_FOUND = "unapplied_cash_not_found"
    UNAPPLIED_CASH_NOT_PARKED = "unapplied_cash_not_parked"
    UNAPPLIED_AMOUNT_INVALID = "unapplied_amount_invalid"
    UNAPPLIED_CASH_APPLICATION_NOT_FOUND = "unapplied_cash_application_not_found"
    APPLIED_AMOUNT_INVALID = "applied_amount_invalid"
    CREDIT_ADJUSTMENT_NOT_FOUND = "credit_adjustment_not_found"
    CREDIT_AMOUNT_INVALID = "credit_amount_invalid"
    ISSUED_INVOICE_VOID_NOT_FOUND = "issued_invoice_void_not_found"
    ISSUED_CREDIT_NOTE_VOID_NOT_FOUND = "issued_credit_note_void_not_found"
    CREDIT_JOURNAL_NOT_FOUND = "credit_journal_not_found"
    CREDIT_NOTE_ALREADY_APPLIED = "credit_note_already_applied"
    VOIDED_AMOUNT_INVALID = "voided_amount_invalid"
    JOURNAL_LINE_AMOUNT_INVALID = "journal_line_amount_invalid"
    CURRENCY_MISMATCH = "currency_mismatch"


class JournalProposalQueryError(ValueError):
    """Raised when a proposal query cannot be authorized or decoded."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class PostingReceiptObservationOutcomeCode(StrEnum):
    """Terminal result of pulling one AIS posting receipt as an observation."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class PostingReceiptObservationRejectionReasonCode(StrEnum):
    """Why a posting-receipt pull was refused without writing an observation."""

    TENANT_NOT_FOUND = "tenant_not_found"
    IDEMPOTENCY_KEY_MISSING = "idempotency_key_missing"
    CROSS_TENANT = "cross_tenant"
    NOT_YET_ACCEPTED = "not_yet_accepted"
    RECEIPT_INVALID = "receipt_invalid"
    TENANT_MISMATCH = "tenant_mismatch"
    TRANSPORT_FAILURE = "transport_failure"
    OBSERVATION_CONFLICT = "observation_conflict"
    AIS_ENDPOINT_UNCONFIGURED = "ais_endpoint_unconfigured"


class PostingReceiptObservationQueryError(ValueError):
    """Raised when a stored observation cannot be authorized or decoded."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class PostingReceiptObservationPresentmentQueryError(ValueError):
    """Raised when a stored observation cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class AisOutboxDrainOutcomeCode(StrEnum):
    """Terminal result of one explicit AIS outbox drain."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


class AisOutboxDrainRejectionReasonCode(StrEnum):
    """Why an AIS outbox drain was refused without writing observations."""

    TENANT_NOT_FOUND = "tenant_not_found"
    AIS_ENDPOINT_UNCONFIGURED = "ais_endpoint_unconfigured"
    AIS_BASE_URL_INSECURE = "ais_base_url_insecure"
    AIS_OUTBOX_INVALID = "ais_outbox_invalid"
    CROSS_TENANT = "cross_tenant"
    TRANSPORT_FAILURE = "transport_failure"


class CollectionCaseOutcomeCode(StrEnum):
    """Terminal result of attempting to persist one collection case or notice."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class CollectionCaseRejectionReasonCode(StrEnum):
    """Why a collection request was refused without capturing payment."""

    TENANT_NOT_FOUND = "tenant_not_found"
    INVOICE_DRAFT_NOT_FOUND = "invoice_draft_not_found"
    COLLECTION_CASE_NOT_FOUND = "collection_case_not_found"
    OUTSTANDING_AMOUNT_INVALID = "outstanding_amount_invalid"
    DUNNING_NOTICE_INVALID = "dunning_notice_invalid"
    COLLECTION_CASE_DISPUTED = "collection_case_disputed"


class PaymentIntentOutcomeCode(StrEnum):
    """Terminal result of attempting to persist one payment intent."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class PaymentIntentRejectionReasonCode(StrEnum):
    """Why a payment-intent request was refused without capturing money."""

    TENANT_NOT_FOUND = "tenant_not_found"
    COLLECTION_CASE_NOT_FOUND = "collection_case_not_found"
    PAYMENT_AMOUNT_INVALID = "payment_amount_invalid"


class PaymentSettlementOutcomeCode(StrEnum):
    """Terminal result of recording a receipt or cancelling a projected intent."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class CreditAdjustmentOutcomeCode(StrEnum):
    """Terminal result of recording one commercial credit adjustment."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class CreditAdjustmentRejectionReasonCode(StrEnum):
    """Why a credit request was refused without writing a credit or proposal."""

    TENANT_NOT_FOUND = "tenant_not_found"
    INVOICE_DRAFT_NOT_FOUND = "invoice_draft_not_found"
    CREDIT_AMOUNT_INVALID = "credit_amount_invalid"
    CREDIT_REASON_INVALID = "credit_reason_invalid"
    CREDIT_EXCEEDS_REMAINING = "credit_exceeds_remaining"
    CREDIT_EXCEEDS_OUTSTANDING = "credit_exceeds_outstanding"
    TAX_SPLIT_INVALID = "tax_split_invalid"


class CreditAdjustmentQueryError(ValueError):
    """Raised when a stored credit adjustment cannot be authorized or decoded."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class SpendBudgetOutcomeCode(StrEnum):
    """Terminal result of publishing one commercial spend budget."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class SpendBudgetOverSignalOutcomeCode(StrEnum):
    """Terminal result of observing one published spend budget for over."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class SpendBudgetApproachingSignalOutcomeCode(StrEnum):
    """Terminal result of observing one published spend budget for approaching."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class SpendBudgetRejectionReasonCode(StrEnum):
    """Why a spend-budget publish was refused without writing a budget."""

    TENANT_NOT_FOUND = "tenant_not_found"
    BILLING_ACCOUNT_NOT_FOUND = "billing_account_not_found"
    BILLING_ACCOUNT_FORBIDDEN = "billing_account_forbidden"
    BUDGET_AMOUNT_INVALID = "budget_amount_invalid"
    CURRENCY_INVALID = "currency_invalid"
    REQUEST_INVALID = "request_invalid"
    SPEND_BUDGET_NOT_FOUND = "spend_budget_not_found"


class SpendBudgetQueryError(ValueError):
    """Raised when a stored spend budget cannot be authorized or decoded."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class SpendBudgetPresentmentQueryError(ValueError):
    """Raised when a stored spend budget cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class SpendBudgetEvaluationPresentmentQueryError(ValueError):
    """Raised when a spend-budget evaluation cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class SpendBudgetOverSignalPresentmentQueryError(ValueError):
    """Raised when an over-signal observation cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class SpendBudgetApproachingSignalPresentmentQueryError(ValueError):
    """Raised when an approaching-signal observation cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class SpendAuthorizationOutcomeCode(StrEnum):
    """Terminal result of an authorization, commitment, or release command."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class SpendAuthorizationRejectionReasonCode(StrEnum):
    """Why a spend-control command was refused without consuming exposure."""

    TENANT_NOT_FOUND = "tenant_not_found"
    BILLING_ACCOUNT_NOT_FOUND = "billing_account_not_found"
    BILLING_ACCOUNT_FORBIDDEN = "billing_account_forbidden"
    SPEND_BUDGET_NOT_FOUND = "spend_budget_not_found"
    AUTHORIZATION_NOT_FOUND = "spend_authorization_not_found"
    AUTHORIZATION_FORBIDDEN = "spend_authorization_forbidden"
    AUTHORIZATION_STATUS_INVALID = "authorization_status_invalid"
    AUTHORIZATION_EXPIRED = "authorization_expired"
    AUTHORIZATION_EXPOSURE_EXCEEDED = "authorization_exposure_exceeded"
    IDEMPOTENCY_KEY_INVALID = "idempotency_key_invalid"
    IDEMPOTENCY_KEY_CONFLICT = "idempotency_key_conflict"
    ACTOR_REFERENCE_INVALID = "actor_reference_invalid"
    PURPOSE_INVALID = "purpose_invalid"
    POLICY_VERSION_INVALID = "policy_version_invalid"
    REQUESTED_AMOUNT_INVALID = "requested_amount_invalid"
    COMMITMENT_AMOUNT_INVALID = "commitment_amount_invalid"
    COMMITMENT_AMOUNT_EXCEEDED = "commitment_amount_exceeded"
    RELEASE_AMOUNT_INVALID = "release_amount_invalid"
    RELEASE_AMOUNT_EXCEEDED = "release_amount_exceeded"
    VALIDITY_WINDOW_INVALID = "validity_window_invalid"
    REQUEST_INVALID = "request_invalid"


class SpendAuthorizationQueryError(ValueError):
    """Raised when an authorization cannot be authorized or presented."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


class PaymentSettlementRejectionReasonCode(StrEnum):
    """Why a settlement request was refused without capturing money or posting."""

    TENANT_NOT_FOUND = "tenant_not_found"
    PAYMENT_INTENT_NOT_FOUND = "payment_intent_not_found"
    PAYMENT_INTENT_NOT_PROJECTED = "payment_intent_not_projected"
    PAYMENT_AMOUNT_INVALID = "payment_amount_invalid"
    PAYMENT_AMOUNT_EXCEEDS_OUTSTANDING = "payment_amount_exceeds_outstanding"
    COLLECTION_CASE_DISPUTED = "collection_case_disputed"


class ExactDecimalError(ValueError):
    """Raised when a quantity cannot be treated as an exact non-negative decimal."""


class JournalLineAmountScaleError(ValueError):
    """Raised when a journal line cannot be represented with six fractional digits."""


class TimeWindowError(ValueError):
    """Raised when a time window or timestamp violates ISO 8601 timezone rules."""
