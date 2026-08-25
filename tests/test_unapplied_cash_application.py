"""Unapplied-cash application tests for applying leftover to another case."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    CollectionCaseService,
    CollectionCaseSettlementService,
    IssuedInvoiceService,
    PaymentIntentService,
    PaymentSettlementService,
    UnappliedCashApplicationPresentmentService,
    UnappliedCashApplicationService,
    UnappliedCashService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.contracts import (
    validate_unapplied_cash_application,
    validate_unapplied_cash_application_presentment,
)
from metering_billing.errors import (
    UnappliedCashApplicationOutcomeCode,
    UnappliedCashApplicationPresentmentQueryError,
    UnappliedCashApplicationRejectionReasonCode,
)
from metering_billing.unapplied_cash_application import (
    UNAPPLIED_CASH_APPLICATION_CONTRACT_VERSION,
    UnappliedCashApplicationResult,
    compute_unapplied_cash_application_payload_hash,
    _enqueue_unapplied_cash_applied,
    _format_applied_at,
    _rejected,
)
from metering_billing.webhook_outbox import EVENT_TYPE_UNAPPLIED_CASH_APPLIED
from metering_billing.usage_ledger import StoredUnappliedCashApplication, generate_record_id
from test_http_app import invoke_http
from test_payment_receipt_presentment import apply_known_morning_receipt
from test_tax_assessment import insert_commercial_draft
from test_unapplied_cash import LEFTOVER, PARKED_MORNING
from test_usage_ingestion import TENANT_ONE, TENANT_TWO


APPLIED_MORNING = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
APPLIED_EVENING = datetime(2026, 8, 18, 22, 0, tzinfo=UTC)
SECOND_CASE_AMOUNT = Decimal("20.00")
TINY_CASE_AMOUNT = LEFTOVER


def park_leftover_and_open_second_case(second_amount=SECOND_CASE_AMOUNT):
    """Park leftover on the morning receipt, then open another same-tenant case."""
    ledger, payment_receipt_id, _intent_id, source_case_id = apply_known_morning_receipt()
    parked = UnappliedCashService(ledger, clock=lambda: PARKED_MORNING).park_unapplied_cash(
        TENANT_ONE, payment_receipt_id, unapplied_amount=LEFTOVER
    )
    draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", second_amount)
    collection = CollectionCaseService(ledger).open_collection_case(TENANT_ONE, draft_id)
    return ledger, parked, collection, source_case_id, payment_receipt_id


class UnappliedCashApplicationTests(unittest.TestCase):
    """Verify leftover applies once to another open case without auto-settle."""

    def test_apply_full_parked_amount_once_without_settling(self) -> None:
        """Full leftover reduces another case once; replay returns the stored id."""
        ledger, parked, collection, source_case_id, payment_receipt_id = (
            park_leftover_and_open_second_case()
        )
        prior_journals = len(ledger.journal_proposals)
        prior_outbox = len(ledger.webhook_outbox_events)
        prior_receipts = len(ledger.payment_receipts)
        prior_write_offs = len(ledger.collection_write_offs)
        prior_settlements = len(ledger.collection_case_settlements)
        prior_parked = len(ledger.unapplied_cash)
        remaining_before = collection.outstanding_amount
        first = UnappliedCashApplicationService(
            ledger, clock=lambda: APPLIED_MORNING
        ).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, collection.collection_case_id
        )
        second = UnappliedCashApplicationService(
            ledger, clock=lambda: APPLIED_EVENING
        ).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, collection.collection_case_id
        )
        self.assertEqual(
            first.unapplied_cash_application_outcome_code,
            UnappliedCashApplicationOutcomeCode.ACCEPTED,
        )
        self.assertEqual(
            second.unapplied_cash_application_outcome_code,
            UnappliedCashApplicationOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertEqual(
            first.unapplied_cash_application_id, second.unapplied_cash_application_id
        )
        self.assertEqual(first.unapplied_cash_id, parked.unapplied_cash_id)
        self.assertEqual(first.collection_case_id, collection.collection_case_id)
        self.assertEqual(first.payment_receipt_id, payment_receipt_id)
        self.assertEqual(first.invoice_draft_id, collection.invoice_draft_id)
        self.assertEqual(first.currency_code, "USD")
        self.assertEqual(first.applied_amount, LEFTOVER)
        self.assertEqual(first.remaining_outstanding_amount, remaining_before - LEFTOVER)
        self.assertEqual(first.unapplied_cash_application_status, "applied")
        self.assertEqual(first.collection_case_status, "open")
        self.assertEqual(first.next_operator_action, "collect")
        self.assertEqual(first.applied_at, APPLIED_MORNING)
        self.assertEqual(second.applied_at, APPLIED_MORNING)
        self.assertTrue(first.source_payload_hash.startswith("sha256:"))
        payload = first.as_contract_dict()
        self.assertEqual(validate_unapplied_cash_application(payload), ())
        self.assertIsInstance(payload["applied_amount"], str)
        self.assertNotIsInstance(payload["applied_amount"], float)
        self.assertNotIn("card_pan", payload)
        self.assertNotIn("legal_invoice_number", payload)
        stored_case = ledger.get_collection_case(collection.collection_case_id)
        self.assertEqual(stored_case.outstanding_amount, remaining_before - LEFTOVER)
        self.assertEqual(stored_case.collection_case_status, "open")
        source_case = ledger.get_collection_case(source_case_id)
        self.assertEqual(source_case.collection_case_status, "settled")
        stored_cash = ledger.get_unapplied_cash(parked.unapplied_cash_id)
        self.assertEqual(stored_cash.unapplied_cash_status, "parked")
        self.assertEqual(stored_cash.unapplied_amount, LEFTOVER)
        other_draft = insert_commercial_draft(ledger, TENANT_ONE, "USD", SECOND_CASE_AMOUNT)
        other_case = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, other_draft
        )
        replay_other = UnappliedCashApplicationService(ledger).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, other_case.collection_case_id
        )
        self.assertEqual(
            replay_other.unapplied_cash_application_outcome_code,
            UnappliedCashApplicationOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertEqual(replay_other.collection_case_id, collection.collection_case_id)
        self.assertEqual(len(ledger.unapplied_cash_applications), 1)
        self.assertEqual(len(ledger.unapplied_cash), prior_parked)
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox + 1)
        applied_events = [
            event
            for event in ledger.webhook_outbox_events.values()
            if event.event_type_code == EVENT_TYPE_UNAPPLIED_CASH_APPLIED
        ]
        self.assertEqual(len(applied_events), 1)
        self.assertEqual(applied_events[0].source_id, first.unapplied_cash_application_id)
        self.assertEqual(len(ledger.payment_receipts), prior_receipts)
        self.assertEqual(len(ledger.collection_write_offs), prior_write_offs)
        self.assertEqual(len(ledger.collection_case_settlements), prior_settlements)
        presented = UnappliedCashApplicationPresentmentService(
            ledger
        ).present_unapplied_cash_application(
            TENANT_ONE, first.unapplied_cash_application_id
        )
        self.assertEqual(presented.applied_amount, LEFTOVER)
        self.assertEqual(presented.next_operator_action, "collect")
        self.assertEqual(
            validate_unapplied_cash_application_presentment(presented.as_contract_dict()),
            (),
        )

    def test_replay_heals_insert_without_outstanding_reduction(self) -> None:
        """A crash after apply insert and before reduce is healed on replay."""
        ledger, parked, collection, _source_case_id, _payment_receipt_id = (
            park_leftover_and_open_second_case()
        )
        leftover = ledger.get_unapplied_cash(parked.unapplied_cash_id)
        self.assertIsNotNone(leftover)
        remaining_before = collection.outstanding_amount
        crash_hash = compute_unapplied_cash_application_payload_hash(
            {
                "unapplied_cash_id": str(leftover.unapplied_cash_id),
                "collection_case_id": str(collection.collection_case_id),
                "payment_receipt_id": str(leftover.payment_receipt_id),
                "currency_code": leftover.currency_code,
                "applied_amount": format_exact_decimal(LEFTOVER),
                "unapplied_amount": format_exact_decimal(leftover.unapplied_amount),
                "unapplied_cash_application_contract_version": 1,
            }
        )
        inserted = ledger.insert_unapplied_cash_application(
            StoredUnappliedCashApplication(
                unapplied_cash_application_id=generate_record_id(),
                tenant_account_id=leftover.tenant_account_id,
                unapplied_cash_id=leftover.unapplied_cash_id,
                collection_case_id=collection.collection_case_id,
                payment_receipt_id=leftover.payment_receipt_id,
                invoice_draft_id=collection.invoice_draft_id,
                unapplied_cash_application_contract_version=1,
                source_payload_hash=crash_hash,
                currency_code=leftover.currency_code,
                applied_amount=LEFTOVER,
                unapplied_cash_application_status="applied",
                applied_at=APPLIED_MORNING,
            )
        )
        unreduced = ledger.get_collection_case(collection.collection_case_id)
        self.assertEqual(unreduced.outstanding_amount, remaining_before)
        self.assertEqual(unreduced.collection_case_status, "open")
        healed = UnappliedCashApplicationService(ledger).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, collection.collection_case_id
        )
        self.assertEqual(
            healed.unapplied_cash_application_outcome_code,
            UnappliedCashApplicationOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertEqual(
            healed.unapplied_cash_application_id, inserted.unapplied_cash_application_id
        )
        reduced = ledger.get_collection_case(collection.collection_case_id)
        self.assertEqual(reduced.outstanding_amount, remaining_before - LEFTOVER)
        self.assertEqual(reduced.collection_case_status, "open")
        self.assertEqual(healed.remaining_outstanding_amount, remaining_before - LEFTOVER)
        already_healed = UnappliedCashApplicationService(ledger).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, collection.collection_case_id
        )
        self.assertEqual(
            already_healed.unapplied_cash_application_outcome_code,
            UnappliedCashApplicationOutcomeCode.DUPLICATE_REPLAY,
        )
        still_reduced = ledger.get_collection_case(collection.collection_case_id)
        self.assertEqual(still_reduced.outstanding_amount, remaining_before - LEFTOVER)
        self.assertEqual(still_reduced.collection_case_status, "open")
        self.assertEqual(len(ledger.unapplied_cash_applications), 1)

    def test_zeroing_remaining_does_not_auto_settle(self) -> None:
        """Exact leftover that zeros remaining leaves settle to #46."""
        ledger, parked, collection, _source_case_id, _receipt_id = (
            park_leftover_and_open_second_case(TINY_CASE_AMOUNT)
        )
        applied = UnappliedCashApplicationService(ledger).apply_unapplied_cash(
            TENANT_ONE,
            parked.unapplied_cash_id,
            collection.collection_case_id,
            applied_amount=LEFTOVER,
            currency_code="USD",
        )
        self.assertEqual(
            applied.unapplied_cash_application_outcome_code,
            UnappliedCashApplicationOutcomeCode.ACCEPTED,
        )
        self.assertEqual(applied.remaining_outstanding_amount, Decimal("0"))
        self.assertEqual(applied.collection_case_status, "open")
        self.assertEqual(applied.next_operator_action, "settle")
        stored_case = ledger.get_collection_case(collection.collection_case_id)
        self.assertEqual(stored_case.outstanding_amount, Decimal("0"))
        self.assertEqual(stored_case.collection_case_status, "open")
        self.assertEqual(len(ledger.collection_case_settlements), 0)
        zeroed = UnappliedCashApplicationPresentmentService(
            ledger
        ).present_unapplied_cash_application(
            TENANT_ONE, applied.unapplied_cash_application_id
        )
        self.assertEqual(zeroed.next_operator_action, "settle")
        self.assertEqual(zeroed.collection_case_status, "open")
        self.assertEqual(
            validate_unapplied_cash_application_presentment(zeroed.as_contract_dict()),
            (),
        )
        settled = CollectionCaseSettlementService(ledger).settle_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(settled.collection_case_settlement_outcome_code.value, "accepted")
        replayed = UnappliedCashApplicationService(ledger).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, collection.collection_case_id
        )
        self.assertEqual(
            replayed.unapplied_cash_application_outcome_code,
            UnappliedCashApplicationOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertEqual(replayed.next_operator_action, "wait")
        self.assertEqual(replayed.collection_case_status, "settled")
        presented = UnappliedCashApplicationPresentmentService(
            ledger
        ).present_unapplied_cash_application(
            TENANT_ONE, applied.unapplied_cash_application_id
        )
        self.assertEqual(presented.next_operator_action, "wait")
        self.assertEqual(presented.collection_case_status, "settled")
        self.assertEqual(
            validate_unapplied_cash_application_presentment(presented.as_contract_dict()),
            (),
        )

    def test_fail_closed_on_settled_currency_over_and_mismatch(self) -> None:
        """Settled cases, currency mismatch, and oversized leftover refuse."""
        ledger, parked, collection, source_case_id, _receipt_id = (
            park_leftover_and_open_second_case()
        )
        service = UnappliedCashApplicationService(ledger)
        settled = service.apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, source_case_id
        )
        self.assertEqual(
            settled.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.COLLECTION_CASE_SETTLED,
        )
        currency = service.apply_unapplied_cash(
            TENANT_ONE,
            parked.unapplied_cash_id,
            collection.collection_case_id,
            currency_code="EUR",
        )
        self.assertEqual(
            currency.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.CURRENCY_MISMATCH,
        )
        exceeds = service.apply_unapplied_cash(
            TENANT_ONE,
            parked.unapplied_cash_id,
            collection.collection_case_id,
            applied_amount=Decimal("21.00"),
        )
        self.assertEqual(
            exceeds.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.APPLIED_AMOUNT_EXCEEDS_PARKED,
        )
        mismatch = service.apply_unapplied_cash(
            TENANT_ONE,
            parked.unapplied_cash_id,
            collection.collection_case_id,
            applied_amount=Decimal("0.0005"),
        )
        self.assertEqual(
            mismatch.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.APPLIED_AMOUNT_MISMATCH,
        )
        tiny_draft = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("0.0001"))
        tiny_case = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, tiny_draft
        )
        over_remaining = service.apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, tiny_case.collection_case_id
        )
        self.assertEqual(
            over_remaining.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.APPLIED_AMOUNT_EXCEEDS_OUTSTANDING,
        )
        self.assertEqual(len(ledger.unapplied_cash_applications), 0)
        eur_draft = insert_commercial_draft(ledger, TENANT_ONE, "EUR", SECOND_CASE_AMOUNT)
        eur_case = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, eur_draft
        )
        case_currency = service.apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, eur_case.collection_case_id
        )
        self.assertEqual(
            case_currency.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.CURRENCY_MISMATCH,
        )

    def test_missing_and_cross_tenant_inputs_fail_closed(self) -> None:
        """A tenant cannot apply another tenant's leftover or case."""
        ledger, parked, collection, _source_case_id, _receipt_id = (
            park_leftover_and_open_second_case()
        )
        service = UnappliedCashApplicationService(ledger)
        missing_tenant = service.apply_unapplied_cash(
            "urn:cwl:missing", parked.unapplied_cash_id, collection.collection_case_id
        )
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.TENANT_NOT_FOUND,
        )
        missing_cash = service.apply_unapplied_cash(
            TENANT_ONE, generate_record_id(), collection.collection_case_id
        )
        self.assertEqual(
            missing_cash.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.UNAPPLIED_CASH_NOT_FOUND,
        )
        missing_case = service.apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, generate_record_id()
        )
        self.assertEqual(
            missing_case.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )
        crossed_cash = service.apply_unapplied_cash(
            TENANT_TWO, parked.unapplied_cash_id, collection.collection_case_id
        )
        self.assertEqual(
            crossed_cash.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.UNAPPLIED_CASH_NOT_FOUND,
        )
        crossed_case = service.apply_unapplied_cash(
            TENANT_TWO, generate_record_id(), collection.collection_case_id
        )
        self.assertEqual(
            crossed_case.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.UNAPPLIED_CASH_NOT_FOUND,
        )
        self.assertEqual(len(ledger.unapplied_cash_applications), 0)

    def test_resolver_ieee_zero_negative_and_negative_remaining_fail_closed(self) -> None:
        """Hollow resolve raises; IEEE, zero, negative leftover, and negative remaining refuse."""
        ledger, parked, collection, _source_case_id, _receipt_id = (
            park_leftover_and_open_second_case()
        )
        service = UnappliedCashApplicationService(ledger)
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.apply_unapplied_cash(
                    TENANT_ONE, parked.unapplied_cash_id, collection.collection_case_id
                )
        floated = service.apply_unapplied_cash(
            TENANT_ONE,
            parked.unapplied_cash_id,
            collection.collection_case_id,
            applied_amount=0.001,  # type: ignore[arg-type]
        )
        self.assertEqual(
            floated.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.REQUEST_INVALID,
        )
        zero = service.apply_unapplied_cash(
            TENANT_ONE,
            parked.unapplied_cash_id,
            collection.collection_case_id,
            applied_amount=Decimal("0"),
        )
        self.assertEqual(
            zero.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.APPLIED_AMOUNT_ZERO,
        )
        negative = service.apply_unapplied_cash(
            TENANT_ONE,
            parked.unapplied_cash_id,
            collection.collection_case_id,
            applied_amount=Decimal("-1"),
        )
        self.assertEqual(
            negative.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.APPLIED_AMOUNT_NEGATIVE,
        )
        nan_amount = service.apply_unapplied_cash(
            TENANT_ONE,
            parked.unapplied_cash_id,
            collection.collection_case_id,
            applied_amount=Decimal("NaN"),
        )
        self.assertEqual(
            nan_amount.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.REQUEST_INVALID,
        )
        infinite = service.apply_unapplied_cash(
            TENANT_ONE,
            parked.unapplied_cash_id,
            collection.collection_case_id,
            applied_amount=Decimal("Infinity"),
        )
        self.assertEqual(
            infinite.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.REQUEST_INVALID,
        )
        empty = UnappliedCashApplicationService()
        self.assertEqual(
            empty.apply_unapplied_cash(
                TENANT_ONE, parked.unapplied_cash_id, collection.collection_case_id
            ).rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.TENANT_NOT_FOUND,
        )
        stored_case = ledger.get_collection_case(collection.collection_case_id)
        ledger.collection_cases[collection.collection_case_id] = type(stored_case)(
            collection_case_id=stored_case.collection_case_id,
            tenant_account_id=stored_case.tenant_account_id,
            invoice_draft_id=stored_case.invoice_draft_id,
            currency_code=stored_case.currency_code,
            collection_case_status=stored_case.collection_case_status,
            outstanding_amount=Decimal("-0.01"),
            opened_at=stored_case.opened_at,
        )
        negative_remaining = service.apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, collection.collection_case_id
        )
        self.assertEqual(
            negative_remaining.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.OUTSTANDING_NEGATIVE,
        )
        self.assertEqual(len(ledger.unapplied_cash_applications), 0)

    def test_http_applies_lists_and_refuses_pan(self) -> None:
        """POST applies leftover; GET item and list never capture or post."""
        ledger, first_parked, first_case, _source_case_id, _receipt_id = (
            park_leftover_and_open_second_case()
        )
        second_parked, second_case = _second_parked_and_case(ledger)
        prior_journals = len(ledger.journal_proposals)
        prior_outbox = len(ledger.webhook_outbox_events)
        app = create_http_app(ledger)
        refused_status, refused_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/unapplied-cash-applications",
            {
                "tenant_reference": TENANT_ONE,
                "unapplied_cash_id": str(first_parked.unapplied_cash_id),
                "card_pan": "4111111111111111",
            },
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "request_invalid")
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/unapplied-cash-applications",
            {
                "tenant_reference": TENANT_ONE,
                "unapplied_cash_id": str(first_parked.unapplied_cash_id),
                "applied_amount": format_exact_decimal(LEFTOVER),
                "currency_code": "USD",
            },
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["unapplied_cash_application_outcome_code"], "accepted")
        self.assertEqual(accepted_body["applied_amount"], format_exact_decimal(LEFTOVER))
        self.assertEqual(accepted_body["collection_case_status"], "open")
        application_id = accepted_body["unapplied_cash_application_id"]
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/unapplied-cash-applications",
            {
                "tenant_reference": TENANT_ONE,
                "unapplied_cash_id": str(first_parked.unapplied_cash_id),
            },
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["unapplied_cash_application_outcome_code"], "duplicate_replay")
        self.assertEqual(replay_body["unapplied_cash_application_id"], application_id)
        second_status, second_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{second_case.collection_case_id}/unapplied-cash-applications",
            {
                "tenant_reference": TENANT_ONE,
                "unapplied_cash_id": str(second_parked.unapplied_cash_id),
            },
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(second_body["unapplied_cash_application_outcome_code"], "accepted")
        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/unapplied-cash-applications/{application_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["unapplied_cash_application_id"], application_id)
        self.assertEqual(validate_unapplied_cash_application_presentment(get_body), ())
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/unapplied-cash-applications/{application_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(
            other_body["rejection_reason_code"], "unapplied_cash_application_not_found"
        )
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/unapplied-cash-applications",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(len(list_body["unapplied_cash_applications"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        page_two_status, page_two = invoke_http(
            app,
            "GET",
            "/v1/unapplied-cash-applications",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(page_two_status, 200)
        self.assertEqual(len(page_two["unapplied_cash_applications"]), 1)
        self.assertIsNone(page_two["next_cursor"])
        missing_tenant_status, missing_tenant_body = invoke_http(
            app,
            "GET",
            f"/v1/unapplied-cash-applications/{application_id}",
        )
        self.assertEqual(missing_tenant_status, 422)
        self.assertEqual(missing_tenant_body["rejection_reason_code"], "tenant_not_found")
        method_status, _method_body = invoke_http(
            app,
            "POST",
            "/v1/unapplied-cash-applications",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 422)
        nested_method_status, _nested_method = invoke_http(
            app,
            "PUT",
            f"/v1/collection-cases/{first_case.collection_case_id}/unapplied-cash-applications",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(nested_method_status, 405)
        item_method_status, _item_method = invoke_http(
            app,
            "PUT",
            f"/v1/unapplied-cash-applications/{application_id}",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(item_method_status, 422)
        number_status, number_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/unapplied-cash-applications",
            {
                "tenant_reference": TENANT_ONE,
                "unapplied_cash_id": str(first_parked.unapplied_cash_id),
                "applied_amount": 1,
            },
        )
        self.assertEqual(number_status, 422)
        self.assertEqual(number_body["rejection_reason_code"], "request_invalid")
        unreadable_status, unreadable_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/unapplied-cash-applications",
            {
                "tenant_reference": TENANT_ONE,
                "unapplied_cash_id": str(first_parked.unapplied_cash_id),
                "applied_amount": "not-a-decimal",
            },
        )
        self.assertEqual(unreadable_status, 422)
        self.assertEqual(unreadable_body["rejection_reason_code"], "request_invalid")
        missing_cash_status, missing_cash_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/unapplied-cash-applications",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(missing_cash_status, 422)
        self.assertEqual(missing_cash_body["rejection_reason_code"], "request_invalid")
        currency_type_status, currency_type_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/unapplied-cash-applications",
            {
                "tenant_reference": TENANT_ONE,
                "unapplied_cash_id": str(first_parked.unapplied_cash_id),
                "currency_code": 840,
            },
        )
        self.assertEqual(currency_type_status, 422)
        self.assertEqual(currency_type_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.UnappliedCashApplicationPresentmentService.present_unapplied_cash_application",
            side_effect=ValueError("boom"),
        ):
            get_boom_status, get_boom = invoke_http(
                app,
                "GET",
                f"/v1/unapplied-cash-applications/{application_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(get_boom_status, 422)
        self.assertEqual(get_boom["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox + 2)
        applied_events = [
            event
            for event in ledger.webhook_outbox_events.values()
            if event.event_type_code == EVENT_TYPE_UNAPPLIED_CASH_APPLIED
        ]
        self.assertEqual(len(applied_events), 2)
        _, issue_body = invoke_http(
            app,
            "POST",
            "/v1/tenant-api-credentials",
            {"tenant_reference": TENANT_ONE, "credential_label": "operator_key"},
        )
        gated_status, gated_body = invoke_http(
            app,
            "GET",
            "/v1/unapplied-cash-applications",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(gated_status, 422)
        self.assertEqual(gated_body["rejection_reason_code"], "api_credential_missing")
        keyed_status, keyed_body = invoke_http(
            app,
            "GET",
            "/v1/unapplied-cash-applications",
            query={"tenant_reference": TENANT_ONE},
            headers={"Authorization": f"Bearer {issue_body['api_credential_secret']}"},
        )
        self.assertEqual(keyed_status, 200)
        self.assertEqual(
            keyed_body["unapplied_cash_applications"][0]["unapplied_cash_application_id"],
            application_id,
        )

    def test_presentment_list_and_query_fail_closed(self) -> None:
        """List pages stay tenant-scoped; illegal cursor and hollow resolve fail."""
        ledger, first_parked, first_case, _source_case_id, _receipt_id = (
            park_leftover_and_open_second_case()
        )
        second_parked, second_case = _second_parked_and_case(ledger)
        UnappliedCashApplicationService(
            ledger, clock=lambda: APPLIED_MORNING
        ).apply_unapplied_cash(
            TENANT_ONE, first_parked.unapplied_cash_id, first_case.collection_case_id
        )
        UnappliedCashApplicationService(
            ledger, clock=lambda: APPLIED_EVENING
        ).apply_unapplied_cash(
            TENANT_ONE, second_parked.unapplied_cash_id, second_case.collection_case_id
        )
        presentment = UnappliedCashApplicationPresentmentService(ledger)
        page = presentment.list_unapplied_cash_applications(TENANT_ONE, page_limit=1)
        self.assertEqual(len(page.unapplied_cash_applications), 1)
        self.assertIsNotNone(page.next_cursor)
        next_page = presentment.list_unapplied_cash_applications(
            TENANT_ONE, cursor=page.next_cursor, page_limit=1
        )
        self.assertEqual(len(next_page.unapplied_cash_applications), 1)
        self.assertNotEqual(
            page.unapplied_cash_applications[0].unapplied_cash_application_id,
            next_page.unapplied_cash_applications[0].unapplied_cash_application_id,
        )
        self.assertIsNone(presentment.list_unapplied_cash_applications(TENANT_ONE).next_cursor)
        self.assertEqual(
            len(presentment.list_unapplied_cash_applications(TENANT_ONE, page_limit="").unapplied_cash_applications),
            2,
        )
        with self.assertRaises(UnappliedCashApplicationPresentmentQueryError) as raised:
            presentment.list_unapplied_cash_applications(TENANT_ONE, cursor="not-a-cursor")
        self.assertEqual(raised.exception.rejection_reason_code, "request_invalid")
        with self.assertRaises(UnappliedCashApplicationPresentmentQueryError) as missing:
            presentment.present_unapplied_cash_application(TENANT_ONE, uuid4())
        self.assertEqual(
            missing.exception.rejection_reason_code, "unapplied_cash_application_not_found"
        )
        with mock.patch.object(presentment.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                presentment.present_unapplied_cash_application(TENANT_ONE, uuid4())
        empty = UnappliedCashApplicationPresentmentService()
        with self.assertRaises(UnappliedCashApplicationPresentmentQueryError) as tenant_missing:
            empty.present_unapplied_cash_application(TENANT_ONE, uuid4())
        self.assertEqual(tenant_missing.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(UnappliedCashApplicationPresentmentQueryError):
            presentment.list_unapplied_cash_applications(TENANT_ONE, page_limit=True)
        with self.assertRaises(UnappliedCashApplicationPresentmentQueryError):
            presentment.list_unapplied_cash_applications(TENANT_ONE, page_limit=object())
        with self.assertRaises(UnappliedCashApplicationPresentmentQueryError):
            presentment.list_unapplied_cash_applications(TENANT_ONE, page_limit="ten")
        with self.assertRaises(UnappliedCashApplicationPresentmentQueryError):
            presentment.list_unapplied_cash_applications(TENANT_ONE, page_limit=0)
        with self.assertRaises(UnappliedCashApplicationPresentmentQueryError):
            presentment.list_unapplied_cash_applications(TENANT_ONE, page_limit=101)
        first_id = page.unapplied_cash_applications[0].unapplied_cash_application_id
        stored_application = ledger.get_unapplied_cash_application(first_id)
        ledger.collection_cases.pop(stored_application.collection_case_id)
        with self.assertRaises(UnappliedCashApplicationPresentmentQueryError) as orphaned:
            presentment.present_unapplied_cash_application(TENANT_ONE, first_id)
        self.assertEqual(
            orphaned.exception.rejection_reason_code, "unapplied_cash_application_not_found"
        )
        orphaned_replay = UnappliedCashApplicationService(ledger).apply_unapplied_cash(
            TENANT_ONE, stored_application.unapplied_cash_id, stored_application.collection_case_id
        )
        self.assertEqual(
            orphaned_replay.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )

    def test_contract_and_result_helpers_fail_closed(self) -> None:
        """Accepted leftover apply stays exact; rejected leftover stays sparse."""
        ledger, parked, collection, _source_case_id, _receipt_id = (
            park_leftover_and_open_second_case()
        )
        accepted = UnappliedCashApplicationService(ledger).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, collection.collection_case_id
        )
        self.assertEqual(validate_unapplied_cash_application(accepted.as_contract_dict()), ())
        rejected = _rejected(
            UnappliedCashApplicationRejectionReasonCode.UNAPPLIED_CASH_NOT_FOUND
        )
        self.assertEqual(validate_unapplied_cash_application(rejected.as_contract_dict()), ())
        self.assertNotIn("unapplied_cash_application_id", rejected.as_contract_dict())
        sparse_rejected = UnappliedCashApplicationResult(
            unapplied_cash_application_outcome_code=UnappliedCashApplicationOutcomeCode.REJECTED,
            unapplied_cash_application_contract_version=UNAPPLIED_CASH_APPLICATION_CONTRACT_VERSION,
            unapplied_cash_application_id=None,
            unapplied_cash_id=None,
            collection_case_id=None,
            payment_receipt_id=None,
            invoice_draft_id=None,
            tenant_reference=None,
            currency_code=None,
            applied_amount=None,
            remaining_outstanding_amount=None,
            unapplied_cash_application_status=None,
            collection_case_status=None,
            applied_at=None,
            source_payload_hash=None,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        self.assertEqual(
            sparse_rejected.as_contract_dict()["rejection_reason_code"],
            "unapplied_cash_not_found",
        )
        missing_id = {
            "unapplied_cash_application_contract_version": 1,
            "unapplied_cash_application_outcome_code": "accepted",
        }
        self.assertTrue(validate_unapplied_cash_application(missing_id))
        self.assertTrue(validate_unapplied_cash_application(["not-an-object"]))
        unknown = {
            "unapplied_cash_application_contract_version": 1,
            "unapplied_cash_application_outcome_code": "posted",
        }
        self.assertTrue(validate_unapplied_cash_application(unknown))
        missing_outcome = {"unapplied_cash_application_contract_version": 1}
        self.assertTrue(validate_unapplied_cash_application(missing_outcome))
        legal = dict(accepted.as_contract_dict())
        legal["legal_invoice_number"] = "INV-1"
        self.assertTrue(validate_unapplied_cash_application(legal))
        rejected_legal = rejected.as_contract_dict()
        rejected_legal["legal_invoice_number"] = "INV-1"
        self.assertTrue(validate_unapplied_cash_application(rejected_legal))
        rejected_credit = rejected.as_contract_dict()
        rejected_credit["legal_credit_note_number"] = "CN-1"
        self.assertTrue(validate_unapplied_cash_application(rejected_credit))
        rejected_missing = {
            "unapplied_cash_application_contract_version": 1,
            "unapplied_cash_application_outcome_code": "rejected",
        }
        self.assertTrue(validate_unapplied_cash_application(rejected_missing))
        zero_amount = dict(accepted.as_contract_dict())
        zero_amount["applied_amount"] = "0"
        self.assertTrue(validate_unapplied_cash_application(zero_amount))
        bad_amount = dict(accepted.as_contract_dict())
        bad_amount["applied_amount"] = "not-decimal"
        self.assertTrue(validate_unapplied_cash_application(bad_amount))
        int_amount = dict(accepted.as_contract_dict())
        int_amount["applied_amount"] = 1
        self.assertTrue(validate_unapplied_cash_application(int_amount))
        presentment = UnappliedCashApplicationPresentmentService(
            ledger
        ).present_unapplied_cash_application(
            TENANT_ONE, accepted.unapplied_cash_application_id
        ).as_contract_dict()
        self.assertEqual(validate_unapplied_cash_application_presentment(presentment), ())
        self.assertTrue(validate_unapplied_cash_application_presentment(["not-an-object"]))
        missing_presentment = dict(presentment)
        del missing_presentment["applied_amount"]
        self.assertTrue(validate_unapplied_cash_application_presentment(missing_presentment))
        zero_presentment = dict(presentment)
        zero_presentment["applied_amount"] = "0"
        self.assertTrue(validate_unapplied_cash_application_presentment(zero_presentment))
        action_mismatch = dict(presentment)
        action_mismatch["next_operator_action"] = "apply"
        self.assertTrue(validate_unapplied_cash_application_presentment(action_mismatch))
        forbidden = dict(presentment)
        forbidden["card_pan"] = "4111111111111111"
        self.assertTrue(validate_unapplied_cash_application_presentment(forbidden))
        float_amount = dict(presentment)
        float_amount["applied_amount"] = 0.001
        self.assertTrue(validate_unapplied_cash_application_presentment(float_amount))
        bad_presentment = dict(presentment)
        bad_presentment["applied_amount"] = "not-decimal"
        self.assertTrue(validate_unapplied_cash_application_presentment(bad_presentment))
        missing_remaining = dict(presentment)
        del missing_remaining["remaining_outstanding_amount"]
        self.assertTrue(validate_unapplied_cash_application_presentment(missing_remaining))
        zero_remaining = dict(presentment)
        zero_remaining["remaining_outstanding_amount"] = "0"
        zero_remaining["next_operator_action"] = "collect"
        self.assertTrue(validate_unapplied_cash_application_presentment(zero_remaining))
        settle_mismatch = dict(presentment)
        settle_mismatch["next_operator_action"] = "settle"
        self.assertTrue(validate_unapplied_cash_application_presentment(settle_mismatch))
        wait_mismatch = dict(presentment)
        wait_mismatch["next_operator_action"] = "wait"
        self.assertTrue(validate_unapplied_cash_application_presentment(wait_mismatch))
        int_remaining = dict(presentment)
        int_remaining["remaining_outstanding_amount"] = 1
        self.assertTrue(validate_unapplied_cash_application_presentment(int_remaining))
        bad_remaining = dict(presentment)
        bad_remaining["remaining_outstanding_amount"] = "not-decimal"
        self.assertTrue(validate_unapplied_cash_application_presentment(bad_remaining))
        negative_remaining = dict(presentment)
        negative_remaining["remaining_outstanding_amount"] = "-0.01"
        self.assertTrue(validate_unapplied_cash_application_presentment(negative_remaining))
        outcome_presentment = dict(presentment)
        outcome_presentment["unapplied_cash_application_outcome_code"] = "accepted"
        self.assertTrue(validate_unapplied_cash_application_presentment(outcome_presentment))
        unsupported = UnappliedCashApplicationResult(
            unapplied_cash_application_outcome_code="posted",  # type: ignore[arg-type]
            unapplied_cash_application_contract_version=1,
            unapplied_cash_application_id=None,
            unapplied_cash_id=None,
            collection_case_id=None,
            payment_receipt_id=None,
            invoice_draft_id=None,
            tenant_reference=None,
            currency_code=None,
            applied_amount=None,
            remaining_outstanding_amount=None,
            unapplied_cash_application_status=None,
            collection_case_status=None,
            applied_at=None,
            source_payload_hash=None,
            next_operator_action="collect",
            rejection_reason_code=None,
        )
        with self.assertRaisesRegex(ValueError, "unsupported unapplied cash application outcome"):
            unsupported.as_contract_dict()
        self.assertTrue(_format_applied_at(APPLIED_MORNING).endswith("Z"))
        with self.assertRaisesRegex(ValueError, "accepted unapplied cash application must include applied_at"):
            _format_applied_at(None)
        webhook_data = accepted.as_webhook_event_data()
        self.assertEqual(
            webhook_data["unapplied_cash_application_id"],
            str(accepted.unapplied_cash_application_id),
        )
        self.assertNotIn("remaining_outstanding_amount", webhook_data)
        self.assertNotIn("issued_invoice_id", webhook_data)
        with self.assertRaisesRegex(ValueError, "rejected unapplied cash application has no webhook event data"):
            rejected.as_webhook_event_data()
        missing_cash = UnappliedCashApplicationResult(
            unapplied_cash_application_outcome_code=UnappliedCashApplicationOutcomeCode.ACCEPTED,
            unapplied_cash_application_contract_version=1,
            unapplied_cash_application_id=generate_record_id(),
            unapplied_cash_id=None,
            collection_case_id=collection.collection_case_id,
            payment_receipt_id=accepted.payment_receipt_id,
            invoice_draft_id=accepted.invoice_draft_id,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            applied_amount=LEFTOVER,
            remaining_outstanding_amount=Decimal("19.999"),
            unapplied_cash_application_status="applied",
            collection_case_status="open",
            applied_at=APPLIED_MORNING,
            source_payload_hash="sha256:" + ("44" * 32),
            next_operator_action="collect",
            rejection_reason_code=None,
        )
        with self.assertRaisesRegex(ValueError, "rejected unapplied cash application has no webhook event data"):
            missing_cash.as_webhook_event_data()
        accepted_without_time = UnappliedCashApplicationResult(
            unapplied_cash_application_outcome_code=UnappliedCashApplicationOutcomeCode.ACCEPTED,
            unapplied_cash_application_contract_version=1,
            unapplied_cash_application_id=generate_record_id(),
            unapplied_cash_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            payment_receipt_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            applied_amount=LEFTOVER,
            remaining_outstanding_amount=Decimal("0"),
            unapplied_cash_application_status="applied",
            collection_case_status="open",
            applied_at=None,
            source_payload_hash="sha256:" + ("ab" * 32),
            next_operator_action="settle",
            rejection_reason_code=None,
        )
        with self.assertRaisesRegex(ValueError, "accepted unapplied cash applications must include applied_at"):
            accepted_without_time.as_webhook_event_data()
        incomplete = UnappliedCashApplicationResult(
            unapplied_cash_application_outcome_code=UnappliedCashApplicationOutcomeCode.ACCEPTED,
            unapplied_cash_application_contract_version=1,
            unapplied_cash_application_id=None,
            unapplied_cash_id=generate_record_id(),
            collection_case_id=collection.collection_case_id,
            payment_receipt_id=accepted.payment_receipt_id,
            invoice_draft_id=accepted.invoice_draft_id,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            applied_amount=LEFTOVER,
            remaining_outstanding_amount=Decimal("19.999"),
            unapplied_cash_application_status="applied",
            collection_case_status="open",
            applied_at=None,
            source_payload_hash="sha256:" + ("cd" * 32),
            next_operator_action="collect",
            rejection_reason_code=None,
        )
        with self.assertRaisesRegex(ValueError, "accepted unapplied cash applications must include identity"):
            _enqueue_unapplied_cash_applied(ledger, TENANT_ONE, incomplete)
        missing_time = UnappliedCashApplicationResult(
            unapplied_cash_application_outcome_code=UnappliedCashApplicationOutcomeCode.ACCEPTED,
            unapplied_cash_application_contract_version=1,
            unapplied_cash_application_id=generate_record_id(),
            unapplied_cash_id=accepted.unapplied_cash_id,
            collection_case_id=collection.collection_case_id,
            payment_receipt_id=accepted.payment_receipt_id,
            invoice_draft_id=accepted.invoice_draft_id,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            applied_amount=LEFTOVER,
            remaining_outstanding_amount=Decimal("19.999"),
            unapplied_cash_application_status="applied",
            collection_case_status="open",
            applied_at=None,
            source_payload_hash="sha256:" + ("ef" * 32),
            next_operator_action="collect",
            rejection_reason_code=None,
        )
        with self.assertRaisesRegex(ValueError, "accepted unapplied cash applications must include identity"):
            _enqueue_unapplied_cash_applied(ledger, TENANT_ONE, missing_time)
        issued_payload = accepted.as_webhook_event_data(generate_record_id())
        self.assertIn("issued_invoice_id", issued_payload)
        orphaned = UnappliedCashApplicationResult(
            unapplied_cash_application_outcome_code=UnappliedCashApplicationOutcomeCode.ACCEPTED,
            unapplied_cash_application_contract_version=1,
            unapplied_cash_application_id=generate_record_id(),
            unapplied_cash_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            payment_receipt_id=accepted.payment_receipt_id,
            invoice_draft_id=accepted.invoice_draft_id,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            applied_amount=LEFTOVER,
            remaining_outstanding_amount=Decimal("19.999"),
            unapplied_cash_application_status="applied",
            collection_case_status="open",
            applied_at=APPLIED_MORNING,
            source_payload_hash="sha256:" + ("11" * 32),
            next_operator_action="collect",
            rejection_reason_code=None,
        )
        _enqueue_unapplied_cash_applied(ledger, TENANT_ONE, orphaned)
        self.assertNotIn("issued_invoice_id", orphaned.as_webhook_event_data())
        no_case = UnappliedCashApplicationResult(
            unapplied_cash_application_outcome_code=UnappliedCashApplicationOutcomeCode.ACCEPTED,
            unapplied_cash_application_contract_version=1,
            unapplied_cash_application_id=generate_record_id(),
            unapplied_cash_id=generate_record_id(),
            collection_case_id=None,
            payment_receipt_id=accepted.payment_receipt_id,
            invoice_draft_id=accepted.invoice_draft_id,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            applied_amount=LEFTOVER,
            remaining_outstanding_amount=Decimal("19.999"),
            unapplied_cash_application_status="applied",
            collection_case_status="open",
            applied_at=APPLIED_MORNING,
            source_payload_hash="sha256:" + ("22" * 32),
            next_operator_action="collect",
            rejection_reason_code=None,
        )
        _enqueue_unapplied_cash_applied(ledger, TENANT_ONE, no_case)
        no_draft = UnappliedCashApplicationResult(
            unapplied_cash_application_outcome_code=UnappliedCashApplicationOutcomeCode.ACCEPTED,
            unapplied_cash_application_contract_version=1,
            unapplied_cash_application_id=generate_record_id(),
            unapplied_cash_id=generate_record_id(),
            collection_case_id=collection.collection_case_id,
            payment_receipt_id=accepted.payment_receipt_id,
            invoice_draft_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            applied_amount=LEFTOVER,
            remaining_outstanding_amount=Decimal("19.999"),
            unapplied_cash_application_status="applied",
            collection_case_status="open",
            applied_at=APPLIED_MORNING,
            source_payload_hash="sha256:" + ("33" * 32),
            next_operator_action="collect",
            rejection_reason_code=None,
        )
        _enqueue_unapplied_cash_applied(ledger, TENANT_ONE, no_draft)

    def test_ledger_insert_fail_closed_branches(self) -> None:
        """Ledger insert refuses invalid status, leftover, and duplicate identity."""
        ledger, parked, collection, source_case_id, payment_receipt_id = (
            park_leftover_and_open_second_case()
        )
        accepted = UnappliedCashApplicationService(ledger).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, collection.collection_case_id
        )
        cash = ledger.get_unapplied_cash(parked.unapplied_cash_id)
        self.assertIsNotNone(cash)
        second_parked, second_case = _second_parked_and_case(ledger)
        invalid_status = StoredUnappliedCashApplication(
            unapplied_cash_application_id=generate_record_id(),
            tenant_account_id=cash.tenant_account_id,
            unapplied_cash_id=second_parked.unapplied_cash_id,
            collection_case_id=second_case.collection_case_id,
            payment_receipt_id=second_parked.payment_receipt_id,
            invoice_draft_id=second_case.invoice_draft_id,
            unapplied_cash_application_contract_version=UNAPPLIED_CASH_APPLICATION_CONTRACT_VERSION,
            source_payload_hash="sha256:" + ("ab" * 32),
            currency_code="USD",
            applied_amount=LEFTOVER,
            unapplied_cash_application_status="parked",
            applied_at=APPLIED_MORNING,
        )
        with self.assertRaisesRegex(ValueError, "unapplied_cash_application_status must be applied"):
            ledger.insert_unapplied_cash_application(invalid_status)
        zero_row = StoredUnappliedCashApplication(
            unapplied_cash_application_id=generate_record_id(),
            tenant_account_id=cash.tenant_account_id,
            unapplied_cash_id=second_parked.unapplied_cash_id,
            collection_case_id=second_case.collection_case_id,
            payment_receipt_id=second_parked.payment_receipt_id,
            invoice_draft_id=second_case.invoice_draft_id,
            unapplied_cash_application_contract_version=UNAPPLIED_CASH_APPLICATION_CONTRACT_VERSION,
            source_payload_hash="sha256:" + ("cd" * 32),
            currency_code="USD",
            applied_amount=Decimal("0"),
            unapplied_cash_application_status="applied",
            applied_at=APPLIED_MORNING,
        )
        with self.assertRaisesRegex(ValueError, "positive exact decimal"):
            ledger.insert_unapplied_cash_application(zero_row)
        duplicate_id = StoredUnappliedCashApplication(
            unapplied_cash_application_id=accepted.unapplied_cash_application_id,
            tenant_account_id=cash.tenant_account_id,
            unapplied_cash_id=second_parked.unapplied_cash_id,
            collection_case_id=second_case.collection_case_id,
            payment_receipt_id=second_parked.payment_receipt_id,
            invoice_draft_id=second_case.invoice_draft_id,
            unapplied_cash_application_contract_version=UNAPPLIED_CASH_APPLICATION_CONTRACT_VERSION,
            source_payload_hash="sha256:" + ("ef" * 32),
            currency_code="USD",
            applied_amount=LEFTOVER,
            unapplied_cash_application_status="applied",
            applied_at=APPLIED_MORNING,
        )
        with self.assertRaisesRegex(ValueError, "unapplied_cash_application_id already stored"):
            ledger.insert_unapplied_cash_application(duplicate_id)
        duplicate_identity = StoredUnappliedCashApplication(
            unapplied_cash_application_id=generate_record_id(),
            tenant_account_id=cash.tenant_account_id,
            unapplied_cash_id=parked.unapplied_cash_id,
            collection_case_id=collection.collection_case_id,
            payment_receipt_id=payment_receipt_id,
            invoice_draft_id=collection.invoice_draft_id,
            unapplied_cash_application_contract_version=UNAPPLIED_CASH_APPLICATION_CONTRACT_VERSION,
            source_payload_hash="sha256:" + ("11" * 32),
            currency_code="USD",
            applied_amount=LEFTOVER,
            unapplied_cash_application_status="applied",
            applied_at=APPLIED_MORNING,
        )
        with self.assertRaisesRegex(ValueError, "immutable and cannot be replaced"):
            ledger.insert_unapplied_cash_application(duplicate_identity)
        with self.assertRaisesRegex(ValueError, "requires a stored collection case"):
            ledger.apply_unapplied_cash_to_collection_case(generate_record_id(), LEFTOVER)
        with self.assertRaisesRegex(ValueError, "positive exact decimal"):
            ledger.apply_unapplied_cash_to_collection_case(
                collection.collection_case_id, Decimal("0")
            )
        with self.assertRaisesRegex(ValueError, "settled collection cases"):
            ledger.apply_unapplied_cash_to_collection_case(source_case_id, LEFTOVER)
        with self.assertRaisesRegex(ValueError, "cannot exceed outstanding"):
            ledger.apply_unapplied_cash_to_collection_case(
                collection.collection_case_id, Decimal("21.00")
            )

    def test_webhook_includes_issued_invoice_when_stored(self) -> None:
        """Envelope carries issued_invoice_id only when that snapshot exists."""
        ledger, parked, collection, _source_case_id, _receipt_id = (
            park_leftover_and_open_second_case()
        )
        issued = IssuedInvoiceService(ledger).issue_invoice(
            TENANT_ONE, collection.invoice_draft_id
        )
        applied = UnappliedCashApplicationService(ledger).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, collection.collection_case_id
        )
        events = [
            event
            for event in ledger.webhook_outbox_events.values()
            if event.event_type_code == EVENT_TYPE_UNAPPLIED_CASH_APPLIED
        ]
        self.assertEqual(len(events), 1)
        data = json.loads(events[0].payload_json)["data"]
        self.assertEqual(data["issued_invoice_id"], str(issued.issued_invoice_id))
        self.assertNotIn("remaining_outstanding_amount", data)
        self.assertEqual(applied.unapplied_cash_application_outcome_code.value, "accepted")


def _second_parked_and_case(ledger):
    """Park leftover on a second same-tenant receipt and open another case."""
    draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", SECOND_CASE_AMOUNT)
    case = CollectionCaseService(ledger).open_collection_case(TENANT_ONE, draft_id)
    intent = PaymentIntentService(ledger).project_payment_intent(
        TENANT_ONE, case.collection_case_id
    )
    if intent.payment_intent_id is None:
        raise AssertionError("second path must project a payment intent")
    receipt = PaymentSettlementService(ledger).record_payment_receipt(
        TENANT_ONE, intent.payment_intent_id, SECOND_CASE_AMOUNT
    )
    if receipt.payment_receipt_id is None:
        raise AssertionError("second path must apply a payment receipt")
    parked = UnappliedCashService(ledger).park_unapplied_cash(
        TENANT_ONE, receipt.payment_receipt_id, unapplied_amount=LEFTOVER
    )
    other_draft = insert_commercial_draft(ledger, TENANT_ONE, "USD", SECOND_CASE_AMOUNT)
    other_case = CollectionCaseService(ledger).open_collection_case(TENANT_ONE, other_draft)
    return parked, other_case


if __name__ == "__main__":
    unittest.main()
