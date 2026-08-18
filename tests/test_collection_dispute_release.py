"""Collection dispute release tests for reopening one held case."""

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
    CollectionDisputeReleasePresentmentService,
    CollectionDisputeReleaseService,
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
    validate_collection_dispute_release,
    validate_collection_dispute_release_presentment,
)
from metering_billing.errors import (
    CollectionCaseOutcomeCode,
    CollectionDisputeOutcomeCode,
    CollectionDisputeRejectionReasonCode,
    CollectionDisputeReleaseOutcomeCode,
    CollectionDisputeReleasePresentmentQueryError,
    CollectionDisputeReleaseRejectionReasonCode,
    CollectionWriteOffOutcomeCode,
    CreditNoteApplicationOutcomeCode,
    IssuedInvoiceVoidOutcomeCode,
    PaymentSettlementOutcomeCode,
    UnappliedCashApplicationOutcomeCode,
)
from metering_billing.collection_dispute_release import (
    CollectionDisputeReleaseResult,
    _enqueue_dispute_released,
    _format_released_at,
    _rejected,
)
from metering_billing.webhook_outbox import EVENT_TYPE_DISPUTE_RELEASED
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


RELEASED_MORNING = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
RELEASED_EVENING = datetime(2026, 8, 18, 21, 0, tzinfo=UTC)


class CollectionDisputeReleaseTests(unittest.TestCase):
    """Verify release-once identity, restored case status, and reopen paths."""

    def test_release_held_case_once_without_changing_remaining(self) -> None:
        """A held dispute releases once, restores open, and leaves outstanding."""
        ledger, collection = open_morning_case_with_outstanding()
        remaining_before = collection.outstanding_amount
        held = CollectionDisputeService(ledger).hold_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        prior_journals = len(ledger.journal_proposals)
        prior_outbox = len(ledger.webhook_outbox_events)
        first = CollectionDisputeReleaseService(
            ledger, clock=lambda: RELEASED_MORNING
        ).release_collection_dispute(TENANT_ONE, held.collection_dispute_id)
        second = CollectionDisputeReleaseService(
            ledger, clock=lambda: RELEASED_EVENING
        ).release_collection_dispute(TENANT_ONE, held.collection_dispute_id)
        self.assertEqual(
            first.collection_dispute_release_outcome_code,
            CollectionDisputeReleaseOutcomeCode.ACCEPTED,
        )
        self.assertEqual(
            second.collection_dispute_release_outcome_code,
            CollectionDisputeReleaseOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertEqual(first.collection_dispute_id, held.collection_dispute_id)
        self.assertEqual(second.collection_dispute_id, first.collection_dispute_id)
        self.assertEqual(first.collection_case_id, collection.collection_case_id)
        self.assertEqual(first.remaining_outstanding_amount, remaining_before)
        self.assertEqual(first.collection_dispute_status, "released")
        self.assertEqual(first.collection_case_status, "open")
        self.assertEqual(first.released_at, RELEASED_MORNING)
        self.assertEqual(second.released_at, RELEASED_MORNING)
        self.assertEqual(first.next_operator_action, "wait")
        payload = first.as_contract_dict()
        self.assertEqual(validate_collection_dispute_release(payload), ())
        self.assertIsInstance(payload["remaining_outstanding_amount"], str)
        self.assertNotIn("card_pan", payload)
        self.assertNotIn("legal_invoice_number", payload)
        stored_case = ledger.get_collection_case(collection.collection_case_id)
        self.assertEqual(stored_case.outstanding_amount, remaining_before)
        self.assertEqual(stored_case.collection_case_status, "open")
        stored_dispute = ledger.get_collection_dispute(held.collection_dispute_id)
        self.assertEqual(stored_dispute.collection_dispute_status, "released")
        self.assertEqual(stored_dispute.remaining_outstanding_amount, remaining_before)
        self.assertEqual(len(ledger.collection_disputes), 1)
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        released_events = [
            event
            for event in ledger.webhook_outbox_events.values()
            if event.event_type_code == EVENT_TYPE_DISPUTE_RELEASED
        ]
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox + 1)
        self.assertEqual(len(released_events), 1)
        self.assertEqual(released_events[0].source_id, first.collection_dispute_id)
        presented = CollectionCasePresentmentService(ledger).present_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(presented.collection_case_status, "open")
        self.assertEqual(presented.collection_outstanding, remaining_before)
        self.assertEqual(presented.next_operator_action, "collect")
        self.assertEqual(presented.next_dunning_notice_code, "first_notice")
        rehold = CollectionDisputeService(ledger).hold_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(
            rehold.rejection_reason_code,
            CollectionDisputeRejectionReasonCode.COLLECTION_DISPUTE_RELEASED,
        )

    def test_release_dunning_case_restores_dunning_and_allows_new_notice(self) -> None:
        """A pre-hold dunning case returns to dunning; new notices are accepted."""
        ledger, collection = open_morning_case_with_outstanding()
        CollectionCaseService(ledger).record_dunning_event(
            TENANT_ONE, collection.collection_case_id, "first_notice"
        )
        remaining_before = collection.outstanding_amount
        held = CollectionDisputeService(ledger).hold_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        released = CollectionDisputeReleaseService(ledger).release_collection_dispute(
            TENANT_ONE, held.collection_dispute_id
        )
        self.assertEqual(released.collection_case_status, "dunning")
        stored_case = ledger.get_collection_case(collection.collection_case_id)
        self.assertEqual(stored_case.collection_case_status, "dunning")
        self.assertEqual(stored_case.outstanding_amount, remaining_before)
        replayed = CollectionCaseService(ledger).record_dunning_event(
            TENANT_ONE, collection.collection_case_id, "first_notice"
        )
        self.assertEqual(
            replayed.collection_case_outcome_code, CollectionCaseOutcomeCode.DUPLICATE_REPLAY
        )
        self.assertEqual(replayed.collection_case_status, "dunning")
        overdue = CollectionCaseService(ledger).record_dunning_event(
            TENANT_ONE, collection.collection_case_id, "overdue_notice"
        )
        self.assertEqual(overdue.collection_case_outcome_code, CollectionCaseOutcomeCode.ACCEPTED)
        self.assertEqual(overdue.collection_case_status, "dunning")

    def test_money_and_close_commands_work_again_after_release(self) -> None:
        """Write-off, settle, void, receipt, credit, and leftover apply after release."""
        write_off_ledger, write_off_case = open_morning_case_with_outstanding()
        held_write_off = CollectionDisputeService(write_off_ledger).hold_collection_case(
            TENANT_ONE, write_off_case.collection_case_id
        )
        CollectionDisputeReleaseService(write_off_ledger).release_collection_dispute(
            TENANT_ONE, held_write_off.collection_dispute_id
        )
        written = CollectionWriteOffService(write_off_ledger).write_off_collection_case(
            TENANT_ONE, write_off_case.collection_case_id
        )
        self.assertEqual(
            written.collection_write_off_outcome_code, CollectionWriteOffOutcomeCode.ACCEPTED
        )
        self.assertEqual(written.remaining_outstanding_amount, Decimal("0"))

        settle_ledger, settle_case = open_morning_case_with_outstanding()
        CollectionWriteOffService(settle_ledger).write_off_collection_case(
            TENANT_ONE, settle_case.collection_case_id
        )
        held_settle = CollectionDisputeService(settle_ledger).hold_collection_case(
            TENANT_ONE, settle_case.collection_case_id
        )
        CollectionDisputeReleaseService(settle_ledger).release_collection_dispute(
            TENANT_ONE, held_settle.collection_dispute_id
        )
        settled = CollectionCaseSettlementService(settle_ledger).settle_collection_case(
            TENANT_ONE, settle_case.collection_case_id
        )
        self.assertEqual(settled.collection_case_settlement_outcome_code.value, "accepted")
        self.assertEqual(
            settle_ledger.get_collection_case(settle_case.collection_case_id).collection_case_status,
            "settled",
        )

        void_ledger, issued, void_case = issue_known_morning_invoice()
        held_void = CollectionDisputeService(void_ledger).hold_collection_case(
            TENANT_ONE, void_case.collection_case_id
        )
        CollectionDisputeReleaseService(void_ledger).release_collection_dispute(
            TENANT_ONE, held_void.collection_dispute_id
        )
        voided = IssuedInvoiceVoidService(void_ledger).void_issued_invoice(
            TENANT_ONE, issued.issued_invoice_id
        )
        self.assertEqual(voided.issued_invoice_void_outcome_code, IssuedInvoiceVoidOutcomeCode.ACCEPTED)

        receipt_ledger, receipt_case = open_morning_case_with_outstanding()
        intent = PaymentIntentService(receipt_ledger).project_payment_intent(
            TENANT_ONE, receipt_case.collection_case_id
        )
        held_receipt = CollectionDisputeService(receipt_ledger).hold_collection_case(
            TENANT_ONE, receipt_case.collection_case_id
        )
        CollectionDisputeReleaseService(receipt_ledger).release_collection_dispute(
            TENANT_ONE, held_receipt.collection_dispute_id
        )
        receipt = PaymentSettlementService(receipt_ledger).record_payment_receipt(
            TENANT_ONE, intent.payment_intent_id, KNOWN_MORNING_TOTAL
        )
        self.assertEqual(receipt.payment_settlement_outcome_code, PaymentSettlementOutcomeCode.ACCEPTED)

        credit_ledger, issued_credit, credit_case = issue_morning_credit_then_open_case()
        held_credit = CollectionDisputeService(credit_ledger).hold_collection_case(
            TENANT_ONE, credit_case.collection_case_id
        )
        CollectionDisputeReleaseService(credit_ledger).release_collection_dispute(
            TENANT_ONE, held_credit.collection_dispute_id
        )
        applied = CreditNoteApplicationService(credit_ledger).apply_credit_note(
            TENANT_ONE, issued_credit.issued_credit_note_id, credit_case.collection_case_id
        )
        self.assertEqual(
            applied.credit_note_application_outcome_code, CreditNoteApplicationOutcomeCode.ACCEPTED
        )

        leftover_ledger, parked, leftover_case, _source, _receipt = (
            park_leftover_and_open_second_case()
        )
        held_leftover = CollectionDisputeService(leftover_ledger).hold_collection_case(
            TENANT_ONE, leftover_case.collection_case_id
        )
        CollectionDisputeReleaseService(leftover_ledger).release_collection_dispute(
            TENANT_ONE, held_leftover.collection_dispute_id
        )
        leftover = UnappliedCashApplicationService(leftover_ledger).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, leftover_case.collection_case_id
        )
        self.assertEqual(
            leftover.unapplied_cash_application_outcome_code,
            UnappliedCashApplicationOutcomeCode.ACCEPTED,
        )

    def test_fail_closed_on_missing_not_held_settled_voided_and_currency(self) -> None:
        """Missing tenant or dispute, not-held rows, settled or voided cases refuse."""
        ledger, collection = open_morning_case_with_outstanding()
        held = CollectionDisputeService(ledger).hold_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        missing_tenant = CollectionDisputeReleaseService(ledger).release_collection_dispute(
            "urn:cwl:missing", held.collection_dispute_id
        )
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            CollectionDisputeReleaseRejectionReasonCode.TENANT_NOT_FOUND,
        )
        missing = CollectionDisputeReleaseService(ledger).release_collection_dispute(
            TENANT_ONE, uuid4()
        )
        self.assertEqual(
            missing.rejection_reason_code,
            CollectionDisputeReleaseRejectionReasonCode.COLLECTION_DISPUTE_NOT_FOUND,
        )
        crossed = CollectionDisputeReleaseService(ledger).release_collection_dispute(
            TENANT_TWO, held.collection_dispute_id
        )
        self.assertEqual(
            crossed.rejection_reason_code,
            CollectionDisputeReleaseRejectionReasonCode.COLLECTION_DISPUTE_NOT_FOUND,
        )
        currency = CollectionDisputeReleaseService(ledger).release_collection_dispute(
            TENANT_ONE, held.collection_dispute_id, currency_code="EUR"
        )
        self.assertEqual(
            currency.rejection_reason_code,
            CollectionDisputeReleaseRejectionReasonCode.CURRENCY_MISMATCH,
        )
        not_held_ledger, not_held_case = open_morning_case_with_outstanding()
        not_held = CollectionDisputeService(not_held_ledger).hold_collection_case(
            TENANT_ONE, not_held_case.collection_case_id
        )
        stored = not_held_ledger.get_collection_dispute(not_held.collection_dispute_id)
        not_held_ledger.collection_disputes[not_held.collection_dispute_id] = replace(
            stored, collection_dispute_status="archived"
        )
        refused_status = CollectionDisputeReleaseService(
            not_held_ledger
        ).release_collection_dispute(TENANT_ONE, not_held.collection_dispute_id)
        self.assertEqual(
            refused_status.rejection_reason_code,
            CollectionDisputeReleaseRejectionReasonCode.COLLECTION_DISPUTE_NOT_HELD,
        )
        settled_ledger, settled_case = open_morning_case_with_outstanding()
        held_settled = CollectionDisputeService(settled_ledger).hold_collection_case(
            TENANT_ONE, settled_case.collection_case_id
        )
        settled_ledger.collection_cases[settled_case.collection_case_id] = replace(
            settled_ledger.get_collection_case(settled_case.collection_case_id),
            collection_case_status="settled",
            outstanding_amount=Decimal("0"),
        )
        settled = CollectionDisputeReleaseService(settled_ledger).release_collection_dispute(
            TENANT_ONE, held_settled.collection_dispute_id
        )
        self.assertEqual(
            settled.rejection_reason_code,
            CollectionDisputeReleaseRejectionReasonCode.COLLECTION_CASE_SETTLED,
        )
        void_ledger, _issued, void_case = issue_known_morning_invoice()
        held_void = CollectionDisputeService(void_ledger).hold_collection_case(
            TENANT_ONE, void_case.collection_case_id
        )
        void_ledger.collection_cases[void_case.collection_case_id] = replace(
            void_ledger.get_collection_case(void_case.collection_case_id),
            collection_case_status="voided",
            outstanding_amount=Decimal("0"),
        )
        voided = CollectionDisputeReleaseService(void_ledger).release_collection_dispute(
            TENANT_ONE, held_void.collection_dispute_id
        )
        self.assertEqual(
            voided.rejection_reason_code,
            CollectionDisputeReleaseRejectionReasonCode.COLLECTION_CASE_VOIDED,
        )
        missing_case_ledger, missing_case = open_morning_case_with_outstanding()
        held_missing_case = CollectionDisputeService(missing_case_ledger).hold_collection_case(
            TENANT_ONE, missing_case.collection_case_id
        )
        del missing_case_ledger.collection_cases[missing_case.collection_case_id]
        missing_case_release = CollectionDisputeReleaseService(
            missing_case_ledger
        ).release_collection_dispute(TENANT_ONE, held_missing_case.collection_dispute_id)
        self.assertEqual(
            missing_case_release.rejection_reason_code,
            CollectionDisputeReleaseRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )

    def test_resolver_hollow_success_raises_value_error(self) -> None:
        """A broken tenant resolve must raise ValueError, not assert."""
        ledger, collection = open_morning_case_with_outstanding()
        held = CollectionDisputeService(ledger).hold_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        service = CollectionDisputeReleaseService(ledger)
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.release_collection_dispute(TENANT_ONE, held.collection_dispute_id)

    def test_http_release_get_and_paged_list_without_capture(self) -> None:
        """POST releases; GET item and list page metadata and never capture payment."""
        ledger, first_case = open_morning_case_with_outstanding()
        second_draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("20.00"))
        second_case = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, second_draft_id
        )
        first_hold = CollectionDisputeService(ledger).hold_collection_case(
            TENANT_ONE, first_case.collection_case_id
        )
        second_hold = CollectionDisputeService(ledger).hold_collection_case(
            TENANT_ONE, second_case.collection_case_id
        )
        prior_outbox = len(ledger.webhook_outbox_events)
        prior_journals = len(ledger.journal_proposals)
        app = create_http_app(ledger)
        refused_status, refused_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-disputes/{first_hold.collection_dispute_id}/releases",
            {"tenant_reference": TENANT_ONE, "card_pan": "4111111111111111"},
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "request_invalid")
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-disputes/{first_hold.collection_dispute_id}/releases",
            {"tenant_reference": TENANT_ONE, "currency_code": "USD"},
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["collection_dispute_release_outcome_code"], "accepted")
        self.assertEqual(accepted_body["collection_dispute_status"], "released")
        self.assertEqual(accepted_body["collection_case_status"], "open")
        self.assertEqual(
            accepted_body["remaining_outstanding_amount"],
            format_exact_decimal(KNOWN_MORNING_TOTAL),
        )
        dispute_id = accepted_body["collection_dispute_id"]
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-disputes/{first_hold.collection_dispute_id}/releases",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["collection_dispute_release_outcome_code"], "duplicate_replay")
        self.assertEqual(replay_body["collection_dispute_id"], dispute_id)
        held_only_status, held_only_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-dispute-releases/{second_hold.collection_dispute_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(held_only_status, 404)
        self.assertEqual(held_only_body["rejection_reason_code"], "collection_dispute_release_not_found")
        second_status, second_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-disputes/{second_hold.collection_dispute_id}/releases",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(second_body["collection_dispute_release_outcome_code"], "accepted")
        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-dispute-releases/{dispute_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["collection_dispute_id"], dispute_id)
        self.assertEqual(get_body["collection_dispute_status"], "released")
        self.assertNotIn("collection_dispute_release_outcome_code", get_body)
        self.assertEqual(validate_collection_dispute_release_presentment(get_body), ())
        header_status, header_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-dispute-releases/{dispute_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(header_status, 200)
        self.assertEqual(header_body, get_body)
        hold_get_status, hold_get_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-disputes/{dispute_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(hold_get_status, 200)
        self.assertEqual(hold_get_body["collection_dispute_status"], "released")
        self.assertEqual(hold_get_body["collection_case_status"], "open")
        case_status, case_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-cases/{first_case.collection_case_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(case_status, 200)
        self.assertEqual(case_body["collection_case_status"], "open")
        self.assertEqual(case_body["next_operator_action"], "collect")
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/collection-dispute-releases",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"collection_dispute_releases", "next_cursor"})
        self.assertEqual(len(list_body["collection_dispute_releases"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        page_two_status, page_two = invoke_http(
            app,
            "GET",
            "/v1/collection-dispute-releases",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(page_two_status, 200)
        self.assertEqual(len(page_two["collection_dispute_releases"]), 1)
        self.assertIsNone(page_two["next_cursor"])
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/collection-dispute-releases",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["collection_dispute_releases"], [])
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-dispute-releases/{dispute_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "collection_dispute_release_not_found")
        missing_status, missing_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-dispute-releases/{dispute_id}",
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        method_status, _method_body = invoke_http(
            app,
            "PUT",
            f"/v1/collection-disputes/{first_hold.collection_dispute_id}/releases",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 405)
        released_events = [
            event
            for event in ledger.webhook_outbox_events.values()
            if event.event_type_code == EVENT_TYPE_DISPUTE_RELEASED
        ]
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox + 2)
        self.assertEqual(len(released_events), 2)
        self.assertEqual(len(ledger.journal_proposals), prior_journals)

    def test_presentment_rejects_invalid_page_and_missing_rows(self) -> None:
        """Presentment fails closed on unreadable cursors and unknown ids."""
        ledger, collection = open_morning_case_with_outstanding()
        held = CollectionDisputeService(ledger).hold_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        released = CollectionDisputeReleaseService(ledger).release_collection_dispute(
            TENANT_ONE, held.collection_dispute_id
        )
        presentment = CollectionDisputeReleasePresentmentService(ledger)
        with self.assertRaises(CollectionDisputeReleasePresentmentQueryError) as missing:
            presentment.present_collection_dispute_release(TENANT_ONE, uuid4())
        self.assertEqual(
            missing.exception.rejection_reason_code, "collection_dispute_release_not_found"
        )
        held_only_ledger, held_only_case = open_morning_case_with_outstanding()
        held_only = CollectionDisputeService(held_only_ledger).hold_collection_case(
            TENANT_ONE, held_only_case.collection_case_id
        )
        with self.assertRaises(CollectionDisputeReleasePresentmentQueryError) as still_held:
            CollectionDisputeReleasePresentmentService(
                held_only_ledger
            ).present_collection_dispute_release(TENANT_ONE, held_only.collection_dispute_id)
        self.assertEqual(
            still_held.exception.rejection_reason_code, "collection_dispute_release_not_found"
        )
        with self.assertRaises(CollectionDisputeReleasePresentmentQueryError) as bad_cursor:
            presentment.list_collection_dispute_releases(TENANT_ONE, cursor="not-a-cursor")
        self.assertEqual(bad_cursor.exception.rejection_reason_code, "request_invalid")
        with self.assertRaises(CollectionDisputeReleasePresentmentQueryError) as bad_limit:
            presentment.list_collection_dispute_releases(TENANT_ONE, page_limit=0)
        self.assertEqual(bad_limit.exception.rejection_reason_code, "request_invalid")
        page = presentment.list_collection_dispute_releases(TENANT_ONE)
        self.assertEqual(len(page.collection_dispute_releases), 1)
        self.assertEqual(
            page.collection_dispute_releases[0].collection_dispute_id,
            released.collection_dispute_id,
        )

    def test_contract_and_ledger_guards_cover_identity(self) -> None:
        """Accepted releases need identity; ledger rows stay append-only."""
        valid = {
            "collection_dispute_release_contract_version": 1,
            "collection_dispute_release_outcome_code": "accepted",
            "collection_dispute_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd70",
            "tenant_reference": TENANT_ONE,
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd21",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "currency_code": "USD",
            "remaining_outstanding_amount": "0.003705",
            "collection_dispute_status": "released",
            "collection_case_status": "open",
            "released_at": "2026-08-18T15:00:00Z",
            "source_payload_hash": "sha256:" + "d" * 64,
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_collection_dispute_release(valid), ())
        rejected = {
            "collection_dispute_release_contract_version": 1,
            "collection_dispute_release_outcome_code": "rejected",
            "rejection_reason_code": "collection_dispute_not_held",
        }
        self.assertEqual(validate_collection_dispute_release(rejected), ())
        missing_id = json.loads(json.dumps(valid))
        del missing_id["collection_dispute_id"]
        self.assertTrue(validate_collection_dispute_release(missing_id))
        self.assertTrue(validate_collection_dispute_release(["not-an-object"]))
        ledger, collection = open_morning_case_with_outstanding()
        held = CollectionDisputeService(ledger).hold_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        CollectionDisputeReleaseService(ledger).release_collection_dispute(
            TENANT_ONE, held.collection_dispute_id
        )
        stored = ledger.get_collection_dispute(held.collection_dispute_id)
        with self.assertRaises(ValueError):
            ledger.mark_collection_dispute_released(uuid4(), RELEASED_MORNING)
        replayed = ledger.mark_collection_dispute_released(
            held.collection_dispute_id, RELEASED_EVENING
        )
        self.assertEqual(replayed.collection_dispute_status, "released")
        self.assertEqual(replayed.released_at, stored.released_at)
        with self.assertRaises(ValueError):
            ledger.mark_collection_case_released_from_dispute(uuid4())
        settled_ledger, settled_case = open_morning_case_with_outstanding()
        settled_ledger.apply_collection_settlement(
            settled_case.collection_case_id, KNOWN_MORNING_TOTAL
        )
        with self.assertRaises(ValueError):
            settled_ledger.mark_collection_case_released_from_dispute(
                settled_case.collection_case_id
            )
        void_ledger, issued, void_case = issue_known_morning_invoice()
        IssuedInvoiceVoidService(void_ledger).void_issued_invoice(
            TENANT_ONE, issued.issued_invoice_id
        )
        with self.assertRaises(ValueError):
            void_ledger.mark_collection_case_released_from_dispute(void_case.collection_case_id)
        missing_remaining = json.loads(json.dumps(valid))
        del missing_remaining["remaining_outstanding_amount"]
        self.assertTrue(validate_collection_dispute_release(missing_remaining))
        unknown_outcome = {
            "collection_dispute_release_contract_version": 1,
            "collection_dispute_release_outcome_code": "posted",
        }
        self.assertTrue(validate_collection_dispute_release(unknown_outcome))
        missing_outcome = {"collection_dispute_release_contract_version": 1}
        self.assertTrue(validate_collection_dispute_release(missing_outcome))
        legal = json.loads(json.dumps(valid))
        legal["legal_invoice_number"] = "INV-1"
        self.assertTrue(validate_collection_dispute_release(legal))
        rejected_legal = {
            "collection_dispute_release_contract_version": 1,
            "collection_dispute_release_outcome_code": "rejected",
            "rejection_reason_code": "collection_dispute_not_held",
            "legal_invoice_number": "INV-1",
        }
        self.assertTrue(validate_collection_dispute_release(rejected_legal))
        rejected_missing_reason = {
            "collection_dispute_release_contract_version": 1,
            "collection_dispute_release_outcome_code": "rejected",
        }
        self.assertTrue(validate_collection_dispute_release(rejected_missing_reason))
        bad_remaining = json.loads(json.dumps(valid))
        bad_remaining["remaining_outstanding_amount"] = "not-decimal"
        self.assertTrue(validate_collection_dispute_release(bad_remaining))
        int_remaining = json.loads(json.dumps(valid))
        int_remaining["remaining_outstanding_amount"] = 1
        self.assertTrue(validate_collection_dispute_release(int_remaining))
        negative = json.loads(json.dumps(valid))
        negative["remaining_outstanding_amount"] = "-1"
        self.assertTrue(validate_collection_dispute_release(negative))
        rejected_credit_legal = {
            "collection_dispute_release_contract_version": 1,
            "collection_dispute_release_outcome_code": "rejected",
            "rejection_reason_code": "collection_dispute_not_held",
            "legal_credit_note_number": "CN-1",
        }
        self.assertTrue(validate_collection_dispute_release(rejected_credit_legal))

    def test_coverage_guards_for_release_and_presentment_edges(self) -> None:
        """Closed branches stay explicit: replay, presentment, constructors, and HTTP."""
        ledger, collection = open_morning_case_with_outstanding()
        held = CollectionDisputeService(ledger).hold_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        released = CollectionDisputeReleaseService(ledger).release_collection_dispute(
            TENANT_ONE, held.collection_dispute_id
        )
        presentment = CollectionDisputeReleasePresentmentService(ledger)
        presented = presentment.present_collection_dispute_release(
            TENANT_ONE, released.collection_dispute_id
        )
        self.assertEqual(
            validate_collection_dispute_release_presentment(presented.as_contract_dict()),
            (),
        )
        with self.assertRaises(CollectionDisputeReleasePresentmentQueryError):
            presentment.list_collection_dispute_releases(TENANT_ONE, page_limit=True)
        with self.assertRaises(CollectionDisputeReleasePresentmentQueryError):
            presentment.list_collection_dispute_releases(TENANT_ONE, page_limit="abc")
        with self.assertRaises(CollectionDisputeReleasePresentmentQueryError):
            presentment.list_collection_dispute_releases(TENANT_ONE, page_limit=101)
        with self.assertRaises(CollectionDisputeReleasePresentmentQueryError):
            presentment.list_collection_dispute_releases(TENANT_ONE, page_limit=1.5)
        default_page = presentment.list_collection_dispute_releases(TENANT_ONE, page_limit="")
        self.assertEqual(len(default_page.collection_dispute_releases), 1)
        with self.assertRaises(CollectionDisputeReleasePresentmentQueryError) as missing_tenant:
            presentment.present_collection_dispute_release(
                "urn:cwl:missing", released.collection_dispute_id
            )
        self.assertEqual(missing_tenant.exception.rejection_reason_code, "tenant_not_found")
        hold_presentment = CollectionDisputePresentmentService(ledger).present_collection_dispute(
            TENANT_ONE, held.collection_dispute_id
        )
        self.assertEqual(hold_presentment.collection_dispute_status, "released")
        self.assertEqual(hold_presentment.collection_case_status, "open")
        mutated = ledger.get_collection_case(collection.collection_case_id)
        ledger.collection_cases[collection.collection_case_id] = replace(
            mutated, outstanding_amount=Decimal("1.00")
        )
        mutated_presentment = presentment.present_collection_dispute_release(
            TENANT_ONE, released.collection_dispute_id
        )
        self.assertEqual(mutated_presentment.remaining_outstanding_amount, Decimal("1.00"))
        mutated_replay = CollectionDisputeReleaseService(ledger).release_collection_dispute(
            TENANT_ONE, held.collection_dispute_id
        )
        self.assertEqual(mutated_replay.remaining_outstanding_amount, Decimal("1.00"))
        self.assertEqual(
            len(
                [
                    event
                    for event in ledger.webhook_outbox_events.values()
                    if event.event_type_code == EVENT_TYPE_DISPUTE_RELEASED
                ]
            ),
            1,
        )
        del ledger.collection_cases[collection.collection_case_id]
        missing_replay = CollectionDisputeReleaseService(ledger).release_collection_dispute(
            TENANT_ONE, held.collection_dispute_id
        )
        self.assertEqual(
            missing_replay.rejection_reason_code,
            CollectionDisputeReleaseRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )
        with self.assertRaises(CollectionDisputeReleasePresentmentQueryError) as missing_case:
            presentment.present_collection_dispute_release(
                TENANT_ONE, released.collection_dispute_id
            )
        self.assertEqual(
            missing_case.exception.rejection_reason_code,
            "collection_dispute_release_not_found",
        )
        with mock.patch.object(presentment.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                presentment.present_collection_dispute_release(
                    TENANT_ONE, released.collection_dispute_id
                )
        CollectionDisputeReleaseService()
        CollectionDisputeReleasePresentmentService()
        with self.assertRaises(ValueError):
            _format_released_at(None)
        unsupported = replace(
            _rejected(CollectionDisputeReleaseRejectionReasonCode.COLLECTION_DISPUTE_NOT_FOUND),
            collection_dispute_release_outcome_code="posted",
        )
        with self.assertRaises(ValueError):
            unsupported.as_contract_dict()
        accepted_without_time = CollectionDisputeReleaseResult(
            collection_dispute_release_outcome_code=CollectionDisputeReleaseOutcomeCode.ACCEPTED,
            collection_dispute_release_contract_version=1,
            collection_dispute_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            issued_invoice_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            remaining_outstanding_amount=KNOWN_MORNING_TOTAL,
            collection_dispute_status="released",
            collection_case_status="open",
            released_at=None,
            source_payload_hash="sha256:" + "e" * 64,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaises(ValueError):
            accepted_without_time.as_contract_dict()
        webhook_data = released.as_webhook_event_data()
        self.assertEqual(
            webhook_data["collection_dispute_id"], str(released.collection_dispute_id)
        )
        self.assertEqual(webhook_data["collection_case_id"], str(collection.collection_case_id))
        self.assertEqual(webhook_data["invoice_draft_id"], str(collection.invoice_draft_id))
        self.assertEqual(webhook_data["source_payload_hash"], released.source_payload_hash)
        self.assertEqual(webhook_data["collection_dispute_release_contract_version"], 1)
        self.assertEqual(webhook_data["currency_code"], "USD")
        self.assertEqual(
            webhook_data["remaining_outstanding_amount"],
            format_exact_decimal(KNOWN_MORNING_TOTAL),
        )
        self.assertEqual(webhook_data["collection_dispute_status"], "released")
        self.assertEqual(webhook_data["released_at"], released.as_contract_dict()["released_at"])
        self.assertNotIn("issued_invoice_id", webhook_data)
        self.assertNotIn("held_at", webhook_data)
        self.assertNotIn("collection_case_status", webhook_data)
        self.assertNotIn("next_operator_action", webhook_data)
        self.assertNotIn("tenant_reference", webhook_data)
        self.assertNotIn("legal_invoice_number", webhook_data)
        rejected = _rejected(
            CollectionDisputeReleaseRejectionReasonCode.COLLECTION_DISPUTE_NOT_FOUND
        )
        with self.assertRaisesRegex(
            ValueError, "rejected collection dispute release has no webhook event data"
        ):
            rejected.as_webhook_event_data()
        missing_case = CollectionDisputeReleaseResult(
            collection_dispute_release_outcome_code=CollectionDisputeReleaseOutcomeCode.ACCEPTED,
            collection_dispute_release_contract_version=1,
            collection_dispute_id=generate_record_id(),
            collection_case_id=None,
            invoice_draft_id=generate_record_id(),
            issued_invoice_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            remaining_outstanding_amount=KNOWN_MORNING_TOTAL,
            collection_dispute_status="released",
            collection_case_status="open",
            released_at=RELEASED_MORNING,
            source_payload_hash="sha256:" + ("11" * 32),
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaisesRegex(
            ValueError, "rejected collection dispute release has no webhook event data"
        ):
            missing_case.as_webhook_event_data()
        missing_draft = CollectionDisputeReleaseResult(
            collection_dispute_release_outcome_code=CollectionDisputeReleaseOutcomeCode.ACCEPTED,
            collection_dispute_release_contract_version=1,
            collection_dispute_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            invoice_draft_id=None,
            issued_invoice_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            remaining_outstanding_amount=KNOWN_MORNING_TOTAL,
            collection_dispute_status="released",
            collection_case_status="open",
            released_at=RELEASED_MORNING,
            source_payload_hash="sha256:" + ("12" * 32),
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaisesRegex(
            ValueError, "rejected collection dispute release has no webhook event data"
        ):
            missing_draft.as_webhook_event_data()
        with self.assertRaisesRegex(
            ValueError, "accepted collection dispute releases must include released_at"
        ):
            accepted_without_time.as_webhook_event_data()
        still_held_result = CollectionDisputeReleaseResult(
            collection_dispute_release_outcome_code=CollectionDisputeReleaseOutcomeCode.ACCEPTED,
            collection_dispute_release_contract_version=1,
            collection_dispute_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            issued_invoice_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            remaining_outstanding_amount=KNOWN_MORNING_TOTAL,
            collection_dispute_status="held",
            collection_case_status="disputed",
            released_at=RELEASED_MORNING,
            source_payload_hash="sha256:" + ("13" * 32),
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaisesRegex(
            ValueError, "collection dispute is not released"
        ):
            still_held_result.as_webhook_event_data()
        incomplete = CollectionDisputeReleaseResult(
            collection_dispute_release_outcome_code=CollectionDisputeReleaseOutcomeCode.ACCEPTED,
            collection_dispute_release_contract_version=1,
            collection_dispute_id=None,
            collection_case_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            issued_invoice_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            remaining_outstanding_amount=None,
            collection_dispute_status="released",
            collection_case_status="open",
            released_at=None,
            source_payload_hash="sha256:" + ("22" * 32),
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaisesRegex(
            ValueError, "accepted collection dispute releases must include identity"
        ):
            _enqueue_dispute_released(ledger, TENANT_ONE, incomplete)
        missing_time = CollectionDisputeReleaseResult(
            collection_dispute_release_outcome_code=CollectionDisputeReleaseOutcomeCode.ACCEPTED,
            collection_dispute_release_contract_version=1,
            collection_dispute_id=generate_record_id(),
            collection_case_id=collection.collection_case_id,
            invoice_draft_id=collection.invoice_draft_id,
            issued_invoice_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            remaining_outstanding_amount=KNOWN_MORNING_TOTAL,
            collection_dispute_status="released",
            collection_case_status="open",
            released_at=None,
            source_payload_hash="sha256:" + ("33" * 32),
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaisesRegex(
            ValueError, "accepted collection dispute releases must include identity"
        ):
            _enqueue_dispute_released(ledger, TENANT_ONE, missing_time)
        missing_remaining = CollectionDisputeReleaseResult(
            collection_dispute_release_outcome_code=CollectionDisputeReleaseOutcomeCode.ACCEPTED,
            collection_dispute_release_contract_version=1,
            collection_dispute_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            issued_invoice_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            remaining_outstanding_amount=None,
            collection_dispute_status="released",
            collection_case_status="open",
            released_at=RELEASED_MORNING,
            source_payload_hash="sha256:" + ("44" * 32),
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaisesRegex(
            ValueError, "accepted collection dispute releases must include remaining outstanding"
        ):
            missing_remaining.as_webhook_event_data()
        orphaned = CollectionDisputeReleaseResult(
            collection_dispute_release_outcome_code=CollectionDisputeReleaseOutcomeCode.ACCEPTED,
            collection_dispute_release_contract_version=1,
            collection_dispute_id=generate_record_id(),
            collection_case_id=collection.collection_case_id,
            invoice_draft_id=collection.invoice_draft_id,
            issued_invoice_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            remaining_outstanding_amount=KNOWN_MORNING_TOTAL,
            collection_dispute_status="released",
            collection_case_status="open",
            released_at=RELEASED_MORNING,
            source_payload_hash="sha256:" + ("55" * 32),
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaisesRegex(
            ValueError, "accepted collection dispute releases must include identity"
        ):
            _enqueue_dispute_released(ledger, TENANT_ONE, orphaned)
        none_reason = CollectionDisputeReleaseResult(
            collection_dispute_release_outcome_code=CollectionDisputeReleaseOutcomeCode.REJECTED,
            collection_dispute_release_contract_version=1,
            collection_dispute_id=None,
            collection_case_id=None,
            invoice_draft_id=None,
            issued_invoice_id=None,
            tenant_reference=None,
            currency_code=None,
            remaining_outstanding_amount=None,
            collection_dispute_status=None,
            collection_case_status=None,
            released_at=None,
            source_payload_hash=None,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        self.assertEqual(
            none_reason.as_contract_dict()["rejection_reason_code"],
            "collection_dispute_not_found",
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
        issued_release = CollectionDisputeReleaseService(issued_ledger).release_collection_dispute(
            TENANT_ONE, issued_hold.collection_dispute_id
        )
        self.assertEqual(issued_release.issued_invoice_id, issued.issued_invoice_id)
        self.assertIn("issued_invoice_id", issued_release.as_contract_dict())
        self.assertEqual(
            issued_release.as_webhook_event_data()["issued_invoice_id"],
            str(issued.issued_invoice_id),
        )
        issued_presentment = CollectionDisputeReleasePresentmentService(
            issued_ledger
        ).present_collection_dispute_release(
            TENANT_ONE, issued_release.collection_dispute_id
        )
        self.assertIn("issued_invoice_id", issued_presentment.as_contract_dict())
        still_held_ledger, still_held_case = open_morning_case_with_outstanding()
        still_held = CollectionDisputeService(still_held_ledger).hold_collection_case(
            TENANT_ONE, still_held_case.collection_case_id
        )
        with self.assertRaises(CollectionDisputeReleasePresentmentQueryError) as held_only:
            CollectionDisputeReleasePresentmentService(
                still_held_ledger
            ).present_collection_dispute_release(TENANT_ONE, still_held.collection_dispute_id)
        self.assertEqual(
            held_only.exception.rejection_reason_code, "collection_dispute_release_not_found"
        )
        held_only_enqueue = CollectionDisputeReleaseResult(
            collection_dispute_release_outcome_code=CollectionDisputeReleaseOutcomeCode.ACCEPTED,
            collection_dispute_release_contract_version=1,
            collection_dispute_id=still_held.collection_dispute_id,
            collection_case_id=still_held.collection_case_id,
            invoice_draft_id=still_held.invoice_draft_id,
            issued_invoice_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            remaining_outstanding_amount=KNOWN_MORNING_TOTAL,
            collection_dispute_status="released",
            collection_case_status="open",
            released_at=RELEASED_MORNING,
            source_payload_hash=still_held.source_payload_hash,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaisesRegex(ValueError, "collection dispute is not released"):
            _enqueue_dispute_released(still_held_ledger, TENANT_ONE, held_only_enqueue)
        stored_held = still_held_ledger.get_collection_dispute(still_held.collection_dispute_id)
        still_held_ledger.collection_disputes[still_held.collection_dispute_id] = replace(
            stored_held,
            collection_dispute_status="released",
            released_at=None,
        )
        with self.assertRaisesRegex(ValueError, "collection dispute is not released"):
            _enqueue_dispute_released(still_held_ledger, TENANT_ONE, held_only_enqueue)
        app = create_http_app(issued_ledger)
        mismatch_status, mismatch_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-disputes/{issued_hold.collection_dispute_id}/releases",
            {"tenant_reference": TENANT_ONE, "currency_code": 1},
        )
        self.assertEqual(mismatch_status, 422)
        self.assertEqual(mismatch_body["rejection_reason_code"], "request_invalid")
        list_status, _list_body = invoke_http(
            app,
            "PUT",
            "/v1/collection-dispute-releases",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(list_status, 405)
        item_status, _item_body = invoke_http(
            app,
            "PUT",
            f"/v1/collection-dispute-releases/{issued_release.collection_dispute_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(item_status, 405)
        presentment_payload = presented.as_summary_dict()
        self.assertEqual(presentment_payload["next_operator_action"], "wait")
        missing_remaining = {
            "collection_dispute_release_presentment_contract_version": 1,
            "collection_dispute_id": str(held.collection_dispute_id),
            "tenant_reference": TENANT_ONE,
            "collection_case_id": str(collection.collection_case_id),
            "invoice_draft_id": str(collection.invoice_draft_id),
            "currency_code": "USD",
            "collection_dispute_status": "released",
            "collection_case_status": "open",
            "released_at": "2026-08-18T15:00:00Z",
            "source_payload_hash": "sha256:" + "d" * 64,
            "next_operator_action": "wait",
        }
        self.assertTrue(validate_collection_dispute_release_presentment(missing_remaining))
        self.assertTrue(validate_collection_dispute_release_presentment(["not-an-object"]))
        bad_presentment_remaining = dict(presented.as_contract_dict())
        bad_presentment_remaining["remaining_outstanding_amount"] = "not-decimal"
        self.assertTrue(
            validate_collection_dispute_release_presentment(bad_presentment_remaining)
        )
        int_presentment_remaining = dict(presented.as_contract_dict())
        int_presentment_remaining["remaining_outstanding_amount"] = 1
        self.assertTrue(
            validate_collection_dispute_release_presentment(int_presentment_remaining)
        )
        wait_presentment = dict(presented.as_contract_dict())
        wait_presentment["next_operator_action"] = "collect"
        self.assertTrue(validate_collection_dispute_release_presentment(wait_presentment))
        legal_presentment = dict(presented.as_contract_dict())
        legal_presentment["legal_invoice_number"] = "INV-1"
        self.assertTrue(validate_collection_dispute_release_presentment(legal_presentment))
        negative_presentment = dict(presented.as_contract_dict())
        negative_presentment["remaining_outstanding_amount"] = "-1"
        self.assertTrue(validate_collection_dispute_release_presentment(negative_presentment))
        zero_ledger, zero_case = open_morning_case_with_outstanding()
        CollectionWriteOffService(zero_ledger).write_off_collection_case(
            TENANT_ONE, zero_case.collection_case_id
        )
        zero_hold = CollectionDisputeService(zero_ledger).hold_collection_case(
            TENANT_ONE, zero_case.collection_case_id
        )
        zero_release = CollectionDisputeReleaseService(zero_ledger).release_collection_dispute(
            TENANT_ONE, zero_hold.collection_dispute_id
        )
        self.assertEqual(zero_release.remaining_outstanding_amount, Decimal("0"))
        self.assertEqual(zero_release.collection_case_status, "open")
        zero_presented = CollectionDisputeReleasePresentmentService(
            zero_ledger
        ).present_collection_dispute_release(TENANT_ONE, zero_hold.collection_dispute_id)
        self.assertEqual(zero_presented.remaining_outstanding_amount, Decimal("0"))
        already_open = zero_ledger.mark_collection_case_released_from_dispute(
            zero_case.collection_case_id
        )
        self.assertEqual(already_open.collection_case_status, "open")
        archived_ledger, archived_case = open_morning_case_with_outstanding()
        archived_ledger.collection_cases[archived_case.collection_case_id] = replace(
            archived_ledger.get_collection_case(archived_case.collection_case_id),
            collection_case_status="archived",
        )
        with self.assertRaises(ValueError):
            archived_ledger.mark_collection_case_released_from_dispute(
                archived_case.collection_case_id
            )
        not_held_mark = still_held_ledger.get_collection_dispute(still_held.collection_dispute_id)
        still_held_ledger.collection_disputes[still_held.collection_dispute_id] = replace(
            not_held_mark, collection_dispute_status="archived"
        )
        with self.assertRaises(ValueError):
            still_held_ledger.mark_collection_dispute_released(
                still_held.collection_dispute_id, RELEASED_MORNING
            )
        with mock.patch(
            "metering_billing.http_app.CollectionDisputeReleasePresentmentService.present_collection_dispute_release",
            side_effect=ValueError("boom"),
        ):
            get_boom_status, get_boom = invoke_http(
                app,
                "GET",
                f"/v1/collection-dispute-releases/{issued_release.collection_dispute_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(get_boom_status, 422)
        self.assertEqual(get_boom["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.CollectionDisputeReleaseService.release_collection_dispute",
            side_effect=ValueError("boom"),
        ):
            post_boom_status, post_boom = invoke_http(
                app,
                "POST",
                f"/v1/collection-disputes/{issued_hold.collection_dispute_id}/releases",
                {"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(post_boom_status, 422)
        self.assertEqual(post_boom["rejection_reason_code"], "request_invalid")
