"""Collection dispute hold tests for pausing dunning on one open case."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    CollectionCasePresentmentService,
    CollectionCaseService,
    CollectionCaseSettlementService,
    CollectionDisputePresentmentService,
    CollectionDisputeService,
    CollectionWriteOffService,
    CreditNoteApplicationService,
    IssuedInvoiceVoidService,
    PaymentIntentService,
    PaymentSettlementService,
    UnappliedCashApplicationService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.contracts import (
    validate_collection_dispute,
    validate_collection_dispute_presentment,
)
from metering_billing.errors import (
    CollectionCaseRejectionReasonCode,
    CollectionCaseSettlementRejectionReasonCode,
    CollectionDisputeOutcomeCode,
    CollectionDisputePresentmentQueryError,
    CollectionDisputeRejectionReasonCode,
    CollectionWriteOffRejectionReasonCode,
    CreditNoteApplicationRejectionReasonCode,
    IssuedInvoiceVoidRejectionReasonCode,
    PaymentSettlementRejectionReasonCode,
    UnappliedCashApplicationRejectionReasonCode,
)
from metering_billing.collection_dispute import (
    CollectionDisputeResult,
    _format_held_at,
    _rejected,
)
from metering_billing.usage_ledger import generate_record_id
from test_collection_case import draft_known_morning
from test_collection_write_off import open_morning_case_with_outstanding
from test_credit_note_application import issue_morning_credit_then_open_case
from test_http_app import invoke_http
from test_issued_invoice_void import issue_known_morning_invoice
from test_tax_assessment import insert_commercial_draft
from test_unapplied_cash_application import park_leftover_and_open_second_case
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL


HELD_MORNING = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)
HELD_EVENING = datetime(2026, 8, 18, 22, 0, tzinfo=UTC)


class CollectionDisputeTests(unittest.TestCase):
    """Verify hold-once identity, disputed status, and fail-closed money paths."""

    def test_hold_open_case_once_without_changing_remaining(self) -> None:
        """An open case holds once, flips to disputed, and leaves outstanding unchanged."""
        ledger, collection = open_morning_case_with_outstanding()
        remaining_before = collection.outstanding_amount
        prior_journals = len(ledger.journal_proposals)
        prior_outbox = len(ledger.webhook_outbox_events)
        prior_receipts = len(ledger.payment_receipts)
        prior_settlements = len(ledger.collection_case_settlements)
        prior_write_offs = len(ledger.collection_write_offs)
        prior_voids = len(ledger.issued_invoice_voids)
        first = CollectionDisputeService(
            ledger, clock=lambda: HELD_MORNING
        ).hold_collection_case(TENANT_ONE, collection.collection_case_id)
        second = CollectionDisputeService(
            ledger, clock=lambda: HELD_EVENING
        ).hold_collection_case(TENANT_ONE, collection.collection_case_id)
        self.assertEqual(
            first.collection_dispute_outcome_code, CollectionDisputeOutcomeCode.ACCEPTED
        )
        self.assertEqual(
            second.collection_dispute_outcome_code,
            CollectionDisputeOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertEqual(first.collection_dispute_id, second.collection_dispute_id)
        self.assertEqual(first.collection_case_id, collection.collection_case_id)
        self.assertEqual(first.invoice_draft_id, collection.invoice_draft_id)
        self.assertEqual(first.currency_code, "USD")
        self.assertEqual(first.remaining_outstanding_amount, remaining_before)
        self.assertEqual(first.collection_dispute_status, "held")
        self.assertEqual(first.collection_case_status, "disputed")
        self.assertEqual(first.held_at, HELD_MORNING)
        self.assertEqual(second.held_at, HELD_MORNING)
        self.assertEqual(first.next_operator_action, "wait")
        self.assertTrue(first.source_payload_hash.startswith("sha256:"))
        payload = first.as_contract_dict()
        self.assertEqual(validate_collection_dispute(payload), ())
        self.assertIsInstance(payload["remaining_outstanding_amount"], str)
        self.assertNotIsInstance(payload["remaining_outstanding_amount"], float)
        self.assertNotIn("card_pan", payload)
        self.assertNotIn("legal_invoice_number", payload)
        stored_case = ledger.get_collection_case(collection.collection_case_id)
        self.assertEqual(stored_case.outstanding_amount, remaining_before)
        self.assertEqual(stored_case.collection_case_status, "disputed")
        self.assertEqual(len(ledger.collection_disputes), 1)
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        self.assertEqual(len(ledger.payment_receipts), prior_receipts)
        self.assertEqual(len(ledger.collection_case_settlements), prior_settlements)
        self.assertEqual(len(ledger.collection_write_offs), prior_write_offs)
        self.assertEqual(len(ledger.issued_invoice_voids), prior_voids)
        presented = CollectionCasePresentmentService(ledger).present_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(presented.collection_case_status, "disputed")
        self.assertEqual(presented.collection_outstanding, remaining_before)
        self.assertIsNone(presented.next_dunning_notice_code)
        self.assertEqual(presented.next_operator_action, "wait")

    def test_hold_dunning_case_leaves_remaining_and_pauses_new_notices(self) -> None:
        """A dunning case holds as disputed; new notices fail and stored notices replay."""
        ledger, collection = open_morning_case_with_outstanding()
        recorded = CollectionCaseService(ledger).record_dunning_event(
            TENANT_ONE, collection.collection_case_id, "first_notice"
        )
        self.assertEqual(recorded.collection_case_status, "dunning")
        remaining_before = collection.outstanding_amount
        held = CollectionDisputeService(ledger).hold_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(held.collection_case_status, "disputed")
        stored_case = ledger.get_collection_case(collection.collection_case_id)
        self.assertEqual(stored_case.outstanding_amount, remaining_before)
        self.assertEqual(stored_case.collection_case_status, "disputed")
        replayed = CollectionCaseService(ledger).record_dunning_event(
            TENANT_ONE, collection.collection_case_id, "first_notice"
        )
        self.assertEqual(replayed.collection_case_outcome_code.value, "duplicate_replay")
        self.assertEqual(replayed.collection_case_status, "disputed")
        refused = CollectionCaseService(ledger).record_dunning_event(
            TENANT_ONE, collection.collection_case_id, "overdue_notice"
        )
        self.assertEqual(
            refused.rejection_reason_code,
            CollectionCaseRejectionReasonCode.COLLECTION_CASE_DISPUTED,
        )
        self.assertEqual(len(ledger.list_collection_dunning_events(collection.collection_case_id)), 1)

    def test_money_and_close_commands_fail_closed_while_disputed(self) -> None:
        """Write-off, settle, void, receipt, credit, and leftover apply refuse a hold."""
        write_off_ledger, write_off_case = open_morning_case_with_outstanding()
        CollectionDisputeService(write_off_ledger).hold_collection_case(
            TENANT_ONE, write_off_case.collection_case_id
        )
        written = CollectionWriteOffService(write_off_ledger).write_off_collection_case(
            TENANT_ONE, write_off_case.collection_case_id
        )
        self.assertEqual(
            written.rejection_reason_code,
            CollectionWriteOffRejectionReasonCode.COLLECTION_CASE_DISPUTED,
        )
        self.assertEqual(len(write_off_ledger.collection_write_offs), 0)
        stored_write_off_case = write_off_ledger.get_collection_case(
            write_off_case.collection_case_id
        )
        self.assertEqual(stored_write_off_case.outstanding_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(stored_write_off_case.collection_case_status, "disputed")

        settle_ledger, settle_case = open_morning_case_with_outstanding()
        CollectionWriteOffService(settle_ledger).write_off_collection_case(
            TENANT_ONE, settle_case.collection_case_id
        )
        CollectionDisputeService(settle_ledger).hold_collection_case(
            TENANT_ONE, settle_case.collection_case_id
        )
        settled = CollectionCaseSettlementService(settle_ledger).settle_collection_case(
            TENANT_ONE, settle_case.collection_case_id
        )
        self.assertEqual(
            settled.rejection_reason_code,
            CollectionCaseSettlementRejectionReasonCode.COLLECTION_CASE_DISPUTED,
        )
        self.assertEqual(len(settle_ledger.collection_case_settlements), 0)
        self.assertEqual(
            settle_ledger.get_collection_case(settle_case.collection_case_id).collection_case_status,
            "disputed",
        )

        void_ledger, issued, void_case = issue_known_morning_invoice()
        CollectionDisputeService(void_ledger).hold_collection_case(
            TENANT_ONE, void_case.collection_case_id
        )
        voided = IssuedInvoiceVoidService(void_ledger).void_issued_invoice(
            TENANT_ONE, issued.issued_invoice_id
        )
        self.assertEqual(
            voided.rejection_reason_code,
            IssuedInvoiceVoidRejectionReasonCode.COLLECTION_CASE_DISPUTED,
        )
        self.assertEqual(len(void_ledger.issued_invoice_voids), 0)
        self.assertEqual(
            void_ledger.get_collection_case(void_case.collection_case_id).collection_case_status,
            "disputed",
        )
        self.assertEqual(
            void_ledger.get_collection_case(void_case.collection_case_id).outstanding_amount,
            KNOWN_MORNING_TOTAL,
        )

        receipt_ledger, receipt_case = open_morning_case_with_outstanding()
        intent = PaymentIntentService(receipt_ledger).project_payment_intent(
            TENANT_ONE, receipt_case.collection_case_id
        )
        CollectionDisputeService(receipt_ledger).hold_collection_case(
            TENANT_ONE, receipt_case.collection_case_id
        )
        receipt = PaymentSettlementService(receipt_ledger).record_payment_receipt(
            TENANT_ONE, intent.payment_intent_id, KNOWN_MORNING_TOTAL
        )
        self.assertEqual(
            receipt.rejection_reason_code,
            PaymentSettlementRejectionReasonCode.COLLECTION_CASE_DISPUTED,
        )
        self.assertEqual(len(receipt_ledger.payment_receipts), 0)
        self.assertEqual(
            receipt_ledger.get_collection_case(receipt_case.collection_case_id).outstanding_amount,
            KNOWN_MORNING_TOTAL,
        )

        credit_ledger, issued_credit, credit_case = issue_morning_credit_then_open_case()
        CollectionDisputeService(credit_ledger).hold_collection_case(
            TENANT_ONE, credit_case.collection_case_id
        )
        applied = CreditNoteApplicationService(credit_ledger).apply_credit_note(
            TENANT_ONE, issued_credit.issued_credit_note_id, credit_case.collection_case_id
        )
        self.assertEqual(
            applied.rejection_reason_code,
            CreditNoteApplicationRejectionReasonCode.COLLECTION_CASE_DISPUTED,
        )
        self.assertEqual(len(credit_ledger.credit_note_applications), 0)
        self.assertEqual(
            credit_ledger.get_collection_case(credit_case.collection_case_id).outstanding_amount,
            KNOWN_MORNING_TOTAL,
        )

        leftover_ledger, parked, leftover_case, _source, _receipt = (
            park_leftover_and_open_second_case()
        )
        CollectionDisputeService(leftover_ledger).hold_collection_case(
            TENANT_ONE, leftover_case.collection_case_id
        )
        leftover = UnappliedCashApplicationService(leftover_ledger).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, leftover_case.collection_case_id
        )
        self.assertEqual(
            leftover.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.COLLECTION_CASE_DISPUTED,
        )
        self.assertEqual(len(leftover_ledger.unapplied_cash_applications), 0)
        self.assertEqual(
            leftover_ledger.get_collection_case(leftover_case.collection_case_id).collection_case_status,
            "disputed",
        )

    def test_fail_closed_on_missing_settled_voided_and_currency(self) -> None:
        """Missing tenant or case, settled or voided cases, and currency mismatch refuse."""
        ledger, collection = open_morning_case_with_outstanding()
        missing_tenant = CollectionDisputeService(ledger).hold_collection_case(
            "urn:cwl:missing", collection.collection_case_id
        )
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            CollectionDisputeRejectionReasonCode.TENANT_NOT_FOUND,
        )
        missing_case = CollectionDisputeService(ledger).hold_collection_case(TENANT_ONE, uuid4())
        self.assertEqual(
            missing_case.rejection_reason_code,
            CollectionDisputeRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )
        crossed = CollectionDisputeService(ledger).hold_collection_case(
            TENANT_TWO, collection.collection_case_id
        )
        self.assertEqual(
            crossed.rejection_reason_code,
            CollectionDisputeRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )
        currency = CollectionDisputeService(ledger).hold_collection_case(
            TENANT_ONE, collection.collection_case_id, currency_code="EUR"
        )
        self.assertEqual(
            currency.rejection_reason_code,
            CollectionDisputeRejectionReasonCode.CURRENCY_MISMATCH,
        )
        settled_ledger, settled_case = open_morning_case_with_outstanding()
        CollectionWriteOffService(settled_ledger).write_off_collection_case(
            TENANT_ONE, settled_case.collection_case_id
        )
        CollectionCaseSettlementService(settled_ledger).settle_collection_case(
            TENANT_ONE, settled_case.collection_case_id
        )
        settled = CollectionDisputeService(settled_ledger).hold_collection_case(
            TENANT_ONE, settled_case.collection_case_id
        )
        self.assertEqual(
            settled.rejection_reason_code,
            CollectionDisputeRejectionReasonCode.COLLECTION_CASE_SETTLED,
        )
        void_ledger, issued, void_case = issue_known_morning_invoice()
        IssuedInvoiceVoidService(void_ledger).void_issued_invoice(
            TENANT_ONE, issued.issued_invoice_id
        )
        voided = CollectionDisputeService(void_ledger).hold_collection_case(
            TENANT_ONE, void_case.collection_case_id
        )
        self.assertEqual(
            voided.rejection_reason_code,
            CollectionDisputeRejectionReasonCode.COLLECTION_CASE_VOIDED,
        )
        self.assertEqual(len(ledger.collection_disputes), 0)
        self.assertEqual(len(settled_ledger.collection_disputes), 0)
        self.assertEqual(len(void_ledger.collection_disputes), 0)

    def test_resolver_hollow_success_raises_value_error(self) -> None:
        """A broken tenant resolve must raise ValueError, not assert."""
        ledger, collection = open_morning_case_with_outstanding()
        service = CollectionDisputeService(ledger)
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.hold_collection_case(TENANT_ONE, collection.collection_case_id)

    def test_http_hold_get_and_paged_list_without_capture(self) -> None:
        """POST holds; GET item and list page metadata and never capture payment."""
        ledger, first_case = open_morning_case_with_outstanding()
        second_draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("20.00"))
        second_case = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, second_draft_id
        )
        prior_outbox = len(ledger.webhook_outbox_events)
        prior_journals = len(ledger.journal_proposals)
        app = create_http_app(ledger)
        refused_status, refused_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/disputes",
            {"tenant_reference": TENANT_ONE, "card_pan": "4111111111111111"},
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.collection_disputes), 0)
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/disputes",
            {"tenant_reference": TENANT_ONE, "currency_code": "USD"},
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["collection_dispute_outcome_code"], "accepted")
        self.assertEqual(accepted_body["collection_case_status"], "disputed")
        self.assertEqual(
            accepted_body["remaining_outstanding_amount"],
            format_exact_decimal(KNOWN_MORNING_TOTAL),
        )
        dispute_id = accepted_body["collection_dispute_id"]
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/disputes",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["collection_dispute_outcome_code"], "duplicate_replay")
        self.assertEqual(replay_body["collection_dispute_id"], dispute_id)
        second_status, second_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{second_case.collection_case_id}/disputes",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(second_body["collection_dispute_outcome_code"], "accepted")
        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-disputes/{dispute_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["collection_dispute_id"], dispute_id)
        self.assertEqual(get_body["collection_case_id"], str(first_case.collection_case_id))
        self.assertNotIn("collection_dispute_outcome_code", get_body)
        self.assertEqual(validate_collection_dispute_presentment(get_body), ())
        header_status, header_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-disputes/{dispute_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(header_status, 200)
        self.assertEqual(header_body, get_body)
        case_status, case_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-cases/{first_case.collection_case_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(case_status, 200)
        self.assertEqual(case_body["collection_case_status"], "disputed")
        self.assertEqual(case_body["next_operator_action"], "wait")
        self.assertNotIn("next_dunning_notice_code", case_body)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/collection-disputes",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"collection_disputes", "next_cursor"})
        self.assertEqual(len(list_body["collection_disputes"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        page_two_status, page_two = invoke_http(
            app,
            "GET",
            "/v1/collection-disputes",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(page_two_status, 200)
        self.assertEqual(len(page_two["collection_disputes"]), 1)
        self.assertIsNone(page_two["next_cursor"])
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/collection-disputes",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["collection_disputes"], [])
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-disputes/{dispute_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "collection_dispute_not_found")
        missing_status, missing_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-disputes/{dispute_id}",
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        method_status, _method_body = invoke_http(
            app,
            "PUT",
            f"/v1/collection-cases/{first_case.collection_case_id}/disputes",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 405)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        self.assertEqual(len(ledger.payment_receipts), 0)
        self.assertEqual(len(ledger.collection_case_settlements), 0)

    def test_presentment_rejects_invalid_page_and_missing_rows(self) -> None:
        """Presentment fails closed on unreadable cursors and unknown ids."""
        ledger, collection = open_morning_case_with_outstanding()
        held = CollectionDisputeService(ledger).hold_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        presentment = CollectionDisputePresentmentService(ledger)
        with self.assertRaises(CollectionDisputePresentmentQueryError) as missing:
            presentment.present_collection_dispute(TENANT_ONE, uuid4())
        self.assertEqual(missing.exception.rejection_reason_code, "collection_dispute_not_found")
        with self.assertRaises(CollectionDisputePresentmentQueryError) as bad_cursor:
            presentment.list_collection_disputes(TENANT_ONE, cursor="not-a-cursor")
        self.assertEqual(bad_cursor.exception.rejection_reason_code, "request_invalid")
        with self.assertRaises(CollectionDisputePresentmentQueryError) as bad_limit:
            presentment.list_collection_disputes(TENANT_ONE, page_limit=0)
        self.assertEqual(bad_limit.exception.rejection_reason_code, "request_invalid")
        page = presentment.list_collection_disputes(TENANT_ONE)
        self.assertEqual(len(page.collection_disputes), 1)
        self.assertEqual(
            page.collection_disputes[0].collection_dispute_id, held.collection_dispute_id
        )

    def test_contract_and_ledger_guards_cover_identity(self) -> None:
        """Accepted holds need identity; ledger rows stay append-only."""
        valid = {
            "collection_dispute_contract_version": 1,
            "collection_dispute_outcome_code": "accepted",
            "collection_dispute_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd70",
            "tenant_reference": TENANT_ONE,
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd21",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "currency_code": "USD",
            "remaining_outstanding_amount": "0.003705",
            "collection_dispute_status": "held",
            "collection_case_status": "disputed",
            "held_at": "2026-08-18T13:00:00Z",
            "source_payload_hash": "sha256:" + "d" * 64,
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_collection_dispute(valid), ())
        rejected = {
            "collection_dispute_contract_version": 1,
            "collection_dispute_outcome_code": "rejected",
            "rejection_reason_code": "collection_case_settled",
        }
        self.assertEqual(validate_collection_dispute(rejected), ())
        missing_id = json.loads(json.dumps(valid))
        del missing_id["collection_dispute_id"]
        self.assertTrue(validate_collection_dispute(missing_id))
        self.assertTrue(validate_collection_dispute(["not-an-object"]))
        ledger, collection = open_morning_case_with_outstanding()
        held = CollectionDisputeService(ledger).hold_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        stored = ledger.get_collection_dispute(held.collection_dispute_id)
        with self.assertRaises(ValueError):
            ledger.insert_collection_dispute(stored)
        colliding = replace(stored, collection_dispute_id=generate_record_id())
        with self.assertRaises(ValueError):
            ledger.insert_collection_dispute(colliding)
        with self.assertRaises(ValueError):
            ledger.insert_collection_dispute(
                replace(stored, collection_dispute_status="released")
            )
        with self.assertRaises(ValueError):
            ledger.insert_collection_dispute(
                replace(
                    stored,
                    collection_dispute_id=generate_record_id(),
                    collection_case_id=generate_record_id(),
                    remaining_outstanding_amount=Decimal("-1"),
                )
            )
        with self.assertRaises(ValueError):
            ledger.mark_collection_case_disputed(uuid4())
        settled_ledger, settled_case = open_morning_case_with_outstanding()
        settled_ledger.apply_collection_settlement(
            settled_case.collection_case_id, KNOWN_MORNING_TOTAL
        )
        with self.assertRaises(ValueError):
            settled_ledger.mark_collection_case_disputed(settled_case.collection_case_id)
        with self.assertRaises(ValueError):
            settled_ledger.apply_collection_settlement(
                settled_case.collection_case_id, Decimal("0.001")
            )
        disputed_ledger, disputed_case = open_morning_case_with_outstanding()
        CollectionDisputeService(disputed_ledger).hold_collection_case(
            TENANT_ONE, disputed_case.collection_case_id
        )
        replayed = disputed_ledger.mark_collection_case_disputed(disputed_case.collection_case_id)
        self.assertEqual(replayed.collection_case_status, "disputed")
        self.assertEqual(replayed.outstanding_amount, KNOWN_MORNING_TOTAL)
        with self.assertRaises(ValueError):
            disputed_ledger.apply_collection_settlement(
                disputed_case.collection_case_id, Decimal("0.001")
            )
        with self.assertRaises(ValueError):
            disputed_ledger.apply_collection_write_off(
                disputed_case.collection_case_id, KNOWN_MORNING_TOTAL
            )
        with self.assertRaises(ValueError):
            disputed_ledger.apply_unapplied_cash_to_collection_case(
                disputed_case.collection_case_id, Decimal("0.001")
            )
        with self.assertRaises(ValueError):
            disputed_ledger.mark_collection_case_settled(disputed_case.collection_case_id)
        with self.assertRaises(ValueError):
            disputed_ledger.mark_collection_case_voided(
                disputed_case.collection_case_id, KNOWN_MORNING_TOTAL
            )
        missing_remaining = json.loads(json.dumps(valid))
        del missing_remaining["remaining_outstanding_amount"]
        self.assertTrue(validate_collection_dispute(missing_remaining))
        unknown_outcome = {
            "collection_dispute_contract_version": 1,
            "collection_dispute_outcome_code": "posted",
        }
        self.assertTrue(validate_collection_dispute(unknown_outcome))
        missing_outcome = {"collection_dispute_contract_version": 1}
        self.assertTrue(validate_collection_dispute(missing_outcome))
        legal = json.loads(json.dumps(valid))
        legal["legal_invoice_number"] = "INV-1"
        self.assertTrue(validate_collection_dispute(legal))
        rejected_legal = {
            "collection_dispute_contract_version": 1,
            "collection_dispute_outcome_code": "rejected",
            "rejection_reason_code": "collection_case_settled",
            "legal_invoice_number": "INV-1",
        }
        self.assertTrue(validate_collection_dispute(rejected_legal))
        rejected_missing_reason = {
            "collection_dispute_contract_version": 1,
            "collection_dispute_outcome_code": "rejected",
        }
        self.assertTrue(validate_collection_dispute(rejected_missing_reason))
        bad_remaining = json.loads(json.dumps(valid))
        bad_remaining["remaining_outstanding_amount"] = "not-decimal"
        self.assertTrue(validate_collection_dispute(bad_remaining))
        int_remaining = json.loads(json.dumps(valid))
        int_remaining["remaining_outstanding_amount"] = 1
        self.assertTrue(validate_collection_dispute(int_remaining))
        negative = json.loads(json.dumps(valid))
        negative["remaining_outstanding_amount"] = "-1"
        self.assertTrue(validate_collection_dispute(negative))
        rejected_credit_legal = {
            "collection_dispute_contract_version": 1,
            "collection_dispute_outcome_code": "rejected",
            "rejection_reason_code": "collection_case_settled",
            "legal_credit_note_number": "CN-1",
        }
        self.assertTrue(validate_collection_dispute(rejected_credit_legal))

    def test_coverage_guards_for_hold_and_presentment_edges(self) -> None:
        """Closed branches stay explicit: replay, presentment, constructors, and HTTP."""
        ledger, collection = open_morning_case_with_outstanding()
        held = CollectionDisputeService(ledger).hold_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        presentment = CollectionDisputePresentmentService(ledger)
        presented = presentment.present_collection_dispute(
            TENANT_ONE, held.collection_dispute_id
        )
        self.assertEqual(validate_collection_dispute_presentment(presented.as_contract_dict()), ())
        with self.assertRaises(CollectionDisputePresentmentQueryError):
            presentment.list_collection_disputes(TENANT_ONE, page_limit=True)
        with self.assertRaises(CollectionDisputePresentmentQueryError):
            presentment.list_collection_disputes(TENANT_ONE, page_limit="abc")
        with self.assertRaises(CollectionDisputePresentmentQueryError):
            presentment.list_collection_disputes(TENANT_ONE, page_limit=101)
        with self.assertRaises(CollectionDisputePresentmentQueryError):
            presentment.list_collection_disputes(TENANT_ONE, page_limit=1.5)
        default_page = presentment.list_collection_disputes(TENANT_ONE, page_limit="")
        self.assertEqual(len(default_page.collection_disputes), 1)
        with self.assertRaises(CollectionDisputePresentmentQueryError) as missing_tenant:
            presentment.present_collection_dispute(
                "urn:cwl:missing", held.collection_dispute_id
            )
        self.assertEqual(missing_tenant.exception.rejection_reason_code, "tenant_not_found")
        mutated = ledger.get_collection_case(collection.collection_case_id)
        ledger.collection_cases[collection.collection_case_id] = replace(
            mutated, outstanding_amount=Decimal("1.00")
        )
        mutated_presentment = presentment.present_collection_dispute(
            TENANT_ONE, held.collection_dispute_id
        )
        self.assertEqual(mutated_presentment.remaining_outstanding_amount, Decimal("1.00"))
        mutated_replay = CollectionDisputeService(ledger).hold_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(mutated_replay.remaining_outstanding_amount, Decimal("1.00"))
        del ledger.collection_cases[collection.collection_case_id]
        missing_replay = CollectionDisputeService(ledger).hold_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(
            missing_replay.rejection_reason_code,
            CollectionDisputeRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )
        with self.assertRaises(CollectionDisputePresentmentQueryError) as missing_case:
            presentment.present_collection_dispute(TENANT_ONE, held.collection_dispute_id)
        self.assertEqual(
            missing_case.exception.rejection_reason_code, "collection_dispute_not_found"
        )
        with mock.patch.object(presentment.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                presentment.present_collection_dispute(
                    TENANT_ONE, held.collection_dispute_id
                )
        CollectionDisputeService()
        CollectionDisputePresentmentService()
        with self.assertRaises(ValueError):
            _format_held_at(None)
        unsupported = replace(
            _rejected(CollectionDisputeRejectionReasonCode.COLLECTION_CASE_NOT_FOUND),
            collection_dispute_outcome_code="posted",
        )
        with self.assertRaises(ValueError):
            unsupported.as_contract_dict()
        accepted_without_time = CollectionDisputeResult(
            collection_dispute_outcome_code=CollectionDisputeOutcomeCode.ACCEPTED,
            collection_dispute_contract_version=1,
            collection_dispute_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            issued_invoice_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            remaining_outstanding_amount=KNOWN_MORNING_TOTAL,
            collection_dispute_status="held",
            collection_case_status="disputed",
            held_at=None,
            source_payload_hash="sha256:" + "e" * 64,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaises(ValueError):
            accepted_without_time.as_contract_dict()
        none_reason = CollectionDisputeResult(
            collection_dispute_outcome_code=CollectionDisputeOutcomeCode.REJECTED,
            collection_dispute_contract_version=1,
            collection_dispute_id=None,
            collection_case_id=None,
            invoice_draft_id=None,
            issued_invoice_id=None,
            tenant_reference=None,
            currency_code=None,
            remaining_outstanding_amount=None,
            collection_dispute_status=None,
            collection_case_status=None,
            held_at=None,
            source_payload_hash=None,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        self.assertEqual(
            none_reason.as_contract_dict()["rejection_reason_code"],
            "collection_case_not_found",
        )
        issued_ledger, _draft_id = draft_known_morning()
        from metering_billing.issued_invoice import IssuedInvoiceService

        issued = IssuedInvoiceService(issued_ledger).issue_invoice(TENANT_ONE, _draft_id)
        issued_case = CollectionCaseService(issued_ledger).open_collection_case(
            TENANT_ONE, _draft_id
        )
        issued_hold = CollectionDisputeService(issued_ledger).hold_collection_case(
            TENANT_ONE, issued_case.collection_case_id
        )
        self.assertEqual(issued_hold.issued_invoice_id, issued.issued_invoice_id)
        self.assertIn("issued_invoice_id", issued_hold.as_contract_dict())
        issued_presentment = CollectionDisputePresentmentService(
            issued_ledger
        ).present_collection_dispute(TENANT_ONE, issued_hold.collection_dispute_id)
        self.assertIn("issued_invoice_id", issued_presentment.as_contract_dict())
        app = create_http_app(issued_ledger)
        mismatch_status, mismatch_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{issued_case.collection_case_id}/disputes",
            {"tenant_reference": TENANT_ONE, "currency_code": 1},
        )
        self.assertEqual(mismatch_status, 422)
        self.assertEqual(mismatch_body["rejection_reason_code"], "request_invalid")
        list_status, _list_body = invoke_http(
            app,
            "PUT",
            "/v1/collection-disputes",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(list_status, 405)
        item_status, _item_body = invoke_http(
            app,
            "PUT",
            f"/v1/collection-disputes/{issued_hold.collection_dispute_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(item_status, 405)
        presentment_payload = presented.as_summary_dict()
        self.assertEqual(presentment_payload["next_operator_action"], "wait")
        missing_remaining = {
            "collection_dispute_presentment_contract_version": 1,
            "collection_dispute_id": str(held.collection_dispute_id),
            "tenant_reference": TENANT_ONE,
            "collection_case_id": str(collection.collection_case_id),
            "invoice_draft_id": str(collection.invoice_draft_id),
            "currency_code": "USD",
            "collection_dispute_status": "held",
            "collection_case_status": "disputed",
            "held_at": "2026-08-18T13:00:00Z",
            "source_payload_hash": "sha256:" + "d" * 64,
            "next_operator_action": "wait",
        }
        self.assertTrue(validate_collection_dispute_presentment(missing_remaining))
        self.assertTrue(validate_collection_dispute_presentment(["not-an-object"]))
        bad_presentment_remaining = dict(presented.as_contract_dict())
        bad_presentment_remaining["remaining_outstanding_amount"] = "not-decimal"
        self.assertTrue(validate_collection_dispute_presentment(bad_presentment_remaining))
        int_presentment_remaining = dict(presented.as_contract_dict())
        int_presentment_remaining["remaining_outstanding_amount"] = 1
        self.assertTrue(validate_collection_dispute_presentment(int_presentment_remaining))
        wait_presentment = dict(presented.as_contract_dict())
        wait_presentment["next_operator_action"] = "collect"
        self.assertTrue(validate_collection_dispute_presentment(wait_presentment))
        legal_presentment = dict(presented.as_contract_dict())
        legal_presentment["legal_invoice_number"] = "INV-1"
        self.assertTrue(validate_collection_dispute_presentment(legal_presentment))
        already_disputed = CollectionDisputeService(issued_ledger).hold_collection_case(
            TENANT_ONE, issued_case.collection_case_id
        )
        self.assertEqual(
            already_disputed.collection_dispute_outcome_code,
            CollectionDisputeOutcomeCode.DUPLICATE_REPLAY,
        )
        orphan_ledger, orphan_case = open_morning_case_with_outstanding()
        orphan_ledger.collection_cases[orphan_case.collection_case_id] = replace(
            orphan_ledger.get_collection_case(orphan_case.collection_case_id),
            collection_case_status="disputed",
        )
        orphan = CollectionDisputeService(orphan_ledger).hold_collection_case(
            TENANT_ONE, orphan_case.collection_case_id
        )
        self.assertEqual(
            orphan.rejection_reason_code,
            CollectionDisputeRejectionReasonCode.COLLECTION_CASE_DISPUTED,
        )
        unknown_ledger, unknown_case = open_morning_case_with_outstanding()
        unknown_ledger.collection_cases[unknown_case.collection_case_id] = replace(
            unknown_ledger.get_collection_case(unknown_case.collection_case_id),
            collection_case_status="archived",
        )
        unknown = CollectionDisputeService(unknown_ledger).hold_collection_case(
            TENANT_ONE, unknown_case.collection_case_id
        )
        self.assertEqual(
            unknown.rejection_reason_code,
            CollectionDisputeRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )
        zero_ledger, zero_case = open_morning_case_with_outstanding()
        CollectionWriteOffService(zero_ledger).write_off_collection_case(
            TENANT_ONE, zero_case.collection_case_id
        )
        zero_hold = CollectionDisputeService(zero_ledger).hold_collection_case(
            TENANT_ONE, zero_case.collection_case_id
        )
        self.assertEqual(zero_hold.remaining_outstanding_amount, Decimal("0"))
        zero_presented = CollectionDisputePresentmentService(
            zero_ledger
        ).present_collection_dispute(TENANT_ONE, zero_hold.collection_dispute_id)
        self.assertEqual(zero_presented.remaining_outstanding_amount, Decimal("0"))
        with self.assertRaises(ValueError):
            zero_ledger.mark_collection_case_settled(zero_case.collection_case_id)
        negative_presentment = dict(presented.as_contract_dict())
        negative_presentment["remaining_outstanding_amount"] = "-1"
        self.assertTrue(validate_collection_dispute_presentment(negative_presentment))
        with mock.patch(
            "metering_billing.http_app.CollectionDisputePresentmentService.present_collection_dispute",
            side_effect=ValueError("boom"),
        ):
            get_boom_status, get_boom = invoke_http(
                app,
                "GET",
                f"/v1/collection-disputes/{issued_hold.collection_dispute_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(get_boom_status, 422)
        self.assertEqual(get_boom["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.CollectionDisputeService.hold_collection_case",
            side_effect=ValueError("boom"),
        ):
            post_boom_status, post_boom = invoke_http(
                app,
                "POST",
                f"/v1/collection-cases/{issued_case.collection_case_id}/disputes",
                {"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(post_boom_status, 422)
        self.assertEqual(post_boom["rejection_reason_code"], "request_invalid")
