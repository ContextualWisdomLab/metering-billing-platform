"""Collection-case settlement tests for explicit settle-when-zero."""

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
    CollectionCaseSettlementPresentmentService,
    CollectionCaseSettlementService,
    CreditNoteApplicationService,
    IssuedInvoiceService,
    create_http_app,
)
from metering_billing.contracts import (
    validate_collection_case_settlement,
    validate_collection_case_settlement_presentment,
)
from metering_billing.errors import (
    CollectionCaseSettlementOutcomeCode,
    CollectionCaseSettlementPresentmentQueryError,
    CollectionCaseSettlementRejectionReasonCode,
)
from metering_billing.collection_case_settlement import (
    CollectionCaseSettlementResult,
    _format_settled_at,
)
from metering_billing.usage_ledger import generate_record_id
from test_collection_case import draft_known_morning
from test_credit_note_application import issue_morning_credit_then_open_case
from test_http_app import invoke_http
from test_tax_assessment import HUNDRED, insert_commercial_draft
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL, seed_rated_ledger


SETTLED_MORNING = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)
SETTLED_EVENING = datetime(2026, 8, 18, 19, 0, tzinfo=UTC)


def open_morning_case_at_zero():
    """Open a morning case and leave it open with exact-zero outstanding."""
    ledger, invoice_draft_id = draft_known_morning()
    collection = CollectionCaseService(ledger).open_collection_case(
        TENANT_ONE, invoice_draft_id
    )
    stored = ledger.get_collection_case(collection.collection_case_id)
    ledger.collection_cases[collection.collection_case_id] = replace(
        stored, outstanding_amount=Decimal("0")
    )
    return ledger, collection


class CollectionCaseSettlementTests(unittest.TestCase):
    """Verify settle-once identity, exact-zero rule, and HTTP presentment."""

    def test_settle_marks_open_zero_case_once(self) -> None:
        """An open case at exact zero settles once and never double-settles."""
        ledger, collection = open_morning_case_at_zero()
        prior_journals = len(ledger.journal_proposals)
        prior_outbox = len(ledger.webhook_outbox_events)
        prior_receipts = len(ledger.payment_receipts)
        first = CollectionCaseSettlementService(
            ledger, clock=lambda: SETTLED_MORNING
        ).settle_collection_case(TENANT_ONE, collection.collection_case_id)
        second = CollectionCaseSettlementService(
            ledger, clock=lambda: SETTLED_EVENING
        ).settle_collection_case(TENANT_ONE, collection.collection_case_id)
        self.assertEqual(
            first.collection_case_settlement_outcome_code,
            CollectionCaseSettlementOutcomeCode.ACCEPTED,
        )
        self.assertEqual(
            second.collection_case_settlement_outcome_code,
            CollectionCaseSettlementOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertEqual(
            first.collection_case_settlement_id, second.collection_case_settlement_id
        )
        self.assertEqual(first.collection_case_id, collection.collection_case_id)
        self.assertEqual(first.invoice_draft_id, collection.invoice_draft_id)
        self.assertIsNone(first.issued_invoice_id)
        self.assertEqual(first.currency_code, "USD")
        self.assertEqual(first.remaining_outstanding_amount, Decimal("0"))
        self.assertEqual(first.collection_case_status, "settled")
        self.assertEqual(first.settled_at, SETTLED_MORNING)
        self.assertEqual(second.settled_at, SETTLED_MORNING)
        self.assertEqual(first.collection_case_settlement_status, "settled")
        self.assertEqual(first.next_operator_action, "wait")
        self.assertTrue(first.source_payload_hash.startswith("sha256:"))
        payload = first.as_contract_dict()
        self.assertEqual(validate_collection_case_settlement(payload), ())
        self.assertNotIn("card_pan", payload)
        self.assertNotIn("write_off_amount", payload)
        stored_case = ledger.get_collection_case(collection.collection_case_id)
        self.assertEqual(stored_case.outstanding_amount, Decimal("0"))
        self.assertEqual(stored_case.collection_case_status, "settled")
        self.assertEqual(len(ledger.collection_case_settlements), 1)
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        self.assertEqual(len(ledger.payment_receipts), prior_receipts)
        self.assertEqual(len(ledger.credit_note_applications), 0)

    def test_fail_closed_when_outstanding_is_not_zero(self) -> None:
        """A positive outstanding writes zero settlement rows."""
        ledger, invoice_draft_id = draft_known_morning()
        collection = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, invoice_draft_id
        )
        refused = CollectionCaseSettlementService(ledger).settle_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(
            refused.collection_case_settlement_outcome_code,
            CollectionCaseSettlementOutcomeCode.REJECTED,
        )
        self.assertEqual(
            refused.rejection_reason_code,
            CollectionCaseSettlementRejectionReasonCode.OUTSTANDING_NOT_ZERO,
        )
        self.assertEqual(collection.outstanding_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(len(ledger.collection_case_settlements), 0)
        stored = ledger.get_collection_case(collection.collection_case_id)
        self.assertEqual(stored.collection_case_status, "open")

    def test_fail_closed_when_case_already_settled_by_credit(self) -> None:
        """Implicit #45 settle-when-zero stays; this command does not rewrite it."""
        ledger, issued, collection = issue_morning_credit_then_open_case()
        applied = CreditNoteApplicationService(ledger).apply_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, collection.collection_case_id
        )
        self.assertEqual(applied.collection_case_status, "settled")
        refused = CollectionCaseSettlementService(ledger).settle_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(
            refused.rejection_reason_code,
            CollectionCaseSettlementRejectionReasonCode.COLLECTION_CASE_SETTLED,
        )
        self.assertEqual(len(ledger.collection_case_settlements), 0)
        self.assertEqual(len(ledger.credit_note_applications), 1)

    def test_issued_invoice_is_preserved_when_stored(self) -> None:
        """A stored issued invoice on the case draft is copied onto the settlement."""
        ledger = seed_rated_ledger()
        invoice_draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        issued_invoice = IssuedInvoiceService(ledger).issue_invoice(TENANT_ONE, invoice_draft_id)
        collection = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, invoice_draft_id
        )
        stored = ledger.get_collection_case(collection.collection_case_id)
        ledger.collection_cases[collection.collection_case_id] = replace(
            stored, outstanding_amount=Decimal("0")
        )
        settled = CollectionCaseSettlementService(ledger).settle_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(
            settled.collection_case_settlement_outcome_code,
            CollectionCaseSettlementOutcomeCode.ACCEPTED,
        )
        self.assertEqual(settled.issued_invoice_id, issued_invoice.issued_invoice_id)
        self.assertIn("issued_invoice_id", settled.as_contract_dict())

    def test_unknown_and_cross_tenant_targets_are_rejected(self) -> None:
        """Missing tenant or case cannot invent a settlement."""
        ledger, collection = open_morning_case_at_zero()
        missing_tenant = CollectionCaseSettlementService(ledger).settle_collection_case(
            "urn:cwl:missing", collection.collection_case_id
        )
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            CollectionCaseSettlementRejectionReasonCode.TENANT_NOT_FOUND,
        )
        missing_case = CollectionCaseSettlementService(ledger).settle_collection_case(
            TENANT_ONE, uuid4()
        )
        self.assertEqual(
            missing_case.rejection_reason_code,
            CollectionCaseSettlementRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )
        crossed = CollectionCaseSettlementService(ledger).settle_collection_case(
            TENANT_TWO, collection.collection_case_id
        )
        self.assertEqual(
            crossed.rejection_reason_code,
            CollectionCaseSettlementRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )
        self.assertEqual(len(ledger.collection_case_settlements), 0)

    def test_resolver_hollow_success_raises_value_error(self) -> None:
        """A broken tenant resolve must raise ValueError, not assert."""
        ledger, collection = open_morning_case_at_zero()
        service = CollectionCaseSettlementService(ledger)
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.settle_collection_case(TENANT_ONE, collection.collection_case_id)

    def test_http_settle_get_and_paged_list_without_capture(self) -> None:
        """POST settles; GET item and list page metadata and never capture payment."""
        ledger, first_case = open_morning_case_at_zero()
        second_draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("20.00"))
        second_case = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, second_draft_id
        )
        second_stored = ledger.get_collection_case(second_case.collection_case_id)
        ledger.collection_cases[second_case.collection_case_id] = replace(
            second_stored, outstanding_amount=Decimal("0")
        )
        prior_outbox = len(ledger.webhook_outbox_events)
        prior_journals = len(ledger.journal_proposals)
        app = create_http_app(ledger)
        refused_status, refused_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/settlements",
            {"tenant_reference": TENANT_ONE, "card_pan": "4111111111111111"},
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.collection_case_settlements), 0)
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/settlements",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["collection_case_settlement_outcome_code"], "accepted")
        self.assertEqual(accepted_body["remaining_outstanding_amount"], "0")
        settlement_id = accepted_body["collection_case_settlement_id"]
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/settlements",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["collection_case_settlement_outcome_code"], "duplicate_replay")
        self.assertEqual(replay_body["collection_case_settlement_id"], settlement_id)
        second_status, second_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{second_case.collection_case_id}/settlements",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(second_body["collection_case_settlement_outcome_code"], "accepted")
        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-case-settlements/{settlement_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["collection_case_settlement_id"], settlement_id)
        self.assertEqual(get_body["collection_case_id"], str(first_case.collection_case_id))
        self.assertNotIn("collection_case_settlement_outcome_code", get_body)
        self.assertEqual(validate_collection_case_settlement_presentment(get_body), ())
        header_status, header_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-case-settlements/{settlement_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(header_status, 200)
        self.assertEqual(header_body, get_body)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/collection-case-settlements",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"collection_case_settlements", "next_cursor"})
        self.assertEqual(len(list_body["collection_case_settlements"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        page_two_status, page_two = invoke_http(
            app,
            "GET",
            "/v1/collection-case-settlements",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(page_two_status, 200)
        self.assertEqual(len(page_two["collection_case_settlements"]), 1)
        self.assertIsNone(page_two["next_cursor"])
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/collection-case-settlements",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["collection_case_settlements"], [])
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-case-settlements/{settlement_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "collection_case_settlement_not_found")
        missing_status, missing_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-case-settlements/{settlement_id}",
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        method_status, _method_body = invoke_http(
            app,
            "PUT",
            f"/v1/collection-cases/{first_case.collection_case_id}/settlements",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 405)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        self.assertEqual(len(ledger.payment_receipts), 0)

    def test_presentment_rejects_invalid_page_and_missing_rows(self) -> None:
        """Presentment fails closed on unreadable cursors and unknown ids."""
        ledger, collection = open_morning_case_at_zero()
        settled = CollectionCaseSettlementService(ledger).settle_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        presentment = CollectionCaseSettlementPresentmentService(ledger)
        with self.assertRaises(CollectionCaseSettlementPresentmentQueryError) as missing:
            presentment.present_collection_case_settlement(TENANT_ONE, uuid4())
        self.assertEqual(
            missing.exception.rejection_reason_code, "collection_case_settlement_not_found"
        )
        with self.assertRaises(CollectionCaseSettlementPresentmentQueryError) as bad_cursor:
            presentment.list_collection_case_settlements(TENANT_ONE, cursor="not-a-cursor")
        self.assertEqual(bad_cursor.exception.rejection_reason_code, "request_invalid")
        with self.assertRaises(CollectionCaseSettlementPresentmentQueryError) as bad_limit:
            presentment.list_collection_case_settlements(TENANT_ONE, page_limit=0)
        self.assertEqual(bad_limit.exception.rejection_reason_code, "request_invalid")
        page = presentment.list_collection_case_settlements(TENANT_ONE)
        self.assertEqual(len(page.collection_case_settlements), 1)
        self.assertEqual(
            page.collection_case_settlements[0].collection_case_settlement_id,
            settled.collection_case_settlement_id,
        )

    def test_contract_and_ledger_guards_cover_identity(self) -> None:
        """Accepted settlements need identity; ledger rows stay append-only."""
        valid = {
            "collection_case_settlement_contract_version": 1,
            "collection_case_settlement_outcome_code": "accepted",
            "collection_case_settlement_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd50",
            "tenant_reference": TENANT_ONE,
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd21",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "currency_code": "USD",
            "remaining_outstanding_amount": "0",
            "collection_case_settlement_status": "settled",
            "collection_case_status": "settled",
            "settled_at": "2026-08-18T10:00:00Z",
            "source_payload_hash": "sha256:" + "d" * 64,
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_collection_case_settlement(valid), ())
        rejected = {
            "collection_case_settlement_contract_version": 1,
            "collection_case_settlement_outcome_code": "rejected",
            "rejection_reason_code": "outstanding_not_zero",
        }
        self.assertEqual(validate_collection_case_settlement(rejected), ())
        missing_id = json.loads(json.dumps(valid))
        del missing_id["collection_case_settlement_id"]
        self.assertTrue(validate_collection_case_settlement(missing_id))
        self.assertTrue(validate_collection_case_settlement(["not-an-object"]))
        ledger, collection = open_morning_case_at_zero()
        settled = CollectionCaseSettlementService(ledger).settle_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        stored = ledger.get_collection_case_settlement(settled.collection_case_settlement_id)
        with self.assertRaises(ValueError):
            ledger.insert_collection_case_settlement(stored)
        colliding = replace(stored, collection_case_settlement_id=generate_record_id())
        with self.assertRaises(ValueError):
            ledger.insert_collection_case_settlement(colliding)
        with self.assertRaises(ValueError):
            ledger.insert_collection_case_settlement(
                replace(stored, collection_case_settlement_status="open")
            )
        with self.assertRaises(ValueError):
            ledger.insert_collection_case_settlement(
                replace(
                    stored,
                    collection_case_settlement_id=generate_record_id(),
                    collection_case_id=generate_record_id(),
                    remaining_outstanding_amount=Decimal("1"),
                )
            )
        with self.assertRaises(ValueError):
            ledger.mark_collection_case_settled(uuid4())
        open_ledger, open_collection = draft_known_morning()
        open_case = CollectionCaseService(open_ledger).open_collection_case(
            TENANT_ONE, open_collection
        )
        with self.assertRaises(ValueError):
            open_ledger.mark_collection_case_settled(open_case.collection_case_id)
        already = ledger.mark_collection_case_settled(collection.collection_case_id)
        self.assertEqual(already.collection_case_status, "settled")
        missing_remaining = json.loads(json.dumps(valid))
        del missing_remaining["remaining_outstanding_amount"]
        self.assertTrue(validate_collection_case_settlement(missing_remaining))
        unknown_outcome = {
            "collection_case_settlement_contract_version": 1,
            "collection_case_settlement_outcome_code": "posted",
        }
        self.assertTrue(validate_collection_case_settlement(unknown_outcome))
        missing_outcome = {"collection_case_settlement_contract_version": 1}
        self.assertTrue(validate_collection_case_settlement(missing_outcome))
        forbidden = json.loads(json.dumps(valid))
        forbidden["write_off_amount"] = "1.00"
        self.assertTrue(validate_collection_case_settlement(forbidden))
        legal = json.loads(json.dumps(valid))
        legal["legal_credit_note_number"] = "CN-1"
        self.assertTrue(validate_collection_case_settlement(legal))
        rejected_write_off = {
            "collection_case_settlement_contract_version": 1,
            "collection_case_settlement_outcome_code": "rejected",
            "rejection_reason_code": "outstanding_not_zero",
            "write_off_amount": "1.00",
        }
        self.assertTrue(validate_collection_case_settlement(rejected_write_off))
        rejected_legal = {
            "collection_case_settlement_contract_version": 1,
            "collection_case_settlement_outcome_code": "rejected",
            "rejection_reason_code": "outstanding_not_zero",
            "legal_credit_note_number": "CN-1",
        }
        self.assertTrue(validate_collection_case_settlement(rejected_legal))
        rejected_missing_reason = {
            "collection_case_settlement_contract_version": 1,
            "collection_case_settlement_outcome_code": "rejected",
        }
        self.assertTrue(validate_collection_case_settlement(rejected_missing_reason))
        nonzero = json.loads(json.dumps(valid))
        nonzero["remaining_outstanding_amount"] = "1.00"
        self.assertTrue(validate_collection_case_settlement(nonzero))
        bad_remaining = json.loads(json.dumps(valid))
        bad_remaining["remaining_outstanding_amount"] = "not-decimal"
        self.assertTrue(validate_collection_case_settlement(bad_remaining))
        int_remaining = json.loads(json.dumps(valid))
        int_remaining["remaining_outstanding_amount"] = 0
        self.assertTrue(validate_collection_case_settlement(int_remaining))
        string_accepted = CollectionCaseSettlementResult(
            collection_case_settlement_outcome_code="accepted",
            collection_case_settlement_contract_version=1,
            collection_case_settlement_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            issued_invoice_id=generate_record_id(),
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            remaining_outstanding_amount=Decimal("0"),
            collection_case_settlement_status="settled",
            collection_case_status="settled",
            settled_at=SETTLED_MORNING,
            source_payload_hash="sha256:" + "f" * 64,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        self.assertEqual(string_accepted.as_contract_dict()["collection_case_settlement_outcome_code"], "accepted")
        self.assertIn("issued_invoice_id", string_accepted.as_contract_dict())
        presentment = CollectionCaseSettlementPresentmentService(ledger).present_collection_case_settlement(
            TENANT_ONE, settled.collection_case_settlement_id
        )
        presentment_payload = presentment.as_contract_dict()
        self.assertEqual(validate_collection_case_settlement_presentment(presentment_payload), ())
        missing_presentment_remaining = json.loads(json.dumps(presentment_payload))
        del missing_presentment_remaining["remaining_outstanding_amount"]
        self.assertTrue(
            validate_collection_case_settlement_presentment(missing_presentment_remaining)
        )
        nonzero_presentment = json.loads(json.dumps(presentment_payload))
        nonzero_presentment["remaining_outstanding_amount"] = "1.00"
        self.assertTrue(validate_collection_case_settlement_presentment(nonzero_presentment))
        wait_mismatch = json.loads(json.dumps(presentment_payload))
        wait_mismatch["next_operator_action"] = "collect"
        self.assertTrue(validate_collection_case_settlement_presentment(wait_mismatch))
        forbidden_presentment = json.loads(json.dumps(presentment_payload))
        forbidden_presentment["write_off_amount"] = "1.00"
        self.assertTrue(validate_collection_case_settlement_presentment(forbidden_presentment))
        self.assertTrue(validate_collection_case_settlement_presentment(["not-an-object"]))
        float_remaining = json.loads(json.dumps(presentment_payload))
        float_remaining["remaining_outstanding_amount"] = 0
        self.assertTrue(validate_collection_case_settlement_presentment(float_remaining))
        bad_presentment_remaining = json.loads(json.dumps(presentment_payload))
        bad_presentment_remaining["remaining_outstanding_amount"] = "not-decimal"
        self.assertTrue(
            validate_collection_case_settlement_presentment(bad_presentment_remaining)
        )

    def test_guards_cover_replay_presentment_and_http_branches(self) -> None:
        """Replay, presentment, constructors, and HTTP refuse extra write paths."""
        ledger, collection = open_morning_case_at_zero()
        settled = CollectionCaseSettlementService(ledger).settle_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        presentment = CollectionCaseSettlementPresentmentService(ledger)
        with self.assertRaises(CollectionCaseSettlementPresentmentQueryError):
            presentment.list_collection_case_settlements(TENANT_ONE, page_limit=True)
        with self.assertRaises(CollectionCaseSettlementPresentmentQueryError):
            presentment.list_collection_case_settlements(TENANT_ONE, page_limit="abc")
        with self.assertRaises(CollectionCaseSettlementPresentmentQueryError):
            presentment.list_collection_case_settlements(TENANT_ONE, page_limit=101)
        with self.assertRaises(CollectionCaseSettlementPresentmentQueryError):
            presentment.list_collection_case_settlements(TENANT_ONE, page_limit=1.5)
        default_page = presentment.list_collection_case_settlements(TENANT_ONE, page_limit="")
        self.assertEqual(len(default_page.collection_case_settlements), 1)
        with self.assertRaises(CollectionCaseSettlementPresentmentQueryError) as missing_tenant:
            presentment.present_collection_case_settlement("urn:cwl:missing", settled.collection_case_settlement_id)
        self.assertEqual(missing_tenant.exception.rejection_reason_code, "tenant_not_found")
        mutated = ledger.get_collection_case(collection.collection_case_id)
        ledger.collection_cases[collection.collection_case_id] = replace(
            mutated, outstanding_amount=Decimal("1.00")
        )
        mutated_presentment = presentment.present_collection_case_settlement(
            TENANT_ONE, settled.collection_case_settlement_id
        )
        self.assertEqual(mutated_presentment.remaining_outstanding_amount, Decimal("1.00"))
        mutated_replay = CollectionCaseSettlementService(ledger).settle_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(mutated_replay.remaining_outstanding_amount, Decimal("1.00"))
        del ledger.collection_cases[collection.collection_case_id]
        missing_replay = CollectionCaseSettlementService(ledger).settle_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(
            missing_replay.rejection_reason_code,
            CollectionCaseSettlementRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )
        with self.assertRaises(CollectionCaseSettlementPresentmentQueryError) as missing_case:
            presentment.present_collection_case_settlement(
                TENANT_ONE, settled.collection_case_settlement_id
            )
        self.assertEqual(
            missing_case.exception.rejection_reason_code,
            "collection_case_settlement_not_found",
        )
        with mock.patch.object(presentment.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                presentment.present_collection_case_settlement(
                    TENANT_ONE, settled.collection_case_settlement_id
                )
        CollectionCaseSettlementService()
        CollectionCaseSettlementPresentmentService()
        with self.assertRaises(ValueError):
            _format_settled_at(None)
        unsupported = CollectionCaseSettlementResult(
            collection_case_settlement_outcome_code="posted",
            collection_case_settlement_contract_version=1,
            collection_case_settlement_id=None,
            collection_case_id=None,
            invoice_draft_id=None,
            issued_invoice_id=None,
            tenant_reference=None,
            currency_code=None,
            remaining_outstanding_amount=None,
            collection_case_settlement_status=None,
            collection_case_status=None,
            settled_at=None,
            source_payload_hash=None,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaises(ValueError):
            unsupported.as_contract_dict()
        accepted_without_time = CollectionCaseSettlementResult(
            collection_case_settlement_outcome_code=CollectionCaseSettlementOutcomeCode.ACCEPTED,
            collection_case_settlement_contract_version=1,
            collection_case_settlement_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            issued_invoice_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            remaining_outstanding_amount=Decimal("0"),
            collection_case_settlement_status="settled",
            collection_case_status="settled",
            settled_at=None,
            source_payload_hash="sha256:" + "e" * 64,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaises(ValueError):
            accepted_without_time.as_contract_dict()
        rejected_without_reason = CollectionCaseSettlementResult(
            collection_case_settlement_outcome_code=CollectionCaseSettlementOutcomeCode.REJECTED,
            collection_case_settlement_contract_version=1,
            collection_case_settlement_id=None,
            collection_case_id=None,
            invoice_draft_id=None,
            issued_invoice_id=None,
            tenant_reference=None,
            currency_code=None,
            remaining_outstanding_amount=None,
            collection_case_settlement_status=None,
            collection_case_status=None,
            settled_at=None,
            source_payload_hash=None,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        self.assertEqual(
            rejected_without_reason.as_contract_dict()["rejection_reason_code"],
            "collection_case_not_found",
        )
        string_rejected = CollectionCaseSettlementResult(
            collection_case_settlement_outcome_code="rejected",
            collection_case_settlement_contract_version=1,
            collection_case_settlement_id=None,
            collection_case_id=None,
            invoice_draft_id=None,
            issued_invoice_id=None,
            tenant_reference=None,
            currency_code=None,
            remaining_outstanding_amount=None,
            collection_case_settlement_status=None,
            collection_case_status=None,
            settled_at=None,
            source_payload_hash=None,
            next_operator_action="wait",
            rejection_reason_code=CollectionCaseSettlementRejectionReasonCode.OUTSTANDING_NOT_ZERO,
        )
        self.assertEqual(string_rejected.as_contract_dict()["rejection_reason_code"], "outstanding_not_zero")
        issued_ledger = seed_rated_ledger()
        invoice_draft_id = insert_commercial_draft(issued_ledger, TENANT_ONE, "USD", HUNDRED)
        issued_invoice = IssuedInvoiceService(issued_ledger).issue_invoice(
            TENANT_ONE, invoice_draft_id
        )
        issued_case = CollectionCaseService(issued_ledger).open_collection_case(
            TENANT_ONE, invoice_draft_id
        )
        stored = issued_ledger.get_collection_case(issued_case.collection_case_id)
        issued_ledger.collection_cases[issued_case.collection_case_id] = replace(
            stored, outstanding_amount=Decimal("0")
        )
        issued_settled = CollectionCaseSettlementService(issued_ledger).settle_collection_case(
            TENANT_ONE, issued_case.collection_case_id
        )
        issued_presentment = CollectionCaseSettlementPresentmentService(
            issued_ledger
        ).present_collection_case_settlement(
            TENANT_ONE, issued_settled.collection_case_settlement_id
        )
        self.assertEqual(issued_presentment.issued_invoice_id, issued_invoice.issued_invoice_id)
        self.assertIn("issued_invoice_id", issued_presentment.as_contract_dict())
        app = create_http_app(issued_ledger)
        invalid_cursor_status, invalid_cursor_body = invoke_http(
            app,
            "GET",
            "/v1/collection-case-settlements",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(invalid_cursor_status, 422)
        self.assertEqual(invalid_cursor_body["rejection_reason_code"], "request_invalid")
        invalid_limit_status, invalid_limit_body = invoke_http(
            app,
            "GET",
            "/v1/collection-case-settlements",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(invalid_limit_status, 422)
        self.assertEqual(invalid_limit_body["rejection_reason_code"], "request_invalid")
        post_list_status, _post_list_body = invoke_http(
            app,
            "POST",
            "/v1/collection-case-settlements",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(post_list_status, 422)
        put_item_status, _put_item_body = invoke_http(
            app,
            "PUT",
            f"/v1/collection-case-settlements/{issued_settled.collection_case_settlement_id}",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(put_item_status, 422)
        open_ledger, invoice_draft_id = draft_known_morning()
        open_case = CollectionCaseService(open_ledger).open_collection_case(
            TENANT_ONE, invoice_draft_id
        )
        open_app = create_http_app(open_ledger)
        refused_status, refused_body = invoke_http(
            open_app,
            "POST",
            f"/v1/collection-cases/{open_case.collection_case_id}/settlements",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "outstanding_not_zero")
        with mock.patch(
            "metering_billing.http_app.CollectionCaseSettlementService.settle_collection_case",
            side_effect=ValueError("boom"),
        ):
            boom_status, boom_body = invoke_http(
                open_app,
                "POST",
                f"/v1/collection-cases/{open_case.collection_case_id}/settlements",
                {"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(boom_status, 422)
        self.assertEqual(boom_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.CollectionCaseSettlementPresentmentService.present_collection_case_settlement",
            side_effect=ValueError("boom"),
        ):
            get_boom_status, get_boom = invoke_http(
                app,
                "GET",
                f"/v1/collection-case-settlements/{issued_settled.collection_case_settlement_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(get_boom_status, 422)
        self.assertEqual(get_boom["rejection_reason_code"], "request_invalid")


if __name__ == "__main__":
    unittest.main()
