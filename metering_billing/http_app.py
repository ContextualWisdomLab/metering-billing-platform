"""Stdlib HTTP accept surface for the already-built commercial billing path.

The application is a thin WSGI adapter:

1. Parse JSON and require a tenant on every write.  Accept optional
   ``X-CWL-Tenant-Reference``; body or query ``tenant_reference`` still works
   when the header is absent.
2. Call the existing in-process services.
3. Return each service ``as_contract_dict`` result as JSON.
4. Let AIS pull persisted journal proposals with GET.  Query never mutates
   ``proposal_status``.
5. Let an operator POST a posting-receipt pull and GET a stored observation.
   The pull stores AIS ``posting_status_code`` without flipping Billing
   ``proposal_status``.
6. Let a buyer POST a commercial credit adjustment and GET the stored
   credit as a commercial statement.  The write records the existing #17
   credit against ``invoice_draft_id`` and refuses PAN and provider secrets.
   Record the credit; AIS pulls the validated journal.  The read does not
   post, call AIS, or start a web UI.
7. Let a buyer POST a rate card and GET the stored catalog as a commercial
   statement.  The write publishes the existing #18 card and refuses PAN
   and provider secrets.  Publish a rate card, then rate a window against
   that version.  Version list and version-item GET stay the #18 catalog
   reads.  The presentment read does not invent a catalog or start a web UI.
   Let a buyer GET a stored usage event as a commercial statement.  The
   write stays the existing #5 ingest and refuses PAN and provider secrets.
   Ingest usage, then rate a window against a published card.
   Let a buyer GET a stored rating run as a commercial statement.  The
   write stays the existing #7 rate-a-window command and refuses PAN and
   provider secrets.  Rate a window, then draft an invoice.
   Let a buyer GET a stored tax assessment as a commercial statement.  The
   write stays the existing #19 assess command and refuses PAN and
   provider secrets.  Publish a tax rate, assess the draft, then propose
   the journal and let AIS pull.
   Let a buyer GET a stored posting-receipt observation as a commercial
   statement.  The write stays the existing #16 pull and refuses PAN and
   provider secrets.  Drain AIS outbox, then store the receipt
   observation.
8. Let a buyer POST a tax rate, assess a draft, and GET those records.
   Publish a tax rate, assess the draft, then propose the journal and let
   AIS pull.  AIS must map ``tax_payable``.
    9. Let an operator GET a stored invoice draft as a commercial statement.
   Open the draft statement, then collect or credit.  The read does not
   post, call AIS, or start a web UI.
    9a. Let an operator POST an issued commercial invoice snapshot from a
    stored invoice draft, then GET that snapshot.  Replay of the same
    tenant and draft returns the stored ``issued_invoice_id``.  Refuse
    PAN, CVC, and provider secrets.  Do not invent statutory numbering,
    capture payment, enqueue a webhook, or call AIS.
    9b. Let an operator POST an issued commercial credit-note snapshot from
    a stored credit adjustment, then GET that snapshot.  Replay of the
    same tenant and credit returns the stored ``issued_credit_note_id``.
    Refuse PAN, CVC, and provider secrets.  First successful issue
    enqueues one existing ``credit_note.issued`` outbox event.      Issue
    the credit note; the validated journal remains available for AIS.
    Do not invent statutory numbering, capture payment, or call AIS.
    ``POST /v1/issued-credit-notes/{issued_credit_note_id}/voids`` records
    one commercial void of an unused issued credit note.  Replay of the
    same tenant and issued credit note returns the stored
    ``issued_credit_note_void_id``.  Collection remaining is unchanged
    because the note was never applied.  GET item and list present the
    stored void.  First successful void enqueues one existing
    ``credit_note.voided`` outbox event.  The void write does not compose
    a journal or call AIS.
    ``POST /v1/issued-credit-note-voids/{issued_credit_note_void_id}/journal-proposals``
    is the explicit later compose.  AIS pull stays existing GET
    journal-proposal routes.
    10. Let an operator issue a tenant API credential.  After one active key
    exists, every ``/v1`` call except credential issue requires that key.
    GET one stored credential or list ``{tenant_api_credentials,
    next_cursor}``.  Issue a key, then send it on every ``/v1`` call;
    revoke when leaked.
    11. Let an operator register an https webhook callback, then run deliveries.
    GET one stored ``webhook_subscription`` or list
    ``{webhook_subscriptions, next_cursor}``.  GET one stored
    ``webhook_outbox_event`` or list ``{webhook_outbox_events,
    next_cursor}``.  GET one stored ``webhook_delivery_attempt`` or list
    ``{webhook_deliveries, next_cursor}``.  AIS may keep polling journal
    proposals.  This path does not flip ``proposal_status`` or call AIS
    posting-receipt.
12. Let an operator drain AIS ``posting_receipt`` outbox events.  Empty
    unpublished pages skip receipt GETs.  Matched rows use the stored
    Billing idempotency key, never the payload URN.  Observations stay
    observations; ``proposal_status`` stays ``validated``.
    13. Let an operator GET a stored collection case as a commercial statement.
    Open the collection case, then collect or credit.  The read does not
    post, call AIS, capture payment, or start a web UI.
    ``GET /v1/collection-aging`` projects open-case remaining into current /
    1-30 / 31-60 / 61-90 / 90+ buckets grouped by currency.
    ``GET /v1/billing-accounts/{billing_account_id}/statement`` projects
    stored issued-invoice totals, unused issued-invoice voids, open
    collection remaining, applied credits, unused issued-credit-note
    voids, write-offs, parked leftover, and refunded leftover for one
    billing account, grouped by currency.  Missing account is HTTP 404.
    ``GET /v1/billing-accounts/{billing_account_id}/rated-spend`` projects
    already-stored rating-run and exclusive invoice-draft line amounts
    for one billing account and half-open window, grouped by
    ``product_code``.  Optional ``group_by=project`` adds
    ``project_reference`` from stored exclusive-account usage.
    Optional ``group_by=credential`` adds ``credential_reference`` from
    stored exclusive-account usage.      Optional ``group_by=principal``
    adds ``billing_principal_reference`` from stored exclusive-account
    usage.  Optional ``group_by=cost_center`` adds
    ``cost_center_reference`` from stored exclusive-account usage.  The
    read does not re-rate or write money.
    ``POST /v1/billing-accounts/{billing_account_id}/spend-budgets``
    publishes one append-only commercial ``spend_budget`` for that
    same-tenant account and half-open window.  Replay of the same
    identity returns the stored ``spend_budget_id``.      GET item and list
    present the stored row.  The write does not compare rated spend,
    stop rating, or compose a journal.
    ``GET /v1/spend-budgets/{spend_budget_id}/evaluation`` compares that
    published budget to already-rated spend for the same tenant, billing
    account, half-open window, and currency.  Same tenant is HTTP 200.
    Unknown or cross-tenant is HTTP 404.  Missing tenant pin is HTTP 422.
    A budget whose billing account belongs to another commercial
    ``tenant_account`` is HTTP 403.  The read does not persist, hard-stop,
    or compose a journal.
    ``POST /v1/spend-budgets/{spend_budget_id}/over-signal`` observes that
    published budget with the same remaining/over math and enqueues one
    existing ``spend_budget.over`` outbox event when utilization is
    ``over``.  Same tenant is HTTP 200.  Unknown or cross-tenant is HTTP
    404.  Missing tenant pin is HTTP 422.  A budget whose billing account
    belongs to another commercial ``tenant_account`` is HTTP 403.  under
    and at write zero over-signal rows.  The write does not persist an
    evaluation snapshot, hard-stop, or compose a journal.
    ``GET /v1/spend-budgets/{spend_budget_id}/over-signal`` presents the
    live over-signal envelope plus zero or one stored ``spend_budget.over``
    webhook-outbox presentment.  Same tenant is HTTP 200.  Unknown or
    cross-tenant is HTTP 404.  Missing tenant pin is HTTP 422.  A budget
    whose billing account belongs to another commercial ``tenant_account``
    is HTTP 403.  The read does not enqueue, persist, hard-stop, or
    compose a journal.
    ``GET /v1/billing-accounts/{billing_account_id}/budget-status`` evaluates
    every published commercial ``spend_budget`` on that same-tenant account.
    The envelope is ``{budget_statuses, next_cursor}``.  Same tenant is
    HTTP 200.  Unknown billing account is HTTP 404.  Cross-tenant account
    is HTTP 403.  Missing tenant pin is HTTP 422.  Unknown or cross-tenant
    budgets are omitted with no leak.  The read does not persist, hard-stop,
    or compose a journal.
    ``POST /v1/issued-invoices/{issued_invoice_id}/voids`` records one
    commercial void of an unused issued invoice.  Replay of the same
    tenant and issued invoice returns the stored
    ``issued_invoice_void_id``.  An unused open or dunning collection
    case closes as ``voided``.  First successful void enqueues one
    existing ``invoice.voided`` outbox event.  GET item and list present
    the stored void.  The void write does not compose a journal.
    ``POST /v1/issued-invoice-voids/{issued_invoice_void_id}/journal-proposals``
    is the explicit later compose.  AIS pull stays existing GET
    journal-proposal routes.  Do not invent a second webhook system,
    refund, write-off rewrite, settlement, statutory numbering, or AIS
    call.
    Cross-tenant account is HTTP 403.  GET one stored
    ``collection_dunning_event`` or list ``{dunning_events, next_cursor}``.
    ``POST /v1/collection-cases/{collection_case_id}/dunning-events`` stays
    the #10 record.  The read does not send mail or capture payment.
    ``POST /v1/collection-cases/{collection_case_id}/credit-note-applications``
    applies one issued credit note onto that open case.  Replay of the
    same tenant and issued credit note returns the stored
    ``credit_note_application_id``.  First successful apply enqueues
    one existing ``credit_note.applied`` outbox event.  GET item and list
    present the stored application.  Do not invent a journal, tax unwind,
    write-off, settlement, statutory numbering, or payment capture.
    ``POST /v1/collection-cases/{collection_case_id}/settlements``
    settles one same-tenant open case whose remaining outstanding is
    exact zero.  Replay of the same tenant and case returns the stored
    ``collection_case_settlement_id``.  First successful settle enqueues
    one existing ``collection.settled`` outbox event.  GET item and list
    present the stored settlement.  Do not invent a journal, tax unwind,
    write-off, statutory numbering, or payment capture.
    ``POST /v1/collection-cases/{collection_case_id}/write-offs``
    writes off leftover remaining outstanding on one same-tenant open
    case.  Replay of the same tenant and case returns the stored
    ``collection_write_off_id`` and never re-zeros outstanding.  First
    successful write-off enqueues one existing ``write_off.recorded``
    outbox event.  GET item and list present the stored write-off.
    ``POST /v1/collection-write-offs/{collection_write_off_id}/journal-proposals``
    composes one existing ``accounting_journal_proposal`` from that
    write-off.  Replay of the same tenant and write-off returns the
    stored ``proposal_id``.  AIS pulls the validated proposal through
    existing GET journal-proposal routes.  Do not invent a tax unwind,
    settlement, statutory numbering, payment capture, or AIS call.
    After write-off, compose the journal, then settle at exact zero.
    ``POST /v1/collection-cases/{collection_case_id}/disputes``
    holds one same-tenant open or dunning case as ``disputed``.
    Replay of the same tenant and case returns the stored
    ``collection_dispute_id`` and never changes remaining outstanding.
    First successful hold enqueues one existing ``dispute.held`` outbox
    event.  GET item and list present the stored hold.  New dunning fails
    closed while held.  ``POST /v1/collection-disputes/{collection_dispute_id}/releases``
    releases one held dispute in place.  Replay of the same tenant and
    dispute returns the stored ``collection_dispute_id`` and never changes
    remaining outstanding.  First successful release enqueues one existing
    ``dispute.released`` outbox event.  Case status returns to ``open`` or
    ``dunning``.  GET release item and list present the stored release.  Do
    not invent a journal, second webhook system, write-off, settlement,
    void rewrite, statutory numbering, payment capture, or AIS call.
    ``POST /v1/credit-adjustments/{credit_adjustment_id}/journal-proposals``
    composes or replays the existing credit ``accounting_journal_proposal``.
    Credit accept already writes that journal.  Replay of the same tenant
    and credit returns the stored ``proposal_id``.  AIS pulls through
    existing GET journal-proposal routes.  Do not invent a second journal
    store, tax unwind, webhook, statutory numbering, or AIS call.
    ``POST /v1/unapplied-cash-refunds/{unapplied_cash_refund_id}/journal-proposals``
    composes one existing ``accounting_journal_proposal`` from a stored
    leftover refund.  Replay of the same tenant and refund returns the
    stored ``proposal_id``.  AIS pulls the validated proposal through
    existing GET journal-proposal routes.  Do not invent a statutory
    account ID, webhook type, PSP, write-off, settlement, or AIS call.
    ``POST /v1/unapplied-cash/{unapplied_cash_id}/journal-proposals``
    composes one existing ``accounting_journal_proposal`` from a stored
    parked leftover.  Replay of the same tenant and leftover returns the
    stored ``proposal_id``.  AIS pulls the validated proposal through
    existing GET journal-proposal routes.  Do not invent a statutory
    account ID, webhook type, PSP, write-off, settlement, refund rewrite,
    or AIS call.
    ``POST /v1/unapplied-cash-applications/{unapplied_cash_application_id}/journal-proposals``
    composes one existing ``accounting_journal_proposal`` from a stored
    leftover apply.  Replay of the same tenant and application returns
    the stored ``proposal_id``.  AIS pulls the validated proposal through
    existing GET journal-proposal routes.  Do not invent a statutory
    account ID, webhook type, PSP, write-off, settlement, park rewrite,
    refund rewrite, or AIS call.
14. Let an operator POST a projected payment intent and GET the stored
    intent as a commercial statement.  Create a projected payment intent,
    then record the receipt.  The write refuses PAN and provider secrets.
    The read does not capture, settle, call AIS, or start a web UI.
15. Let an operator POST an applied payment receipt and GET the stored
    receipt as a commercial statement.  Record the receipt; the cash
    journal is already validated for AIS to pull.      The write refuses PAN
    and provider secrets.  The read does not capture, post, call AIS, or
    start a web UI.
    ``POST /v1/payment-receipts/{payment_receipt_id}/unapplied-cash``
    parks leftover remittance against one stored receipt.  Replay of
    the same tenant and receipt returns the stored ``unapplied_cash_id``.
    #12 still rejects overpay.  GET item and list present the parked
    leftover.  After leftover exists,
    ``POST /v1/unapplied-cash/{unapplied_cash_id}/journal-proposals``
    composes one validated cash/unapplied-cash journal.  Do not invent a
    webhook, write-off, settlement, credit note, or AIS call.  Do not
    auto-apply leftover to another case.
    ``POST /v1/collection-cases/{collection_case_id}/unapplied-cash-applications``
    applies one parked leftover onto that open case.  Replay of the same
    tenant and leftover returns the stored
    ``unapplied_cash_application_id``.  First successful apply enqueues
    one existing ``unapplied_cash.applied`` outbox event.  Remaining
    zero does not settle.  GET item and list present the stored
    application.  After apply exists,
    ``POST /v1/unapplied-cash-applications/{unapplied_cash_application_id}/journal-proposals``
    composes one validated unapplied-cash/AR journal.  Do not invent a
    write-off, credit note, AIS call, settlement command, or second
    webhook system.
    ``POST /v1/unapplied-cash/{unapplied_cash_id}/refunds`` records one
    commercial refund of the parked leftover.  Replay of the same tenant
    and leftover returns the stored ``unapplied_cash_refund_id``.  First
    successful refund enqueues one existing ``refund.recorded`` outbox
    event.  GET item and list present the stored refund.  Do not invent
    a journal, write-off, settlement, credit note, PSP capture, AIS
    call, or second webhook system.

Money stays exact-decimal strings.  The adapter never posts a journal, never
stores a card PAN, and never calls a named payment provider.  AIS pulls
validated proposals and later returns ``posting_receipt``.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qs, unquote
from uuid import UUID
from wsgiref.simple_server import make_server
from wsgiref.types import StartResponse, WSGIEnvironment

from metering_billing.ais_outbox_drain import AisOutboxDrainService
from metering_billing.accounting_export import AccountingExportService
from metering_billing.collection_case import CollectionCaseService
from metering_billing.credit_adjustment import CreditAdjustmentService
from metering_billing.errors import (
    AccountStatementPresentmentQueryError,
    RatedSpendPresentmentQueryError,
    SpendBudgetEvaluationPresentmentQueryError,
    SpendBudgetOverSignalPresentmentQueryError,
    SpendBudgetPresentmentQueryError,
    CollectionAgingPresentmentQueryError,
    CollectionCasePresentmentQueryError,
    DunningEventPresentmentQueryError,
    CreditAdjustmentPresentmentQueryError,
    RateCardPresentmentQueryError,
    UsageEventPresentmentQueryError,
    RatingRunPresentmentQueryError,
    TaxAssessmentPresentmentQueryError,
    PaymentIntentPresentmentQueryError,
    PaymentReceiptPresentmentQueryError,
    ExactDecimalError,
    JournalProposalQueryError,
    PostingReceiptObservationQueryError,
    PostingReceiptObservationPresentmentQueryError,
    RateCardQueryError,
    InvoicePresentmentQueryError,
    IssuedInvoicePresentmentQueryError,
    IssuedInvoiceVoidPresentmentQueryError,
    IssuedCreditNotePresentmentQueryError,
    IssuedCreditNoteVoidPresentmentQueryError,
    CreditNoteApplicationPresentmentQueryError,
    CollectionCaseSettlementPresentmentQueryError,
    CollectionWriteOffPresentmentQueryError,
    CollectionDisputePresentmentQueryError,
    CollectionDisputeReleasePresentmentQueryError,
    UnappliedCashPresentmentQueryError,
    UnappliedCashApplicationPresentmentQueryError,
    UnappliedCashRefundPresentmentQueryError,
    TenantApiCredentialPresentmentQueryError,
    TenantApiCredentialQueryError,
    TaxAssessmentQueryError,
    TaxRateQueryError,
    TimeWindowError,
    WebhookDeliveryPresentmentQueryError,
    WebhookOutboxEventPresentmentQueryError,
    WebhookSubscriptionPresentmentQueryError,
    WebhookSubscriptionQueryError,
)
from metering_billing.rate_card import RateCardService
from metering_billing.tax_assessment import TaxAssessmentService
from metering_billing.tax_rate import TaxRateService
from metering_billing.invoice_draft import InvoiceDraftService
from metering_billing.issued_invoice import IssuedInvoiceService
from metering_billing.issued_invoice_presentment import IssuedInvoicePresentmentService
from metering_billing.issued_invoice_void import IssuedInvoiceVoidService
from metering_billing.issued_invoice_void_presentment import (
    IssuedInvoiceVoidPresentmentService,
)
from metering_billing.issued_credit_note import IssuedCreditNoteService
from metering_billing.issued_credit_note_presentment import IssuedCreditNotePresentmentService
from metering_billing.issued_credit_note_void import IssuedCreditNoteVoidService
from metering_billing.issued_credit_note_void_presentment import (
    IssuedCreditNoteVoidPresentmentService,
)
from metering_billing.credit_note_application import CreditNoteApplicationService
from metering_billing.credit_note_application_presentment import (
    CreditNoteApplicationPresentmentService,
)
from metering_billing.collection_case_settlement import CollectionCaseSettlementService
from metering_billing.collection_case_settlement_presentment import (
    CollectionCaseSettlementPresentmentService,
)
from metering_billing.collection_write_off import CollectionWriteOffService
from metering_billing.collection_write_off_presentment import (
    CollectionWriteOffPresentmentService,
)
from metering_billing.collection_dispute import CollectionDisputeService
from metering_billing.collection_dispute_presentment import (
    CollectionDisputePresentmentService,
)
from metering_billing.collection_dispute_release import CollectionDisputeReleaseService
from metering_billing.collection_dispute_release_presentment import (
    CollectionDisputeReleasePresentmentService,
)
from metering_billing.unapplied_cash import UnappliedCashService
from metering_billing.unapplied_cash_presentment import UnappliedCashPresentmentService
from metering_billing.unapplied_cash_application import UnappliedCashApplicationService
from metering_billing.unapplied_cash_application_presentment import (
    UnappliedCashApplicationPresentmentService,
)
from metering_billing.unapplied_cash_refund import UnappliedCashRefundService
from metering_billing.unapplied_cash_refund_presentment import (
    UnappliedCashRefundPresentmentService,
)
from metering_billing.exact_decimal import parse_exact_decimal
from metering_billing.account_statement_presentment import (
    AccountStatementPresentmentService,
)
from metering_billing.rated_spend_presentment import RatedSpendPresentmentService
from metering_billing.spend_budget import SpendBudgetService
from metering_billing.spend_budget_presentment import SpendBudgetPresentmentService
from metering_billing.spend_budget_evaluation_presentment import (
    SpendBudgetEvaluationPresentmentService,
)
from metering_billing.spend_budget_over_signal import SpendBudgetOverSignalService
from metering_billing.spend_budget_over_signal_presentment import (
    SpendBudgetOverSignalPresentmentService,
)
from metering_billing.collection_aging_presentment import (
    Clock,
    CollectionAgingPresentmentService,
)
from metering_billing.collection_case_presentment import CollectionCasePresentmentService
from metering_billing.dunning_event_presentment import DunningEventPresentmentService
from metering_billing.invoice_presentment import InvoicePresentmentService
from metering_billing.payment_intent_presentment import PaymentIntentPresentmentService
from metering_billing.credit_adjustment_presentment import CreditAdjustmentPresentmentService
from metering_billing.rate_card_presentment import RateCardPresentmentService
from metering_billing.usage_event_presentment import UsageEventPresentmentService
from metering_billing.rating_run_presentment import RatingRunPresentmentService
from metering_billing.tax_assessment_presentment import TaxAssessmentPresentmentService
from metering_billing.posting_receipt_observation_presentment import (
    PostingReceiptObservationPresentmentService,
)
from metering_billing.webhook_delivery_presentment import WebhookDeliveryPresentmentService
from metering_billing.webhook_outbox_event_presentment import (
    WebhookOutboxEventPresentmentService,
)
from metering_billing.webhook_subscription_presentment import (
    WebhookSubscriptionPresentmentService,
)
from metering_billing.payment_receipt_presentment import PaymentReceiptPresentmentService
from metering_billing.tenant_api_credential import TenantApiCredentialService
from metering_billing.tenant_api_credential_presentment import (
    TenantApiCredentialPresentmentService,
)
from metering_billing.webhook_outbox import WebhookDeliveryService, WebhookSubscriptionService
from metering_billing.payment_intent import PaymentIntentService
from metering_billing.payment_settlement import PaymentSettlementService
from metering_billing.posting_receipt import AisPostingReceiptClient, PostingReceiptPullService
from metering_billing.time_window import TimeWindow
from metering_billing.usage_ingestion import UsageIngestionService
from metering_billing.usage_ledger import MemoryUsageLedger
from metering_billing.usage_rating import UsageRatingService


WSGIApp = Callable[[WSGIEnvironment, StartResponse], Iterable[bytes]]
COLLECTION_CASE_COLLECTION_PATH = "/v1/collection-cases"
COLLECTION_AGING_PATH = "/v1/collection-aging"
BILLING_ACCOUNT_STATEMENT_PATH = re.compile(
    r"^/v1/billing-accounts/([0-9a-fA-F-]{36})/statement$"
)
BILLING_ACCOUNT_RATED_SPEND_PATH = re.compile(
    r"^/v1/billing-accounts/([0-9a-fA-F-]{36})/rated-spend$"
)
BILLING_ACCOUNT_BUDGET_STATUS_PATH = re.compile(
    r"^/v1/billing-accounts/([0-9a-fA-F-]{36})/budget-status$"
)
BILLING_ACCOUNT_SPEND_BUDGETS_PATH = re.compile(
    r"^/v1/billing-accounts/([0-9a-fA-F-]{36})/spend-budgets$"
)
SPEND_BUDGET_COLLECTION_PATH = "/v1/spend-budgets"
SPEND_BUDGET_ITEM_PATH = re.compile(r"^/v1/spend-budgets/([0-9a-fA-F-]{36})$")
SPEND_BUDGET_EVALUATION_PATH = re.compile(
    r"^/v1/spend-budgets/([0-9a-fA-F-]{36})/evaluation$"
)
SPEND_BUDGET_OVER_SIGNAL_PATH = re.compile(
    r"^/v1/spend-budgets/([0-9a-fA-F-]{36})/over-signal$"
)
COLLECTION_CASE_ITEM_PATH = re.compile(r"^/v1/collection-cases/([0-9a-fA-F-]{36})$")
COLLECTION_DUNNING_PATH = re.compile(
    r"^/v1/collection-cases/([0-9a-fA-F-]{36})/dunning-events$"
)
DUNNING_EVENT_COLLECTION_PATH = "/v1/dunning-events"
DUNNING_EVENT_ITEM_PATH = re.compile(r"^/v1/dunning-events/([0-9a-fA-F-]{36})$")
PAYMENT_INTENT_COLLECTION_PATH = "/v1/payment-intents"
PAYMENT_INTENT_ITEM_PATH = re.compile(r"^/v1/payment-intents/([0-9a-fA-F-]{36})$")
PAYMENT_CANCEL_PATH = re.compile(r"^/v1/payment-intents/([0-9a-fA-F-]{36})/cancel$")
PAYMENT_RECEIPT_COLLECTION_PATH = "/v1/payment-receipts"
PAYMENT_RECEIPT_ITEM_PATH = re.compile(r"^/v1/payment-receipts/([0-9a-fA-F-]{36})$")
UNAPPLIED_CASH_NESTED_PATH = re.compile(
    r"^/v1/payment-receipts/([0-9a-fA-F-]{36})/unapplied-cash$"
)
UNAPPLIED_CASH_COLLECTION_PATH = "/v1/unapplied-cash"
UNAPPLIED_CASH_ITEM_PATH = re.compile(r"^/v1/unapplied-cash/([0-9a-fA-F-]{36})$")
UNAPPLIED_CASH_JOURNAL_NESTED_PATH = re.compile(
    r"^/v1/unapplied-cash/([0-9a-fA-F-]{36})/journal-proposals$"
)
UNAPPLIED_CASH_REFUND_NESTED_PATH = re.compile(
    r"^/v1/unapplied-cash/([0-9a-fA-F-]{36})/refunds$"
)
UNAPPLIED_CASH_REFUND_COLLECTION_PATH = "/v1/unapplied-cash-refunds"
UNAPPLIED_CASH_REFUND_ITEM_PATH = re.compile(
    r"^/v1/unapplied-cash-refunds/([0-9a-fA-F-]{36})$"
)
REFUND_JOURNAL_NESTED_PATH = re.compile(
    r"^/v1/unapplied-cash-refunds/([0-9a-fA-F-]{36})/journal-proposals$"
)
UNAPPLIED_CASH_APPLICATION_NESTED_PATH = re.compile(
    r"^/v1/collection-cases/([0-9a-fA-F-]{36})/unapplied-cash-applications$"
)
UNAPPLIED_CASH_APPLICATION_COLLECTION_PATH = "/v1/unapplied-cash-applications"
UNAPPLIED_CASH_APPLICATION_ITEM_PATH = re.compile(
    r"^/v1/unapplied-cash-applications/([0-9a-fA-F-]{36})$"
)
UNAPPLIED_CASH_APPLICATION_JOURNAL_NESTED_PATH = re.compile(
    r"^/v1/unapplied-cash-applications/([0-9a-fA-F-]{36})/journal-proposals$"
)
FORBIDDEN_PAYMENT_INTENT_KEYS = frozenset(
    {
        "card_pan",
        "primary_account_number",
        "card_number",
        "pan",
        "cvc",
        "cvv",
        "card_cvc",
        "provider_secret",
        "api_credential_secret",
        "provider_charge_id",
    }
)
JOURNAL_PROPOSAL_ITEM_PATH = re.compile(r"^/v1/journal-proposals/([0-9a-fA-F-]{36})$")
POSTING_RECEIPT_COLLECTION_PATH = "/v1/posting-receipt-observations"
POSTING_RECEIPT_ITEM_PREFIX = "/v1/posting-receipt-observations/"
CREDIT_ADJUSTMENT_COLLECTION_PATH = "/v1/credit-adjustments"
CREDIT_ADJUSTMENT_ITEM_PATH = re.compile(r"^/v1/credit-adjustments/([0-9a-fA-F-]{36})$")
USAGE_EVENT_COLLECTION_PATH = "/v1/usage-events"
USAGE_EVENT_ITEM_PATH = re.compile(r"^/v1/usage-events/([0-9a-fA-F-]{36})$")
RATING_RUN_COLLECTION_PATH = "/v1/rating-runs"
RATING_RUN_ITEM_PATH = re.compile(r"^/v1/rating-runs/([0-9a-fA-F-]{36})$")
RATE_CARD_COLLECTION_PATH = "/v1/rate-cards"
RATE_CARD_ITEM_PATH = re.compile(r"^/v1/rate-cards/([0-9a-fA-F-]{36})$")
RATE_CARD_VERSIONS_PATH = re.compile(r"^/v1/rate-cards/([0-9a-fA-F-]{36})/versions$")
RATE_CARD_VERSION_ITEM_PATH = re.compile(
    r"^/v1/rate-card-versions/([0-9a-fA-F-]{36}|[0-9]+)$"
)
TAX_RATE_COLLECTION_PATH = "/v1/tax-rates"
TAX_RATE_VERSION_ITEM_PATH = re.compile(
    r"^/v1/tax-rate-versions/([0-9a-fA-F-]{36}|[0-9]+)$"
)
TAX_ASSESSMENT_COLLECTION_PATH = "/v1/tax-assessments"
TAX_ASSESSMENT_ITEM_PATH = re.compile(r"^/v1/tax-assessments/([0-9a-fA-F-]{36})$")
INVOICE_DRAFT_COLLECTION_PATH = "/v1/invoice-drafts"
INVOICE_DRAFT_ITEM_PATH = re.compile(r"^/v1/invoice-drafts/([0-9a-fA-F-]{36})$")
ISSUED_INVOICE_NESTED_PATH = re.compile(
    r"^/v1/invoice-drafts/([0-9a-fA-F-]{36})/issued-invoices$"
)
ISSUED_INVOICE_COLLECTION_PATH = "/v1/issued-invoices"
ISSUED_INVOICE_ITEM_PATH = re.compile(r"^/v1/issued-invoices/([0-9a-fA-F-]{36})$")
ISSUED_INVOICE_VOID_NESTED_PATH = re.compile(
    r"^/v1/issued-invoices/([0-9a-fA-F-]{36})/voids$"
)
ISSUED_INVOICE_VOID_COLLECTION_PATH = "/v1/issued-invoice-voids"
ISSUED_INVOICE_VOID_ITEM_PATH = re.compile(
    r"^/v1/issued-invoice-voids/([0-9a-fA-F-]{36})$"
)
VOID_JOURNAL_NESTED_PATH = re.compile(
    r"^/v1/issued-invoice-voids/([0-9a-fA-F-]{36})/journal-proposals$"
)
ISSUED_CREDIT_NOTE_NESTED_PATH = re.compile(
    r"^/v1/credit-adjustments/([0-9a-fA-F-]{36})/issued-credit-notes$"
)
CREDIT_JOURNAL_NESTED_PATH = re.compile(
    r"^/v1/credit-adjustments/([0-9a-fA-F-]{36})/journal-proposals$"
)
ISSUED_CREDIT_NOTE_COLLECTION_PATH = "/v1/issued-credit-notes"
ISSUED_CREDIT_NOTE_ITEM_PATH = re.compile(r"^/v1/issued-credit-notes/([0-9a-fA-F-]{36})$")
ISSUED_CREDIT_NOTE_VOID_NESTED_PATH = re.compile(
    r"^/v1/issued-credit-notes/([0-9a-fA-F-]{36})/voids$"
)
ISSUED_CREDIT_NOTE_VOID_COLLECTION_PATH = "/v1/issued-credit-note-voids"
ISSUED_CREDIT_NOTE_VOID_ITEM_PATH = re.compile(
    r"^/v1/issued-credit-note-voids/([0-9a-fA-F-]{36})$"
)
CREDIT_NOTE_VOID_JOURNAL_NESTED_PATH = re.compile(
    r"^/v1/issued-credit-note-voids/([0-9a-fA-F-]{36})/journal-proposals$"
)
CREDIT_NOTE_APPLICATION_NESTED_PATH = re.compile(
    r"^/v1/collection-cases/([0-9a-fA-F-]{36})/credit-note-applications$"
)
CREDIT_NOTE_APPLICATION_COLLECTION_PATH = "/v1/credit-note-applications"
CREDIT_NOTE_APPLICATION_ITEM_PATH = re.compile(
    r"^/v1/credit-note-applications/([0-9a-fA-F-]{36})$"
)
COLLECTION_CASE_SETTLEMENT_NESTED_PATH = re.compile(
    r"^/v1/collection-cases/([0-9a-fA-F-]{36})/settlements$"
)
COLLECTION_CASE_SETTLEMENT_COLLECTION_PATH = "/v1/collection-case-settlements"
COLLECTION_CASE_SETTLEMENT_ITEM_PATH = re.compile(
    r"^/v1/collection-case-settlements/([0-9a-fA-F-]{36})$"
)
COLLECTION_WRITE_OFF_NESTED_PATH = re.compile(
    r"^/v1/collection-cases/([0-9a-fA-F-]{36})/write-offs$"
)
COLLECTION_WRITE_OFF_COLLECTION_PATH = "/v1/collection-write-offs"
COLLECTION_WRITE_OFF_ITEM_PATH = re.compile(
    r"^/v1/collection-write-offs/([0-9a-fA-F-]{36})$"
)
COLLECTION_DISPUTE_NESTED_PATH = re.compile(
    r"^/v1/collection-cases/([0-9a-fA-F-]{36})/disputes$"
)
COLLECTION_DISPUTE_COLLECTION_PATH = "/v1/collection-disputes"
COLLECTION_DISPUTE_ITEM_PATH = re.compile(
    r"^/v1/collection-disputes/([0-9a-fA-F-]{36})$"
)
COLLECTION_DISPUTE_RELEASE_NESTED_PATH = re.compile(
    r"^/v1/collection-disputes/([0-9a-fA-F-]{36})/releases$"
)
COLLECTION_DISPUTE_RELEASE_COLLECTION_PATH = "/v1/collection-dispute-releases"
COLLECTION_DISPUTE_RELEASE_ITEM_PATH = re.compile(
    r"^/v1/collection-dispute-releases/([0-9a-fA-F-]{36})$"
)
WRITE_OFF_JOURNAL_NESTED_PATH = re.compile(
    r"^/v1/collection-write-offs/([0-9a-fA-F-]{36})/journal-proposals$"
)
TENANT_API_CREDENTIAL_COLLECTION_PATH = "/v1/tenant-api-credentials"
TENANT_API_CREDENTIAL_REVOKE_PATH = re.compile(
    r"^/v1/tenant-api-credentials/([0-9a-fA-F-]{36})/revoke$"
)
TENANT_API_CREDENTIAL_ITEM_PATH = re.compile(
    r"^/v1/tenant-api-credentials/([0-9a-fA-F-]{36})$"
)
WEBHOOK_SUBSCRIPTION_COLLECTION_PATH = "/v1/webhook-subscriptions"
WEBHOOK_SUBSCRIPTION_REVOKE_PATH = re.compile(
    r"^/v1/webhook-subscriptions/([0-9a-fA-F-]{36})/revoke$"
)
WEBHOOK_SUBSCRIPTION_ITEM_PATH = re.compile(
    r"^/v1/webhook-subscriptions/([0-9a-fA-F-]{36})$"
)
WEBHOOK_DELIVERY_COLLECTION_PATH = "/v1/webhook-deliveries"
WEBHOOK_DELIVERY_ITEM_PATH = re.compile(r"^/v1/webhook-deliveries/([0-9a-fA-F-]{36})$")
WEBHOOK_OUTBOX_EVENT_COLLECTION_PATH = "/v1/webhook-outbox-events"
WEBHOOK_OUTBOX_EVENT_ITEM_PATH = re.compile(
    r"^/v1/webhook-outbox-events/([0-9a-fA-F-]{36})$"
)
AIS_OUTBOX_DRAIN_COLLECTION_PATH = "/v1/ais-outbox-drains"
API_KEY_HEADER_ENVIRON = "HTTP_X_CWL_API_KEY"
AUTHORIZATION_HEADER_ENVIRON = "HTTP_AUTHORIZATION"
KNOWN_POST_PATHS = frozenset(
    {
        "/v1/journal-proposals",
        "/v1/cash-journal-proposals",
    }
)
SUCCESS_OUTCOMES = frozenset({"accepted", "duplicate_replay"})
TENANT_HEADER_ENVIRON = "HTTP_X_CWL_TENANT_REFERENCE"


class HttpRequestError(ValueError):
    """Raised when the HTTP adapter cannot decode or authorize a write."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


def create_http_app(
    ledger: MemoryUsageLedger | None = None,
    *,
    ais_base_url: str | None = None,
    ais_client: AisPostingReceiptClient | None = None,
    clock: Clock | None = None,
) -> WSGIApp:
    """Return a stdlib WSGI app bound to one shared commercial ledger."""
    shared_ledger = MemoryUsageLedger() if ledger is None else ledger
    ingestion = UsageIngestionService(shared_ledger)
    rating = UsageRatingService(shared_ledger)
    drafts = InvoiceDraftService(shared_ledger)
    issuers = IssuedInvoiceService(shared_ledger)
    issued_presentments = IssuedInvoicePresentmentService(shared_ledger)
    issued_invoice_voids = IssuedInvoiceVoidService(shared_ledger)
    issued_invoice_void_presentments = IssuedInvoiceVoidPresentmentService(
        shared_ledger
    )
    credit_note_issuers = IssuedCreditNoteService(shared_ledger)
    credit_note_presentments = IssuedCreditNotePresentmentService(shared_ledger)
    issued_credit_note_voids = IssuedCreditNoteVoidService(shared_ledger)
    issued_credit_note_void_presentments = IssuedCreditNoteVoidPresentmentService(
        shared_ledger
    )
    credit_note_applications = CreditNoteApplicationService(shared_ledger)
    credit_note_application_presentments = CreditNoteApplicationPresentmentService(
        shared_ledger
    )
    collection_case_settlements = CollectionCaseSettlementService(shared_ledger)
    collection_case_settlement_presentments = CollectionCaseSettlementPresentmentService(
        shared_ledger
    )
    collection_write_offs = CollectionWriteOffService(shared_ledger)
    collection_write_off_presentments = CollectionWriteOffPresentmentService(
        shared_ledger
    )
    collection_disputes = CollectionDisputeService(shared_ledger)
    collection_dispute_presentments = CollectionDisputePresentmentService(
        shared_ledger
    )
    collection_dispute_releases = CollectionDisputeReleaseService(shared_ledger)
    collection_dispute_release_presentments = CollectionDisputeReleasePresentmentService(
        shared_ledger
    )
    unapplied_cash = UnappliedCashService(shared_ledger)
    unapplied_cash_presentments = UnappliedCashPresentmentService(shared_ledger)
    unapplied_cash_applications = UnappliedCashApplicationService(shared_ledger)
    unapplied_cash_application_presentments = UnappliedCashApplicationPresentmentService(
        shared_ledger
    )
    unapplied_cash_refunds = UnappliedCashRefundService(shared_ledger)
    unapplied_cash_refund_presentments = UnappliedCashRefundPresentmentService(
        shared_ledger
    )
    exports = AccountingExportService(shared_ledger)
    collections = CollectionCaseService(shared_ledger)
    intents = PaymentIntentService(shared_ledger)
    settlements = PaymentSettlementService(shared_ledger)
    if ais_client is not None:
        resolved_ais_client = ais_client
    elif ais_base_url:
        resolved_ais_client = AisPostingReceiptClient(ais_base_url)
    else:
        resolved_ais_client = None
    pulls = PostingReceiptPullService(shared_ledger, ais_client=resolved_ais_client)
    credits = CreditAdjustmentService(shared_ledger)
    catalogs = RateCardService(shared_ledger)
    tax_rates = TaxRateService(shared_ledger)
    assessments = TaxAssessmentService(shared_ledger)
    presentments = InvoicePresentmentService(shared_ledger)
    case_presentments = CollectionCasePresentmentService(shared_ledger)
    aging_presentments = CollectionAgingPresentmentService(shared_ledger, clock=clock)
    account_statement_presentments = AccountStatementPresentmentService(
        shared_ledger, clock=clock
    )
    rated_spend_presentments = RatedSpendPresentmentService(shared_ledger)
    spend_budgets = SpendBudgetService(shared_ledger, clock=clock)
    spend_budget_presentments = SpendBudgetPresentmentService(shared_ledger)
    spend_budget_evaluations = SpendBudgetEvaluationPresentmentService(shared_ledger)
    spend_budget_over_signals = SpendBudgetOverSignalService(shared_ledger, clock=clock)
    spend_budget_over_signal_presentments = SpendBudgetOverSignalPresentmentService(
        shared_ledger
    )
    dunning_presentments = DunningEventPresentmentService(shared_ledger)
    intent_presentments = PaymentIntentPresentmentService(shared_ledger)
    receipt_presentments = PaymentReceiptPresentmentService(shared_ledger)
    credit_presentments = CreditAdjustmentPresentmentService(shared_ledger)
    catalog_presentments = RateCardPresentmentService(shared_ledger)
    usage_presentments = UsageEventPresentmentService(shared_ledger)
    rating_presentments = RatingRunPresentmentService(shared_ledger)
    tax_assessment_presentments = TaxAssessmentPresentmentService(shared_ledger)
    observation_presentments = PostingReceiptObservationPresentmentService(shared_ledger)
    delivery_presentments = WebhookDeliveryPresentmentService(shared_ledger)
    outbox_presentments = WebhookOutboxEventPresentmentService(shared_ledger)
    credentials = TenantApiCredentialService(shared_ledger)
    credential_presentments = TenantApiCredentialPresentmentService(shared_ledger)
    webhooks = WebhookSubscriptionService(shared_ledger)
    subscription_presentments = WebhookSubscriptionPresentmentService(shared_ledger)
    deliveries = WebhookDeliveryService(shared_ledger)
    drains = AisOutboxDrainService(shared_ledger, ais_client=resolved_ais_client)

    def _authorized_tenant(
        environ: WSGIEnvironment,
        payload: Mapping[str, Any],
        *,
        require_credential: bool = True,
    ) -> str:
        """Resolve the tenant pin and enforce an active key after bootstrap."""
        tenant_reference = _resolve_tenant_pin(environ, payload)
        if not require_credential:
            return tenant_reference
        try:
            credentials.authorize_request(tenant_reference, _extract_api_key(environ))
        except TenantApiCredentialQueryError as error:
            raise HttpRequestError(error.rejection_reason_code) from error
        return tenant_reference

    def application(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        """Dispatch one HTTP request onto the existing commercial services."""
        method = str(environ.get("REQUEST_METHOD") or "GET")
        path = str(environ.get("PATH_INFO") or "/")
        route_name, path_values = _resolve_route(method, path)
        if route_name is None:
            return _send_json(start_response, 404, {"rejection_reason_code": "route_not_found"})
        if route_name == "method_not_allowed":
            return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "http_method_not_allowed":
            return _send_json(
                start_response, 405, {"rejection_reason_code": "method_not_allowed"}
            )
        if route_name == "healthz":
            return _send_json(start_response, 200, {"status": "ok"})
        if route_name in {"list_tenant_api_credentials", "get_tenant_api_credential"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_tenant_api_credentials":
                    page = credential_presentments.list_tenant_api_credentials(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = credential_presentments.present_tenant_api_credential(
                    tenant_reference,
                    _parse_uuid(
                        path_values["tenant_api_credential_id"],
                        "tenant_api_credential_id",
                    ),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except TenantApiCredentialPresentmentQueryError as error:
                status_code = (
                    404 if error.rejection_reason_code == "api_credential_not_found" else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"tenant_api_credentials", "revoke_tenant_api_credential"}:
            try:
                payload = _read_json_object(environ)
                if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
                    raise HttpRequestError("request_invalid")
                if route_name == "tenant_api_credentials":
                    tenant_reference = _authorized_tenant(
                        environ, payload, require_credential=False
                    )
                    result = credentials.issue_credential(
                        tenant_reference, payload.get("credential_label")
                    )
                    return _send_json(
                        start_response, _status_for_result(result), result.as_contract_dict()
                    )
                tenant_reference = _authorized_tenant(environ, payload)
                result = credentials.revoke_credential(
                    tenant_reference,
                    _parse_uuid(path_values["tenant_api_credential_id"], "tenant_api_credential_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except TenantApiCredentialQueryError as error:
                status_code = (
                    404 if error.rejection_reason_code == "api_credential_not_found" else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_webhook_subscriptions", "get_webhook_subscription"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_webhook_subscriptions":
                    page = subscription_presentments.list_webhook_subscriptions(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = subscription_presentments.present_webhook_subscription(
                    tenant_reference,
                    _parse_uuid(
                        path_values["webhook_subscription_id"],
                        "webhook_subscription_id",
                    ),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except WebhookSubscriptionPresentmentQueryError as error:
                status_code = (
                    404
                    if error.rejection_reason_code == "webhook_subscription_not_found"
                    else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {
            "webhook_subscriptions",
            "revoke_webhook_subscription",
            "webhook_deliveries",
        }:
            try:
                payload = _read_json_object(environ)
                tenant_reference = _authorized_tenant(environ, payload)
                if route_name == "webhook_subscriptions":
                    if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
                        raise HttpRequestError("request_invalid")
                    result = webhooks.register_subscription(
                        tenant_reference,
                        payload.get("callback_url"),
                        payload.get("event_type_codes"),
                    )
                    return _send_json(
                        start_response, _status_for_result(result), result.as_contract_dict()
                    )
                if route_name == "webhook_deliveries":
                    if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
                        raise HttpRequestError("request_invalid")
                    result = deliveries.deliver_due_events(tenant_reference)
                    return _send_json(
                        start_response, _status_for_result(result), result.as_contract_dict()
                    )
                if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
                    raise HttpRequestError("request_invalid")
                result = webhooks.revoke_subscription(
                    tenant_reference,
                    _parse_uuid(path_values["webhook_subscription_id"], "webhook_subscription_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except WebhookSubscriptionQueryError as error:
                status_code = (
                    404 if error.rejection_reason_code == "webhook_subscription_not_found" else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_webhook_deliveries", "get_webhook_delivery"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_webhook_deliveries":
                    page = delivery_presentments.list_webhook_deliveries(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = delivery_presentments.present_webhook_delivery(
                    tenant_reference,
                    _parse_uuid(path_values["delivery_attempt_id"], "delivery_attempt_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except WebhookDeliveryPresentmentQueryError as error:
                status_code = (
                    404
                    if error.rejection_reason_code == "webhook_delivery_not_found"
                    else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_webhook_outbox_events", "get_webhook_outbox_event"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_webhook_outbox_events":
                    page = outbox_presentments.list_webhook_outbox_events(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = outbox_presentments.present_webhook_outbox_event(
                    tenant_reference,
                    _parse_uuid(path_values["outbox_event_id"], "outbox_event_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except WebhookOutboxEventPresentmentQueryError as error:
                status_code = (
                    404
                    if error.rejection_reason_code == "webhook_outbox_event_not_found"
                    else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_journal_proposals", "get_journal_proposal"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_journal_proposals":
                    page = exports.list_journal_proposals(
                        tenant_reference,
                        proposal_status=query.get("proposal_status"),
                        proposed_after=query.get("proposed_after"),
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = exports.get_journal_proposal(
                    tenant_reference,
                    _parse_uuid(path_values["proposal_id"], "proposal_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except JournalProposalQueryError as error:
                status_code = 404 if error.rejection_reason_code == "proposal_not_found" else 422
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_posting_receipt_observations", "get_posting_receipt_observation"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_posting_receipt_observations":
                    page = observation_presentments.list_posting_receipt_observations(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = pulls.get_posting_receipt_observation(
                    tenant_reference, path_values["idempotency_key"]
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except PostingReceiptObservationPresentmentQueryError as error:
                status_code = (
                    404 if error.rejection_reason_code == "observation_not_found" else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except PostingReceiptObservationQueryError as error:
                status_code = 404 if error.rejection_reason_code == "observation_not_found" else 422
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "posting_receipt_observations":
            try:
                payload = _read_json_object(environ)
                if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
                    raise HttpRequestError("request_invalid")
                tenant_reference = _authorized_tenant(environ, payload)
                idempotency_key = payload.get("idempotency_key")
                if not isinstance(idempotency_key, str) or not idempotency_key:
                    raise HttpRequestError("idempotency_key_missing")
                result = pulls.pull_posting_receipt(tenant_reference, idempotency_key)
                return _send_json(
                    start_response, _status_for_result(result), result.as_contract_dict()
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "ais_outbox_drains":
            try:
                payload = _read_json_object(environ)
                tenant_reference = _authorized_tenant(environ, payload)
                result = drains.drain_ais_outbox(tenant_reference)
                return _send_json(
                    start_response, _status_for_result(result), result.as_contract_dict()
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_credit_adjustments", "get_credit_adjustment"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_credit_adjustments":
                    page = credit_presentments.list_credit_adjustments(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = credit_presentments.present_credit_adjustment(
                    tenant_reference,
                    _parse_uuid(path_values["credit_adjustment_id"], "credit_adjustment_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except CreditAdjustmentPresentmentQueryError as error:
                status_code = (
                    404 if error.rejection_reason_code == "credit_adjustment_not_found" else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_usage_events", "get_usage_event"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_usage_events":
                    page = usage_presentments.list_usage_events(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = usage_presentments.present_usage_event(
                    tenant_reference,
                    _parse_uuid(path_values["usage_event_id"], "usage_event_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except UsageEventPresentmentQueryError as error:
                status_code = (
                    404 if error.rejection_reason_code == "usage_event_not_found" else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_rating_runs", "get_rating_run"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_rating_runs":
                    page = rating_presentments.list_rating_runs(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = rating_presentments.present_rating_run(
                    tenant_reference,
                    _parse_uuid(path_values["rating_run_id"], "rating_run_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except RatingRunPresentmentQueryError as error:
                status_code = (
                    404 if error.rejection_reason_code == "rating_run_not_found" else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_rate_cards", "get_rate_card"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_rate_cards":
                    page = catalog_presentments.list_rate_cards(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = catalog_presentments.present_rate_card(
                    tenant_reference,
                    _parse_uuid(path_values["rate_card_id"], "rate_card_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except RateCardPresentmentQueryError as error:
                status_code = (
                    404 if error.rejection_reason_code == "rate_card_not_found" else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {
            "list_rate_card_versions",
            "get_rate_card_version",
        }:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_rate_card_versions":
                    page = catalogs.list_rate_card_versions(
                        tenant_reference,
                        _parse_uuid(path_values["rate_card_id"], "rate_card_id"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = catalogs.get_rate_card_version(
                    tenant_reference,
                    _parse_rate_card_version(path_values["rate_card_version"]),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except RateCardQueryError as error:
                status_code = (
                    404 if error.rejection_reason_code == "rate_card_not_found" else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_tax_rates", "get_tax_rate_version"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_tax_rates":
                    page = tax_rates.list_tax_rates(tenant_reference)
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = tax_rates.get_tax_rate_version(
                    tenant_reference,
                    _parse_rate_card_version(path_values["tax_rate_version"]),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except TaxRateQueryError as error:
                status_code = (
                    404 if error.rejection_reason_code == "tax_rate_not_found" else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_tax_assessments", "get_tax_assessment"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_tax_assessments":
                    page = tax_assessment_presentments.list_tax_assessments(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = assessments.get_tax_assessment(
                    tenant_reference,
                    _parse_uuid(path_values["tax_assessment_id"], "tax_assessment_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except TaxAssessmentPresentmentQueryError as error:
                status_code = (
                    404 if error.rejection_reason_code == "tax_assessment_not_found" else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except TaxAssessmentQueryError as error:
                status_code = (
                    404 if error.rejection_reason_code == "tax_assessment_not_found" else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_invoice_drafts", "get_invoice_draft"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_invoice_drafts":
                    page = presentments.list_invoice_drafts(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = presentments.present_invoice_draft(
                    tenant_reference,
                    _parse_uuid(path_values["invoice_draft_id"], "invoice_draft_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except InvoicePresentmentQueryError as error:
                status_code = (
                    404 if error.rejection_reason_code == "invoice_draft_not_found" else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_credit_note_applications", "get_credit_note_application"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_credit_note_applications":
                    page = credit_note_application_presentments.list_credit_note_applications(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = credit_note_application_presentments.present_credit_note_application(
                    tenant_reference,
                    _parse_uuid(
                        path_values["credit_note_application_id"],
                        "credit_note_application_id",
                    ),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except CreditNoteApplicationPresentmentQueryError as error:
                status_code = (
                    404
                    if error.rejection_reason_code == "credit_note_application_not_found"
                    else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {
            "list_collection_case_settlements",
            "get_collection_case_settlement",
        }:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_collection_case_settlements":
                    page = collection_case_settlement_presentments.list_collection_case_settlements(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = collection_case_settlement_presentments.present_collection_case_settlement(
                    tenant_reference,
                    _parse_uuid(
                        path_values["collection_case_settlement_id"],
                        "collection_case_settlement_id",
                    ),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except CollectionCaseSettlementPresentmentQueryError as error:
                status_code = (
                    404
                    if error.rejection_reason_code == "collection_case_settlement_not_found"
                    else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_issued_credit_notes", "get_issued_credit_note"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_issued_credit_notes":
                    page = credit_note_presentments.list_issued_credit_notes(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = credit_note_presentments.present_issued_credit_note(
                    tenant_reference,
                    _parse_uuid(path_values["issued_credit_note_id"], "issued_credit_note_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except IssuedCreditNotePresentmentQueryError as error:
                status_code = (
                    404
                    if error.rejection_reason_code == "issued_credit_note_not_found"
                    else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_issued_invoices", "get_issued_invoice"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_issued_invoices":
                    page = issued_presentments.list_issued_invoices(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = issued_presentments.present_issued_invoice(
                    tenant_reference,
                    _parse_uuid(path_values["issued_invoice_id"], "issued_invoice_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except IssuedInvoicePresentmentQueryError as error:
                status_code = (
                    404 if error.rejection_reason_code == "issued_invoice_not_found" else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_dunning_events", "get_dunning_event"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_dunning_events":
                    page = dunning_presentments.list_dunning_events(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = dunning_presentments.present_dunning_event(
                    tenant_reference,
                    _parse_uuid(path_values["dunning_event_id"], "dunning_event_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except DunningEventPresentmentQueryError as error:
                status_code = (
                    404 if error.rejection_reason_code == "dunning_event_not_found" else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "account_statement":
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                result = account_statement_presentments.present_account_statement(
                    tenant_reference,
                    _parse_uuid(path_values["billing_account_id"], "billing_account_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except AccountStatementPresentmentQueryError as error:
                if error.rejection_reason_code == "billing_account_not_found":
                    status_code = 404
                elif error.rejection_reason_code == "billing_account_forbidden":
                    status_code = 403
                else:
                    status_code = 422
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "rated_spend":
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                window = TimeWindow.from_iso8601(
                    query.get("window_started_at", ""),
                    query.get("window_ended_at", ""),
                )
                result = rated_spend_presentments.present_rated_spend(
                    tenant_reference,
                    _parse_uuid(path_values["billing_account_id"], "billing_account_id"),
                    window,
                    group_by=query.get("group_by", "product"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except RatedSpendPresentmentQueryError as error:
                if error.rejection_reason_code == "billing_account_not_found":
                    status_code = 404
                elif error.rejection_reason_code == "billing_account_forbidden":
                    status_code = 403
                else:
                    status_code = 422
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "list_billing_account_budget_statuses":
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                page = spend_budget_evaluations.list_billing_account_budget_statuses(
                    tenant_reference,
                    _parse_uuid(path_values["billing_account_id"], "billing_account_id"),
                    cursor=query.get("cursor"),
                    page_limit=query.get("page_limit"),
                )
                return _send_json(start_response, 200, page.as_contract_dict())
            except SpendBudgetEvaluationPresentmentQueryError as error:
                if error.rejection_reason_code == "billing_account_not_found":
                    status_code = 404
                elif error.rejection_reason_code == "billing_account_forbidden":
                    status_code = 403
                else:
                    status_code = 422
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "get_spend_budget_evaluation":
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                result = spend_budget_evaluations.present_spend_budget_evaluation(
                    tenant_reference,
                    _parse_uuid(path_values["spend_budget_id"], "spend_budget_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except SpendBudgetEvaluationPresentmentQueryError as error:
                if error.rejection_reason_code == "spend_budget_not_found":
                    status_code = 404
                elif error.rejection_reason_code == "billing_account_not_found":
                    status_code = 404
                elif error.rejection_reason_code == "billing_account_forbidden":
                    status_code = 403
                else:
                    status_code = 422
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "get_spend_budget_over_signal":
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                result = spend_budget_over_signal_presentments.present_spend_budget_over_signal(
                    tenant_reference,
                    _parse_uuid(path_values["spend_budget_id"], "spend_budget_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except SpendBudgetOverSignalPresentmentQueryError as error:
                if error.rejection_reason_code == "spend_budget_not_found":
                    status_code = 404
                elif error.rejection_reason_code == "billing_account_not_found":
                    status_code = 404
                elif error.rejection_reason_code == "billing_account_forbidden":
                    status_code = 403
                else:
                    status_code = 422
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_spend_budgets", "get_spend_budget"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_spend_budgets":
                    page = spend_budget_presentments.list_spend_budgets(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = spend_budget_presentments.present_spend_budget(
                    tenant_reference,
                    _parse_uuid(path_values["spend_budget_id"], "spend_budget_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except SpendBudgetPresentmentQueryError as error:
                status_code = (
                    404 if error.rejection_reason_code == "spend_budget_not_found" else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "collection_aging":
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                result = aging_presentments.present_collection_aging(tenant_reference)
                return _send_json(start_response, 200, result.as_contract_dict())
            except CollectionAgingPresentmentQueryError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_collection_cases", "get_collection_case"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_collection_cases":
                    page = case_presentments.list_collection_cases(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = case_presentments.present_collection_case(
                    tenant_reference,
                    _parse_uuid(path_values["collection_case_id"], "collection_case_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except CollectionCasePresentmentQueryError as error:
                status_code = (
                    404 if error.rejection_reason_code == "collection_case_not_found" else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_payment_intents", "get_payment_intent"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_payment_intents":
                    page = intent_presentments.list_payment_intents(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = intent_presentments.present_payment_intent(
                    tenant_reference,
                    _parse_uuid(path_values["payment_intent_id"], "payment_intent_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except PaymentIntentPresentmentQueryError as error:
                status_code = (
                    404 if error.rejection_reason_code == "payment_intent_not_found" else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_payment_receipts", "get_payment_receipt"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_payment_receipts":
                    page = receipt_presentments.list_payment_receipts(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = receipt_presentments.present_payment_receipt(
                    tenant_reference,
                    _parse_uuid(path_values["payment_receipt_id"], "payment_receipt_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except PaymentReceiptPresentmentQueryError as error:
                status_code = (
                    404 if error.rejection_reason_code == "payment_receipt_not_found" else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {"list_unapplied_cash", "get_unapplied_cash"}:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_unapplied_cash":
                    page = unapplied_cash_presentments.list_unapplied_cash(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = unapplied_cash_presentments.present_unapplied_cash(
                    tenant_reference,
                    _parse_uuid(
                        path_values["unapplied_cash_id"],
                        "unapplied_cash_id",
                    ),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except UnappliedCashPresentmentQueryError as error:
                status_code = (
                    404
                    if error.rejection_reason_code == "unapplied_cash_not_found"
                    else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {
            "list_unapplied_cash_applications",
            "get_unapplied_cash_application",
        }:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_unapplied_cash_applications":
                    page = unapplied_cash_application_presentments.list_unapplied_cash_applications(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = unapplied_cash_application_presentments.present_unapplied_cash_application(
                    tenant_reference,
                    _parse_uuid(
                        path_values["unapplied_cash_application_id"],
                        "unapplied_cash_application_id",
                    ),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except UnappliedCashApplicationPresentmentQueryError as error:
                status_code = (
                    404
                    if error.rejection_reason_code == "unapplied_cash_application_not_found"
                    else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {
            "list_unapplied_cash_refunds",
            "get_unapplied_cash_refund",
        }:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_unapplied_cash_refunds":
                    page = unapplied_cash_refund_presentments.list_unapplied_cash_refunds(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = unapplied_cash_refund_presentments.present_unapplied_cash_refund(
                    tenant_reference,
                    _parse_uuid(
                        path_values["unapplied_cash_refund_id"],
                        "unapplied_cash_refund_id",
                    ),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except UnappliedCashRefundPresentmentQueryError as error:
                status_code = (
                    404
                    if error.rejection_reason_code == "unapplied_cash_refund_not_found"
                    else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "unapplied_cash":
            try:
                payload = _read_json_object(environ)
                if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
                    raise HttpRequestError("request_invalid")
                tenant_reference = _authorized_tenant(environ, payload)
                raw_amount = payload.get("unapplied_amount")
                parsed_amount = None
                if raw_amount is not None:
                    if not isinstance(raw_amount, str):
                        raise HttpRequestError("request_invalid")
                    parsed_amount = parse_exact_decimal(raw_amount)
                currency_code = payload.get("currency_code")
                if currency_code is not None and not isinstance(currency_code, str):
                    raise HttpRequestError("request_invalid")
                result = unapplied_cash.park_unapplied_cash(
                    tenant_reference,
                    _parse_uuid(path_values["payment_receipt_id"], "payment_receipt_id"),
                    unapplied_amount=parsed_amount,
                    currency_code=currency_code,
                )
                return _send_json(
                    start_response, _status_for_result(result), result.as_contract_dict()
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "unapplied_cash_applications":
            try:
                payload = _read_json_object(environ)
                if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
                    raise HttpRequestError("request_invalid")
                tenant_reference = _authorized_tenant(environ, payload)
                raw_amount = payload.get("applied_amount")
                parsed_amount = None
                if raw_amount is not None:
                    if not isinstance(raw_amount, str):
                        raise HttpRequestError("request_invalid")
                    parsed_amount = parse_exact_decimal(raw_amount)
                currency_code = payload.get("currency_code")
                if currency_code is not None and not isinstance(currency_code, str):
                    raise HttpRequestError("request_invalid")
                result = unapplied_cash_applications.apply_unapplied_cash(
                    tenant_reference,
                    _parse_uuid(payload.get("unapplied_cash_id"), "unapplied_cash_id"),
                    _parse_uuid(path_values["collection_case_id"], "collection_case_id"),
                    applied_amount=parsed_amount,
                    currency_code=currency_code,
                )
                return _send_json(
                    start_response, _status_for_result(result), result.as_contract_dict()
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "unapplied_cash_refunds":
            try:
                payload = _read_json_object(environ)
                if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
                    raise HttpRequestError("request_invalid")
                tenant_reference = _authorized_tenant(environ, payload)
                raw_amount = payload.get("refund_amount")
                parsed_amount = None
                if raw_amount is not None:
                    if not isinstance(raw_amount, str):
                        raise HttpRequestError("request_invalid")
                    parsed_amount = parse_exact_decimal(raw_amount)
                currency_code = payload.get("currency_code")
                if currency_code is not None and not isinstance(currency_code, str):
                    raise HttpRequestError("request_invalid")
                result = unapplied_cash_refunds.refund_unapplied_cash(
                    tenant_reference,
                    _parse_uuid(path_values["unapplied_cash_id"], "unapplied_cash_id"),
                    refund_amount=parsed_amount,
                    currency_code=currency_code,
                )
                return _send_json(
                    start_response, _status_for_result(result), result.as_contract_dict()
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "credit_note_applications":
            try:
                payload = _read_json_object(environ)
                if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
                    raise HttpRequestError("request_invalid")
                tenant_reference = _authorized_tenant(environ, payload)
                result = credit_note_applications.apply_credit_note(
                    tenant_reference,
                    _parse_uuid(payload.get("issued_credit_note_id"), "issued_credit_note_id"),
                    _parse_uuid(path_values["collection_case_id"], "collection_case_id"),
                )
                return _send_json(
                    start_response, _status_for_result(result), result.as_contract_dict()
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "collection_case_settlements":
            try:
                payload = _read_json_object(environ)
                if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
                    raise HttpRequestError("request_invalid")
                tenant_reference = _authorized_tenant(environ, payload)
                result = collection_case_settlements.settle_collection_case(
                    tenant_reference,
                    _parse_uuid(path_values["collection_case_id"], "collection_case_id"),
                )
                return _send_json(
                    start_response, _status_for_result(result), result.as_contract_dict()
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {
            "list_collection_write_offs",
            "get_collection_write_off",
        }:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_collection_write_offs":
                    page = collection_write_off_presentments.list_collection_write_offs(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = collection_write_off_presentments.present_collection_write_off(
                    tenant_reference,
                    _parse_uuid(
                        path_values["collection_write_off_id"],
                        "collection_write_off_id",
                    ),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except CollectionWriteOffPresentmentQueryError as error:
                status_code = (
                    404
                    if error.rejection_reason_code == "collection_write_off_not_found"
                    else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "collection_write_offs":
            try:
                payload = _read_json_object(environ)
                if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
                    raise HttpRequestError("request_invalid")
                tenant_reference = _authorized_tenant(environ, payload)
                raw_amount = payload.get("write_off_amount")
                parsed_amount = None
                if raw_amount is not None:
                    if not isinstance(raw_amount, str):
                        raise HttpRequestError("request_invalid")
                    parsed_amount = parse_exact_decimal(raw_amount)
                currency_code = payload.get("currency_code")
                if currency_code is not None and not isinstance(currency_code, str):
                    raise HttpRequestError("request_invalid")
                result = collection_write_offs.write_off_collection_case(
                    tenant_reference,
                    _parse_uuid(path_values["collection_case_id"], "collection_case_id"),
                    write_off_amount=parsed_amount,
                    currency_code=currency_code,
                )
                return _send_json(
                    start_response, _status_for_result(result), result.as_contract_dict()
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {
            "list_collection_disputes",
            "get_collection_dispute",
        }:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_collection_disputes":
                    page = collection_dispute_presentments.list_collection_disputes(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = collection_dispute_presentments.present_collection_dispute(
                    tenant_reference,
                    _parse_uuid(
                        path_values["collection_dispute_id"],
                        "collection_dispute_id",
                    ),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except CollectionDisputePresentmentQueryError as error:
                status_code = (
                    404
                    if error.rejection_reason_code == "collection_dispute_not_found"
                    else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "collection_disputes":
            try:
                payload = _read_json_object(environ)
                if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
                    raise HttpRequestError("request_invalid")
                tenant_reference = _authorized_tenant(environ, payload)
                currency_code = payload.get("currency_code")
                if currency_code is not None and not isinstance(currency_code, str):
                    raise HttpRequestError("request_invalid")
                result = collection_disputes.hold_collection_case(
                    tenant_reference,
                    _parse_uuid(path_values["collection_case_id"], "collection_case_id"),
                    currency_code=currency_code,
                )
                return _send_json(
                    start_response, _status_for_result(result), result.as_contract_dict()
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {
            "list_collection_dispute_releases",
            "get_collection_dispute_release",
        }:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_collection_dispute_releases":
                    page = collection_dispute_release_presentments.list_collection_dispute_releases(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = collection_dispute_release_presentments.present_collection_dispute_release(
                    tenant_reference,
                    _parse_uuid(
                        path_values["collection_dispute_id"],
                        "collection_dispute_id",
                    ),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except CollectionDisputeReleasePresentmentQueryError as error:
                status_code = (
                    404
                    if error.rejection_reason_code == "collection_dispute_release_not_found"
                    else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "collection_dispute_releases":
            try:
                payload = _read_json_object(environ)
                if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
                    raise HttpRequestError("request_invalid")
                tenant_reference = _authorized_tenant(environ, payload)
                currency_code = payload.get("currency_code")
                if currency_code is not None and not isinstance(currency_code, str):
                    raise HttpRequestError("request_invalid")
                result = collection_dispute_releases.release_collection_dispute(
                    tenant_reference,
                    _parse_uuid(
                        path_values["collection_dispute_id"],
                        "collection_dispute_id",
                    ),
                    currency_code=currency_code,
                )
                return _send_json(
                    start_response, _status_for_result(result), result.as_contract_dict()
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "issued_credit_notes":
            try:
                payload = _read_json_object(environ)
                if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
                    raise HttpRequestError("request_invalid")
                tenant_reference = _authorized_tenant(environ, payload)
                result = credit_note_issuers.issue_credit_note(
                    tenant_reference,
                    _parse_uuid(path_values["credit_adjustment_id"], "credit_adjustment_id"),
                )
                return _send_json(
                    start_response, _status_for_result(result), result.as_contract_dict()
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {
            "list_issued_invoice_voids",
            "get_issued_invoice_void",
        }:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_issued_invoice_voids":
                    page = issued_invoice_void_presentments.list_issued_invoice_voids(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = issued_invoice_void_presentments.present_issued_invoice_void(
                    tenant_reference,
                    _parse_uuid(
                        path_values["issued_invoice_void_id"],
                        "issued_invoice_void_id",
                    ),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except IssuedInvoiceVoidPresentmentQueryError as error:
                status_code = (
                    404
                    if error.rejection_reason_code == "issued_invoice_void_not_found"
                    else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name in {
            "list_issued_credit_note_voids",
            "get_issued_credit_note_void",
        }:
            try:
                query = _read_query(environ)
                tenant_reference = _authorized_tenant(environ, query)
                if route_name == "list_issued_credit_note_voids":
                    page = issued_credit_note_void_presentments.list_issued_credit_note_voids(
                        tenant_reference,
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = issued_credit_note_void_presentments.present_issued_credit_note_void(
                    tenant_reference,
                    _parse_uuid(
                        path_values["issued_credit_note_void_id"],
                        "issued_credit_note_void_id",
                    ),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except IssuedCreditNoteVoidPresentmentQueryError as error:
                status_code = (
                    404
                    if error.rejection_reason_code == "issued_credit_note_void_not_found"
                    else 422
                )
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "issued_invoice_voids":
            try:
                payload = _read_json_object(environ)
                if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
                    raise HttpRequestError("request_invalid")
                tenant_reference = _authorized_tenant(environ, payload)
                currency_code = payload.get("currency_code")
                if currency_code is not None and not isinstance(currency_code, str):
                    raise HttpRequestError("request_invalid")
                result = issued_invoice_voids.void_issued_invoice(
                    tenant_reference,
                    _parse_uuid(path_values["issued_invoice_id"], "issued_invoice_id"),
                    currency_code=currency_code,
                )
                return _send_json(
                    start_response, _status_for_result(result), result.as_contract_dict()
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "issued_credit_note_voids":
            try:
                payload = _read_json_object(environ)
                if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
                    raise HttpRequestError("request_invalid")
                tenant_reference = _authorized_tenant(environ, payload)
                currency_code = payload.get("currency_code")
                if currency_code is not None and not isinstance(currency_code, str):
                    raise HttpRequestError("request_invalid")
                result = issued_credit_note_voids.void_issued_credit_note(
                    tenant_reference,
                    _parse_uuid(
                        path_values["issued_credit_note_id"], "issued_credit_note_id"
                    ),
                    currency_code=currency_code,
                )
                return _send_json(
                    start_response, _status_for_result(result), result.as_contract_dict()
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "spend_budgets":
            try:
                payload = _read_json_object(environ)
                if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
                    raise HttpRequestError("request_invalid")
                tenant_reference = _authorized_tenant(environ, payload)
                currency_code = payload.get("currency_code")
                window_started_at = payload.get("window_started_at")
                window_ended_at = payload.get("window_ended_at")
                source_payload_hash = payload.get("source_payload_hash")
                if not isinstance(currency_code, str):
                    raise HttpRequestError("request_invalid")
                if not isinstance(window_started_at, str) or not isinstance(window_ended_at, str):
                    raise HttpRequestError("request_invalid")
                if source_payload_hash is not None and not isinstance(source_payload_hash, str):
                    raise HttpRequestError("request_invalid")
                window = TimeWindow.from_iso8601(window_started_at, window_ended_at)
                result = spend_budgets.publish_spend_budget(
                    tenant_reference,
                    _parse_uuid(path_values["billing_account_id"], "billing_account_id"),
                    currency_code,
                    payload.get("budget_amount"),
                    window,
                    source_payload_hash=source_payload_hash,
                )
                if result.spend_budget_outcome_code.value == "rejected":
                    reason = (
                        result.rejection_reason_code.value
                        if result.rejection_reason_code is not None
                        else "request_invalid"
                    )
                    if reason == "billing_account_not_found":
                        status_code = 404
                    elif reason == "billing_account_forbidden":
                        status_code = 403
                    else:
                        status_code = 422
                    return _send_json(
                        start_response,
                        status_code,
                        result.as_contract_dict(),
                    )
                return _send_json(
                    start_response, _status_for_result(result), result.as_contract_dict()
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "spend_budget_over_signals":
            try:
                payload = _read_json_object(environ)
                if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
                    raise HttpRequestError("request_invalid")
                tenant_reference = _authorized_tenant(environ, payload)
                result = spend_budget_over_signals.observe_spend_budget_over(
                    tenant_reference,
                    _parse_uuid(path_values["spend_budget_id"], "spend_budget_id"),
                )
                if result.spend_budget_over_signal_outcome_code.value == "rejected":
                    reason = result.rejection_reason_text()
                    if reason == "spend_budget_not_found":
                        status_code = 404
                    elif reason == "billing_account_not_found":
                        status_code = 404
                    elif reason == "billing_account_forbidden":
                        status_code = 403
                    else:
                        status_code = 422
                    return _send_json(
                        start_response,
                        status_code,
                        result.as_contract_dict(),
                    )
                return _send_json(
                    start_response, _status_for_result(result), result.as_contract_dict()
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "issued_invoices":
            try:
                payload = _read_json_object(environ)
                if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
                    raise HttpRequestError("request_invalid")
                tenant_reference = _authorized_tenant(environ, payload)
                result = issuers.issue_invoice(
                    tenant_reference,
                    _parse_uuid(path_values["invoice_draft_id"], "invoice_draft_id"),
                    due_at=payload.get("due_at"),
                )
                return _send_json(
                    start_response, _status_for_result(result), result.as_contract_dict()
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        try:
            payload = _read_json_object(environ)
            tenant_reference = _authorized_tenant(environ, payload)
            body, status_code = _dispatch_write(
                route_name,
                path_values,
                tenant_reference,
                payload,
                ingestion,
                rating,
                drafts,
                exports,
                collections,
                intents,
                settlements,
                credits,
                catalogs,
                tax_rates,
                assessments,
            )
        except HttpRequestError as error:
            return _send_json(
                start_response,
                422,
                {"rejection_reason_code": error.rejection_reason_code},
            )
        except (ExactDecimalError, TimeWindowError, ValueError):
            return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        return _send_json(start_response, status_code, body)

    return application


def main(arguments: tuple[str, ...] | None = None) -> int:
    """Serve the HTTP accept surface on ``0.0.0.0:$PORT``.

    *arguments* is accepted so tests can invoke the entrypoint without
    touching ``sys.argv``.  The standalone process binds every interface so a
    container or Render web service can reach it.
    """
    del arguments
    port = int(os.environ.get("PORT", "8000"))
    ais_base_url = os.environ.get("AIS_BASE_URL") or None
    httpd = make_server("0.0.0.0", port, create_http_app(ais_base_url=ais_base_url))
    httpd.serve_forever()
    return 0


def _resolve_route(method: str, path: str) -> tuple[str | None, dict[str, str]]:
    """Return a route name or mark an unknown / wrong-method path."""
    if path == "/healthz":
        if method == "GET":
            return "healthz", {}
        return "method_not_allowed", {}
    if path == TENANT_API_CREDENTIAL_COLLECTION_PATH:
        if method == "POST":
            return "tenant_api_credentials", {}
        if method == "GET":
            return "list_tenant_api_credentials", {}
        return "method_not_allowed", {}
    if path == WEBHOOK_SUBSCRIPTION_COLLECTION_PATH:
        if method == "POST":
            return "webhook_subscriptions", {}
        if method == "GET":
            return "list_webhook_subscriptions", {}
        return "method_not_allowed", {}
    if path == WEBHOOK_DELIVERY_COLLECTION_PATH:
        if method == "POST":
            return "webhook_deliveries", {}
        if method == "GET":
            return "list_webhook_deliveries", {}
        return "method_not_allowed", {}
    if path == WEBHOOK_OUTBOX_EVENT_COLLECTION_PATH:
        if method == "GET":
            return "list_webhook_outbox_events", {}
        return "method_not_allowed", {}
    webhook_outbox_match = WEBHOOK_OUTBOX_EVENT_ITEM_PATH.fullmatch(path)
    if webhook_outbox_match is not None:
        if method == "GET":
            return "get_webhook_outbox_event", {
                "outbox_event_id": webhook_outbox_match.group(1)
            }
        return "method_not_allowed", {}
    webhook_delivery_match = WEBHOOK_DELIVERY_ITEM_PATH.fullmatch(path)
    if webhook_delivery_match is not None:
        if method == "GET":
            return "get_webhook_delivery", {
                "delivery_attempt_id": webhook_delivery_match.group(1)
            }
        return "method_not_allowed", {}
    if path == AIS_OUTBOX_DRAIN_COLLECTION_PATH:
        if method == "POST":
            return "ais_outbox_drains", {}
        return "method_not_allowed", {}
    webhook_revoke_match = WEBHOOK_SUBSCRIPTION_REVOKE_PATH.fullmatch(path)
    if webhook_revoke_match is not None:
        if method == "POST":
            return "revoke_webhook_subscription", {
                "webhook_subscription_id": webhook_revoke_match.group(1)
            }
        return "method_not_allowed", {}
    webhook_subscription_match = WEBHOOK_SUBSCRIPTION_ITEM_PATH.fullmatch(path)
    if webhook_subscription_match is not None:
        if method == "GET":
            return "get_webhook_subscription", {
                "webhook_subscription_id": webhook_subscription_match.group(1)
            }
        return "method_not_allowed", {}
    revoke_match = TENANT_API_CREDENTIAL_REVOKE_PATH.fullmatch(path)
    if revoke_match is not None:
        if method == "POST":
            return "revoke_tenant_api_credential", {
                "tenant_api_credential_id": revoke_match.group(1)
            }
        return "method_not_allowed", {}
    credential_match = TENANT_API_CREDENTIAL_ITEM_PATH.fullmatch(path)
    if credential_match is not None:
        if method == "GET":
            return "get_tenant_api_credential", {
                "tenant_api_credential_id": credential_match.group(1)
            }
        return "method_not_allowed", {}
    dunning_match = COLLECTION_DUNNING_PATH.fullmatch(path)
    if dunning_match is not None:
        if method == "POST":
            return "dunning_events", {"collection_case_id": dunning_match.group(1)}
        return "method_not_allowed", {}
    application_nested = CREDIT_NOTE_APPLICATION_NESTED_PATH.fullmatch(path)
    if application_nested is not None:
        if method == "POST":
            return "credit_note_applications", {
                "collection_case_id": application_nested.group(1)
            }
        return "http_method_not_allowed", {}
    unapplied_application_nested = UNAPPLIED_CASH_APPLICATION_NESTED_PATH.fullmatch(path)
    if unapplied_application_nested is not None:
        if method == "POST":
            return "unapplied_cash_applications", {
                "collection_case_id": unapplied_application_nested.group(1)
            }
        return "http_method_not_allowed", {}
    settlement_nested = COLLECTION_CASE_SETTLEMENT_NESTED_PATH.fullmatch(path)
    if settlement_nested is not None:
        if method == "POST":
            return "collection_case_settlements", {
                "collection_case_id": settlement_nested.group(1)
            }
        return "http_method_not_allowed", {}
    write_off_nested = COLLECTION_WRITE_OFF_NESTED_PATH.fullmatch(path)
    if write_off_nested is not None:
        if method == "POST":
            return "collection_write_offs", {
                "collection_case_id": write_off_nested.group(1)
            }
        return "http_method_not_allowed", {}
    dispute_nested = COLLECTION_DISPUTE_NESTED_PATH.fullmatch(path)
    if dispute_nested is not None:
        if method == "POST":
            return "collection_disputes", {
                "collection_case_id": dispute_nested.group(1)
            }
        return "http_method_not_allowed", {}
    if path == COLLECTION_DISPUTE_COLLECTION_PATH:
        if method == "GET":
            return "list_collection_disputes", {}
        return "http_method_not_allowed", {}
    dispute_release_nested = COLLECTION_DISPUTE_RELEASE_NESTED_PATH.fullmatch(path)
    if dispute_release_nested is not None:
        if method == "POST":
            return "collection_dispute_releases", {
                "collection_dispute_id": dispute_release_nested.group(1)
            }
        return "http_method_not_allowed", {}
    if path == COLLECTION_DISPUTE_RELEASE_COLLECTION_PATH:
        if method == "GET":
            return "list_collection_dispute_releases", {}
        return "http_method_not_allowed", {}
    dispute_release_match = COLLECTION_DISPUTE_RELEASE_ITEM_PATH.fullmatch(path)
    if dispute_release_match is not None:
        if method == "GET":
            return "get_collection_dispute_release", {
                "collection_dispute_id": dispute_release_match.group(1)
            }
        return "http_method_not_allowed", {}
    dispute_match = COLLECTION_DISPUTE_ITEM_PATH.fullmatch(path)
    if dispute_match is not None:
        if method == "GET":
            return "get_collection_dispute", {
                "collection_dispute_id": dispute_match.group(1)
            }
        return "http_method_not_allowed", {}
    if path == COLLECTION_WRITE_OFF_COLLECTION_PATH:
        if method == "GET":
            return "list_collection_write_offs", {}
        return "method_not_allowed", {}
    write_off_journal_nested = WRITE_OFF_JOURNAL_NESTED_PATH.fullmatch(path)
    if write_off_journal_nested is not None:
        if method == "POST":
            return "write_off_journal_proposals", {
                "collection_write_off_id": write_off_journal_nested.group(1)
            }
        return "http_method_not_allowed", {}
    write_off_match = COLLECTION_WRITE_OFF_ITEM_PATH.fullmatch(path)
    if write_off_match is not None:
        if method == "GET":
            return "get_collection_write_off", {
                "collection_write_off_id": write_off_match.group(1)
            }
        return "method_not_allowed", {}
    if path == COLLECTION_CASE_SETTLEMENT_COLLECTION_PATH:
        if method == "GET":
            return "list_collection_case_settlements", {}
        return "method_not_allowed", {}
    settlement_match = COLLECTION_CASE_SETTLEMENT_ITEM_PATH.fullmatch(path)
    if settlement_match is not None:
        if method == "GET":
            return "get_collection_case_settlement", {
                "collection_case_settlement_id": settlement_match.group(1)
            }
        return "method_not_allowed", {}
    if path == CREDIT_NOTE_APPLICATION_COLLECTION_PATH:
        if method == "GET":
            return "list_credit_note_applications", {}
        return "method_not_allowed", {}
    application_match = CREDIT_NOTE_APPLICATION_ITEM_PATH.fullmatch(path)
    if application_match is not None:
        if method == "GET":
            return "get_credit_note_application", {
                "credit_note_application_id": application_match.group(1)
            }
        return "method_not_allowed", {}
    if path == UNAPPLIED_CASH_APPLICATION_COLLECTION_PATH:
        if method == "GET":
            return "list_unapplied_cash_applications", {}
        return "method_not_allowed", {}
    apply_journal_nested = UNAPPLIED_CASH_APPLICATION_JOURNAL_NESTED_PATH.fullmatch(path)
    if apply_journal_nested is not None:
        if method == "POST":
            return "unapplied_cash_application_journal_proposals", {
                "unapplied_cash_application_id": apply_journal_nested.group(1)
            }
        return "http_method_not_allowed", {}
    unapplied_application_match = UNAPPLIED_CASH_APPLICATION_ITEM_PATH.fullmatch(path)
    if unapplied_application_match is not None:
        if method == "GET":
            return "get_unapplied_cash_application", {
                "unapplied_cash_application_id": unapplied_application_match.group(1)
            }
        return "method_not_allowed", {}
    if path == DUNNING_EVENT_COLLECTION_PATH:
        if method == "GET":
            return "list_dunning_events", {}
        return "method_not_allowed", {}
    dunning_event_match = DUNNING_EVENT_ITEM_PATH.fullmatch(path)
    if dunning_event_match is not None:
        if method == "GET":
            return "get_dunning_event", {
                "dunning_event_id": dunning_event_match.group(1)
            }
        return "method_not_allowed", {}
    statement_match = BILLING_ACCOUNT_STATEMENT_PATH.fullmatch(path)
    if statement_match is not None:
        if method == "GET":
            return "account_statement", {"billing_account_id": statement_match.group(1)}
        return "method_not_allowed", {}
    spend_match = BILLING_ACCOUNT_RATED_SPEND_PATH.fullmatch(path)
    if spend_match is not None:
        if method == "GET":
            return "rated_spend", {"billing_account_id": spend_match.group(1)}
        return "method_not_allowed", {}
    budget_status_match = BILLING_ACCOUNT_BUDGET_STATUS_PATH.fullmatch(path)
    if budget_status_match is not None:
        if method == "GET":
            return "list_billing_account_budget_statuses", {
                "billing_account_id": budget_status_match.group(1)
            }
        return "method_not_allowed", {}
    budget_nested = BILLING_ACCOUNT_SPEND_BUDGETS_PATH.fullmatch(path)
    if budget_nested is not None:
        if method == "POST":
            return "spend_budgets", {"billing_account_id": budget_nested.group(1)}
        return "method_not_allowed", {}
    if path == SPEND_BUDGET_COLLECTION_PATH:
        if method == "GET":
            return "list_spend_budgets", {}
        return "method_not_allowed", {}
    budget_evaluation_match = SPEND_BUDGET_EVALUATION_PATH.fullmatch(path)
    if budget_evaluation_match is not None:
        if method == "GET":
            return "get_spend_budget_evaluation", {
                "spend_budget_id": budget_evaluation_match.group(1)
            }
        return "method_not_allowed", {}
    budget_over_signal_match = SPEND_BUDGET_OVER_SIGNAL_PATH.fullmatch(path)
    if budget_over_signal_match is not None:
        if method == "GET":
            return "get_spend_budget_over_signal", {
                "spend_budget_id": budget_over_signal_match.group(1)
            }
        if method == "POST":
            return "spend_budget_over_signals", {
                "spend_budget_id": budget_over_signal_match.group(1)
            }
        return "method_not_allowed", {}
    budget_match = SPEND_BUDGET_ITEM_PATH.fullmatch(path)
    if budget_match is not None:
        if method == "GET":
            return "get_spend_budget", {"spend_budget_id": budget_match.group(1)}
        return "method_not_allowed", {}
    if path == COLLECTION_AGING_PATH:
        if method == "GET":
            return "collection_aging", {}
        return "method_not_allowed", {}
    if path == COLLECTION_CASE_COLLECTION_PATH:
        if method == "POST":
            return "collection_cases", {}
        if method == "GET":
            return "list_collection_cases", {}
        return "method_not_allowed", {}
    collection_match = COLLECTION_CASE_ITEM_PATH.fullmatch(path)
    if collection_match is not None:
        if method == "GET":
            return "get_collection_case", {"collection_case_id": collection_match.group(1)}
        return "method_not_allowed", {}
    if path == PAYMENT_INTENT_COLLECTION_PATH:
        if method == "POST":
            return "payment_intents", {}
        if method == "GET":
            return "list_payment_intents", {}
        return "method_not_allowed", {}
    cancel_match = PAYMENT_CANCEL_PATH.fullmatch(path)
    if cancel_match is not None:
        if method == "POST":
            return "cancel_payment_intent", {"payment_intent_id": cancel_match.group(1)}
        return "method_not_allowed", {}
    payment_intent_match = PAYMENT_INTENT_ITEM_PATH.fullmatch(path)
    if payment_intent_match is not None:
        if method == "GET":
            return "get_payment_intent", {"payment_intent_id": payment_intent_match.group(1)}
        return "method_not_allowed", {}
    if path == PAYMENT_RECEIPT_COLLECTION_PATH:
        if method == "POST":
            return "payment_receipts", {}
        if method == "GET":
            return "list_payment_receipts", {}
        return "method_not_allowed", {}
    unapplied_nested = UNAPPLIED_CASH_NESTED_PATH.fullmatch(path)
    if unapplied_nested is not None:
        if method == "POST":
            return "unapplied_cash", {"payment_receipt_id": unapplied_nested.group(1)}
        return "method_not_allowed", {}
    leftover_journal_nested = UNAPPLIED_CASH_JOURNAL_NESTED_PATH.fullmatch(path)
    if leftover_journal_nested is not None:
        if method == "POST":
            return "unapplied_cash_journal_proposals", {
                "unapplied_cash_id": leftover_journal_nested.group(1)
            }
        return "http_method_not_allowed", {}
    refund_nested = UNAPPLIED_CASH_REFUND_NESTED_PATH.fullmatch(path)
    if refund_nested is not None:
        if method == "POST":
            return "unapplied_cash_refunds", {
                "unapplied_cash_id": refund_nested.group(1)
            }
        return "http_method_not_allowed", {}
    if path == UNAPPLIED_CASH_REFUND_COLLECTION_PATH:
        if method == "GET":
            return "list_unapplied_cash_refunds", {}
        return "method_not_allowed", {}
    refund_journal_nested = REFUND_JOURNAL_NESTED_PATH.fullmatch(path)
    if refund_journal_nested is not None:
        if method == "POST":
            return "refund_journal_proposals", {
                "unapplied_cash_refund_id": refund_journal_nested.group(1)
            }
        return "http_method_not_allowed", {}
    refund_match = UNAPPLIED_CASH_REFUND_ITEM_PATH.fullmatch(path)
    if refund_match is not None:
        if method == "GET":
            return "get_unapplied_cash_refund", {
                "unapplied_cash_refund_id": refund_match.group(1)
            }
        return "method_not_allowed", {}
    if path == UNAPPLIED_CASH_COLLECTION_PATH:
        if method == "GET":
            return "list_unapplied_cash", {}
        return "method_not_allowed", {}
    unapplied_match = UNAPPLIED_CASH_ITEM_PATH.fullmatch(path)
    if unapplied_match is not None:
        if method == "GET":
            return "get_unapplied_cash", {"unapplied_cash_id": unapplied_match.group(1)}
        return "method_not_allowed", {}
    payment_receipt_match = PAYMENT_RECEIPT_ITEM_PATH.fullmatch(path)
    if payment_receipt_match is not None:
        if method == "GET":
            return "get_payment_receipt", {"payment_receipt_id": payment_receipt_match.group(1)}
        return "method_not_allowed", {}
    if path == INVOICE_DRAFT_COLLECTION_PATH:
        if method == "POST":
            return "invoice_drafts", {}
        if method == "GET":
            return "list_invoice_drafts", {}
        return "method_not_allowed", {}
    issued_nested = ISSUED_INVOICE_NESTED_PATH.fullmatch(path)
    if issued_nested is not None:
        if method == "POST":
            return "issued_invoices", {"invoice_draft_id": issued_nested.group(1)}
        return "method_not_allowed", {}
    if path == ISSUED_INVOICE_COLLECTION_PATH:
        if method == "GET":
            return "list_issued_invoices", {}
        return "method_not_allowed", {}
    issued_void_nested = ISSUED_INVOICE_VOID_NESTED_PATH.fullmatch(path)
    if issued_void_nested is not None:
        if method == "POST":
            return "issued_invoice_voids", {
                "issued_invoice_id": issued_void_nested.group(1)
            }
        return "http_method_not_allowed", {}
    if path == ISSUED_INVOICE_VOID_COLLECTION_PATH:
        if method == "GET":
            return "list_issued_invoice_voids", {}
        return "method_not_allowed", {}
    void_journal_nested = VOID_JOURNAL_NESTED_PATH.fullmatch(path)
    if void_journal_nested is not None:
        if method == "POST":
            return "issued_invoice_void_journal_proposals", {
                "issued_invoice_void_id": void_journal_nested.group(1)
            }
        return "http_method_not_allowed", {}
    issued_void_match = ISSUED_INVOICE_VOID_ITEM_PATH.fullmatch(path)
    if issued_void_match is not None:
        if method == "GET":
            return "get_issued_invoice_void", {
                "issued_invoice_void_id": issued_void_match.group(1)
            }
        return "method_not_allowed", {}
    issued_match = ISSUED_INVOICE_ITEM_PATH.fullmatch(path)
    if issued_match is not None:
        if method == "GET":
            return "get_issued_invoice", {"issued_invoice_id": issued_match.group(1)}
        return "method_not_allowed", {}
    draft_match = INVOICE_DRAFT_ITEM_PATH.fullmatch(path)
    if draft_match is not None:
        if method == "GET":
            return "get_invoice_draft", {"invoice_draft_id": draft_match.group(1)}
        return "method_not_allowed", {}
    if path == "/v1/journal-proposals":
        if method == "POST":
            return "journal_proposals", {}
        if method == "GET":
            return "list_journal_proposals", {}
        return "method_not_allowed", {}
    if path == USAGE_EVENT_COLLECTION_PATH:
        if method == "POST":
            return "usage_events", {}
        if method == "GET":
            return "list_usage_events", {}
        return "method_not_allowed", {}
    usage_match = USAGE_EVENT_ITEM_PATH.fullmatch(path)
    if usage_match is not None:
        if method == "GET":
            return "get_usage_event", {"usage_event_id": usage_match.group(1)}
        return "method_not_allowed", {}
    if path == RATING_RUN_COLLECTION_PATH:
        if method == "POST":
            return "rating_runs", {}
        if method == "GET":
            return "list_rating_runs", {}
        return "method_not_allowed", {}
    rating_match = RATING_RUN_ITEM_PATH.fullmatch(path)
    if rating_match is not None:
        if method == "GET":
            return "get_rating_run", {"rating_run_id": rating_match.group(1)}
        return "method_not_allowed", {}
    if path == RATE_CARD_COLLECTION_PATH:
        if method == "POST":
            return "rate_cards", {}
        if method == "GET":
            return "list_rate_cards", {}
        return "method_not_allowed", {}
    if path == TAX_RATE_COLLECTION_PATH:
        if method == "POST":
            return "tax_rates", {}
        if method == "GET":
            return "list_tax_rates", {}
        return "method_not_allowed", {}
    tax_version_match = TAX_RATE_VERSION_ITEM_PATH.fullmatch(path)
    if tax_version_match is not None:
        if method == "GET":
            return "get_tax_rate_version", {"tax_rate_version": tax_version_match.group(1)}
        return "method_not_allowed", {}
    if path == TAX_ASSESSMENT_COLLECTION_PATH:
        if method == "POST":
            return "tax_assessments", {}
        if method == "GET":
            return "list_tax_assessments", {}
        return "method_not_allowed", {}
    tax_assessment_match = TAX_ASSESSMENT_ITEM_PATH.fullmatch(path)
    if tax_assessment_match is not None:
        if method == "GET":
            return "get_tax_assessment", {"tax_assessment_id": tax_assessment_match.group(1)}
        return "method_not_allowed", {}
    versions_match = RATE_CARD_VERSIONS_PATH.fullmatch(path)
    if versions_match is not None:
        if method == "GET":
            return "list_rate_card_versions", {"rate_card_id": versions_match.group(1)}
        return "method_not_allowed", {}
    card_match = RATE_CARD_ITEM_PATH.fullmatch(path)
    if card_match is not None:
        if method == "GET":
            return "get_rate_card", {"rate_card_id": card_match.group(1)}
        return "method_not_allowed", {}
    version_match = RATE_CARD_VERSION_ITEM_PATH.fullmatch(path)
    if version_match is not None:
        if method == "GET":
            return "get_rate_card_version", {"rate_card_version": version_match.group(1)}
        return "method_not_allowed", {}
    if path == CREDIT_ADJUSTMENT_COLLECTION_PATH:
        if method == "POST":
            return "credit_adjustments", {}
        if method == "GET":
            return "list_credit_adjustments", {}
        return "method_not_allowed", {}
    credit_journal_nested = CREDIT_JOURNAL_NESTED_PATH.fullmatch(path)
    if credit_journal_nested is not None:
        if method == "POST":
            return "credit_journal_proposals", {
                "credit_adjustment_id": credit_journal_nested.group(1)
            }
        return "http_method_not_allowed", {}
    credit_note_nested = ISSUED_CREDIT_NOTE_NESTED_PATH.fullmatch(path)
    if credit_note_nested is not None:
        if method == "POST":
            return "issued_credit_notes", {"credit_adjustment_id": credit_note_nested.group(1)}
        return "method_not_allowed", {}
    if path == ISSUED_CREDIT_NOTE_COLLECTION_PATH:
        if method == "GET":
            return "list_issued_credit_notes", {}
        return "method_not_allowed", {}
    credit_note_void_nested = ISSUED_CREDIT_NOTE_VOID_NESTED_PATH.fullmatch(path)
    if credit_note_void_nested is not None:
        if method == "POST":
            return "issued_credit_note_voids", {
                "issued_credit_note_id": credit_note_void_nested.group(1)
            }
        return "http_method_not_allowed", {}
    if path == ISSUED_CREDIT_NOTE_VOID_COLLECTION_PATH:
        if method == "GET":
            return "list_issued_credit_note_voids", {}
        return "method_not_allowed", {}
    credit_note_void_journal_nested = CREDIT_NOTE_VOID_JOURNAL_NESTED_PATH.fullmatch(path)
    if credit_note_void_journal_nested is not None:
        if method == "POST":
            return "issued_credit_note_void_journal_proposals", {
                "issued_credit_note_void_id": credit_note_void_journal_nested.group(1)
            }
        return "http_method_not_allowed", {}
    credit_note_void_match = ISSUED_CREDIT_NOTE_VOID_ITEM_PATH.fullmatch(path)
    if credit_note_void_match is not None:
        if method == "GET":
            return "get_issued_credit_note_void", {
                "issued_credit_note_void_id": credit_note_void_match.group(1)
            }
        return "method_not_allowed", {}
    credit_note_match = ISSUED_CREDIT_NOTE_ITEM_PATH.fullmatch(path)
    if credit_note_match is not None:
        if method == "GET":
            return "get_issued_credit_note", {
                "issued_credit_note_id": credit_note_match.group(1)
            }
        return "method_not_allowed", {}
    credit_match = CREDIT_ADJUSTMENT_ITEM_PATH.fullmatch(path)
    if credit_match is not None:
        if method == "GET":
            return "get_credit_adjustment", {"credit_adjustment_id": credit_match.group(1)}
        return "method_not_allowed", {}
    if path == POSTING_RECEIPT_COLLECTION_PATH:
        if method == "POST":
            return "posting_receipt_observations", {}
        if method == "GET":
            return "list_posting_receipt_observations", {}
        return "method_not_allowed", {}
    if path.startswith(POSTING_RECEIPT_ITEM_PREFIX):
        raw_key = unquote(path[len(POSTING_RECEIPT_ITEM_PREFIX) :])
        if not raw_key:
            return None, {}
        if method == "GET":
            return "get_posting_receipt_observation", {"idempotency_key": raw_key}
        return "method_not_allowed", {}
    proposal_match = JOURNAL_PROPOSAL_ITEM_PATH.fullmatch(path)
    if proposal_match is not None:
        if method == "GET":
            return "get_journal_proposal", {"proposal_id": proposal_match.group(1)}
        return "method_not_allowed", {}
    if path in KNOWN_POST_PATHS:
        if method == "POST":
            return path.removeprefix("/v1/").replace("-", "_"), {}
        return "method_not_allowed", {}
    return None, {}


def _read_query(environ: WSGIEnvironment) -> dict[str, str]:
    """Return the first value for each query-string field."""
    raw_query = str(environ.get("QUERY_STRING") or "")
    parsed = parse_qs(raw_query, keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items()}


def _read_json_object(environ: WSGIEnvironment) -> dict[str, Any]:
    """Read one JSON object from the WSGI input stream."""
    length_text = environ.get("CONTENT_LENGTH") or "0"
    try:
        content_length = int(length_text)
    except ValueError as error:
        raise HttpRequestError("request_invalid") from error
    input_stream = environ.get("wsgi.input")
    if content_length < 0 or input_stream is None:
        raise HttpRequestError("request_invalid")
    raw = input_stream.read(content_length)
    if not raw:
        raise HttpRequestError("request_invalid")
    try:
        loaded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HttpRequestError("request_invalid") from error
    if not isinstance(loaded, dict):
        raise HttpRequestError("request_invalid")
    return loaded


def _extract_api_key(environ: WSGIEnvironment) -> str | None:
    """Return the Bearer or X-CWL-Api-Key secret, if present.

    Both headers may be sent when they match.  A scheme other than Bearer, an
    empty secret, or a header mismatch fails closed.
    """
    authorization = environ.get(AUTHORIZATION_HEADER_ENVIRON)
    header_key = environ.get(API_KEY_HEADER_ENVIRON)
    bearer: str | None = None
    if isinstance(authorization, str) and authorization:
        if not authorization.startswith("Bearer "):
            raise HttpRequestError("api_credential_invalid")
        bearer = authorization[len("Bearer ") :]
        if not bearer:
            raise HttpRequestError("api_credential_invalid")
    api_key = header_key if isinstance(header_key, str) and header_key else None
    if bearer is not None and api_key is not None:
        if bearer != api_key:
            raise HttpRequestError("request_invalid")
        return bearer
    if bearer is not None:
        return bearer
    return api_key


def _header_tenant(environ: WSGIEnvironment) -> str | None:
    """Return the optional X-CWL-Tenant-Reference pin, if present."""
    raw_value = environ.get(TENANT_HEADER_ENVIRON)
    if not isinstance(raw_value, str) or not raw_value:
        return None
    return raw_value


def _payload_tenant(payload: Mapping[str, Any]) -> str | None:
    """Return tenant_reference from a JSON body or query string, if present."""
    tenant_reference = payload.get("tenant_reference")
    if not isinstance(tenant_reference, str) or not tenant_reference:
        return None
    return tenant_reference


def _require_tenant(payload: Mapping[str, Any]) -> str:
    """Return the write tenant or reject a request that omitted it."""
    tenant_reference = _payload_tenant(payload)
    if tenant_reference is None:
        raise HttpRequestError("tenant_not_found")
    return tenant_reference


def _resolve_tenant_pin(environ: WSGIEnvironment, payload: Mapping[str, Any]) -> str:
    """Resolve the tenant from header, body/query, or both when they agree.

    ``X-CWL-Tenant-Reference`` is optional.  Body or query ``tenant_reference``
    still works when the header is absent.  If both are present they must be
    identical; a mismatch is ``request_invalid``.
    """
    header_tenant = _header_tenant(environ)
    payload_tenant = _payload_tenant(payload)
    if header_tenant is not None and payload_tenant is not None:
        if header_tenant != payload_tenant:
            raise HttpRequestError("request_invalid")
        return header_tenant
    if header_tenant is not None:
        return header_tenant
    return _require_tenant(payload)


def _parse_rate_card_version(value: object) -> UUID | int:
    """Parse a published version identifier or tenant-scoped version number."""
    if not isinstance(value, str) or not value:
        raise HttpRequestError("request_invalid")
    if value.isdigit():
        return int(value)
    try:
        return UUID(value)
    except ValueError as error:
        raise HttpRequestError("request_invalid") from error


def _parse_uuid(value: object, field_name: str) -> UUID:
    """Parse a UUID string from a body field or path segment."""
    del field_name
    if not isinstance(value, str):
        raise HttpRequestError("request_invalid")
    try:
        return UUID(value)
    except ValueError as error:
        raise HttpRequestError("request_invalid") from error


def _dispatch_write(
    route_name: str,
    path_values: Mapping[str, str],
    tenant_reference: str,
    payload: Mapping[str, Any],
    ingestion: UsageIngestionService,
    rating: UsageRatingService,
    drafts: InvoiceDraftService,
    exports: AccountingExportService,
    collections: CollectionCaseService,
    intents: PaymentIntentService,
    settlements: PaymentSettlementService,
    credits: CreditAdjustmentService | None = None,
    catalogs: RateCardService | None = None,
    tax_rates: TaxRateService | None = None,
    assessments: TaxAssessmentService | None = None,
) -> tuple[dict[str, object], int]:
    """Call one commercial service and map its contract to an HTTP status."""
    if route_name == "usage_events":
        if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
            raise HttpRequestError("request_invalid")
        events = payload.get("events")
        if isinstance(events, list):
            for event in events:
                if isinstance(event, Mapping) and FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(event):
                    raise HttpRequestError("request_invalid")
            receipt = ingestion.ingest_usage_batch(events)
            body = receipt.as_contract_dict()
            accepted = int(body["accepted_event_count"])
            replays = int(body["duplicate_replay_count"])
            rejected = int(body["rejected_event_count"])
            status_code = 422 if rejected > 0 and accepted == 0 and replays == 0 else 200
            return body, status_code
        result = ingestion.ingest_usage_event(payload)
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "rating_runs":
        if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
            raise HttpRequestError("request_invalid")
        window = TimeWindow.from_iso8601(
            str(payload.get("window_started_at")),
            str(payload.get("window_ended_at")),
        )
        rate_card_version = payload.get("rate_card_version")
        if not isinstance(rate_card_version, int) or isinstance(rate_card_version, bool):
            raise HttpRequestError("request_invalid")
        result = rating.rate_usage_window(tenant_reference, window, rate_card_version)
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "invoice_drafts":
        result = drafts.draft_invoice(
            tenant_reference, _parse_uuid(payload.get("rating_run_id"), "rating_run_id")
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "journal_proposals":
        result = exports.propose_journal(
            tenant_reference, _parse_uuid(payload.get("invoice_draft_id"), "invoice_draft_id")
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "collection_cases":
        result = collections.open_collection_case(
            tenant_reference, _parse_uuid(payload.get("invoice_draft_id"), "invoice_draft_id")
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "dunning_events":
        if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
            raise HttpRequestError("request_invalid")
        notice_code = payload.get("dunning_notice_code")
        if not isinstance(notice_code, str):
            raise HttpRequestError("request_invalid")
        result = collections.record_dunning_event(
            tenant_reference,
            _parse_uuid(path_values["collection_case_id"], "collection_case_id"),
            notice_code,
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "payment_intents":
        if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
            raise HttpRequestError("request_invalid")
        result = intents.project_payment_intent(
            tenant_reference,
            _parse_uuid(payload.get("collection_case_id"), "collection_case_id"),
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "payment_receipts":
        if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
            raise HttpRequestError("request_invalid")
        result = settlements.record_payment_receipt(
            tenant_reference,
            _parse_uuid(payload.get("payment_intent_id"), "payment_intent_id"),
            payload.get("received_amount"),
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "cancel_payment_intent":
        result = settlements.cancel_payment_intent(
            tenant_reference,
            _parse_uuid(path_values["payment_intent_id"], "payment_intent_id"),
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "cash_journal_proposals":
        result = exports.propose_cash_journal(
            tenant_reference,
            _parse_uuid(payload.get("payment_receipt_id"), "payment_receipt_id"),
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "issued_invoice_void_journal_proposals":
        if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
            raise HttpRequestError("request_invalid")
        currency_code = payload.get("currency_code")
        if currency_code is not None and not isinstance(currency_code, str):
            raise HttpRequestError("request_invalid")
        result = exports.propose_void_journal(
            tenant_reference,
            _parse_uuid(path_values["issued_invoice_void_id"], "issued_invoice_void_id"),
            currency_code=currency_code,
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "issued_credit_note_void_journal_proposals":
        if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
            raise HttpRequestError("request_invalid")
        currency_code = payload.get("currency_code")
        if currency_code is not None and not isinstance(currency_code, str):
            raise HttpRequestError("request_invalid")
        result = exports.propose_credit_note_void_journal(
            tenant_reference,
            _parse_uuid(
                path_values["issued_credit_note_void_id"],
                "issued_credit_note_void_id",
            ),
            currency_code=currency_code,
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "write_off_journal_proposals":
        if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
            raise HttpRequestError("request_invalid")
        currency_code = payload.get("currency_code")
        if currency_code is not None and not isinstance(currency_code, str):
            raise HttpRequestError("request_invalid")
        result = exports.propose_write_off_journal(
            tenant_reference,
            _parse_uuid(path_values["collection_write_off_id"], "collection_write_off_id"),
            currency_code=currency_code,
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "refund_journal_proposals":
        if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
            raise HttpRequestError("request_invalid")
        currency_code = payload.get("currency_code")
        if currency_code is not None and not isinstance(currency_code, str):
            raise HttpRequestError("request_invalid")
        result = exports.propose_refund_journal(
            tenant_reference,
            _parse_uuid(path_values["unapplied_cash_refund_id"], "unapplied_cash_refund_id"),
            currency_code=currency_code,
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "unapplied_cash_journal_proposals":
        if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
            raise HttpRequestError("request_invalid")
        currency_code = payload.get("currency_code")
        if currency_code is not None and not isinstance(currency_code, str):
            raise HttpRequestError("request_invalid")
        result = exports.propose_unapplied_cash_journal(
            tenant_reference,
            _parse_uuid(path_values["unapplied_cash_id"], "unapplied_cash_id"),
            currency_code=currency_code,
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "unapplied_cash_application_journal_proposals":
        if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
            raise HttpRequestError("request_invalid")
        currency_code = payload.get("currency_code")
        if currency_code is not None and not isinstance(currency_code, str):
            raise HttpRequestError("request_invalid")
        result = exports.propose_unapplied_cash_application_journal(
            tenant_reference,
            _parse_uuid(
                path_values["unapplied_cash_application_id"],
                "unapplied_cash_application_id",
            ),
            currency_code=currency_code,
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "credit_journal_proposals":
        if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
            raise HttpRequestError("request_invalid")
        currency_code = payload.get("currency_code")
        if currency_code is not None and not isinstance(currency_code, str):
            raise HttpRequestError("request_invalid")
        result = exports.propose_credit_journal(
            tenant_reference,
            _parse_uuid(path_values["credit_adjustment_id"], "credit_adjustment_id"),
            currency_code=currency_code,
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "credit_adjustments":
        if credits is None:
            raise HttpRequestError("request_invalid")
        if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
            raise HttpRequestError("request_invalid")
        reason = payload.get("credit_reason_code")
        if not isinstance(reason, str):
            raise HttpRequestError("request_invalid")
        result = credits.record_credit_adjustment(
            tenant_reference,
            _parse_uuid(payload.get("invoice_draft_id"), "invoice_draft_id"),
            payload.get("credit_amount"),
            reason,
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "rate_cards":
        if catalogs is None:
            raise HttpRequestError("request_invalid")
        if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
            raise HttpRequestError("request_invalid")
        rate_card_name = payload.get("rate_card_name")
        currency_code = payload.get("currency_code")
        lines = payload.get("lines")
        if (
            not isinstance(rate_card_name, str)
            or not isinstance(currency_code, str)
            or not isinstance(lines, list)
        ):
            raise HttpRequestError("request_invalid")
        result = catalogs.publish_rate_card(
            tenant_reference, rate_card_name, currency_code, lines
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "tax_rates":
        if tax_rates is None:
            raise HttpRequestError("request_invalid")
        result = tax_rates.publish_tax_rate(
            tenant_reference, payload.get("tax_code"), payload.get("tax_rate")
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "tax_assessments":
        if assessments is None:
            raise HttpRequestError("request_invalid")
        if FORBIDDEN_PAYMENT_INTENT_KEYS.intersection(payload):
            raise HttpRequestError("request_invalid")
        version_value = payload.get("tax_rate_version")
        if isinstance(version_value, str):
            parsed_version = _parse_rate_card_version(version_value)
        elif isinstance(version_value, int) and not isinstance(version_value, bool):
            parsed_version = version_value
        else:
            raise HttpRequestError("request_invalid")
        result = assessments.assess_tax(
            tenant_reference,
            _parse_uuid(payload.get("invoice_draft_id"), "invoice_draft_id"),
            parsed_version,
        )
        return result.as_contract_dict(), _status_for_result(result)
    raise HttpRequestError("request_invalid")


def _status_for_result(result: object) -> int:
    """Map a service outcome to HTTP 200 or 422.

    Published journal proposals omit ``*_outcome_code`` from ``as_contract_dict``.
    Status therefore comes from the in-process result, not from JSON shape.
    """
    for name in dir(result):
        if not name.endswith("_outcome_code"):
            continue
        value = getattr(result, name)
        text = value.value if hasattr(value, "value") else str(value)
        if text in SUCCESS_OUTCOMES:
            return 200
        return 422
    return 422


def _status_for_contract(payload: Mapping[str, object]) -> int:
    """Map accepted and replay outcome fields to 200; everything else stays 422."""
    for key, value in payload.items():
        if not key.endswith("_outcome_code"):
            continue
        if value in SUCCESS_OUTCOMES:
            return 200
        return 422
    if payload.get("proposal_id") and payload.get("proposal_status") != "rejected":
        return 200
    return 422


def _send_json(
    start_response: StartResponse, status_code: int, payload: Mapping[str, object]
) -> Iterable[bytes]:
    """Write a JSON response and return the encoded body."""
    reason = {
        200: "OK",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        422: "Unprocessable Entity",
    }.get(status_code, "OK")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    start_response(
        f"{status_code} {reason}",
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ],
    )
    return [body]


if __name__ == "__main__":
    raise SystemExit(main())
