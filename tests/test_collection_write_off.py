"""Collection write-off tests for leftover uncollectable remaining."""

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
    CollectionCaseSettlementService,
    CollectionWriteOffPresentmentService,
    CollectionWriteOffService,
    IssuedInvoiceService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.contracts import (
    validate_collection_write_off,
    validate_collection_write_off_presentment,
)
from metering_billing.errors import (
    CollectionWriteOffOutcomeCode,
    CollectionWriteOffPresentmentQueryError,
    CollectionWriteOffRejectionReasonCode,
)
from metering_billing.collection_write_off import (
    CollectionWriteOffResult,
    _format_written_off_at,
    _rejected,
)
from metering_billing.usage_ledger import generate_record_id
from test_collection_case import draft_known_morning
from test_collection_case_settlement import open_morning_case_at_zero
from test_http_app import invoke_http
from test_tax_assessment import HUNDRED, insert_commercial_draft
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL, seed_rated_ledger


WRITTEN_OFF_MORNING = datetime(2026, 8, 18, 11, 0, tzinfo=UTC)
WRITTEN_OFF_EVENING = datetime(2026, 8, 18, 20, 0, tzinfo=UTC)
LEFTOVER = Decimal("0.001")


def open_morning_case_with_outstanding(outstanding: Decimal | None = None):
    """Open a morning case, optionally replacing remaining outstanding."""
    ledger, invoice_draft_id = draft_known_morning()
    collection = CollectionCaseService(ledger).open_collection_case(
        TENANT_ONE, invoice_draft_id
    )
    if outstanding is not None:
        stored = ledger.get_collection_case(collection.collection_case_id)
        ledger.collection_cases[collection.collection_case_id] = replace(
            stored, outstanding_amount=outstanding
        )
        collection = ledger.get_collection_case(collection.collection_case_id)
    return ledger, collection


class CollectionWriteOffTests(unittest.TestCase):
    """Verify write-off-once identity, exact remaining, and HTTP presentment."""

    def test_write_off_zeros_remaining_once_without_settling(self) -> None:
        """An open leftover writes off once and leaves the case unsettleable by this path."""
        ledger, collection = open_morning_case_with_outstanding()
        prior_journals = len(ledger.journal_proposals)
        prior_outbox = len(ledger.webhook_outbox_events)
        prior_receipts = len(ledger.payment_receipts)
        prior_settlements = len(ledger.collection_case_settlements)
        first = CollectionWriteOffService(
            ledger, clock=lambda: WRITTEN_OFF_MORNING
        ).write_off_collection_case(TENANT_ONE, collection.collection_case_id)
        second = CollectionWriteOffService(
            ledger, clock=lambda: WRITTEN_OFF_EVENING
        ).write_off_collection_case(TENANT_ONE, collection.collection_case_id)
        self.assertEqual(
            first.collection_write_off_outcome_code,
            CollectionWriteOffOutcomeCode.ACCEPTED,
        )
        self.assertEqual(
            second.collection_write_off_outcome_code,
            CollectionWriteOffOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertEqual(first.collection_write_off_id, second.collection_write_off_id)
        self.assertEqual(first.collection_case_id, collection.collection_case_id)
        self.assertEqual(first.invoice_draft_id, collection.invoice_draft_id)
        self.assertIsNone(first.issued_invoice_id)
        self.assertEqual(first.currency_code, "USD")
        self.assertEqual(first.write_off_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(first.remaining_outstanding_amount, Decimal("0"))
        self.assertEqual(first.collection_case_status, "open")
        self.assertEqual(first.written_off_at, WRITTEN_OFF_MORNING)
        self.assertEqual(second.written_off_at, WRITTEN_OFF_MORNING)
        self.assertEqual(first.collection_write_off_status, "recorded")
        self.assertEqual(first.next_operator_action, "settle")
        self.assertTrue(first.source_payload_hash.startswith("sha256:"))
        payload = first.as_contract_dict()
        self.assertEqual(validate_collection_write_off(payload), ())
        self.assertNotIn("card_pan", payload)
        self.assertNotIn("legal_invoice_number", payload)
        stored_case = ledger.get_collection_case(collection.collection_case_id)
        self.assertEqual(stored_case.outstanding_amount, Decimal("0"))
        self.assertEqual(stored_case.collection_case_status, "open")
        self.assertEqual(len(ledger.collection_write_offs), 1)
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        self.assertEqual(len(ledger.payment_receipts), prior_receipts)
        self.assertEqual(len(ledger.collection_case_settlements), prior_settlements)
        settled = CollectionCaseSettlementService(ledger).settle_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(settled.collection_case_settlement_outcome_code.value, "accepted")
        self.assertEqual(
            ledger.get_collection_case(collection.collection_case_id).collection_case_status,
            "settled",
        )
        self.assertEqual(len(ledger.collection_write_offs), 1)

    def test_optional_amount_must_equal_remaining(self) -> None:
        """Omitting amount writes off remaining; a matching amount is accepted."""
        ledger, collection = open_morning_case_with_outstanding(LEFTOVER)
        omitted = CollectionWriteOffService(ledger).write_off_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(omitted.write_off_amount, LEFTOVER)
        self.assertEqual(omitted.remaining_outstanding_amount, Decimal("0"))
        ledger_two, collection_two = open_morning_case_with_outstanding(LEFTOVER)
        matched = CollectionWriteOffService(ledger_two).write_off_collection_case(
            TENANT_ONE, collection_two.collection_case_id, write_off_amount=LEFTOVER
        )
        self.assertEqual(matched.write_off_amount, LEFTOVER)
        ledger_three, collection_three = open_morning_case_with_outstanding(LEFTOVER)
        refused = CollectionWriteOffService(ledger_three).write_off_collection_case(
            TENANT_ONE, collection_three.collection_case_id, write_off_amount=Decimal("1.00")
        )
        self.assertEqual(
            refused.rejection_reason_code,
            CollectionWriteOffRejectionReasonCode.WRITE_OFF_AMOUNT_MISMATCH,
        )
        self.assertEqual(len(ledger_three.collection_write_offs), 0)
        stored = ledger_three.get_collection_case(collection_three.collection_case_id)
        self.assertEqual(stored.outstanding_amount, LEFTOVER)

    def test_fail_closed_on_zero_negative_settled_and_currency(self) -> None:
        """Zero remaining, negative remaining, settled cases, and currency mismatch refuse."""
        zero_ledger, zero_case = open_morning_case_at_zero()
        zero = CollectionWriteOffService(zero_ledger).write_off_collection_case(
            TENANT_ONE, zero_case.collection_case_id
        )
        self.assertEqual(
            zero.rejection_reason_code,
            CollectionWriteOffRejectionReasonCode.OUTSTANDING_ALREADY_ZERO,
        )
        negative_ledger, negative_case = open_morning_case_with_outstanding(Decimal("-1"))
        negative = CollectionWriteOffService(negative_ledger).write_off_collection_case(
            TENANT_ONE, negative_case.collection_case_id
        )
        self.assertEqual(
            negative.rejection_reason_code,
            CollectionWriteOffRejectionReasonCode.OUTSTANDING_NEGATIVE,
        )
        settled_ledger, settled_case = open_morning_case_with_outstanding()
        settled_ledger.apply_collection_settlement(
            settled_case.collection_case_id, KNOWN_MORNING_TOTAL
        )
        settled = CollectionWriteOffService(settled_ledger).write_off_collection_case(
            TENANT_ONE, settled_case.collection_case_id
        )
        self.assertEqual(
            settled.rejection_reason_code,
            CollectionWriteOffRejectionReasonCode.COLLECTION_CASE_SETTLED,
        )
        currency_ledger, currency_case = open_morning_case_with_outstanding()
        currency = CollectionWriteOffService(currency_ledger).write_off_collection_case(
            TENANT_ONE, currency_case.collection_case_id, currency_code="EUR"
        )
        self.assertEqual(
            currency.rejection_reason_code,
            CollectionWriteOffRejectionReasonCode.CURRENCY_MISMATCH,
        )
        self.assertEqual(len(zero_ledger.collection_write_offs), 0)
        self.assertEqual(len(negative_ledger.collection_write_offs), 0)
        self.assertEqual(len(settled_ledger.collection_write_offs), 0)
        self.assertEqual(len(currency_ledger.collection_write_offs), 0)

    def test_unknown_and_cross_tenant_targets_are_rejected(self) -> None:
        """Missing tenant or case cannot invent a write-off."""
        ledger, collection = open_morning_case_with_outstanding()
        missing_tenant = CollectionWriteOffService(ledger).write_off_collection_case(
            "urn:cwl:missing", collection.collection_case_id
        )
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            CollectionWriteOffRejectionReasonCode.TENANT_NOT_FOUND,
        )
        missing_case = CollectionWriteOffService(ledger).write_off_collection_case(
            TENANT_ONE, uuid4()
        )
        self.assertEqual(
            missing_case.rejection_reason_code,
            CollectionWriteOffRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )
        crossed = CollectionWriteOffService(ledger).write_off_collection_case(
            TENANT_TWO, collection.collection_case_id
        )
        self.assertEqual(
            crossed.rejection_reason_code,
            CollectionWriteOffRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )
        self.assertEqual(len(ledger.collection_write_offs), 0)

    def test_issued_invoice_is_preserved_when_stored(self) -> None:
        """A stored issued invoice on the case draft is copied onto the write-off."""
        ledger = seed_rated_ledger()
        invoice_draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        issued_invoice = IssuedInvoiceService(ledger).issue_invoice(TENANT_ONE, invoice_draft_id)
        collection = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, invoice_draft_id
        )
        written = CollectionWriteOffService(ledger).write_off_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(written.issued_invoice_id, issued_invoice.issued_invoice_id)
        self.assertIn("issued_invoice_id", written.as_contract_dict())

    def test_resolver_hollow_success_raises_value_error(self) -> None:
        """A broken tenant resolve must raise ValueError, not assert."""
        ledger, collection = open_morning_case_with_outstanding()
        service = CollectionWriteOffService(ledger)
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.write_off_collection_case(TENANT_ONE, collection.collection_case_id)

    def test_http_write_off_get_and_paged_list_without_capture(self) -> None:
        """POST writes off; GET item and list page metadata and never capture payment."""
        ledger, first_case = open_morning_case_with_outstanding(LEFTOVER)
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
            f"/v1/collection-cases/{first_case.collection_case_id}/write-offs",
            {"tenant_reference": TENANT_ONE, "card_pan": "4111111111111111"},
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.collection_write_offs), 0)
        mismatch_status, mismatch_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/write-offs",
            {"tenant_reference": TENANT_ONE, "write_off_amount": "1.00"},
        )
        self.assertEqual(mismatch_status, 422)
        self.assertEqual(mismatch_body["rejection_reason_code"], "write_off_amount_mismatch")
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/write-offs",
            {
                "tenant_reference": TENANT_ONE,
                "write_off_amount": format_exact_decimal(LEFTOVER),
                "currency_code": "USD",
            },
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["collection_write_off_outcome_code"], "accepted")
        self.assertEqual(accepted_body["write_off_amount"], format_exact_decimal(LEFTOVER))
        self.assertEqual(accepted_body["remaining_outstanding_amount"], "0")
        self.assertEqual(accepted_body["collection_case_status"], "open")
        write_off_id = accepted_body["collection_write_off_id"]
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/write-offs",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["collection_write_off_outcome_code"], "duplicate_replay")
        self.assertEqual(replay_body["collection_write_off_id"], write_off_id)
        second_status, second_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{second_case.collection_case_id}/write-offs",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(second_body["collection_write_off_outcome_code"], "accepted")
        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-write-offs/{write_off_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["collection_write_off_id"], write_off_id)
        self.assertEqual(get_body["collection_case_id"], str(first_case.collection_case_id))
        self.assertNotIn("collection_write_off_outcome_code", get_body)
        self.assertEqual(validate_collection_write_off_presentment(get_body), ())
        header_status, header_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-write-offs/{write_off_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(header_status, 200)
        self.assertEqual(header_body, get_body)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/collection-write-offs",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"collection_write_offs", "next_cursor"})
        self.assertEqual(len(list_body["collection_write_offs"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        page_two_status, page_two = invoke_http(
            app,
            "GET",
            "/v1/collection-write-offs",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(page_two_status, 200)
        self.assertEqual(len(page_two["collection_write_offs"]), 1)
        self.assertIsNone(page_two["next_cursor"])
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/collection-write-offs",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["collection_write_offs"], [])
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-write-offs/{write_off_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "collection_write_off_not_found")
        missing_status, missing_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-write-offs/{write_off_id}",
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        method_status, _method_body = invoke_http(
            app,
            "PUT",
            f"/v1/collection-cases/{first_case.collection_case_id}/write-offs",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 405)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        self.assertEqual(len(ledger.payment_receipts), 0)
        self.assertEqual(len(ledger.collection_case_settlements), 0)

    def test_presentment_rejects_invalid_page_and_missing_rows(self) -> None:
        """Presentment fails closed on unreadable cursors and unknown ids."""
        ledger, collection = open_morning_case_with_outstanding(LEFTOVER)
        written = CollectionWriteOffService(ledger).write_off_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        presentment = CollectionWriteOffPresentmentService(ledger)
        with self.assertRaises(CollectionWriteOffPresentmentQueryError) as missing:
            presentment.present_collection_write_off(TENANT_ONE, uuid4())
        self.assertEqual(
            missing.exception.rejection_reason_code, "collection_write_off_not_found"
        )
        with self.assertRaises(CollectionWriteOffPresentmentQueryError) as bad_cursor:
            presentment.list_collection_write_offs(TENANT_ONE, cursor="not-a-cursor")
        self.assertEqual(bad_cursor.exception.rejection_reason_code, "request_invalid")
        with self.assertRaises(CollectionWriteOffPresentmentQueryError) as bad_limit:
            presentment.list_collection_write_offs(TENANT_ONE, page_limit=0)
        self.assertEqual(bad_limit.exception.rejection_reason_code, "request_invalid")
        page = presentment.list_collection_write_offs(TENANT_ONE)
        self.assertEqual(len(page.collection_write_offs), 1)
        self.assertEqual(
            page.collection_write_offs[0].collection_write_off_id,
            written.collection_write_off_id,
        )

    def test_contract_and_ledger_guards_cover_identity(self) -> None:
        """Accepted write-offs need identity; ledger rows stay append-only."""
        valid = {
            "collection_write_off_contract_version": 1,
            "collection_write_off_outcome_code": "accepted",
            "collection_write_off_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd60",
            "tenant_reference": TENANT_ONE,
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd21",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "currency_code": "USD",
            "write_off_amount": "0.001",
            "remaining_outstanding_amount": "0",
            "collection_write_off_status": "recorded",
            "collection_case_status": "open",
            "written_off_at": "2026-08-18T11:00:00Z",
            "source_payload_hash": "sha256:" + "d" * 64,
            "next_operator_action": "settle",
        }
        self.assertEqual(validate_collection_write_off(valid), ())
        rejected = {
            "collection_write_off_contract_version": 1,
            "collection_write_off_outcome_code": "rejected",
            "rejection_reason_code": "outstanding_already_zero",
        }
        self.assertEqual(validate_collection_write_off(rejected), ())
        missing_id = json.loads(json.dumps(valid))
        del missing_id["collection_write_off_id"]
        self.assertTrue(validate_collection_write_off(missing_id))
        self.assertTrue(validate_collection_write_off(["not-an-object"]))
        ledger, collection = open_morning_case_with_outstanding(LEFTOVER)
        written = CollectionWriteOffService(ledger).write_off_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        stored = ledger.get_collection_write_off(written.collection_write_off_id)
        with self.assertRaises(ValueError):
            ledger.insert_collection_write_off(stored)
        colliding = replace(stored, collection_write_off_id=generate_record_id())
        with self.assertRaises(ValueError):
            ledger.insert_collection_write_off(colliding)
        with self.assertRaises(ValueError):
            ledger.insert_collection_write_off(
                replace(stored, collection_write_off_status="settled")
            )
        with self.assertRaises(ValueError):
            ledger.insert_collection_write_off(
                replace(
                    stored,
                    collection_write_off_id=generate_record_id(),
                    collection_case_id=generate_record_id(),
                    remaining_outstanding_amount=Decimal("1"),
                )
            )
        with self.assertRaises(ValueError):
            ledger.insert_collection_write_off(
                replace(
                    stored,
                    collection_write_off_id=generate_record_id(),
                    collection_case_id=generate_record_id(),
                    write_off_amount=Decimal("0"),
                )
            )
        with self.assertRaises(ValueError):
            ledger.apply_collection_write_off(uuid4(), LEFTOVER)
        with self.assertRaises(ValueError):
            ledger.apply_collection_write_off(collection.collection_case_id, Decimal("0"))
        leftover_ledger, leftover_case = open_morning_case_with_outstanding(LEFTOVER)
        with self.assertRaises(ValueError):
            leftover_ledger.apply_collection_write_off(
                leftover_case.collection_case_id, Decimal("1.00")
            )
        settled_ledger, settled_case = open_morning_case_with_outstanding()
        settled_ledger.apply_collection_settlement(
            settled_case.collection_case_id, KNOWN_MORNING_TOTAL
        )
        with self.assertRaises(ValueError):
            settled_ledger.apply_collection_write_off(
                settled_case.collection_case_id, Decimal("0")
            )
        missing_remaining = json.loads(json.dumps(valid))
        del missing_remaining["remaining_outstanding_amount"]
        self.assertTrue(validate_collection_write_off(missing_remaining))
        unknown_outcome = {
            "collection_write_off_contract_version": 1,
            "collection_write_off_outcome_code": "posted",
        }
        self.assertTrue(validate_collection_write_off(unknown_outcome))
        missing_outcome = {"collection_write_off_contract_version": 1}
        self.assertTrue(validate_collection_write_off(missing_outcome))
        legal = json.loads(json.dumps(valid))
        legal["legal_invoice_number"] = "INV-1"
        self.assertTrue(validate_collection_write_off(legal))
        rejected_legal = {
            "collection_write_off_contract_version": 1,
            "collection_write_off_outcome_code": "rejected",
            "rejection_reason_code": "outstanding_already_zero",
            "legal_invoice_number": "INV-1",
        }
        self.assertTrue(validate_collection_write_off(rejected_legal))
        rejected_missing_reason = {
            "collection_write_off_contract_version": 1,
            "collection_write_off_outcome_code": "rejected",
        }
        self.assertTrue(validate_collection_write_off(rejected_missing_reason))
        nonzero = json.loads(json.dumps(valid))
        nonzero["remaining_outstanding_amount"] = "1.00"
        self.assertTrue(validate_collection_write_off(nonzero))
        bad_remaining = json.loads(json.dumps(valid))
        bad_remaining["remaining_outstanding_amount"] = "not-decimal"
        self.assertTrue(validate_collection_write_off(bad_remaining))
        int_remaining = json.loads(json.dumps(valid))
        int_remaining["remaining_outstanding_amount"] = 0
        self.assertTrue(validate_collection_write_off(int_remaining))
        missing_amount = json.loads(json.dumps(valid))
        del missing_amount["write_off_amount"]
        self.assertTrue(validate_collection_write_off(missing_amount))
        zero_amount = json.loads(json.dumps(valid))
        zero_amount["write_off_amount"] = "0"
        self.assertTrue(validate_collection_write_off(zero_amount))
        bad_amount = json.loads(json.dumps(valid))
        bad_amount["write_off_amount"] = 1
        self.assertTrue(validate_collection_write_off(bad_amount))

    def test_coverage_guards_for_write_off_and_presentment_edges(self) -> None:
        """Closed branches stay explicit: replay, presentment, constructors, and HTTP."""
        ledger, collection = open_morning_case_with_outstanding(LEFTOVER)
        written = CollectionWriteOffService(ledger).write_off_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        presentment = CollectionWriteOffPresentmentService(ledger)
        presented = presentment.present_collection_write_off(
            TENANT_ONE, written.collection_write_off_id
        )
        presentment_payload = presented.as_contract_dict()
        with self.assertRaises(CollectionWriteOffPresentmentQueryError):
            presentment.list_collection_write_offs(TENANT_ONE, page_limit=True)
        with self.assertRaises(CollectionWriteOffPresentmentQueryError):
            presentment.list_collection_write_offs(TENANT_ONE, page_limit="abc")
        with self.assertRaises(CollectionWriteOffPresentmentQueryError):
            presentment.list_collection_write_offs(TENANT_ONE, page_limit=101)
        with self.assertRaises(CollectionWriteOffPresentmentQueryError):
            presentment.list_collection_write_offs(TENANT_ONE, page_limit=1.5)
        default_page = presentment.list_collection_write_offs(TENANT_ONE, page_limit="")
        self.assertEqual(len(default_page.collection_write_offs), 1)
        with self.assertRaises(CollectionWriteOffPresentmentQueryError) as missing_tenant:
            presentment.present_collection_write_off(
                "urn:cwl:missing", written.collection_write_off_id
            )
        self.assertEqual(missing_tenant.exception.rejection_reason_code, "tenant_not_found")
        mutated = ledger.get_collection_case(collection.collection_case_id)
        ledger.collection_cases[collection.collection_case_id] = replace(
            mutated, outstanding_amount=Decimal("1.00")
        )
        mutated_presentment = presentment.present_collection_write_off(
            TENANT_ONE, written.collection_write_off_id
        )
        self.assertEqual(mutated_presentment.remaining_outstanding_amount, Decimal("1.00"))
        mutated_replay = CollectionWriteOffService(ledger).write_off_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(mutated_replay.remaining_outstanding_amount, Decimal("1.00"))
        del ledger.collection_cases[collection.collection_case_id]
        missing_replay = CollectionWriteOffService(ledger).write_off_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(
            missing_replay.rejection_reason_code,
            CollectionWriteOffRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )
        with self.assertRaises(CollectionWriteOffPresentmentQueryError) as missing_case:
            presentment.present_collection_write_off(
                TENANT_ONE, written.collection_write_off_id
            )
        self.assertEqual(
            missing_case.exception.rejection_reason_code,
            "collection_write_off_not_found",
        )
        with mock.patch.object(presentment.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                presentment.present_collection_write_off(
                    TENANT_ONE, written.collection_write_off_id
                )
        CollectionWriteOffService()
        CollectionWriteOffPresentmentService()
        with self.assertRaises(ValueError):
            _format_written_off_at(None)
        unsupported = replace(
            _rejected(CollectionWriteOffRejectionReasonCode.COLLECTION_CASE_NOT_FOUND),
            collection_write_off_outcome_code="posted",
        )
        with self.assertRaises(ValueError):
            unsupported.as_contract_dict()
        accepted_without_time = CollectionWriteOffResult(
            collection_write_off_outcome_code=CollectionWriteOffOutcomeCode.ACCEPTED,
            collection_write_off_contract_version=1,
            collection_write_off_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            issued_invoice_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            write_off_amount=LEFTOVER,
            remaining_outstanding_amount=Decimal("0"),
            collection_write_off_status="recorded",
            collection_case_status="open",
            written_off_at=None,
            source_payload_hash="sha256:" + "e" * 64,
            next_operator_action="settle",
            rejection_reason_code=None,
        )
        with self.assertRaises(ValueError):
            accepted_without_time.as_contract_dict()
        none_reason = CollectionWriteOffResult(
            collection_write_off_outcome_code=CollectionWriteOffOutcomeCode.REJECTED,
            collection_write_off_contract_version=1,
            collection_write_off_id=None,
            collection_case_id=None,
            invoice_draft_id=None,
            issued_invoice_id=None,
            tenant_reference=None,
            currency_code=None,
            write_off_amount=None,
            remaining_outstanding_amount=None,
            collection_write_off_status=None,
            collection_case_status=None,
            written_off_at=None,
            source_payload_hash=None,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        self.assertEqual(
            none_reason.as_contract_dict()["rejection_reason_code"],
            "collection_case_not_found",
        )
        string_rejected = CollectionWriteOffResult(
            collection_write_off_outcome_code="rejected",
            collection_write_off_contract_version=1,
            collection_write_off_id=None,
            collection_case_id=None,
            invoice_draft_id=None,
            issued_invoice_id=None,
            tenant_reference=None,
            currency_code=None,
            write_off_amount=None,
            remaining_outstanding_amount=None,
            collection_write_off_status=None,
            collection_case_status=None,
            written_off_at=None,
            source_payload_hash=None,
            next_operator_action="wait",
            rejection_reason_code=CollectionWriteOffRejectionReasonCode.OUTSTANDING_ALREADY_ZERO,
        )
        self.assertEqual(
            string_rejected.as_contract_dict()["rejection_reason_code"],
            "outstanding_already_zero",
        )
        string_accepted = CollectionWriteOffResult(
            collection_write_off_outcome_code="accepted",
            collection_write_off_contract_version=1,
            collection_write_off_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            issued_invoice_id=generate_record_id(),
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            write_off_amount=LEFTOVER,
            remaining_outstanding_amount=Decimal("0"),
            collection_write_off_status="recorded",
            collection_case_status="open",
            written_off_at=WRITTEN_OFF_MORNING,
            source_payload_hash="sha256:" + "f" * 64,
            next_operator_action="settle",
            rejection_reason_code=None,
        )
        self.assertEqual(
            string_accepted.as_contract_dict()["collection_write_off_outcome_code"],
            "accepted",
        )
        self.assertIn("issued_invoice_id", string_accepted.as_contract_dict())
        self.assertEqual(validate_collection_write_off_presentment(presentment_payload), ())
        self.assertIn("collection_write_off_id", presented.as_summary_dict())
        missing_presentment_remaining = json.loads(json.dumps(presentment_payload))
        del missing_presentment_remaining["remaining_outstanding_amount"]
        self.assertTrue(
            validate_collection_write_off_presentment(missing_presentment_remaining)
        )
        nonzero_presentment = json.loads(json.dumps(presentment_payload))
        nonzero_presentment["remaining_outstanding_amount"] = "1.00"
        nonzero_presentment["next_operator_action"] = "settle"
        self.assertTrue(validate_collection_write_off_presentment(nonzero_presentment))
        wait_mismatch = json.loads(json.dumps(presentment_payload))
        wait_mismatch["remaining_outstanding_amount"] = "0"
        wait_mismatch["next_operator_action"] = "wait"
        self.assertTrue(validate_collection_write_off_presentment(wait_mismatch))
        forbidden_presentment = json.loads(json.dumps(presentment_payload))
        forbidden_presentment["legal_invoice_number"] = "INV-1"
        self.assertTrue(validate_collection_write_off_presentment(forbidden_presentment))
        self.assertTrue(validate_collection_write_off_presentment(["not-an-object"]))
        float_remaining = json.loads(json.dumps(presentment_payload))
        float_remaining["remaining_outstanding_amount"] = 0
        self.assertTrue(validate_collection_write_off_presentment(float_remaining))
        bad_presentment_remaining = json.loads(json.dumps(presentment_payload))
        bad_presentment_remaining["remaining_outstanding_amount"] = "not-decimal"
        self.assertTrue(
            validate_collection_write_off_presentment(bad_presentment_remaining)
        )
        missing_applied = json.loads(json.dumps(presentment_payload))
        del missing_applied["write_off_amount"]
        self.assertTrue(validate_collection_write_off_presentment(missing_applied))
        zero_applied = json.loads(json.dumps(presentment_payload))
        zero_applied["write_off_amount"] = "0"
        self.assertTrue(validate_collection_write_off_presentment(zero_applied))
        unreadable_applied = json.loads(json.dumps(presentment_payload))
        unreadable_applied["write_off_amount"] = "nope"
        self.assertTrue(validate_collection_write_off_presentment(unreadable_applied))
        int_applied = json.loads(json.dumps(presentment_payload))
        int_applied["write_off_amount"] = 1
        self.assertTrue(validate_collection_write_off_presentment(int_applied))
        currency_ledger, currency_case = open_morning_case_with_outstanding(LEFTOVER)
        same_currency = CollectionWriteOffService(currency_ledger).write_off_collection_case(
            TENANT_ONE, currency_case.collection_case_id, currency_code="USD"
        )
        self.assertEqual(same_currency.collection_write_off_outcome_code.value, "accepted")
        app = create_http_app(currency_ledger)
        bad_amount_status, bad_amount_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{currency_case.collection_case_id}/write-offs",
            {"tenant_reference": TENANT_ONE, "write_off_amount": 1},
        )
        self.assertEqual(bad_amount_status, 422)
        self.assertEqual(bad_amount_body["rejection_reason_code"], "request_invalid")
        collection_method_status, _collection_method = invoke_http(
            app,
            "POST",
            "/v1/collection-write-offs",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(collection_method_status, 422)
        item_method_status, _item_method = invoke_http(
            app,
            "PUT",
            f"/v1/collection-write-offs/{written.collection_write_off_id}",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(item_method_status, 422)
        with self.assertRaises(CollectionWriteOffPresentmentQueryError) as list_missing:
            presentment.list_collection_write_offs("urn:cwl:missing")
        self.assertEqual(list_missing.exception.rejection_reason_code, "tenant_not_found")
        empty = CollectionWriteOffService()
        self.assertEqual(
            empty.write_off_collection_case(TENANT_ONE, uuid4()).rejection_reason_code,
            CollectionWriteOffRejectionReasonCode.TENANT_NOT_FOUND,
        )


if __name__ == "__main__":
    unittest.main()
