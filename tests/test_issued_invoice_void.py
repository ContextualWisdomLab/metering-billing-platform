"""Issued-invoice void tests for unused commercial issue reversal."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    CollectionAgingPresentmentService,
    CollectionCasePresentmentService,
    CollectionCaseService,
    CollectionCaseSettlementService,
    CollectionWriteOffService,
    CreditNoteApplicationService,
    IssuedInvoiceService,
    IssuedInvoiceVoidPresentmentService,
    IssuedInvoiceVoidService,
    PaymentIntentService,
    PaymentSettlementService,
    UnappliedCashApplicationService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.contracts import (
    validate_issued_invoice_void,
    validate_issued_invoice_void_presentment,
)
from metering_billing.errors import (
    CollectionCaseSettlementRejectionReasonCode,
    IssuedInvoiceVoidOutcomeCode,
    IssuedInvoiceVoidPresentmentQueryError,
    IssuedInvoiceVoidRejectionReasonCode,
)
from metering_billing.issued_invoice_void import (
    IssuedInvoiceVoidResult,
    _enqueue_invoice_voided,
    _format_voided_at,
    _rejected,
)
from metering_billing.usage_ledger import generate_record_id
from metering_billing.webhook_outbox import EVENT_TYPE_INVOICE_VOIDED
from test_collection_case import draft_known_morning
from test_credit_note_application import issue_morning_credit_then_open_case
from test_http_app import invoke_http
from test_tax_assessment import insert_commercial_draft
from test_unapplied_cash_application import park_leftover_and_open_second_case
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL


VOIDED_MORNING = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
VOIDED_EVENING = datetime(2026, 8, 18, 21, 0, tzinfo=UTC)


def issue_known_morning_invoice(open_case: bool = True):
    """Issue the known-morning invoice and optionally open its collection case."""
    ledger, invoice_draft_id = draft_known_morning()
    issued = IssuedInvoiceService(ledger).issue_invoice(TENANT_ONE, invoice_draft_id)
    collection = None
    if open_case:
        collection = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, invoice_draft_id
        )
    return ledger, issued, collection


class IssuedInvoiceVoidTests(unittest.TestCase):
    """Verify void-once identity, unused-case close, and HTTP presentment."""

    def test_void_closes_unused_open_case_once_without_journal(self) -> None:
        """An unused issued invoice voids once and closes the case as voided."""
        ledger, issued, collection = issue_known_morning_invoice()
        prior_journals = len(ledger.journal_proposals)
        prior_outbox = len(ledger.webhook_outbox_events)
        prior_receipts = len(ledger.payment_receipts)
        prior_settlements = len(ledger.collection_case_settlements)
        first = IssuedInvoiceVoidService(
            ledger, clock=lambda: VOIDED_MORNING
        ).void_issued_invoice(TENANT_ONE, issued.issued_invoice_id)
        second = IssuedInvoiceVoidService(
            ledger, clock=lambda: VOIDED_EVENING
        ).void_issued_invoice(TENANT_ONE, issued.issued_invoice_id)
        self.assertEqual(
            first.issued_invoice_void_outcome_code, IssuedInvoiceVoidOutcomeCode.ACCEPTED
        )
        self.assertEqual(
            second.issued_invoice_void_outcome_code,
            IssuedInvoiceVoidOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertEqual(first.issued_invoice_void_id, second.issued_invoice_void_id)
        self.assertEqual(first.issued_invoice_id, issued.issued_invoice_id)
        self.assertEqual(first.invoice_draft_id, issued.invoice_draft_id)
        self.assertEqual(first.collection_case_id, collection.collection_case_id)
        self.assertEqual(first.currency_code, "USD")
        self.assertEqual(first.voided_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(first.remaining_outstanding_amount, Decimal("0"))
        self.assertEqual(first.issued_invoice_void_status, "recorded")
        self.assertEqual(first.collection_case_status, "voided")
        self.assertEqual(first.voided_at, VOIDED_MORNING)
        self.assertEqual(second.voided_at, VOIDED_MORNING)
        self.assertEqual(first.next_operator_action, "wait")
        self.assertTrue(first.source_payload_hash.startswith("sha256:"))
        payload = first.as_contract_dict()
        self.assertEqual(validate_issued_invoice_void(payload), ())
        self.assertIsInstance(payload["voided_amount"], str)
        self.assertNotIsInstance(payload["voided_amount"], float)
        self.assertNotIn("card_pan", payload)
        self.assertNotIn("legal_invoice_number", payload)
        stored_case = ledger.get_collection_case(collection.collection_case_id)
        self.assertEqual(stored_case.outstanding_amount, Decimal("0"))
        self.assertEqual(stored_case.collection_case_status, "voided")
        stored_invoice = ledger.get_issued_invoice(issued.issued_invoice_id)
        self.assertEqual(stored_invoice.issued_invoice_status, "issued")
        self.assertEqual(len(ledger.issued_invoice_voids), 1)
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        voided_events = [
            event
            for event in ledger.webhook_outbox_events.values()
            if event.event_type_code == EVENT_TYPE_INVOICE_VOIDED
        ]
        self.assertEqual(len(voided_events), 1)
        self.assertEqual(voided_events[0].source_id, first.issued_invoice_void_id)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox + 1)
        self.assertEqual(len(ledger.payment_receipts), prior_receipts)
        self.assertEqual(len(ledger.collection_case_settlements), prior_settlements)
        settled = CollectionCaseSettlementService(ledger).settle_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(
            settled.rejection_reason_code,
            CollectionCaseSettlementRejectionReasonCode.COLLECTION_CASE_VOIDED,
        )
        presented_case = CollectionCasePresentmentService(ledger).present_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(presented_case.collection_case_status, "voided")
        self.assertEqual(presented_case.next_operator_action, "wait")
        self.assertIsNone(presented_case.next_dunning_notice_code)
        aging = CollectionAgingPresentmentService(ledger).present_collection_aging(
            TENANT_ONE
        )
        self.assertEqual(aging.as_contract_dict()["currencies"], [])

    def test_void_without_collection_case_does_not_invent_one(self) -> None:
        """An unused issued invoice without a case still voids once."""
        ledger, issued, _collection = issue_known_morning_invoice(open_case=False)
        first = IssuedInvoiceVoidService(
            ledger, clock=lambda: VOIDED_MORNING
        ).void_issued_invoice(TENANT_ONE, issued.issued_invoice_id)
        replay = IssuedInvoiceVoidService(
            ledger, clock=lambda: VOIDED_EVENING
        ).void_issued_invoice(TENANT_ONE, issued.issued_invoice_id)
        self.assertEqual(
            first.issued_invoice_void_outcome_code, IssuedInvoiceVoidOutcomeCode.ACCEPTED
        )
        self.assertEqual(
            replay.issued_invoice_void_outcome_code,
            IssuedInvoiceVoidOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertIsNone(first.collection_case_id)
        self.assertIsNone(first.collection_case_status)
        self.assertIsNone(first.remaining_outstanding_amount)
        payload = first.as_contract_dict()
        self.assertNotIn("collection_case_id", payload)
        self.assertNotIn("collection_case_status", payload)
        self.assertNotIn("remaining_outstanding_amount", payload)
        self.assertEqual(validate_issued_invoice_void(payload), ())
        self.assertEqual(len(ledger.collection_cases), 0)
        webhook_data = first.as_webhook_event_data()
        self.assertNotIn("collection_case_id", webhook_data)
        self.assertNotIn("remaining_outstanding_amount", webhook_data)
        self.assertNotIn("collection_case_status", webhook_data)
        self.assertEqual(webhook_data["issued_invoice_void_id"], str(first.issued_invoice_void_id))
        voided_events = [
            event
            for event in ledger.webhook_outbox_events.values()
            if event.event_type_code == EVENT_TYPE_INVOICE_VOIDED
        ]
        self.assertEqual(len(voided_events), 1)
        self.assertEqual(voided_events[0].source_id, first.issued_invoice_void_id)

    def test_dunning_case_and_projected_intent_still_void(self) -> None:
        """Dunning history and a projected intent do not block an unused void."""
        ledger, issued, collection = issue_known_morning_invoice()
        CollectionCaseService(ledger).record_dunning_event(
            TENANT_ONE, collection.collection_case_id, "first_notice"
        )
        PaymentIntentService(ledger).project_payment_intent(
            TENANT_ONE, collection.collection_case_id
        )
        result = IssuedInvoiceVoidService(ledger).void_issued_invoice(
            TENANT_ONE, issued.issued_invoice_id
        )
        self.assertEqual(
            result.issued_invoice_void_outcome_code, IssuedInvoiceVoidOutcomeCode.ACCEPTED
        )
        stored = ledger.get_collection_case(collection.collection_case_id)
        self.assertEqual(stored.collection_case_status, "voided")
        presented = CollectionCasePresentmentService(ledger).present_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(presented.collection_case_status, "voided")
        self.assertIsNone(presented.next_dunning_notice_code)

    def test_fail_closed_on_money_facts_and_isolation(self) -> None:
        """Cash, credit, write-off, leftover apply, and isolation refuse the void."""
        paid_ledger, paid_issued, paid_case = issue_known_morning_invoice()
        intent = PaymentIntentService(paid_ledger).project_payment_intent(
            TENANT_ONE, paid_case.collection_case_id
        )
        PaymentSettlementService(paid_ledger).record_payment_receipt(
            TENANT_ONE, intent.payment_intent_id, KNOWN_MORNING_TOTAL
        )
        paid = IssuedInvoiceVoidService(paid_ledger).void_issued_invoice(
            TENANT_ONE, paid_issued.issued_invoice_id
        )
        self.assertEqual(
            paid.rejection_reason_code,
            IssuedInvoiceVoidRejectionReasonCode.PAYMENT_RECEIPT_EXISTS,
        )
        self.assertEqual(len(paid_ledger.issued_invoice_voids), 0)

        credit_ledger, credit_note, credit_case = issue_morning_credit_then_open_case()
        credit_issued = IssuedInvoiceService(credit_ledger).issue_invoice(
            TENANT_ONE, credit_case.invoice_draft_id
        )
        CreditNoteApplicationService(credit_ledger).apply_credit_note(
            TENANT_ONE,
            credit_note.issued_credit_note_id,
            credit_case.collection_case_id,
        )
        credited = IssuedInvoiceVoidService(credit_ledger).void_issued_invoice(
            TENANT_ONE, credit_issued.issued_invoice_id
        )
        self.assertEqual(
            credited.rejection_reason_code,
            IssuedInvoiceVoidRejectionReasonCode.CREDIT_NOTE_ALREADY_APPLIED,
        )

        write_off_ledger, write_off_issued, write_off_case = issue_known_morning_invoice()
        CollectionWriteOffService(write_off_ledger).write_off_collection_case(
            TENANT_ONE, write_off_case.collection_case_id
        )
        written = IssuedInvoiceVoidService(write_off_ledger).void_issued_invoice(
            TENANT_ONE, write_off_issued.issued_invoice_id
        )
        self.assertEqual(
            written.rejection_reason_code,
            IssuedInvoiceVoidRejectionReasonCode.COLLECTION_WRITE_OFF_EXISTS,
        )

        leftover_ledger, parked, leftover_case, _source, _receipt = (
            park_leftover_and_open_second_case()
        )
        leftover_issued = IssuedInvoiceService(leftover_ledger).issue_invoice(
            TENANT_ONE, leftover_case.invoice_draft_id
        )
        UnappliedCashApplicationService(leftover_ledger).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, leftover_case.collection_case_id
        )
        applied = IssuedInvoiceVoidService(leftover_ledger).void_issued_invoice(
            TENANT_ONE, leftover_issued.issued_invoice_id
        )
        self.assertEqual(
            applied.rejection_reason_code,
            IssuedInvoiceVoidRejectionReasonCode.UNAPPLIED_CASH_ALREADY_APPLIED,
        )

        settled_ledger, settled_issued, settled_case = issue_known_morning_invoice()
        settled_ledger.collection_cases[settled_case.collection_case_id] = replace(
            settled_ledger.get_collection_case(settled_case.collection_case_id),
            outstanding_amount=Decimal("0"),
        )
        CollectionCaseSettlementService(settled_ledger).settle_collection_case(
            TENANT_ONE, settled_case.collection_case_id
        )
        settled = IssuedInvoiceVoidService(settled_ledger).void_issued_invoice(
            TENANT_ONE, settled_issued.issued_invoice_id
        )
        self.assertEqual(
            settled.rejection_reason_code,
            IssuedInvoiceVoidRejectionReasonCode.COLLECTION_CASE_SETTLED,
        )

        mismatch_ledger, mismatch_issued, mismatch_case = issue_known_morning_invoice()
        mismatch_ledger.collection_cases[mismatch_case.collection_case_id] = replace(
            mismatch_ledger.get_collection_case(mismatch_case.collection_case_id),
            outstanding_amount=Decimal("1.00"),
        )
        mismatch = IssuedInvoiceVoidService(mismatch_ledger).void_issued_invoice(
            TENANT_ONE, mismatch_issued.issued_invoice_id
        )
        self.assertEqual(
            mismatch.rejection_reason_code,
            IssuedInvoiceVoidRejectionReasonCode.OUTSTANDING_MISMATCH,
        )

        missing = IssuedInvoiceVoidService(mismatch_ledger).void_issued_invoice(
            TENANT_ONE, uuid4()
        )
        self.assertEqual(
            missing.rejection_reason_code,
            IssuedInvoiceVoidRejectionReasonCode.ISSUED_INVOICE_NOT_FOUND,
        )
        crossed = IssuedInvoiceVoidService(mismatch_ledger).void_issued_invoice(
            TENANT_TWO, mismatch_issued.issued_invoice_id
        )
        self.assertEqual(
            crossed.rejection_reason_code,
            IssuedInvoiceVoidRejectionReasonCode.ISSUED_INVOICE_NOT_FOUND,
        )
        tenant = IssuedInvoiceVoidService(mismatch_ledger).void_issued_invoice(
            "urn:cwl:missing", mismatch_issued.issued_invoice_id
        )
        self.assertEqual(
            tenant.rejection_reason_code,
            IssuedInvoiceVoidRejectionReasonCode.TENANT_NOT_FOUND,
        )
        currency = IssuedInvoiceVoidService(mismatch_ledger).void_issued_invoice(
            TENANT_ONE, mismatch_issued.issued_invoice_id, currency_code="EUR"
        )
        self.assertEqual(
            currency.rejection_reason_code,
            IssuedInvoiceVoidRejectionReasonCode.CURRENCY_MISMATCH,
        )
        matching_currency = IssuedInvoiceVoidService(
            mismatch_ledger
        ).void_issued_invoice(
            TENANT_ONE, mismatch_issued.issued_invoice_id, currency_code="USD"
        )
        self.assertEqual(
            matching_currency.rejection_reason_code,
            IssuedInvoiceVoidRejectionReasonCode.OUTSTANDING_MISMATCH,
        )

    def test_resolver_hollow_success_raises_value_error(self) -> None:
        """A broken tenant resolve must raise ValueError, not assert."""
        ledger, issued, _collection = issue_known_morning_invoice()
        service = IssuedInvoiceVoidService(ledger)
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.void_issued_invoice(TENANT_ONE, issued.issued_invoice_id)

    def test_http_void_get_and_paged_list_without_ais(self) -> None:
        """POST voids; GET item and list page metadata and never call AIS."""
        ledger, first_issued, _first_case = issue_known_morning_invoice()
        second_draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("20.00"))
        second_issued = IssuedInvoiceService(ledger).issue_invoice(
            TENANT_ONE, second_draft_id
        )
        CollectionCaseService(ledger).open_collection_case(TENANT_ONE, second_draft_id)
        prior_outbox = len(ledger.webhook_outbox_events)
        prior_journals = len(ledger.journal_proposals)
        app = create_http_app(ledger)
        refused_status, refused_body = invoke_http(
            app,
            "POST",
            f"/v1/issued-invoices/{first_issued.issued_invoice_id}/voids",
            {"tenant_reference": TENANT_ONE, "card_pan": "4111111111111111"},
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.issued_invoice_voids), 0)
        typed_status, typed_body = invoke_http(
            app,
            "POST",
            f"/v1/issued-invoices/{first_issued.issued_invoice_id}/voids",
            {"tenant_reference": TENANT_ONE, "currency_code": 1},
        )
        self.assertEqual(typed_status, 422)
        self.assertEqual(typed_body["rejection_reason_code"], "request_invalid")
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            f"/v1/issued-invoices/{first_issued.issued_invoice_id}/voids",
            {"tenant_reference": TENANT_ONE, "currency_code": "USD"},
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["issued_invoice_void_outcome_code"], "accepted")
        self.assertEqual(accepted_body["voided_amount"], format_exact_decimal(KNOWN_MORNING_TOTAL))
        self.assertEqual(accepted_body["remaining_outstanding_amount"], "0")
        self.assertEqual(accepted_body["collection_case_status"], "voided")
        void_id = accepted_body["issued_invoice_void_id"]
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            f"/v1/issued-invoices/{first_issued.issued_invoice_id}/voids",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["issued_invoice_void_outcome_code"], "duplicate_replay")
        self.assertEqual(replay_body["issued_invoice_void_id"], void_id)
        second_status, second_body = invoke_http(
            app,
            "POST",
            f"/v1/issued-invoices/{second_issued.issued_invoice_id}/voids",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(second_body["issued_invoice_void_outcome_code"], "accepted")
        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/issued-invoice-voids/{void_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["issued_invoice_void_id"], void_id)
        self.assertEqual(get_body["issued_invoice_id"], str(first_issued.issued_invoice_id))
        self.assertNotIn("issued_invoice_void_outcome_code", get_body)
        self.assertEqual(validate_issued_invoice_void_presentment(get_body), ())
        header_status, header_body = invoke_http(
            app,
            "GET",
            f"/v1/issued-invoice-voids/{void_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(header_status, 200)
        self.assertEqual(header_body, get_body)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/issued-invoice-voids",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"issued_invoice_voids", "next_cursor"})
        self.assertEqual(len(list_body["issued_invoice_voids"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        page_two_status, page_two = invoke_http(
            app,
            "GET",
            "/v1/issued-invoice-voids",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(page_two_status, 200)
        self.assertEqual(len(page_two["issued_invoice_voids"]), 1)
        self.assertIsNone(page_two["next_cursor"])
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/issued-invoice-voids",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["issued_invoice_voids"], [])
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/issued-invoice-voids/{void_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "issued_invoice_void_not_found")
        missing_status, missing_body = invoke_http(
            app,
            "GET",
            f"/v1/issued-invoice-voids/{void_id}",
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        method_status, _method_body = invoke_http(
            app,
            "PUT",
            f"/v1/issued-invoices/{first_issued.issued_invoice_id}/voids",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 405)
        get_method_status, get_method_body = invoke_http(
            app,
            "POST",
            "/v1/issued-invoice-voids",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_method_status, 422)
        self.assertEqual(get_method_body["rejection_reason_code"], "request_invalid")
        item_method_status, item_method_body = invoke_http(
            app,
            "POST",
            f"/v1/issued-invoice-voids/{void_id}",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(item_method_status, 422)
        self.assertEqual(item_method_body["rejection_reason_code"], "request_invalid")
        voided_events = [
            event
            for event in ledger.webhook_outbox_events.values()
            if event.event_type_code == EVENT_TYPE_INVOICE_VOIDED
        ]
        self.assertEqual(len(voided_events), 2)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox + 2)
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        self.assertEqual(len(ledger.payment_receipts), 0)
        self.assertEqual(len(ledger.collection_case_settlements), 0)

    def test_presentment_rejects_invalid_page_and_missing_rows(self) -> None:
        """Presentment fails closed on unreadable cursors and unknown ids."""
        ledger, issued, _collection = issue_known_morning_invoice()
        voided = IssuedInvoiceVoidService(ledger).void_issued_invoice(
            TENANT_ONE, issued.issued_invoice_id
        )
        presentment = IssuedInvoiceVoidPresentmentService(ledger)
        with self.assertRaises(IssuedInvoiceVoidPresentmentQueryError) as missing:
            presentment.present_issued_invoice_void(TENANT_ONE, uuid4())
        self.assertEqual(
            missing.exception.rejection_reason_code, "issued_invoice_void_not_found"
        )
        with self.assertRaises(IssuedInvoiceVoidPresentmentQueryError) as bad_cursor:
            presentment.list_issued_invoice_voids(TENANT_ONE, cursor="not-a-cursor")
        self.assertEqual(bad_cursor.exception.rejection_reason_code, "request_invalid")
        with self.assertRaises(IssuedInvoiceVoidPresentmentQueryError) as bad_limit:
            presentment.list_issued_invoice_voids(TENANT_ONE, page_limit=0)
        self.assertEqual(bad_limit.exception.rejection_reason_code, "request_invalid")
        page = presentment.list_issued_invoice_voids(TENANT_ONE)
        self.assertEqual(len(page.issued_invoice_voids), 1)
        self.assertEqual(
            page.issued_invoice_voids[0].issued_invoice_void_id,
            voided.issued_invoice_void_id,
        )

    def test_contract_and_ledger_guards_cover_identity(self) -> None:
        """Accepted voids need identity; ledger rows stay append-only."""
        valid = {
            "issued_invoice_void_contract_version": 1,
            "issued_invoice_void_outcome_code": "accepted",
            "issued_invoice_void_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd80",
            "tenant_reference": TENANT_ONE,
            "issued_invoice_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd81",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd21",
            "currency_code": "USD",
            "voided_amount": format_exact_decimal(KNOWN_MORNING_TOTAL),
            "remaining_outstanding_amount": "0",
            "issued_invoice_void_status": "recorded",
            "collection_case_status": "voided",
            "voided_at": "2026-08-18T12:00:00Z",
            "source_payload_hash": "sha256:" + "d" * 64,
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_issued_invoice_void(valid), ())
        rejected = {
            "issued_invoice_void_contract_version": 1,
            "issued_invoice_void_outcome_code": "rejected",
            "rejection_reason_code": "issued_invoice_not_found",
        }
        self.assertEqual(validate_issued_invoice_void(rejected), ())
        missing_id = json.loads(json.dumps(valid))
        del missing_id["issued_invoice_void_id"]
        self.assertTrue(validate_issued_invoice_void(missing_id))
        self.assertTrue(validate_issued_invoice_void(["not-an-object"]))
        ledger, issued, collection = issue_known_morning_invoice()
        voided = IssuedInvoiceVoidService(ledger).void_issued_invoice(
            TENANT_ONE, issued.issued_invoice_id
        )
        stored = ledger.get_issued_invoice_void(voided.issued_invoice_void_id)
        with self.assertRaises(ValueError):
            ledger.insert_issued_invoice_void(stored)
        colliding = replace(stored, issued_invoice_void_id=generate_record_id())
        with self.assertRaises(ValueError):
            ledger.insert_issued_invoice_void(colliding)
        with self.assertRaises(ValueError):
            ledger.insert_issued_invoice_void(
                replace(stored, issued_invoice_void_status="settled")
            )
        with self.assertRaises(ValueError):
            ledger.insert_issued_invoice_void(
                replace(
                    stored,
                    issued_invoice_void_id=generate_record_id(),
                    issued_invoice_id=generate_record_id(),
                    remaining_outstanding_amount=Decimal("1"),
                )
            )
        with self.assertRaises(ValueError):
            ledger.insert_issued_invoice_void(
                replace(
                    stored,
                    issued_invoice_void_id=generate_record_id(),
                    issued_invoice_id=generate_record_id(),
                    voided_amount=Decimal("0"),
                )
            )
        with self.assertRaises(ValueError):
            ledger.mark_collection_case_voided(uuid4(), KNOWN_MORNING_TOTAL)
        already = ledger.mark_collection_case_voided(
            collection.collection_case_id, KNOWN_MORNING_TOTAL
        )
        self.assertEqual(already.collection_case_status, "voided")
        settled_ledger, _settled_issued, settled_case = issue_known_morning_invoice()
        settled_ledger.apply_collection_settlement(
            settled_case.collection_case_id, KNOWN_MORNING_TOTAL
        )
        with self.assertRaises(ValueError):
            settled_ledger.mark_collection_case_voided(
                settled_case.collection_case_id, KNOWN_MORNING_TOTAL
            )
        mismatch_ledger, _mismatch_issued, mismatch_case = issue_known_morning_invoice()
        with self.assertRaises(ValueError):
            mismatch_ledger.mark_collection_case_voided(
                mismatch_case.collection_case_id, Decimal("1.00")
            )
        voided_ledger, _voided_issued, voided_case = issue_known_morning_invoice()
        voided_ledger.mark_collection_case_voided(
            voided_case.collection_case_id, KNOWN_MORNING_TOTAL
        )
        with self.assertRaises(ValueError):
            voided_ledger.mark_collection_case_settled(voided_case.collection_case_id)
        with self.assertRaises(ValueError):
            voided_ledger.apply_collection_settlement(
                voided_case.collection_case_id, Decimal("1.00")
            )
        with self.assertRaises(ValueError):
            voided_ledger.apply_collection_write_off(
                voided_case.collection_case_id, Decimal("1.00")
            )
        with self.assertRaises(ValueError):
            voided_ledger.apply_unapplied_cash_to_collection_case(
                voided_case.collection_case_id, Decimal("1.00")
            )
        missing_remaining = json.loads(json.dumps(valid))
        del missing_remaining["remaining_outstanding_amount"]
        self.assertEqual(validate_issued_invoice_void(missing_remaining), ())
        unknown_outcome = {
            "issued_invoice_void_contract_version": 1,
            "issued_invoice_void_outcome_code": "posted",
        }
        self.assertTrue(validate_issued_invoice_void(unknown_outcome))
        missing_outcome = {"issued_invoice_void_contract_version": 1}
        self.assertTrue(validate_issued_invoice_void(missing_outcome))
        legal = json.loads(json.dumps(valid))
        legal["legal_invoice_number"] = "INV-1"
        self.assertTrue(validate_issued_invoice_void(legal))
        rejected_legal = {
            "issued_invoice_void_contract_version": 1,
            "issued_invoice_void_outcome_code": "rejected",
            "rejection_reason_code": "issued_invoice_not_found",
            "legal_invoice_number": "INV-1",
        }
        self.assertTrue(validate_issued_invoice_void(rejected_legal))
        rejected_missing_reason = {
            "issued_invoice_void_contract_version": 1,
            "issued_invoice_void_outcome_code": "rejected",
        }
        self.assertTrue(validate_issued_invoice_void(rejected_missing_reason))
        nonzero = json.loads(json.dumps(valid))
        nonzero["remaining_outstanding_amount"] = "1.00"
        self.assertTrue(validate_issued_invoice_void(nonzero))
        bad_remaining = json.loads(json.dumps(valid))
        bad_remaining["remaining_outstanding_amount"] = "not-decimal"
        self.assertTrue(validate_issued_invoice_void(bad_remaining))
        int_remaining = json.loads(json.dumps(valid))
        int_remaining["remaining_outstanding_amount"] = 0
        self.assertTrue(validate_issued_invoice_void(int_remaining))
        missing_amount = json.loads(json.dumps(valid))
        del missing_amount["voided_amount"]
        self.assertTrue(validate_issued_invoice_void(missing_amount))
        zero_amount = json.loads(json.dumps(valid))
        zero_amount["voided_amount"] = "0"
        self.assertTrue(validate_issued_invoice_void(zero_amount))
        bad_amount = json.loads(json.dumps(valid))
        bad_amount["voided_amount"] = 1
        self.assertTrue(validate_issued_invoice_void(bad_amount))
        unreadable_amount = json.loads(json.dumps(valid))
        unreadable_amount["voided_amount"] = "not-decimal"
        self.assertTrue(validate_issued_invoice_void(unreadable_amount))
        rejected_credit_legal = {
            "issued_invoice_void_contract_version": 1,
            "issued_invoice_void_outcome_code": "rejected",
            "rejection_reason_code": "issued_invoice_not_found",
            "legal_credit_note_number": "CN-1",
        }
        self.assertTrue(validate_issued_invoice_void(rejected_credit_legal))

    def test_coverage_guards_for_void_and_presentment_edges(self) -> None:
        """Closed branches stay explicit: replay, presentment, constructors, and HTTP."""
        ledger, issued, collection = issue_known_morning_invoice()
        voided = IssuedInvoiceVoidService(ledger).void_issued_invoice(
            TENANT_ONE, issued.issued_invoice_id
        )
        presentment = IssuedInvoiceVoidPresentmentService(ledger)
        presented = presentment.present_issued_invoice_void(
            TENANT_ONE, voided.issued_invoice_void_id
        )
        presentment_payload = presented.as_contract_dict()
        with self.assertRaises(IssuedInvoiceVoidPresentmentQueryError):
            presentment.list_issued_invoice_voids(TENANT_ONE, page_limit=True)
        with self.assertRaises(IssuedInvoiceVoidPresentmentQueryError):
            presentment.list_issued_invoice_voids(TENANT_ONE, page_limit="abc")
        with self.assertRaises(IssuedInvoiceVoidPresentmentQueryError):
            presentment.list_issued_invoice_voids(TENANT_ONE, page_limit=101)
        with self.assertRaises(IssuedInvoiceVoidPresentmentQueryError):
            presentment.list_issued_invoice_voids(TENANT_ONE, page_limit=1.5)
        default_page = presentment.list_issued_invoice_voids(TENANT_ONE, page_limit="")
        self.assertEqual(len(default_page.issued_invoice_voids), 1)
        with self.assertRaises(IssuedInvoiceVoidPresentmentQueryError) as missing_tenant:
            presentment.present_issued_invoice_void(
                "urn:cwl:missing", voided.issued_invoice_void_id
            )
        self.assertEqual(missing_tenant.exception.rejection_reason_code, "tenant_not_found")
        mutated = ledger.get_collection_case(collection.collection_case_id)
        ledger.collection_cases[collection.collection_case_id] = replace(
            mutated, outstanding_amount=Decimal("1.00")
        )
        mutated_presentment = presentment.present_issued_invoice_void(
            TENANT_ONE, voided.issued_invoice_void_id
        )
        self.assertEqual(mutated_presentment.remaining_outstanding_amount, Decimal("1.00"))
        mutated_replay = IssuedInvoiceVoidService(ledger).void_issued_invoice(
            TENANT_ONE, issued.issued_invoice_id
        )
        self.assertEqual(mutated_replay.remaining_outstanding_amount, Decimal("1.00"))
        del ledger.collection_cases[collection.collection_case_id]
        missing_replay = IssuedInvoiceVoidService(ledger).void_issued_invoice(
            TENANT_ONE, issued.issued_invoice_id
        )
        self.assertEqual(
            missing_replay.rejection_reason_code,
            IssuedInvoiceVoidRejectionReasonCode.ISSUED_INVOICE_NOT_FOUND,
        )
        with self.assertRaises(IssuedInvoiceVoidPresentmentQueryError) as missing_case:
            presentment.present_issued_invoice_void(
                TENANT_ONE, voided.issued_invoice_void_id
            )
        self.assertEqual(
            missing_case.exception.rejection_reason_code,
            "issued_invoice_void_not_found",
        )
        with mock.patch.object(presentment.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                presentment.present_issued_invoice_void(
                    TENANT_ONE, voided.issued_invoice_void_id
                )
        IssuedInvoiceVoidService()
        IssuedInvoiceVoidPresentmentService()
        with self.assertRaises(ValueError):
            _format_voided_at(None)
        unsupported = replace(
            _rejected(IssuedInvoiceVoidRejectionReasonCode.ISSUED_INVOICE_NOT_FOUND),
            issued_invoice_void_outcome_code="posted",
        )
        with self.assertRaises(ValueError):
            unsupported.as_contract_dict()
        accepted_without_time = IssuedInvoiceVoidResult(
            issued_invoice_void_outcome_code=IssuedInvoiceVoidOutcomeCode.ACCEPTED,
            issued_invoice_void_contract_version=1,
            issued_invoice_void_id=generate_record_id(),
            issued_invoice_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            voided_amount=KNOWN_MORNING_TOTAL,
            remaining_outstanding_amount=Decimal("0"),
            issued_invoice_void_status="recorded",
            collection_case_status="voided",
            voided_at=None,
            source_payload_hash="sha256:" + "e" * 64,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaises(ValueError):
            accepted_without_time.as_contract_dict()
        none_reason = IssuedInvoiceVoidResult(
            issued_invoice_void_outcome_code=IssuedInvoiceVoidOutcomeCode.REJECTED,
            issued_invoice_void_contract_version=1,
            issued_invoice_void_id=None,
            issued_invoice_id=None,
            invoice_draft_id=None,
            collection_case_id=None,
            tenant_reference=None,
            currency_code=None,
            voided_amount=None,
            remaining_outstanding_amount=None,
            issued_invoice_void_status=None,
            collection_case_status=None,
            voided_at=None,
            source_payload_hash=None,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        self.assertEqual(
            none_reason.as_contract_dict()["rejection_reason_code"],
            "issued_invoice_not_found",
        )
        string_rejected = IssuedInvoiceVoidResult(
            issued_invoice_void_outcome_code="rejected",
            issued_invoice_void_contract_version=1,
            issued_invoice_void_id=None,
            issued_invoice_id=None,
            invoice_draft_id=None,
            collection_case_id=None,
            tenant_reference=None,
            currency_code=None,
            voided_amount=None,
            remaining_outstanding_amount=None,
            issued_invoice_void_status=None,
            collection_case_status=None,
            voided_at=None,
            source_payload_hash=None,
            next_operator_action="wait",
            rejection_reason_code=IssuedInvoiceVoidRejectionReasonCode.CURRENCY_MISMATCH,
        )
        self.assertEqual(
            string_rejected.as_contract_dict()["rejection_reason_code"],
            "currency_mismatch",
        )
        string_accepted = IssuedInvoiceVoidResult(
            issued_invoice_void_outcome_code="accepted",
            issued_invoice_void_contract_version=1,
            issued_invoice_void_id=generate_record_id(),
            issued_invoice_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            voided_amount=KNOWN_MORNING_TOTAL,
            remaining_outstanding_amount=Decimal("0"),
            issued_invoice_void_status="recorded",
            collection_case_status="voided",
            voided_at=VOIDED_MORNING,
            source_payload_hash="sha256:" + "f" * 64,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        self.assertEqual(
            string_accepted.as_contract_dict()["issued_invoice_void_outcome_code"],
            "accepted",
        )
        self.assertIn("collection_case_id", string_accepted.as_contract_dict())
        webhook_data = voided.as_webhook_event_data()
        self.assertEqual(
            webhook_data["issued_invoice_void_id"], str(voided.issued_invoice_void_id)
        )
        self.assertEqual(webhook_data["issued_invoice_id"], str(issued.issued_invoice_id))
        self.assertEqual(webhook_data["invoice_draft_id"], str(issued.invoice_draft_id))
        self.assertEqual(
            webhook_data["collection_case_id"], str(collection.collection_case_id)
        )
        self.assertEqual(webhook_data["source_payload_hash"], voided.source_payload_hash)
        self.assertEqual(webhook_data["issued_invoice_void_contract_version"], 1)
        self.assertEqual(webhook_data["currency_code"], "USD")
        self.assertEqual(webhook_data["voided_amount"], format_exact_decimal(KNOWN_MORNING_TOTAL))
        self.assertEqual(webhook_data["issued_invoice_void_status"], "recorded")
        self.assertEqual(webhook_data["voided_at"], voided.as_contract_dict()["voided_at"])
        self.assertNotIn("remaining_outstanding_amount", webhook_data)
        self.assertNotIn("collection_case_status", webhook_data)
        self.assertNotIn("tenant_reference", webhook_data)
        self.assertNotIn("legal_invoice_number", webhook_data)
        rejected = _rejected(IssuedInvoiceVoidRejectionReasonCode.ISSUED_INVOICE_NOT_FOUND)
        with self.assertRaisesRegex(
            ValueError, "rejected issued-invoice void has no webhook event data"
        ):
            rejected.as_webhook_event_data()
        missing_invoice = IssuedInvoiceVoidResult(
            issued_invoice_void_outcome_code=IssuedInvoiceVoidOutcomeCode.ACCEPTED,
            issued_invoice_void_contract_version=1,
            issued_invoice_void_id=generate_record_id(),
            issued_invoice_id=None,
            invoice_draft_id=generate_record_id(),
            collection_case_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            voided_amount=KNOWN_MORNING_TOTAL,
            remaining_outstanding_amount=Decimal("0"),
            issued_invoice_void_status="recorded",
            collection_case_status=None,
            voided_at=VOIDED_MORNING,
            source_payload_hash="sha256:" + ("11" * 32),
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaisesRegex(
            ValueError, "rejected issued-invoice void has no webhook event data"
        ):
            missing_invoice.as_webhook_event_data()
        missing_draft = IssuedInvoiceVoidResult(
            issued_invoice_void_outcome_code=IssuedInvoiceVoidOutcomeCode.ACCEPTED,
            issued_invoice_void_contract_version=1,
            issued_invoice_void_id=generate_record_id(),
            issued_invoice_id=generate_record_id(),
            invoice_draft_id=None,
            collection_case_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            voided_amount=KNOWN_MORNING_TOTAL,
            remaining_outstanding_amount=Decimal("0"),
            issued_invoice_void_status="recorded",
            collection_case_status=None,
            voided_at=VOIDED_MORNING,
            source_payload_hash="sha256:" + ("12" * 32),
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaisesRegex(
            ValueError, "rejected issued-invoice void has no webhook event data"
        ):
            missing_draft.as_webhook_event_data()
        with self.assertRaisesRegex(
            ValueError, "accepted issued-invoice voids must include voided_at"
        ):
            accepted_without_time.as_webhook_event_data()
        incomplete = IssuedInvoiceVoidResult(
            issued_invoice_void_outcome_code=IssuedInvoiceVoidOutcomeCode.ACCEPTED,
            issued_invoice_void_contract_version=1,
            issued_invoice_void_id=None,
            issued_invoice_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            collection_case_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            voided_amount=KNOWN_MORNING_TOTAL,
            remaining_outstanding_amount=None,
            issued_invoice_void_status="recorded",
            collection_case_status=None,
            voided_at=None,
            source_payload_hash="sha256:" + ("22" * 32),
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaisesRegex(
            ValueError, "accepted issued-invoice voids must include identity"
        ):
            _enqueue_invoice_voided(ledger, TENANT_ONE, incomplete)
        missing_time = IssuedInvoiceVoidResult(
            issued_invoice_void_outcome_code=IssuedInvoiceVoidOutcomeCode.ACCEPTED,
            issued_invoice_void_contract_version=1,
            issued_invoice_void_id=generate_record_id(),
            issued_invoice_id=issued.issued_invoice_id,
            invoice_draft_id=issued.invoice_draft_id,
            collection_case_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            voided_amount=KNOWN_MORNING_TOTAL,
            remaining_outstanding_amount=None,
            issued_invoice_void_status="recorded",
            collection_case_status=None,
            voided_at=None,
            source_payload_hash="sha256:" + ("33" * 32),
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaisesRegex(
            ValueError, "accepted issued-invoice voids must include identity"
        ):
            _enqueue_invoice_voided(ledger, TENANT_ONE, missing_time)
        self.assertEqual(validate_issued_invoice_void_presentment(presentment_payload), ())
        self.assertIn("issued_invoice_void_id", presented.as_summary_dict())
        missing_presentment_remaining = json.loads(json.dumps(presentment_payload))
        del missing_presentment_remaining["remaining_outstanding_amount"]
        self.assertEqual(
            validate_issued_invoice_void_presentment(missing_presentment_remaining), ()
        )
        nonzero_presentment = json.loads(json.dumps(presentment_payload))
        nonzero_presentment["remaining_outstanding_amount"] = "1.00"
        nonzero_presentment["next_operator_action"] = "wait"
        self.assertTrue(validate_issued_invoice_void_presentment(nonzero_presentment))
        wait_mismatch = json.loads(json.dumps(presentment_payload))
        wait_mismatch["remaining_outstanding_amount"] = "0"
        wait_mismatch["next_operator_action"] = "collect"
        self.assertTrue(validate_issued_invoice_void_presentment(wait_mismatch))
        forbidden_presentment = json.loads(json.dumps(presentment_payload))
        forbidden_presentment["legal_invoice_number"] = "INV-1"
        self.assertTrue(validate_issued_invoice_void_presentment(forbidden_presentment))
        self.assertTrue(validate_issued_invoice_void_presentment(["not-an-object"]))
        float_remaining = json.loads(json.dumps(presentment_payload))
        float_remaining["remaining_outstanding_amount"] = 0
        self.assertTrue(validate_issued_invoice_void_presentment(float_remaining))
        bad_presentment_remaining = json.loads(json.dumps(presentment_payload))
        bad_presentment_remaining["remaining_outstanding_amount"] = "not-decimal"
        self.assertTrue(
            validate_issued_invoice_void_presentment(bad_presentment_remaining)
        )
        missing_voided = json.loads(json.dumps(presentment_payload))
        del missing_voided["voided_amount"]
        self.assertTrue(validate_issued_invoice_void_presentment(missing_voided))
        zero_voided = json.loads(json.dumps(presentment_payload))
        zero_voided["voided_amount"] = "0"
        self.assertTrue(validate_issued_invoice_void_presentment(zero_voided))
        int_voided = json.loads(json.dumps(presentment_payload))
        int_voided["voided_amount"] = 1
        self.assertTrue(validate_issued_invoice_void_presentment(int_voided))
        bad_voided = json.loads(json.dumps(presentment_payload))
        bad_voided["voided_amount"] = "not-decimal"
        self.assertTrue(validate_issued_invoice_void_presentment(bad_voided))
        legal_presentment = json.loads(json.dumps(presentment_payload))
        legal_presentment["legal_credit_note_number"] = "CN-1"
        self.assertTrue(validate_issued_invoice_void_presentment(legal_presentment))
        outcome_presentment = json.loads(json.dumps(presentment_payload))
        outcome_presentment["issued_invoice_void_outcome_code"] = "accepted"
        self.assertTrue(validate_issued_invoice_void_presentment(outcome_presentment))
        pan_presentment = json.loads(json.dumps(presentment_payload))
        pan_presentment["card_pan"] = "4111111111111111"
        self.assertTrue(validate_issued_invoice_void_presentment(pan_presentment))
        unknown_status = issue_known_morning_invoice()
        unknown_ledger, unknown_issued, unknown_case = unknown_status
        unknown_ledger.collection_cases[unknown_case.collection_case_id] = replace(
            unknown_ledger.get_collection_case(unknown_case.collection_case_id),
            collection_case_status="cancelled",
        )
        cancelled = IssuedInvoiceVoidService(unknown_ledger).void_issued_invoice(
            TENANT_ONE, unknown_issued.issued_invoice_id
        )
        self.assertEqual(
            cancelled.rejection_reason_code,
            IssuedInvoiceVoidRejectionReasonCode.COLLECTION_CASE_SETTLED,
        )
        missing_issued = issue_known_morning_invoice()
        missing_issued_ledger, missing_issued_invoice, missing_issued_case = missing_issued
        del missing_issued_ledger.issued_invoice_index[
            (
                missing_issued_ledger.get_issued_invoice(
                    missing_issued_invoice.issued_invoice_id
                ).tenant_account_id,
                missing_issued_invoice.invoice_draft_id,
            )
        ]
        missing_index = IssuedInvoiceVoidService(missing_issued_ledger).void_issued_invoice(
            TENANT_ONE, missing_issued_invoice.issued_invoice_id
        )
        self.assertEqual(
            missing_index.rejection_reason_code,
            IssuedInvoiceVoidRejectionReasonCode.OUTSTANDING_MISMATCH,
        )
        prevoided_ledger, prevoided_issued, prevoided_case = issue_known_morning_invoice()
        prevoided_ledger.mark_collection_case_voided(
            prevoided_case.collection_case_id, KNOWN_MORNING_TOTAL
        )
        already_voided_case = IssuedInvoiceVoidService(prevoided_ledger).void_issued_invoice(
            TENANT_ONE, prevoided_issued.issued_invoice_id
        )
        self.assertEqual(
            already_voided_case.rejection_reason_code,
            IssuedInvoiceVoidRejectionReasonCode.COLLECTION_CASE_SETTLED,
        )
        stored_dunning = issue_known_morning_invoice()
        dunning_ledger, dunning_issued, dunning_case = stored_dunning
        dunning_ledger.collection_cases[dunning_case.collection_case_id] = replace(
            dunning_ledger.get_collection_case(dunning_case.collection_case_id),
            collection_case_status="dunning",
        )
        dunning_void = IssuedInvoiceVoidService(dunning_ledger).void_issued_invoice(
            TENANT_ONE, dunning_issued.issued_invoice_id
        )
        self.assertEqual(
            dunning_void.issued_invoice_void_outcome_code,
            IssuedInvoiceVoidOutcomeCode.ACCEPTED,
        )
        zero_remaining = issue_known_morning_invoice()
        zero_ledger, zero_issued, zero_case = zero_remaining
        zero_ledger.collection_cases[zero_case.collection_case_id] = replace(
            zero_ledger.get_collection_case(zero_case.collection_case_id),
            outstanding_amount=Decimal("0"),
        )
        zeroed = IssuedInvoiceVoidService(zero_ledger).void_issued_invoice(
            TENANT_ONE, zero_issued.issued_invoice_id
        )
        self.assertEqual(
            zeroed.rejection_reason_code,
            IssuedInvoiceVoidRejectionReasonCode.OUTSTANDING_MISMATCH,
        )
        app = create_http_app(ledger)
        missing_tenant_status, missing_tenant_body = invoke_http(
            app,
            "POST",
            f"/v1/issued-invoices/{issued.issued_invoice_id}/voids",
            {},
        )
        self.assertEqual(missing_tenant_status, 422)
        self.assertEqual(missing_tenant_body["rejection_reason_code"], "tenant_not_found")
        with mock.patch(
            "metering_billing.http_app.IssuedInvoiceVoidService.void_issued_invoice",
            side_effect=ValueError("bad clock"),
        ):
            error_status, error_body = invoke_http(
                app,
                "POST",
                f"/v1/issued-invoices/{issued.issued_invoice_id}/voids",
                {"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(error_status, 422)
        self.assertEqual(error_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.IssuedInvoiceVoidPresentmentService.present_issued_invoice_void",
            side_effect=ValueError("bad presentment"),
        ):
            present_error_status, present_error_body = invoke_http(
                app,
                "GET",
                f"/v1/issued-invoice-voids/{voided.issued_invoice_void_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(present_error_status, 422)
        self.assertEqual(present_error_body["rejection_reason_code"], "request_invalid")
        no_case_ledger, no_case_issued, _none = issue_known_morning_invoice(open_case=False)
        no_case_void = IssuedInvoiceVoidService(no_case_ledger).void_issued_invoice(
            TENANT_ONE, no_case_issued.issued_invoice_id
        )
        no_case_presentment = IssuedInvoiceVoidPresentmentService(
            no_case_ledger
        ).present_issued_invoice_void(TENANT_ONE, no_case_void.issued_invoice_void_id)
        self.assertIsNone(no_case_presentment.collection_case_id)
        self.assertNotIn("collection_case_id", no_case_presentment.as_contract_dict())
        self.assertNotIn(
            "remaining_outstanding_amount", no_case_presentment.as_summary_dict()
        )
