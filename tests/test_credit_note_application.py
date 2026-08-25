"""Credit-note application tests for reducing collection outstanding."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import UUID, uuid4

from metering_billing import (
    CollectionCaseService,
    CreditAdjustmentService,
    CreditNoteApplicationPresentmentService,
    CreditNoteApplicationService,
    IssuedCreditNoteService,
    IssuedInvoiceService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.contracts import (
    validate_credit_note_application,
    validate_credit_note_application_presentment,
)
from metering_billing.credit_note_application import (
    CreditNoteApplicationResult,
    _enqueue_credit_note_applied,
    _format_applied_at,
    _rejected,
)
from metering_billing.webhook_outbox import EVENT_TYPE_CREDIT_NOTE_APPLIED
from metering_billing.errors import (
    CreditNoteApplicationOutcomeCode,
    CreditNoteApplicationPresentmentQueryError,
    CreditNoteApplicationRejectionReasonCode,
)
from metering_billing.usage_ledger import generate_record_id
from test_collection_case import draft_known_morning
from test_http_app import invoke_http
from test_issued_credit_note import record_known_morning_credit
from test_tax_assessment import HUNDRED, insert_commercial_draft
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL, seed_rated_ledger


APPLIED_MORNING = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)
APPLIED_EVENING = datetime(2026, 8, 18, 18, 0, tzinfo=UTC)
PARTIAL_CREDIT = Decimal("0.001")


def issue_morning_credit_then_open_case():
    """Record and issue a morning credit, then open the full-outstanding case."""
    ledger, credit = record_known_morning_credit()
    issued = IssuedCreditNoteService(ledger).issue_credit_note(
        TENANT_ONE, credit.credit_adjustment_id
    )
    collection = CollectionCaseService(ledger).open_collection_case(
        TENANT_ONE, credit.invoice_draft_id
    )
    return ledger, issued, collection


class CreditNoteApplicationTests(unittest.TestCase):
    """Verify apply-once identity, exact outstanding math, and HTTP presentment."""

    def test_apply_reduces_outstanding_by_inclusive_amount_once(self) -> None:
        """An issued credit note applied to a later case reduces net collectible money."""
        ledger, issued, collection = issue_morning_credit_then_open_case()
        prior_journals = len(ledger.journal_proposals)
        prior_outbox = len(ledger.webhook_outbox_events)
        self.assertEqual(collection.outstanding_amount, KNOWN_MORNING_TOTAL)
        first = CreditNoteApplicationService(
            ledger, clock=lambda: APPLIED_MORNING
        ).apply_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, collection.collection_case_id
        )
        second = CreditNoteApplicationService(
            ledger, clock=lambda: APPLIED_EVENING
        ).apply_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, collection.collection_case_id
        )
        self.assertEqual(
            first.credit_note_application_outcome_code,
            CreditNoteApplicationOutcomeCode.ACCEPTED,
        )
        self.assertEqual(
            second.credit_note_application_outcome_code,
            CreditNoteApplicationOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertEqual(first.credit_note_application_id, second.credit_note_application_id)
        self.assertEqual(first.issued_credit_note_id, issued.issued_credit_note_id)
        self.assertEqual(first.collection_case_id, collection.collection_case_id)
        self.assertEqual(first.invoice_draft_id, issued.invoice_draft_id)
        self.assertIsNone(first.issued_invoice_id)
        self.assertEqual(first.currency_code, "USD")
        self.assertEqual(first.applied_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(first.remaining_outstanding_amount, Decimal("0"))
        self.assertEqual(first.collection_case_status, "settled")
        self.assertEqual(first.applied_at, APPLIED_MORNING)
        self.assertEqual(second.applied_at, APPLIED_MORNING)
        self.assertEqual(first.credit_note_application_status, "applied")
        self.assertTrue(first.source_payload_hash.startswith("sha256:"))
        payload = first.as_contract_dict()
        self.assertEqual(validate_credit_note_application(payload), ())
        self.assertNotIn("card_pan", payload)
        self.assertNotIn("credit_note_number", payload)
        stored_case = ledger.get_collection_case(collection.collection_case_id)
        self.assertEqual(stored_case.outstanding_amount, Decimal("0"))
        self.assertEqual(stored_case.collection_case_status, "settled")
        self.assertEqual(len(ledger.credit_note_applications), 1)
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox + 1)
        applied_events = [
            event
            for event in ledger.webhook_outbox_events.values()
            if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_APPLIED
        ]
        self.assertEqual(len(applied_events), 1)
        self.assertEqual(applied_events[0].source_id, first.credit_note_application_id)
        envelope = json.loads(applied_events[0].payload_json)
        self.assertEqual(envelope["event_type_code"], EVENT_TYPE_CREDIT_NOTE_APPLIED)
        data = envelope["data"]
        self.assertEqual(data["credit_note_application_id"], str(first.credit_note_application_id))
        self.assertEqual(data["issued_credit_note_id"], str(first.issued_credit_note_id))
        self.assertEqual(data["collection_case_id"], str(first.collection_case_id))
        self.assertEqual(data["invoice_draft_id"], str(first.invoice_draft_id))
        self.assertEqual(data["source_payload_hash"], first.source_payload_hash)
        self.assertEqual(data["credit_note_application_contract_version"], 1)
        self.assertEqual(
            data["issued_credit_note_contract_version"],
            first.issued_credit_note_contract_version,
        )
        self.assertEqual(
            data["issued_credit_note_source_payload_hash"],
            first.issued_credit_note_source_payload_hash,
        )
        self.assertEqual(data["currency_code"], "USD")
        self.assertEqual(data["applied_amount"], format_exact_decimal(KNOWN_MORNING_TOTAL))
        self.assertEqual(data["credit_note_application_status"], "applied")
        self.assertEqual(data["applied_at"], first.as_contract_dict()["applied_at"])
        self.assertNotIn("issued_invoice_id", data)
        self.assertNotIn("remaining_outstanding_amount", data)
        self.assertNotIn("collection_case_status", data)
        self.assertNotIn("card_pan", json.dumps(envelope))
        self.assertNotIn("credit_note_number", json.dumps(envelope))
        self.assertNotIn("legal_credit_note_number", json.dumps(envelope))
        self.assertNotIn("webhook_secret", json.dumps(envelope))
        webhook_data = first.as_webhook_event_data()
        self.assertEqual(webhook_data["applied_amount"], format_exact_decimal(KNOWN_MORNING_TOTAL))
        self.assertNotIn("remaining_outstanding_amount", webhook_data)
        self.assertNotIn("credit_note_application_outcome_code", webhook_data)
        self.assertNotIn("next_operator_action", webhook_data)
        self.assertEqual(issued.issued_credit_note_status, "issued")
        self.assertEqual(len(ledger.issued_credit_notes), 1)

    def test_insert_identity_replay_does_not_reduce_outstanding_again(self) -> None:
        """A concurrent insert race is duplicate_replay and never double-reduces."""
        ledger, invoice_draft_id = draft_known_morning()
        credit = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, PARTIAL_CREDIT, "goodwill"
        )
        issued = IssuedCreditNoteService(ledger).issue_credit_note(
            TENANT_ONE, credit.credit_adjustment_id
        )
        collection = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, invoice_draft_id
        )
        first = CreditNoteApplicationService(
            ledger, clock=lambda: APPLIED_MORNING
        ).apply_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, collection.collection_case_id
        )
        remaining = ledger.get_collection_case(collection.collection_case_id).outstanding_amount

        class BlindFindLedger:
            """Force the insert path used after a concurrent identity race."""

            def __init__(self, inner: object) -> None:
                self._inner = inner

            def find_credit_note_application(self, *args, **kwargs):
                return None

            def insert_credit_note_application(self, row):
                return self._inner.find_credit_note_application(
                    row.tenant_account_id, row.issued_credit_note_id
                )

            def __getattr__(self, name: str):
                return getattr(self._inner, name)

        raced = CreditNoteApplicationService(
            BlindFindLedger(ledger), clock=lambda: APPLIED_EVENING
        ).apply_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, collection.collection_case_id
        )
        self.assertEqual(
            raced.credit_note_application_outcome_code,
            CreditNoteApplicationOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertEqual(raced.credit_note_application_id, first.credit_note_application_id)
        self.assertEqual(
            ledger.get_collection_case(collection.collection_case_id).outstanding_amount,
            remaining,
        )
        self.assertEqual(len(ledger.credit_note_applications), 1)

    def test_partial_credit_leaves_residual_outstanding(self) -> None:
        """A smaller issued credit reduces outstanding without inventing a journal."""
        ledger, invoice_draft_id = draft_known_morning()
        credit = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, PARTIAL_CREDIT, "goodwill"
        )
        issued = IssuedCreditNoteService(ledger).issue_credit_note(
            TENANT_ONE, credit.credit_adjustment_id
        )
        collection = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, invoice_draft_id
        )
        applied = CreditNoteApplicationService(ledger).apply_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, collection.collection_case_id
        )
        self.assertEqual(
            applied.credit_note_application_outcome_code,
            CreditNoteApplicationOutcomeCode.ACCEPTED,
        )
        self.assertEqual(applied.applied_amount, PARTIAL_CREDIT)
        self.assertEqual(
            applied.remaining_outstanding_amount, KNOWN_MORNING_TOTAL - PARTIAL_CREDIT
        )
        self.assertEqual(applied.collection_case_status, "open")
        self.assertEqual(applied.next_operator_action, "collect")
        applied_events = [
            event
            for event in ledger.webhook_outbox_events.values()
            if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_APPLIED
        ]
        self.assertEqual(len(applied_events), 1)
        self.assertEqual(applied_events[0].source_id, applied.credit_note_application_id)
        envelope = json.loads(applied_events[0].payload_json)
        self.assertEqual(
            envelope["data"]["applied_amount"], format_exact_decimal(PARTIAL_CREDIT)
        )
        self.assertNotIn("remaining_outstanding_amount", envelope["data"])
        self.assertNotIn("collection.settled", json.dumps(envelope))

    def test_fail_closed_when_case_settled_or_outstanding_would_go_negative(self) -> None:
        """Settled cases and over-application write zero application rows."""
        ledger, issued, collection = issue_morning_credit_then_open_case()
        ledger.apply_collection_settlement(collection.collection_case_id, KNOWN_MORNING_TOTAL)
        settled = CreditNoteApplicationService(ledger).apply_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, collection.collection_case_id
        )
        self.assertEqual(
            settled.credit_note_application_outcome_code,
            CreditNoteApplicationOutcomeCode.REJECTED,
        )
        self.assertEqual(
            settled.rejection_reason_code,
            CreditNoteApplicationRejectionReasonCode.COLLECTION_CASE_SETTLED,
        )
        ledger_two, issued_two, collection_two = issue_morning_credit_then_open_case()
        ledger_two.apply_collection_settlement(collection_two.collection_case_id, PARTIAL_CREDIT)
        exceeded = CreditNoteApplicationService(ledger_two).apply_credit_note(
            TENANT_ONE, issued_two.issued_credit_note_id, collection_two.collection_case_id
        )
        self.assertEqual(
            exceeded.rejection_reason_code,
            CreditNoteApplicationRejectionReasonCode.CREDIT_EXCEEDS_OUTSTANDING,
        )
        self.assertEqual(len(ledger.credit_note_applications), 0)
        self.assertEqual(len(ledger_two.credit_note_applications), 0)
        self.assertEqual(
            len(
                [
                    event
                    for event in ledger.webhook_outbox_events.values()
                    if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_APPLIED
                ]
            ),
            0,
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in ledger_two.webhook_outbox_events.values()
                    if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_APPLIED
                ]
            ),
            0,
        )

    def test_fail_closed_on_currency_and_invoice_mismatch(self) -> None:
        """A credit cannot reduce another draft or a different currency case."""
        ledger, issued, collection = issue_morning_credit_then_open_case()
        foreign_draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("20.00"))
        foreign_case = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, foreign_draft_id
        )
        mismatched = CreditNoteApplicationService(ledger).apply_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, foreign_case.collection_case_id
        )
        self.assertEqual(
            mismatched.rejection_reason_code,
            CreditNoteApplicationRejectionReasonCode.INVOICE_MISMATCH,
        )
        mutated = replace(ledger.collection_cases[collection.collection_case_id], currency_code="EUR")
        ledger.collection_cases[collection.collection_case_id] = mutated
        currency = CreditNoteApplicationService(ledger).apply_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, collection.collection_case_id
        )
        self.assertEqual(
            currency.rejection_reason_code,
            CreditNoteApplicationRejectionReasonCode.CURRENCY_MISMATCH,
        )
        self.assertEqual(len(ledger.credit_note_applications), 0)

    def test_issued_invoice_must_match_the_case_draft_when_stored(self) -> None:
        """A stored issued_invoice_id must be the case draft's issued invoice."""
        ledger = seed_rated_ledger()
        invoice_draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        issued_invoice = IssuedInvoiceService(ledger).issue_invoice(TENANT_ONE, invoice_draft_id)
        credit = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, Decimal("11.00"), "goodwill"
        )
        issued = IssuedCreditNoteService(ledger).issue_credit_note(
            TENANT_ONE, credit.credit_adjustment_id
        )
        self.assertEqual(issued.issued_invoice_id, issued_invoice.issued_invoice_id)
        other_draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("20.00"))
        other_case = CollectionCaseService(ledger).open_collection_case(TENANT_ONE, other_draft_id)
        mismatched = CreditNoteApplicationService(ledger).apply_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, other_case.collection_case_id
        )
        self.assertEqual(
            mismatched.rejection_reason_code,
            CreditNoteApplicationRejectionReasonCode.INVOICE_MISMATCH,
        )
        same_case = CollectionCaseService(ledger).open_collection_case(TENANT_ONE, invoice_draft_id)
        applied = CreditNoteApplicationService(ledger).apply_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, same_case.collection_case_id
        )
        self.assertEqual(
            applied.credit_note_application_outcome_code,
            CreditNoteApplicationOutcomeCode.ACCEPTED,
        )
        self.assertEqual(applied.issued_invoice_id, issued_invoice.issued_invoice_id)

    def test_unknown_and_cross_tenant_targets_are_rejected(self) -> None:
        """Missing tenant, note, or case cannot invent an application."""
        ledger, issued, collection = issue_morning_credit_then_open_case()
        missing_tenant = CreditNoteApplicationService(ledger).apply_credit_note(
            "urn:cwl:missing", issued.issued_credit_note_id, collection.collection_case_id
        )
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            CreditNoteApplicationRejectionReasonCode.TENANT_NOT_FOUND,
        )
        missing_note = CreditNoteApplicationService(ledger).apply_credit_note(
            TENANT_ONE, uuid4(), collection.collection_case_id
        )
        self.assertEqual(
            missing_note.rejection_reason_code,
            CreditNoteApplicationRejectionReasonCode.ISSUED_CREDIT_NOTE_NOT_FOUND,
        )
        missing_case = CreditNoteApplicationService(ledger).apply_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, uuid4()
        )
        self.assertEqual(
            missing_case.rejection_reason_code,
            CreditNoteApplicationRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )
        crossed = CreditNoteApplicationService(ledger).apply_credit_note(
            TENANT_TWO, issued.issued_credit_note_id, collection.collection_case_id
        )
        self.assertIn(
            crossed.rejection_reason_code,
            {
                CreditNoteApplicationRejectionReasonCode.ISSUED_CREDIT_NOTE_NOT_FOUND,
                CreditNoteApplicationRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
            },
        )
        self.assertEqual(len(ledger.credit_note_applications), 0)

    def test_resolver_hollow_success_raises_value_error(self) -> None:
        """A broken tenant resolve must raise ValueError, not assert."""
        ledger, issued, collection = issue_morning_credit_then_open_case()
        service = CreditNoteApplicationService(ledger)
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.apply_credit_note(
                    TENANT_ONE, issued.issued_credit_note_id, collection.collection_case_id
                )

    def test_http_apply_get_and_paged_list_without_capture(self) -> None:
        """POST applies; GET item and list page metadata and never capture payment."""
        ledger, first_issued, first_case = issue_morning_credit_then_open_case()
        second_draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("20.00"))
        second_credit = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, second_draft_id, Decimal("20.00"), "billing_error"
        )
        second_issued = IssuedCreditNoteService(ledger).issue_credit_note(
            TENANT_ONE, second_credit.credit_adjustment_id
        )
        second_case = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, second_draft_id
        )
        prior_outbox = len(ledger.webhook_outbox_events)
        prior_journals = len(ledger.journal_proposals)
        app = create_http_app(ledger)
        refused_status, refused_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/credit-note-applications",
            {
                "tenant_reference": TENANT_ONE,
                "issued_credit_note_id": str(first_issued.issued_credit_note_id),
                "card_pan": "4111111111111111",
            },
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.credit_note_applications), 0)
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/credit-note-applications",
            {
                "tenant_reference": TENANT_ONE,
                "issued_credit_note_id": str(first_issued.issued_credit_note_id),
            },
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["credit_note_application_outcome_code"], "accepted")
        self.assertEqual(
            accepted_body["applied_amount"], format_exact_decimal(KNOWN_MORNING_TOTAL)
        )
        self.assertEqual(accepted_body["remaining_outstanding_amount"], "0")
        application_id = accepted_body["credit_note_application_id"]
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/credit-note-applications",
            {
                "tenant_reference": TENANT_ONE,
                "issued_credit_note_id": str(first_issued.issued_credit_note_id),
            },
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["credit_note_application_outcome_code"], "duplicate_replay")
        self.assertEqual(replay_body["credit_note_application_id"], application_id)
        second_status, second_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{second_case.collection_case_id}/credit-note-applications",
            {
                "tenant_reference": TENANT_ONE,
                "issued_credit_note_id": str(second_issued.issued_credit_note_id),
            },
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(second_body["credit_note_application_outcome_code"], "accepted")
        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/credit-note-applications/{application_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["credit_note_application_id"], application_id)
        self.assertEqual(
            get_body["issued_credit_note_id"], str(first_issued.issued_credit_note_id)
        )
        self.assertNotIn("credit_note_application_outcome_code", get_body)
        self.assertEqual(validate_credit_note_application_presentment(get_body), ())
        header_status, header_body = invoke_http(
            app,
            "GET",
            f"/v1/credit-note-applications/{application_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(header_status, 200)
        self.assertEqual(header_body, get_body)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/credit-note-applications",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"credit_note_applications", "next_cursor"})
        self.assertEqual(len(list_body["credit_note_applications"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        page_two_status, page_two = invoke_http(
            app,
            "GET",
            "/v1/credit-note-applications",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(page_two_status, 200)
        self.assertEqual(len(page_two["credit_note_applications"]), 1)
        self.assertIsNone(page_two["next_cursor"])
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/credit-note-applications",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["credit_note_applications"], [])
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/credit-note-applications/{application_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "credit_note_application_not_found")
        missing_status, missing_body = invoke_http(
            app,
            "GET",
            f"/v1/credit-note-applications/{application_id}",
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        method_status, method_body = invoke_http(
            app,
            "PUT",
            f"/v1/collection-cases/{first_case.collection_case_id}/credit-note-applications",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 405)
        collection_method_status, _collection_method = invoke_http(
            app,
            "POST",
            "/v1/credit-note-applications",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(collection_method_status, 422)
        item_method_status, _item_method = invoke_http(
            app,
            "PUT",
            f"/v1/credit-note-applications/{application_id}",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(item_method_status, 422)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox + 2)
        self.assertEqual(
            len(
                [
                    event
                    for event in ledger.webhook_outbox_events.values()
                    if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_APPLIED
                ]
            ),
            2,
        )
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        self.assertEqual(len(ledger.payment_receipts), 0)
        invalid_cursor_status, invalid_cursor = invoke_http(
            app,
            "GET",
            "/v1/credit-note-applications",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(invalid_cursor_status, 422)
        self.assertEqual(invalid_cursor["rejection_reason_code"], "request_invalid")
        invalid_limit_status, invalid_limit = invoke_http(
            app,
            "GET",
            "/v1/credit-note-applications",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(invalid_limit_status, 422)
        self.assertEqual(invalid_limit["rejection_reason_code"], "request_invalid")
        missing_note_status, missing_note = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{first_case.collection_case_id}/credit-note-applications",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(missing_note_status, 422)
        self.assertEqual(missing_note["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.CreditNoteApplicationService.apply_credit_note",
            side_effect=ValueError("boom"),
        ):
            boom_status, boom_body = invoke_http(
                app,
                "POST",
                f"/v1/collection-cases/{second_case.collection_case_id}/credit-note-applications",
                {
                    "tenant_reference": TENANT_ONE,
                    "issued_credit_note_id": str(second_issued.issued_credit_note_id),
                },
            )
        self.assertEqual(boom_status, 422)
        self.assertEqual(boom_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.CreditNoteApplicationPresentmentService.present_credit_note_application",
            side_effect=ValueError("boom"),
        ):
            get_boom_status, get_boom = invoke_http(
                app,
                "GET",
                f"/v1/credit-note-applications/{application_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(get_boom_status, 422)
        self.assertEqual(get_boom["rejection_reason_code"], "request_invalid")

    def test_presentment_rejects_invalid_page_and_missing_rows(self) -> None:
        """Presentment fails closed on unreadable cursors and unknown ids."""
        ledger, issued, collection = issue_morning_credit_then_open_case()
        applied = CreditNoteApplicationService(ledger).apply_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, collection.collection_case_id
        )
        presentment = CreditNoteApplicationPresentmentService(ledger)
        with self.assertRaises(CreditNoteApplicationPresentmentQueryError) as missing:
            presentment.present_credit_note_application(TENANT_ONE, uuid4())
        self.assertEqual(
            missing.exception.rejection_reason_code, "credit_note_application_not_found"
        )
        with self.assertRaises(CreditNoteApplicationPresentmentQueryError) as bad_cursor:
            presentment.list_credit_note_applications(TENANT_ONE, cursor="not-a-cursor")
        self.assertEqual(bad_cursor.exception.rejection_reason_code, "request_invalid")
        with self.assertRaises(CreditNoteApplicationPresentmentQueryError) as bad_limit:
            presentment.list_credit_note_applications(TENANT_ONE, page_limit=0)
        self.assertEqual(bad_limit.exception.rejection_reason_code, "request_invalid")
        page = presentment.list_credit_note_applications(TENANT_ONE)
        self.assertEqual(len(page.credit_note_applications), 1)
        self.assertEqual(
            page.credit_note_applications[0].credit_note_application_id,
            applied.credit_note_application_id,
        )

    def test_contract_and_ledger_guards_cover_identity(self) -> None:
        """Accepted applications need identity; ledger rows stay append-only."""
        valid = {
            "credit_note_application_contract_version": 1,
            "credit_note_application_outcome_code": "accepted",
            "credit_note_application_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd30",
            "tenant_reference": TENANT_ONE,
            "issued_credit_note_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd20",
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd21",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "currency_code": "USD",
            "applied_amount": "0.003705",
            "remaining_outstanding_amount": "0",
            "credit_note_application_status": "applied",
            "collection_case_status": "settled",
            "applied_at": "2026-08-18T09:00:00Z",
            "source_payload_hash": "sha256:" + "b" * 64,
            "issued_credit_note_source_payload_hash": "sha256:" + "c" * 64,
            "issued_credit_note_contract_version": 1,
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_credit_note_application(valid), ())
        rejected = {
            "credit_note_application_contract_version": 1,
            "credit_note_application_outcome_code": "rejected",
            "rejection_reason_code": "collection_case_settled",
        }
        self.assertEqual(validate_credit_note_application(rejected), ())
        missing_id = json.loads(json.dumps(valid))
        del missing_id["credit_note_application_id"]
        self.assertTrue(validate_credit_note_application(missing_id))
        self.assertTrue(validate_credit_note_application(["not-an-object"]))
        ledger, issued, collection = issue_morning_credit_then_open_case()
        applied = CreditNoteApplicationService(ledger).apply_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, collection.collection_case_id
        )
        stored = ledger.get_credit_note_application(applied.credit_note_application_id)
        with self.assertRaises(ValueError):
            ledger.insert_credit_note_application(stored)
        colliding = replace(stored, credit_note_application_id=generate_record_id())
        with self.assertRaises(ValueError):
            ledger.insert_credit_note_application(colliding)
        with self.assertRaises(ValueError):
            ledger.insert_credit_note_application(
                replace(stored, credit_note_application_status="open")
            )
        with self.assertRaises(ValueError):
            ledger.insert_credit_note_application(
                replace(
                    stored,
                    credit_note_application_id=generate_record_id(),
                    applied_amount=Decimal("0"),
                )
            )
        legal = json.loads(json.dumps(valid))
        legal["legal_credit_note_number"] = "CN-1"
        self.assertTrue(validate_credit_note_application(legal))
        rejected_legal = json.loads(json.dumps(rejected))
        rejected_legal["legal_credit_note_number"] = "CN-1"
        self.assertTrue(validate_credit_note_application(rejected_legal))
        rejected_missing = {
            "credit_note_application_contract_version": 1,
            "credit_note_application_outcome_code": "rejected",
        }
        self.assertTrue(validate_credit_note_application(rejected_missing))
        bad_amount = json.loads(json.dumps(valid))
        bad_amount["applied_amount"] = 1
        self.assertTrue(validate_credit_note_application(bad_amount))
        zero_amount = json.loads(json.dumps(valid))
        zero_amount["applied_amount"] = "0"
        self.assertTrue(validate_credit_note_application(zero_amount))
        unreadable_amount = json.loads(json.dumps(valid))
        unreadable_amount["applied_amount"] = "not-decimal"
        self.assertTrue(validate_credit_note_application(unreadable_amount))
        missing_amount = json.loads(json.dumps(valid))
        del missing_amount["applied_amount"]
        self.assertTrue(validate_credit_note_application(missing_amount))
        unknown_outcome = {
            "credit_note_application_contract_version": 1,
            "credit_note_application_outcome_code": "accepted",
        }
        unknown_outcome["credit_note_application_outcome_code"] = "unknown"
        self.assertTrue(validate_credit_note_application(unknown_outcome))

    def test_coverage_guards_for_apply_and_presentment_edges(self) -> None:
        """Closed branches stay explicit: replay, invoice, presentment, and HTTP."""
        empty = CreditNoteApplicationService()
        self.assertEqual(
            empty.apply_credit_note(TENANT_ONE, uuid4(), uuid4()).rejection_reason_code,
            CreditNoteApplicationRejectionReasonCode.TENANT_NOT_FOUND,
        )
        CreditNoteApplicationPresentmentService()
        with self.assertRaises(ValueError):
            _format_applied_at(None)
        unsupported = replace(
            _rejected(CreditNoteApplicationRejectionReasonCode.INVOICE_MISMATCH),
            credit_note_application_outcome_code="nope",
        )
        with self.assertRaises(ValueError):
            unsupported.as_contract_dict()
        accepted_without_time = CreditNoteApplicationResult(
            credit_note_application_outcome_code=CreditNoteApplicationOutcomeCode.ACCEPTED,
            credit_note_application_contract_version=1,
            credit_note_application_id=generate_record_id(),
            issued_credit_note_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            issued_invoice_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            applied_amount=Decimal("1.00"),
            remaining_outstanding_amount=Decimal("0"),
            credit_note_application_status="applied",
            collection_case_status="settled",
            applied_at=None,
            source_payload_hash="sha256:" + "e" * 64,
            issued_credit_note_source_payload_hash="sha256:" + "f" * 64,
            issued_credit_note_contract_version=1,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaises(ValueError):
            accepted_without_time.as_contract_dict()
        with self.assertRaises(ValueError):
            accepted_without_time.as_webhook_event_data()
        none_reason = CreditNoteApplicationResult(
            credit_note_application_outcome_code=CreditNoteApplicationOutcomeCode.REJECTED,
            credit_note_application_contract_version=1,
            credit_note_application_id=None,
            issued_credit_note_id=None,
            collection_case_id=None,
            invoice_draft_id=None,
            issued_invoice_id=None,
            tenant_reference=None,
            currency_code=None,
            applied_amount=None,
            remaining_outstanding_amount=None,
            credit_note_application_status=None,
            collection_case_status=None,
            applied_at=None,
            source_payload_hash=None,
            issued_credit_note_source_payload_hash=None,
            issued_credit_note_contract_version=None,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        self.assertEqual(
            none_reason.as_contract_dict()["rejection_reason_code"],
            "collection_case_not_found",
        )
        with self.assertRaises(ValueError):
            none_reason.as_webhook_event_data()
        missing_note = replace(accepted_without_time, issued_credit_note_id=None)
        with self.assertRaises(ValueError):
            missing_note.as_webhook_event_data()
        incomplete = CreditNoteApplicationResult(
            credit_note_application_outcome_code=CreditNoteApplicationOutcomeCode.ACCEPTED,
            credit_note_application_contract_version=1,
            credit_note_application_id=None,
            issued_credit_note_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            issued_invoice_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            applied_amount=Decimal("1.00"),
            remaining_outstanding_amount=Decimal("0"),
            credit_note_application_status="applied",
            collection_case_status="settled",
            applied_at=None,
            source_payload_hash="sha256:" + "a" * 64,
            issued_credit_note_source_payload_hash="sha256:" + "b" * 64,
            issued_credit_note_contract_version=1,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaises(ValueError):
            _enqueue_credit_note_applied(empty.ledger, TENANT_ONE, incomplete)
        missing_time = CreditNoteApplicationResult(
            credit_note_application_outcome_code=CreditNoteApplicationOutcomeCode.ACCEPTED,
            credit_note_application_contract_version=1,
            credit_note_application_id=generate_record_id(),
            issued_credit_note_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            issued_invoice_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            applied_amount=Decimal("1.00"),
            remaining_outstanding_amount=Decimal("0"),
            credit_note_application_status="applied",
            collection_case_status="settled",
            applied_at=None,
            source_payload_hash="sha256:" + "a" * 64,
            issued_credit_note_source_payload_hash="sha256:" + "b" * 64,
            issued_credit_note_contract_version=1,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaises(ValueError):
            _enqueue_credit_note_applied(empty.ledger, TENANT_ONE, missing_time)
        ledger, issued, collection = issue_morning_credit_then_open_case()
        applied = CreditNoteApplicationService(
            ledger, clock=lambda: APPLIED_MORNING
        ).apply_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, collection.collection_case_id
        )
        presentment = CreditNoteApplicationPresentmentService(ledger)
        with mock.patch.object(presentment.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                presentment.present_credit_note_application(
                    TENANT_ONE, applied.credit_note_application_id
                )
        with self.assertRaises(CreditNoteApplicationPresentmentQueryError) as unknown_tenant:
            presentment.list_credit_note_applications("urn:cwl:missing")
        self.assertEqual(unknown_tenant.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(CreditNoteApplicationPresentmentQueryError):
            presentment.list_credit_note_applications(TENANT_ONE, page_limit=True)
        with self.assertRaises(CreditNoteApplicationPresentmentQueryError):
            presentment.list_credit_note_applications(TENANT_ONE, page_limit="abc")
        with self.assertRaises(CreditNoteApplicationPresentmentQueryError):
            presentment.list_credit_note_applications(TENANT_ONE, page_limit=101)
        with self.assertRaises(CreditNoteApplicationPresentmentQueryError):
            presentment.list_credit_note_applications(TENANT_ONE, page_limit=1.5)
        presentment.list_credit_note_applications(TENANT_ONE, cursor="", page_limit="")
        del ledger.collection_cases[collection.collection_case_id]
        replay_missing = CreditNoteApplicationService(ledger).apply_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, collection.collection_case_id
        )
        self.assertEqual(
            replay_missing.rejection_reason_code,
            CreditNoteApplicationRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )
        with self.assertRaises(CreditNoteApplicationPresentmentQueryError) as missing_case:
            presentment.present_credit_note_application(
                TENANT_ONE, applied.credit_note_application_id
            )
        self.assertEqual(
            missing_case.exception.rejection_reason_code,
            "credit_note_application_not_found",
        )
        ledger_invoice = seed_rated_ledger()
        invoice_draft_id = insert_commercial_draft(ledger_invoice, TENANT_ONE, "USD", HUNDRED)
        issued_invoice = IssuedInvoiceService(ledger_invoice).issue_invoice(
            TENANT_ONE, invoice_draft_id
        )
        credit = CreditAdjustmentService(ledger_invoice).record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, Decimal("11.00"), "goodwill"
        )
        issued_note = IssuedCreditNoteService(ledger_invoice).issue_credit_note(
            TENANT_ONE, credit.credit_adjustment_id
        )
        same_case = CollectionCaseService(ledger_invoice).open_collection_case(
            TENANT_ONE, invoice_draft_id
        )
        mutated_note = replace(
            ledger_invoice.issued_credit_notes[issued_note.issued_credit_note_id],
            issued_invoice_id=uuid4(),
        )
        ledger_invoice.issued_credit_notes[issued_note.issued_credit_note_id] = mutated_note
        mismatched_invoice = CreditNoteApplicationService(ledger_invoice).apply_credit_note(
            TENANT_ONE, issued_note.issued_credit_note_id, same_case.collection_case_id
        )
        self.assertEqual(
            mismatched_invoice.rejection_reason_code,
            CreditNoteApplicationRejectionReasonCode.INVOICE_MISMATCH,
        )
        ledger_invoice.issued_credit_notes[issued_note.issued_credit_note_id] = replace(
            mutated_note, issued_invoice_id=issued_invoice.issued_invoice_id
        )
        accepted = CreditNoteApplicationService(ledger_invoice).apply_credit_note(
            TENANT_ONE, issued_note.issued_credit_note_id, same_case.collection_case_id
        )
        payload = accepted.as_contract_dict()
        self.assertEqual(payload["issued_invoice_id"], str(issued_invoice.issued_invoice_id))
        applied_events = [
            event
            for event in ledger_invoice.webhook_outbox_events.values()
            if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_APPLIED
        ]
        self.assertEqual(len(applied_events), 1)
        envelope = json.loads(applied_events[0].payload_json)
        self.assertEqual(
            envelope["data"]["issued_invoice_id"], str(issued_invoice.issued_invoice_id)
        )
        self.assertEqual(
            accepted.as_webhook_event_data()["issued_invoice_id"],
            str(issued_invoice.issued_invoice_id),
        )
        presented = CreditNoteApplicationPresentmentService(
            ledger_invoice
        ).present_credit_note_application(TENANT_ONE, accepted.credit_note_application_id)
        self.assertEqual(
            presented.as_contract_dict()["issued_invoice_id"],
            str(issued_invoice.issued_invoice_id),
        )
        self.assertIn("credit_note_application_id", presented.as_summary_dict())
        presentment_payload = presented.as_contract_dict()
        self.assertEqual(validate_credit_note_application_presentment(presentment_payload), ())
        forbidden = json.loads(json.dumps(presentment_payload))
        forbidden["legal_credit_note_number"] = "CN-1"
        self.assertTrue(validate_credit_note_application_presentment(forbidden))
        self.assertTrue(validate_credit_note_application_presentment(["not-an-object"]))
        bad_remaining = json.loads(json.dumps(presentment_payload))
        bad_remaining["remaining_outstanding_amount"] = -1
        self.assertTrue(validate_credit_note_application_presentment(bad_remaining))
        negative_remaining = json.loads(json.dumps(presentment_payload))
        negative_remaining["remaining_outstanding_amount"] = "-1"
        self.assertTrue(validate_credit_note_application_presentment(negative_remaining))
        unreadable_remaining = json.loads(json.dumps(presentment_payload))
        unreadable_remaining["remaining_outstanding_amount"] = "nope"
        self.assertTrue(validate_credit_note_application_presentment(unreadable_remaining))
        residual_action = json.loads(json.dumps(presentment_payload))
        residual_action["remaining_outstanding_amount"] = "1.00"
        residual_action["next_operator_action"] = "wait"
        self.assertTrue(validate_credit_note_application_presentment(residual_action))
        settled_action = json.loads(json.dumps(presentment_payload))
        settled_action["remaining_outstanding_amount"] = "0"
        settled_action["next_operator_action"] = "collect"
        self.assertTrue(validate_credit_note_application_presentment(settled_action))
        bad_applied = json.loads(json.dumps(presentment_payload))
        bad_applied["applied_amount"] = 1
        self.assertTrue(validate_credit_note_application_presentment(bad_applied))
        zero_applied = json.loads(json.dumps(presentment_payload))
        zero_applied["applied_amount"] = "0"
        self.assertTrue(validate_credit_note_application_presentment(zero_applied))
        unreadable_applied = json.loads(json.dumps(presentment_payload))
        unreadable_applied["applied_amount"] = "nope"
        self.assertTrue(validate_credit_note_application_presentment(unreadable_applied))
        missing_applied = json.loads(json.dumps(presentment_payload))
        del missing_applied["applied_amount"]
        self.assertTrue(validate_credit_note_application_presentment(missing_applied))
        missing_remaining = json.loads(json.dumps(presentment_payload))
        del missing_remaining["remaining_outstanding_amount"]
        self.assertTrue(validate_credit_note_application_presentment(missing_remaining))


if __name__ == "__main__":
    unittest.main()
