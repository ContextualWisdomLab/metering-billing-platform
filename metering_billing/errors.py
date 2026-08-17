"""Typed outcomes and rejection reasons for usage, rating, drafts, exports, collections, intents, settlement, and observations.

Reason codes are stable operational vocabulary.  They are safe to persist in
audit receipts and do not require masking: they describe control failures, not
customer content.
"""

from __future__ import annotations

from enum import StrEnum


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


class TenantApiCredentialQueryError(ValueError):
    """Raised when a stored API credential cannot be authorized or decoded."""

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


class PaymentSettlementRejectionReasonCode(StrEnum):
    """Why a settlement request was refused without capturing money or posting."""

    TENANT_NOT_FOUND = "tenant_not_found"
    PAYMENT_INTENT_NOT_FOUND = "payment_intent_not_found"
    PAYMENT_INTENT_NOT_PROJECTED = "payment_intent_not_projected"
    PAYMENT_AMOUNT_INVALID = "payment_amount_invalid"
    PAYMENT_AMOUNT_EXCEEDS_OUTSTANDING = "payment_amount_exceeds_outstanding"


class ExactDecimalError(ValueError):
    """Raised when a quantity cannot be treated as an exact non-negative decimal."""


class TimeWindowError(ValueError):
    """Raised when a time window or timestamp violates ISO 8601 timezone rules."""
