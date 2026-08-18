"""Issued-credit-note void tests for unused commercial credit-note reversal."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    CollectionCaseService,
    CreditAdjustmentService,
    CreditNoteApplicationService,
    IssuedCreditNoteService,
    IssuedCreditNoteVoidPresentmentService,
    IssuedCreditNoteVoidService,
    IssuedInvoiceService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.contracts import (
    validate_issued_credit_note_void,
    validate_issued_credit_note_void_presentment,
)
from metering_billing.errors import (
    CreditNoteApplicationRejectionReasonCode,
    IssuedCreditNoteVoidOutcomeCode,
    IssuedCreditNoteVoidPresentmentQueryError,
    IssuedCreditNoteVoidRejectionReasonCode,
)
from metering_billing.issued_credit_note_void import (
    IssuedCreditNoteVoidResult,
    _format_voided_at,
    _rejected,
)
from metering_billing.usage_ledger import generate_record_id
from test_credit_note_application import issue_morning_credit_then_open_case
from test_http_app import invoke_http
from test_issued_credit_note import record_known_morning_credit
from test_tax_assessment import insert_commercial_draft
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL


VOIDED_MORNING = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
VOIDED_EVENING = datetime(2026, 8, 18, 21, 0, tzinfo=UTC)


def issue_known_morning_credit_note(open_case: bool = False):
    """Issue the known-morning credit note and optionally open its case."""
    ledger, credit = record_known_morning_credit()
    issued = IssuedCreditNoteService(ledger).issue_credit_note(
        TENANT_ONE, credit.credit_adjustment_id
    )
    collection = None
    if open_case:
        collection = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, credit.invoice_draft_id
        )
    return ledger, issued, collection


class IssuedCreditNoteVoidTests(unittest.TestCase):
    """Verify void-once identity, unused-note isolation, and HTTP presentment."""

    def test_void_unused_credit_note_once_without_journal_or_webhook(self) -> None:
        """An unused issued credit note voids once and never changes remaining."""
        ledger, issued, collection = issue_known_morning_credit_note(open_case=True)
        prior_journals = len(ledger.journal_proposals)
        prior_outbox = len(ledger.webhook_outbox_events)
        prior_applications = len(ledger.credit_note_applications)
        first = IssuedCreditNoteVoidService(
            ledger, clock=lambda: VOIDED_MORNING
        ).void_issued_credit_note(TENANT_ONE, issued.issued_credit_note_id)
        second = IssuedCreditNoteVoidService(
            ledger, clock=lambda: VOIDED_EVENING
        ).void_issued_credit_note(TENANT_ONE, issued.issued_credit_note_id)
        self.assertEqual(
            first.issued_credit_note_void_outcome_code,
            IssuedCreditNoteVoidOutcomeCode.ACCEPTED,
        )
        self.assertEqual(
            second.issued_credit_note_void_outcome_code,
            IssuedCreditNoteVoidOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertEqual(first.issued_credit_note_void_id, second.issued_credit_note_void_id)
        self.assertEqual(first.issued_credit_note_id, issued.issued_credit_note_id)
        self.assertEqual(first.credit_adjustment_id, issued.credit_adjustment_id)
        self.assertEqual(first.invoice_draft_id, issued.invoice_draft_id)
        self.assertEqual(first.currency_code, "USD")
        self.assertEqual(first.voided_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(first.issued_credit_note_void_status, "recorded")
        self.assertEqual(first.voided_at, VOIDED_MORNING)
        self.assertEqual(second.voided_at, VOIDED_MORNING)
        self.assertEqual(first.next_operator_action, "wait")
        self.assertTrue(first.source_payload_hash.startswith("sha256:"))
        payload = first.as_contract_dict()
        self.assertEqual(validate_issued_credit_note_void(payload), ())
        self.assertIsInstance(payload["voided_amount"], str)
        self.assertNotIsInstance(payload["voided_amount"], float)
        self.assertNotIn("card_pan", payload)
        self.assertNotIn("legal_credit_note_number", payload)
        self.assertNotIn("vat_register_id", payload)
        self.assertNotIn("remaining_outstanding_amount", payload)
        stored_note = ledger.get_issued_credit_note(issued.issued_credit_note_id)
        self.assertEqual(stored_note.issued_credit_note_status, "issued")
        stored_case = ledger.get_collection_case(collection.collection_case_id)
        self.assertEqual(stored_case.outstanding_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(stored_case.collection_case_status, "open")
        self.assertEqual(len(ledger.issued_credit_note_voids), 1)
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        self.assertEqual(len(ledger.credit_note_applications), prior_applications)
        applied = CreditNoteApplicationService(ledger).apply_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, collection.collection_case_id
        )
        self.assertEqual(
            applied.rejection_reason_code,
            CreditNoteApplicationRejectionReasonCode.ISSUED_CREDIT_NOTE_VOIDED,
        )
        self.assertEqual(
            ledger.get_collection_case(collection.collection_case_id).outstanding_amount,
            KNOWN_MORNING_TOTAL,
        )

    def test_void_without_collection_case_does_not_invent_one(self) -> None:
        """An unused issued credit note without a case still voids once."""
        ledger, issued, _collection = issue_known_morning_credit_note()
        prior_outbox = len(ledger.webhook_outbox_events)
        first = IssuedCreditNoteVoidService(
            ledger, clock=lambda: VOIDED_MORNING
        ).void_issued_credit_note(TENANT_ONE, issued.issued_credit_note_id)
        replay = IssuedCreditNoteVoidService(
            ledger, clock=lambda: VOIDED_EVENING
        ).void_issued_credit_note(TENANT_ONE, issued.issued_credit_note_id)
        self.assertEqual(
            first.issued_credit_note_void_outcome_code,
            IssuedCreditNoteVoidOutcomeCode.ACCEPTED,
        )
        self.assertEqual(
            replay.issued_credit_note_void_outcome_code,
            IssuedCreditNoteVoidOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertIsNone(first.issued_invoice_id)
        payload = first.as_contract_dict()
        self.assertNotIn("issued_invoice_id", payload)
        self.assertNotIn("collection_case_id", payload)
        self.assertEqual(validate_issued_credit_note_void(payload), ())
        self.assertEqual(len(ledger.collection_cases), 0)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)

    def test_fail_closed_when_applied_or_isolated(self) -> None:
        """Applied notes, missing notes, and tenant isolation refuse the void."""
        applied_ledger, applied_note, applied_case = issue_morning_credit_then_open_case()
        CreditNoteApplicationService(applied_ledger).apply_credit_note(
            TENANT_ONE,
            applied_note.issued_credit_note_id,
            applied_case.collection_case_id,
        )
        applied = IssuedCreditNoteVoidService(applied_ledger).void_issued_credit_note(
            TENANT_ONE, applied_note.issued_credit_note_id
        )
        self.assertEqual(
            applied.rejection_reason_code,
            IssuedCreditNoteVoidRejectionReasonCode.CREDIT_NOTE_ALREADY_APPLIED,
        )
        self.assertEqual(len(applied_ledger.issued_credit_note_voids), 0)
        remaining = applied_ledger.get_collection_case(applied_case.collection_case_id)
        self.assertEqual(
            remaining.outstanding_amount,
            KNOWN_MORNING_TOTAL - applied_note.tax_inclusive_amount,
        )

        ledger, issued, _collection = issue_known_morning_credit_note()
        missing = IssuedCreditNoteVoidService(ledger).void_issued_credit_note(
            TENANT_ONE, uuid4()
        )
        self.assertEqual(
            missing.rejection_reason_code,
            IssuedCreditNoteVoidRejectionReasonCode.ISSUED_CREDIT_NOTE_NOT_FOUND,
        )
        crossed = IssuedCreditNoteVoidService(ledger).void_issued_credit_note(
            TENANT_TWO, issued.issued_credit_note_id
        )
        self.assertEqual(
            crossed.rejection_reason_code,
            IssuedCreditNoteVoidRejectionReasonCode.ISSUED_CREDIT_NOTE_NOT_FOUND,
        )
        tenant = IssuedCreditNoteVoidService(ledger).void_issued_credit_note(
            "urn:cwl:missing", issued.issued_credit_note_id
        )
        self.assertEqual(
            tenant.rejection_reason_code,
            IssuedCreditNoteVoidRejectionReasonCode.TENANT_NOT_FOUND,
        )
        currency = IssuedCreditNoteVoidService(ledger).void_issued_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, currency_code="EUR"
        )
        self.assertEqual(
            currency.rejection_reason_code,
            IssuedCreditNoteVoidRejectionReasonCode.CURRENCY_MISMATCH,
        )
        matching = IssuedCreditNoteVoidService(ledger).void_issued_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, currency_code="USD"
        )
        self.assertEqual(
            matching.issued_credit_note_void_outcome_code,
            IssuedCreditNoteVoidOutcomeCode.ACCEPTED,
        )

    def test_http_void_get_and_paged_list_without_ais(self) -> None:
        """POST voids; GET item and list page metadata and never call AIS."""
        ledger, first_issued, _first_case = issue_known_morning_credit_note()
        second_draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("20.00"))
        second_credit = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, second_draft_id, Decimal("20.00"), "billing_error"
        )
        second_issued = IssuedCreditNoteService(ledger).issue_credit_note(
            TENANT_ONE, second_credit.credit_adjustment_id
        )
        prior_outbox = len(ledger.webhook_outbox_events)
        prior_journals = len(ledger.journal_proposals)
        app = create_http_app(ledger)
        refused_status, refused_body = invoke_http(
            app,
            "POST",
            f"/v1/issued-credit-notes/{first_issued.issued_credit_note_id}/voids",
            {"tenant_reference": TENANT_ONE, "card_pan": "4111111111111111"},
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.issued_credit_note_voids), 0)
        typed_status, typed_body = invoke_http(
            app,
            "POST",
            f"/v1/issued-credit-notes/{first_issued.issued_credit_note_id}/voids",
            {"tenant_reference": TENANT_ONE, "currency_code": 1},
        )
        self.assertEqual(typed_status, 422)
        self.assertEqual(typed_body["rejection_reason_code"], "request_invalid")
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            f"/v1/issued-credit-notes/{first_issued.issued_credit_note_id}/voids",
            {"tenant_reference": TENANT_ONE, "currency_code": "USD"},
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["issued_credit_note_void_outcome_code"], "accepted")
        self.assertEqual(
            accepted_body["voided_amount"], format_exact_decimal(KNOWN_MORNING_TOTAL)
        )
        void_id = accepted_body["issued_credit_note_void_id"]
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            f"/v1/issued-credit-notes/{first_issued.issued_credit_note_id}/voids",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["issued_credit_note_void_outcome_code"], "duplicate_replay")
        self.assertEqual(replay_body["issued_credit_note_void_id"], void_id)
        second_status, second_body = invoke_http(
            app,
            "POST",
            f"/v1/issued-credit-notes/{second_issued.issued_credit_note_id}/voids",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(second_body["issued_credit_note_void_outcome_code"], "accepted")
        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/issued-credit-note-voids/{void_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["issued_credit_note_void_id"], void_id)
        self.assertEqual(
            get_body["issued_credit_note_id"], str(first_issued.issued_credit_note_id)
        )
        self.assertNotIn("issued_credit_note_void_outcome_code", get_body)
        self.assertEqual(validate_issued_credit_note_void_presentment(get_body), ())
        header_status, header_body = invoke_http(
            app,
            "GET",
            f"/v1/issued-credit-note-voids/{void_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(header_status, 200)
        self.assertEqual(header_body, get_body)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/issued-credit-note-voids",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"issued_credit_note_voids", "next_cursor"})
        self.assertEqual(len(list_body["issued_credit_note_voids"]), 1)
        self.assertEqual(
            set(list_body["issued_credit_note_voids"][0]),
            {
                "issued_credit_note_void_id",
                "issued_credit_note_id",
                "voided_amount",
                "voided_at",
                "next_operator_action",
            },
        )
        page_two_status, page_two = invoke_http(
            app,
            "GET",
            "/v1/issued-credit-note-voids",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(page_two_status, 200)
        self.assertEqual(len(page_two["issued_credit_note_voids"]), 1)
        self.assertIsNone(page_two["next_cursor"])
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/issued-credit-note-voids",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["issued_credit_note_voids"], [])
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/issued-credit-note-voids/{void_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(
            other_body["rejection_reason_code"], "issued_credit_note_void_not_found"
        )
        missing_status, missing_body = invoke_http(
            app,
            "GET",
            f"/v1/issued-credit-note-voids/{void_id}",
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        method_status, _method_body = invoke_http(
            app,
            "PUT",
            f"/v1/issued-credit-notes/{first_issued.issued_credit_note_id}/voids",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 405)
        get_method_status, get_method_body = invoke_http(
            app,
            "POST",
            "/v1/issued-credit-note-voids",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_method_status, 422)
        self.assertEqual(get_method_body["rejection_reason_code"], "request_invalid")
        item_method_status, item_method_body = invoke_http(
            app,
            "POST",
            f"/v1/issued-credit-note-voids/{void_id}",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(item_method_status, 422)
        self.assertEqual(item_method_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        self.assertEqual(len(ledger.journal_proposals), prior_journals)

    def test_presentment_rejects_invalid_page_and_missing_rows(self) -> None:
        """Presentment fails closed on unreadable cursors and unknown ids."""
        ledger, issued, _collection = issue_known_morning_credit_note()
        voided = IssuedCreditNoteVoidService(ledger).void_issued_credit_note(
            TENANT_ONE, issued.issued_credit_note_id
        )
        presentment = IssuedCreditNoteVoidPresentmentService(ledger)
        with self.assertRaises(IssuedCreditNoteVoidPresentmentQueryError) as missing:
            presentment.present_issued_credit_note_void(TENANT_ONE, uuid4())
        self.assertEqual(
            missing.exception.rejection_reason_code, "issued_credit_note_void_not_found"
        )
        with self.assertRaises(IssuedCreditNoteVoidPresentmentQueryError) as crossed:
            presentment.present_issued_credit_note_void(
                TENANT_TWO, voided.issued_credit_note_void_id
            )
        self.assertEqual(
            crossed.exception.rejection_reason_code, "issued_credit_note_void_not_found"
        )
        with self.assertRaises(IssuedCreditNoteVoidPresentmentQueryError) as bad_cursor:
            presentment.list_issued_credit_note_voids(TENANT_ONE, cursor="not-a-cursor")
        self.assertEqual(bad_cursor.exception.rejection_reason_code, "request_invalid")
        with self.assertRaises(IssuedCreditNoteVoidPresentmentQueryError) as bad_limit:
            presentment.list_issued_credit_note_voids(TENANT_ONE, page_limit=0)
        self.assertEqual(bad_limit.exception.rejection_reason_code, "request_invalid")
        page = presentment.list_issued_credit_note_voids(TENANT_ONE)
        self.assertEqual(len(page.issued_credit_note_voids), 1)
        self.assertEqual(
            page.issued_credit_note_voids[0].issued_credit_note_void_id,
            voided.issued_credit_note_void_id,
        )

    def test_contract_and_ledger_guards_cover_identity(self) -> None:
        """Accepted voids need identity; ledger rows stay append-only."""
        valid = {
            "issued_credit_note_void_contract_version": 1,
            "issued_credit_note_void_outcome_code": "accepted",
            "issued_credit_note_void_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd90",
            "tenant_reference": TENANT_ONE,
            "issued_credit_note_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd91",
            "credit_adjustment_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd92",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "currency_code": "USD",
            "voided_amount": format_exact_decimal(KNOWN_MORNING_TOTAL),
            "issued_credit_note_void_status": "recorded",
            "voided_at": "2026-08-18T12:00:00Z",
            "source_payload_hash": "sha256:" + "e" * 64,
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_issued_credit_note_void(valid), ())
        rejected = {
            "issued_credit_note_void_contract_version": 1,
            "issued_credit_note_void_outcome_code": "rejected",
            "rejection_reason_code": "issued_credit_note_not_found",
        }
        self.assertEqual(validate_issued_credit_note_void(rejected), ())
        missing_id = json.loads(json.dumps(valid))
        del missing_id["issued_credit_note_void_id"]
        self.assertTrue(validate_issued_credit_note_void(missing_id))
        self.assertTrue(validate_issued_credit_note_void(["not-an-object"]))
        ledger, issued, _collection = issue_known_morning_credit_note()
        voided = IssuedCreditNoteVoidService(ledger).void_issued_credit_note(
            TENANT_ONE, issued.issued_credit_note_id
        )
        stored = ledger.get_issued_credit_note_void(voided.issued_credit_note_void_id)
        with self.assertRaises(ValueError):
            ledger.insert_issued_credit_note_void(stored)
        colliding = replace(stored, issued_credit_note_void_id=generate_record_id())
        with self.assertRaises(ValueError):
            ledger.insert_issued_credit_note_void(colliding)
        with self.assertRaises(ValueError):
            ledger.insert_issued_credit_note_void(
                replace(stored, issued_credit_note_void_status="settled")
            )
        with self.assertRaises(ValueError):
            ledger.insert_issued_credit_note_void(
                replace(
                    stored,
                    issued_credit_note_void_id=generate_record_id(),
                    issued_credit_note_id=generate_record_id(),
                    voided_amount=Decimal("0"),
                )
            )
        unknown_outcome = {
            "issued_credit_note_void_contract_version": 1,
            "issued_credit_note_void_outcome_code": "posted",
        }
        self.assertTrue(validate_issued_credit_note_void(unknown_outcome))
        missing_outcome = {"issued_credit_note_void_contract_version": 1}
        self.assertTrue(validate_issued_credit_note_void(missing_outcome))
        legal = json.loads(json.dumps(valid))
        legal["legal_credit_note_number"] = "CN-1"
        self.assertTrue(validate_issued_credit_note_void(legal))
        rejected_legal = {
            "issued_credit_note_void_contract_version": 1,
            "issued_credit_note_void_outcome_code": "rejected",
            "rejection_reason_code": "issued_credit_note_not_found",
            "legal_credit_note_number": "CN-1",
        }
        self.assertTrue(validate_issued_credit_note_void(rejected_legal))
        rejected_missing_reason = {
            "issued_credit_note_void_contract_version": 1,
            "issued_credit_note_void_outcome_code": "rejected",
        }
        self.assertTrue(validate_issued_credit_note_void(rejected_missing_reason))
        missing_amount = json.loads(json.dumps(valid))
        del missing_amount["voided_amount"]
        self.assertTrue(validate_issued_credit_note_void(missing_amount))
        zero_amount = json.loads(json.dumps(valid))
        zero_amount["voided_amount"] = "0"
        self.assertTrue(validate_issued_credit_note_void(zero_amount))
        bad_amount = json.loads(json.dumps(valid))
        bad_amount["voided_amount"] = 1
        self.assertTrue(validate_issued_credit_note_void(bad_amount))
        unreadable_amount = json.loads(json.dumps(valid))
        unreadable_amount["voided_amount"] = "not-decimal"
        self.assertTrue(validate_issued_credit_note_void(unreadable_amount))

    def test_coverage_guards_for_void_and_presentment_edges(self) -> None:
        """Closed branches stay explicit: replay, presentment, constructors, and HTTP."""
        ledger, issued, _collection = issue_known_morning_credit_note()
        voided = IssuedCreditNoteVoidService(ledger).void_issued_credit_note(
            TENANT_ONE, issued.issued_credit_note_id
        )
        presentment = IssuedCreditNoteVoidPresentmentService(ledger)
        presented = presentment.present_issued_credit_note_void(
            TENANT_ONE, voided.issued_credit_note_void_id
        )
        if presented.issued_invoice_id is not None:
            self.assertIn("issued_invoice_id", presented.as_contract_dict())
        linked_ledger, credit = record_known_morning_credit()
        issued_invoice = IssuedInvoiceService(linked_ledger).issue_invoice(
            TENANT_ONE, credit.invoice_draft_id
        )
        linked_note = IssuedCreditNoteService(linked_ledger).issue_credit_note(
            TENANT_ONE, credit.credit_adjustment_id
        )
        linked_void = IssuedCreditNoteVoidService(linked_ledger).void_issued_credit_note(
            TENANT_ONE, linked_note.issued_credit_note_id
        )
        self.assertEqual(linked_void.issued_invoice_id, issued_invoice.issued_invoice_id)
        self.assertIn("issued_invoice_id", linked_void.as_contract_dict())
        linked_presented = IssuedCreditNoteVoidPresentmentService(
            linked_ledger
        ).present_issued_credit_note_void(
            TENANT_ONE, linked_void.issued_credit_note_void_id
        )
        self.assertIn("issued_invoice_id", linked_presented.as_contract_dict())
        with self.assertRaises(IssuedCreditNoteVoidPresentmentQueryError):
            presentment.list_issued_credit_note_voids(TENANT_ONE, page_limit=True)
        with self.assertRaises(IssuedCreditNoteVoidPresentmentQueryError):
            presentment.list_issued_credit_note_voids(TENANT_ONE, page_limit="abc")
        with self.assertRaises(IssuedCreditNoteVoidPresentmentQueryError):
            presentment.list_issued_credit_note_voids(TENANT_ONE, page_limit=101)
        with self.assertRaises(IssuedCreditNoteVoidPresentmentQueryError):
            presentment.list_issued_credit_note_voids(TENANT_ONE, page_limit=1.5)
        listed = presentment.list_issued_credit_note_voids(TENANT_ONE, cursor="")
        self.assertEqual(len(listed.issued_credit_note_voids), 1)
        empty_limit = presentment.list_issued_credit_note_voids(TENANT_ONE, page_limit="")
        self.assertEqual(len(empty_limit.issued_credit_note_voids), 1)
        defaulted = IssuedCreditNoteVoidService()
        self.assertIsNotNone(defaulted.ledger)
        default_presentment = IssuedCreditNoteVoidPresentmentService()
        self.assertIsNotNone(default_presentment.ledger)
        with self.assertRaises(IssuedCreditNoteVoidPresentmentQueryError):
            default_presentment.list_issued_credit_note_voids(TENANT_ONE)
        none_reason = _rejected(None)
        self.assertEqual(
            none_reason.as_contract_dict()["rejection_reason_code"],
            "issued_credit_note_not_found",
        )
        with self.assertRaises(ValueError):
            IssuedCreditNoteVoidResult(
                issued_credit_note_void_outcome_code="posted",
                issued_credit_note_void_contract_version=1,
                issued_credit_note_void_id=None,
                issued_credit_note_id=None,
                credit_adjustment_id=None,
                invoice_draft_id=None,
                issued_invoice_id=None,
                tenant_reference=None,
                currency_code=None,
                voided_amount=None,
                issued_credit_note_void_status=None,
                voided_at=None,
                source_payload_hash=None,
                next_operator_action="wait",
                rejection_reason_code=None,
            ).as_contract_dict()
        with self.assertRaises(ValueError):
            _format_voided_at(None)
        service = IssuedCreditNoteVoidService(ledger)
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.void_issued_credit_note(TENANT_ONE, issued.issued_credit_note_id)
        app = create_http_app(ledger)
        with mock.patch(
            "metering_billing.http_app.IssuedCreditNoteVoidPresentmentService.list_issued_credit_note_voids",
            side_effect=IssuedCreditNoteVoidPresentmentQueryError(
                "issued_credit_note_void_not_found"
            ),
        ):
            not_found_status, not_found_body = invoke_http(
                app,
                "GET",
                "/v1/issued-credit-note-voids",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(not_found_status, 404)
        self.assertEqual(
            not_found_body["rejection_reason_code"], "issued_credit_note_void_not_found"
        )
        with mock.patch(
            "metering_billing.http_app.IssuedCreditNoteVoidService.void_issued_credit_note",
            side_effect=ValueError("closed"),
        ):
            void_value_status, void_value_body = invoke_http(
                create_http_app(ledger),
                "POST",
                f"/v1/issued-credit-notes/{issued.issued_credit_note_id}/voids",
                {"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(void_value_status, 422)
        self.assertEqual(void_value_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.IssuedCreditNoteVoidPresentmentService.list_issued_credit_note_voids",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/issued-credit-note-voids",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        presentment_payload = presented.as_contract_dict()
        self.assertEqual(validate_issued_credit_note_void_presentment(presentment_payload), ())
        self.assertTrue(validate_issued_credit_note_void_presentment(["not-an-object"]))
        missing_amount = json.loads(json.dumps(presentment_payload))
        del missing_amount["voided_amount"]
        self.assertTrue(validate_issued_credit_note_void_presentment(missing_amount))
        zero_amount = json.loads(json.dumps(presentment_payload))
        zero_amount["voided_amount"] = "0"
        self.assertTrue(validate_issued_credit_note_void_presentment(zero_amount))
        int_amount = json.loads(json.dumps(presentment_payload))
        int_amount["voided_amount"] = 1
        self.assertTrue(validate_issued_credit_note_void_presentment(int_amount))
        bad_amount = json.loads(json.dumps(presentment_payload))
        bad_amount["voided_amount"] = "not-decimal"
        self.assertTrue(validate_issued_credit_note_void_presentment(bad_amount))
        legal = json.loads(json.dumps(presentment_payload))
        legal["legal_credit_note_number"] = "CN-1"
        self.assertTrue(validate_issued_credit_note_void_presentment(legal))
        outcome = json.loads(json.dumps(presentment_payload))
        outcome["issued_credit_note_void_outcome_code"] = "accepted"
        self.assertTrue(validate_issued_credit_note_void_presentment(outcome))
        if presented.issued_invoice_id is None:
            with_invoice = json.loads(json.dumps(presentment_payload))
            with_invoice["issued_invoice_id"] = str(uuid4())
            self.assertEqual(
                validate_issued_credit_note_void_presentment(with_invoice), ()
            )
        action = json.loads(json.dumps(presentment_payload))
        action["next_operator_action"] = "collect"
        self.assertTrue(validate_issued_credit_note_void_presentment(action))
