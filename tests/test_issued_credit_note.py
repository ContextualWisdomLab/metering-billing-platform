"""Issued-credit-note tests for immutable commercial snapshots from credits."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import UUID, uuid4

from metering_billing import (
    CreditAdjustmentService,
    IssuedCreditNotePresentmentService,
    IssuedCreditNoteService,
    IssuedInvoiceService,
    TaxAssessmentService,
    TaxRateService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.contracts import (
    validate_issued_credit_note,
    validate_issued_credit_note_presentment,
)
from metering_billing.errors import (
    IssuedCreditNoteOutcomeCode,
    IssuedCreditNotePresentmentQueryError,
    IssuedCreditNoteRejectionReasonCode,
)
from metering_billing.issued_credit_note import next_operator_action
from metering_billing.issued_credit_note_presentment import (
    next_operator_action as presentment_action,
)
from metering_billing.usage_ledger import generate_record_id
from metering_billing.webhook_outbox import EVENT_TYPE_CREDIT_ADJUSTMENT_RECORDED
from test_collection_case import draft_known_morning
from test_http_app import invoke_http
from test_tax_assessment import HUNDRED, STANDARD_TAX_RATE, insert_commercial_draft
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL, seed_rated_ledger


ISSUED_MORNING = datetime(2026, 8, 17, 21, 0, tzinfo=UTC)
ISSUED_EVENING = datetime(2026, 8, 17, 22, 0, tzinfo=UTC)
TAXED_CREDIT = Decimal("11.00")


def record_known_morning_credit():
    """Record the known morning credit and return ledger plus result."""
    ledger, invoice_draft_id = draft_known_morning()
    credit = CreditAdjustmentService(ledger).record_credit_adjustment(
        TENANT_ONE, invoice_draft_id, KNOWN_MORNING_TOTAL, "rating_correction"
    )
    return ledger, credit


class IssuedCreditNoteTests(unittest.TestCase):
    """Verify idempotent issue, exact credit totals, and metadata-only GET."""

    def test_known_credit_issues_immutable_untaxed_snapshot(self) -> None:
        """A known morning credit freezes exclusive/tax-zero/inclusive amounts."""
        ledger, credit = record_known_morning_credit()
        prior_outbox = len(ledger.webhook_outbox_events)
        first = IssuedCreditNoteService(ledger, clock=lambda: ISSUED_MORNING).issue_credit_note(
            TENANT_ONE, credit.credit_adjustment_id
        )
        second = IssuedCreditNoteService(ledger, clock=lambda: ISSUED_EVENING).issue_credit_note(
            TENANT_ONE, credit.credit_adjustment_id
        )
        self.assertEqual(first.issued_credit_note_outcome_code, IssuedCreditNoteOutcomeCode.ACCEPTED)
        self.assertEqual(
            second.issued_credit_note_outcome_code,
            IssuedCreditNoteOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertEqual(first.issued_credit_note_id, second.issued_credit_note_id)
        self.assertEqual(first.credit_adjustment_id, credit.credit_adjustment_id)
        self.assertEqual(first.invoice_draft_id, credit.invoice_draft_id)
        self.assertIsNone(first.issued_invoice_id)
        self.assertEqual(first.currency_code, "USD")
        self.assertEqual(first.tax_exclusive_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(first.tax_amount, Decimal("0"))
        self.assertEqual(first.tax_inclusive_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(first.issued_credit_note_status, "issued")
        self.assertEqual(first.issued_at, ISSUED_MORNING)
        self.assertEqual(first.next_operator_action, "wait")
        self.assertEqual(first.credit_reason_code, "rating_correction")
        self.assertEqual(first.credit_adjustment_source_payload_hash, credit.source_payload_hash)
        self.assertEqual(first.credit_adjustment_contract_version, 1)
        self.assertTrue(first.source_payload_hash.startswith("sha256:"))
        self.assertEqual(
            first.idempotency_key,
            (
                f"{TENANT_ONE}:issued_credit_note:{first.issued_credit_note_id}:"
                f"{first.source_payload_hash}:v1"
            ),
        )
        payload = first.as_contract_dict()
        self.assertEqual(validate_issued_credit_note(payload), ())
        self.assertNotIn("issued_invoice_id", payload)
        self.assertNotIn("issued_credit_note_lines", payload)
        self.assertNotIn("credit_note_number", payload)
        self.assertNotIn("legal_credit_note_number", payload)
        self.assertNotIn("card_pan", payload)
        self.assertEqual(credit.credit_adjustment_outcome_code.value, "accepted")
        self.assertEqual(len(ledger.issued_credit_notes), 1)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        self.assertEqual(
            len(
                [
                    event
                    for event in ledger.webhook_outbox_events.values()
                    if event.event_type_code == EVENT_TYPE_CREDIT_ADJUSTMENT_RECORDED
                ]
            ),
            1,
        )
        self.assertNotIn("credit_note.issued", json.dumps(payload))

    def test_taxed_credit_and_issued_invoice_link_are_preserved(self) -> None:
        """A taxed credit copies the split; issued_invoice_id is stored when present."""
        ledger = seed_rated_ledger()
        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        invoice_draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        TaxAssessmentService(ledger).assess_tax(TENANT_ONE, invoice_draft_id, 1)
        issued_invoice = IssuedInvoiceService(ledger, clock=lambda: ISSUED_MORNING).issue_invoice(
            TENANT_ONE, invoice_draft_id
        )
        credit = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, TAXED_CREDIT, "goodwill"
        )
        issued = IssuedCreditNoteService(ledger, clock=lambda: ISSUED_EVENING).issue_credit_note(
            TENANT_ONE, credit.credit_adjustment_id
        )
        self.assertEqual(issued.tax_exclusive_amount, Decimal("10.00"))
        self.assertEqual(issued.tax_amount, Decimal("1.00"))
        self.assertEqual(issued.tax_inclusive_amount, TAXED_CREDIT)
        self.assertEqual(issued.issued_invoice_id, issued_invoice.issued_invoice_id)
        payload = issued.as_contract_dict()
        self.assertEqual(payload["issued_invoice_id"], str(issued_invoice.issued_invoice_id))
        self.assertEqual(payload["credit_reason_code"], "goodwill")
        self.assertEqual(validate_issued_credit_note(payload), ())
        replay = IssuedCreditNoteService(ledger, clock=lambda: ISSUED_MORNING).issue_credit_note(
            TENANT_ONE, credit.credit_adjustment_id
        )
        self.assertEqual(replay.issued_credit_note_outcome_code, IssuedCreditNoteOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(replay.issued_invoice_id, issued_invoice.issued_invoice_id)
        self.assertEqual(len(ledger.issued_credit_notes), 1)
        presented = IssuedCreditNotePresentmentService(ledger).present_issued_credit_note(
            TENANT_ONE, issued.issued_credit_note_id
        )
        presented_payload = presented.as_contract_dict()
        self.assertEqual(
            presented_payload["issued_invoice_id"],
            str(issued_invoice.issued_invoice_id),
        )
        self.assertEqual(validate_issued_credit_note_presentment(presented_payload), ())

    def test_http_issue_get_and_paged_list_without_capture(self) -> None:
        """POST issues; GET item and list page metadata and never capture payment."""
        ledger, first_credit = record_known_morning_credit()
        second_draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("20.00"))
        second_credit = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, second_draft_id, Decimal("20.00"), "billing_error"
        )
        prior_outbox = len(ledger.webhook_outbox_events)
        app = create_http_app(ledger)
        refused_status, refused_body = invoke_http(
            app,
            "POST",
            f"/v1/credit-adjustments/{first_credit.credit_adjustment_id}/issued-credit-notes",
            {"tenant_reference": TENANT_ONE, "card_pan": "4111111111111111"},
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.issued_credit_notes), 0)
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            f"/v1/credit-adjustments/{first_credit.credit_adjustment_id}/issued-credit-notes",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["issued_credit_note_outcome_code"], "accepted")
        self.assertEqual(
            accepted_body["tax_inclusive_amount"],
            format_exact_decimal(KNOWN_MORNING_TOTAL),
        )
        self.assertEqual(accepted_body["next_operator_action"], "wait")
        issued_credit_note_id = accepted_body["issued_credit_note_id"]
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            f"/v1/credit-adjustments/{first_credit.credit_adjustment_id}/issued-credit-notes",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["issued_credit_note_outcome_code"], "duplicate_replay")
        self.assertEqual(replay_body["issued_credit_note_id"], issued_credit_note_id)
        second_status, second_issue = invoke_http(
            app,
            "POST",
            f"/v1/credit-adjustments/{second_credit.credit_adjustment_id}/issued-credit-notes",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(second_issue["issued_credit_note_outcome_code"], "accepted")
        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/issued-credit-notes/{issued_credit_note_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["issued_credit_note_id"], issued_credit_note_id)
        self.assertEqual(get_body["credit_adjustment_id"], str(first_credit.credit_adjustment_id))
        self.assertEqual(get_body["next_operator_action"], "wait")
        self.assertNotIn("issued_credit_note_outcome_code", get_body)
        self.assertEqual(validate_issued_credit_note_presentment(get_body), ())
        header_status, header_body = invoke_http(
            app,
            "GET",
            f"/v1/issued-credit-notes/{issued_credit_note_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(header_status, 200)
        self.assertEqual(header_body, get_body)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/issued-credit-notes",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"issued_credit_notes", "next_cursor"})
        self.assertNotIn("items", list_body)
        self.assertNotIn("cursor", list_body)
        self.assertEqual(len(list_body["issued_credit_notes"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        first_summary = list_body["issued_credit_notes"][0]
        self.assertEqual(
            set(first_summary),
            {
                "issued_credit_note_id",
                "credit_adjustment_id",
                "currency_code",
                "tax_inclusive_amount",
                "issued_at",
                "next_operator_action",
            },
        )
        page_two_status, page_two = invoke_http(
            app,
            "GET",
            "/v1/issued-credit-notes",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(page_two_status, 200)
        self.assertEqual(len(page_two["issued_credit_notes"]), 1)
        self.assertIsNone(page_two["next_cursor"])
        listed_ids = {
            first_summary["issued_credit_note_id"],
            page_two["issued_credit_notes"][0]["issued_credit_note_id"],
        }
        self.assertEqual(listed_ids, {issued_credit_note_id, second_issue["issued_credit_note_id"]})
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/issued-credit-notes",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["issued_credit_notes"], [])
        self.assertIsNone(empty_body["next_cursor"])
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        self.assertEqual(len(ledger.payment_intents), 0)

    def test_cross_tenant_and_unknown_reads_are_404_without_leak(self) -> None:
        """Missing tenant is 422; unknown or cross-tenant ids stay 404."""
        ledger, credit = record_known_morning_credit()
        issued = IssuedCreditNoteService(ledger).issue_credit_note(
            TENANT_ONE, credit.credit_adjustment_id
        )
        app = create_http_app(ledger)
        missing_status, missing_body = invoke_http(
            app,
            "GET",
            f"/v1/issued-credit-notes/{issued.issued_credit_note_id}",
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/issued-credit-notes/{issued.issued_credit_note_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "issued_credit_note_not_found")
        self.assertNotIn("tax_inclusive_amount", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/issued-credit-notes/{uuid4()}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "issued_credit_note_not_found")
        with self.assertRaises(IssuedCreditNotePresentmentQueryError) as crossed:
            IssuedCreditNotePresentmentService(ledger).present_issued_credit_note(
                TENANT_TWO, issued.issued_credit_note_id
            )
        self.assertEqual(crossed.exception.rejection_reason_code, "issued_credit_note_not_found")
        unknown_issue_status, unknown_issue = invoke_http(
            app,
            "POST",
            f"/v1/credit-adjustments/{uuid4()}/issued-credit-notes",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_issue_status, 422)
        self.assertEqual(unknown_issue["rejection_reason_code"], "credit_adjustment_not_found")
        crossed_issue = IssuedCreditNoteService(ledger).issue_credit_note(
            TENANT_TWO, credit.credit_adjustment_id
        )
        self.assertEqual(
            crossed_issue.issued_credit_note_outcome_code,
            IssuedCreditNoteOutcomeCode.REJECTED,
        )
        self.assertEqual(
            crossed_issue.rejection_reason_code,
            IssuedCreditNoteRejectionReasonCode.CREDIT_ADJUSTMENT_NOT_FOUND,
        )

    def test_list_filters_and_helpers_fail_closed(self) -> None:
        """Illegal filters stay 422; helpers cover page bounds and actions."""
        self.assertEqual(next_operator_action(), "wait")
        self.assertEqual(presentment_action(), "wait")
        ledger, credit = record_known_morning_credit()
        issued = IssuedCreditNoteService(ledger, clock=lambda: ISSUED_MORNING).issue_credit_note(
            TENANT_ONE, credit.credit_adjustment_id
        )
        app = create_http_app(ledger)
        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/issued-credit-notes",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")
        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/issued-credit-notes",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")
        method_status, method_body = invoke_http(
            app,
            "PUT",
            f"/v1/issued-credit-notes/{issued.issued_credit_note_id}",
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        collection_method_status, collection_method_body = invoke_http(
            app,
            "PUT",
            "/v1/issued-credit-notes",
        )
        self.assertEqual(collection_method_status, 422)
        self.assertEqual(collection_method_body["rejection_reason_code"], "request_invalid")
        nested_method_status, nested_method_body = invoke_http(
            app,
            "PUT",
            f"/v1/credit-adjustments/{credit.credit_adjustment_id}/issued-credit-notes",
        )
        self.assertEqual(nested_method_status, 422)
        self.assertEqual(nested_method_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.IssuedCreditNotePresentmentService.list_issued_credit_notes",
            side_effect=IssuedCreditNotePresentmentQueryError("issued_credit_note_not_found"),
        ):
            not_found_status, not_found_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/issued-credit-notes",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(not_found_status, 404)
        self.assertEqual(not_found_body["rejection_reason_code"], "issued_credit_note_not_found")
        with mock.patch(
            "metering_billing.http_app.IssuedCreditNoteService.issue_credit_note",
            side_effect=ValueError("closed"),
        ):
            issue_value_status, issue_value_body = invoke_http(
                create_http_app(ledger),
                "POST",
                f"/v1/credit-adjustments/{credit.credit_adjustment_id}/issued-credit-notes",
                {"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(issue_value_status, 422)
        self.assertEqual(issue_value_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.IssuedCreditNotePresentmentService.list_issued_credit_notes",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/issued-credit-notes",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        empty = IssuedCreditNotePresentmentService()
        self.assertIsNotNone(empty.ledger)
        with self.assertRaises(IssuedCreditNotePresentmentQueryError):
            empty.list_issued_credit_notes(TENANT_ONE)
        service = IssuedCreditNotePresentmentService(ledger)
        listed = service.list_issued_credit_notes(TENANT_ONE, cursor="")
        self.assertEqual(len(listed.issued_credit_notes), 1)
        with self.assertRaises(IssuedCreditNotePresentmentQueryError):
            service.list_issued_credit_notes(TENANT_ONE, page_limit=True)
        with self.assertRaises(IssuedCreditNotePresentmentQueryError):
            service.list_issued_credit_notes(TENANT_ONE, page_limit=101)
        with self.assertRaises(IssuedCreditNotePresentmentQueryError):
            service.list_issued_credit_notes(TENANT_ONE, page_limit=1.5)
        with self.assertRaises(IssuedCreditNotePresentmentQueryError):
            service.list_issued_credit_notes(TENANT_ONE, page_limit="x")
        with self.assertRaises(IssuedCreditNotePresentmentQueryError):
            service.list_issued_credit_notes(TENANT_ONE, cursor="bad|cursor")
        empty_limit = service.list_issued_credit_notes(TENANT_ONE, page_limit="")
        self.assertEqual(len(empty_limit.issued_credit_notes), 1)
        self.assertEqual(
            service.list_issued_credit_notes(TENANT_ONE, page_limit=50).next_cursor,
            None,
        )
        with self.assertRaises(IssuedCreditNotePresentmentQueryError):
            service.present_issued_credit_note(TENANT_ONE, uuid4())
        with self.assertRaises(IssuedCreditNotePresentmentQueryError):
            service.present_issued_credit_note("", issued.issued_credit_note_id)
        self.assertEqual(listed.issued_credit_notes[0].issued_at, ISSUED_MORNING)
        self.assertIsInstance(issued.issued_credit_note_id, UUID)
        writer = IssuedCreditNoteService()
        self.assertIsNotNone(writer.ledger)
        missing_tenant = writer.issue_credit_note(TENANT_ONE, credit.credit_adjustment_id)
        self.assertEqual(
            missing_tenant.issued_credit_note_outcome_code,
            IssuedCreditNoteOutcomeCode.REJECTED,
        )
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            IssuedCreditNoteRejectionReasonCode.TENANT_NOT_FOUND,
        )
        stored = next(iter(ledger.issued_credit_notes.values()))
        with self.assertRaises(ValueError):
            ledger.insert_issued_credit_note(stored)
        with self.assertRaises(ValueError):
            ledger.insert_issued_credit_note(
                stored.__class__(
                    issued_credit_note_id=generate_record_id(),
                    tenant_account_id=stored.tenant_account_id,
                    credit_adjustment_id=stored.credit_adjustment_id,
                    invoice_draft_id=stored.invoice_draft_id,
                    issued_invoice_id=None,
                    issued_credit_note_contract_version=stored.issued_credit_note_contract_version,
                    credit_adjustment_contract_version=stored.credit_adjustment_contract_version,
                    credit_reason_code=stored.credit_reason_code,
                    credit_adjustment_source_payload_hash=stored.credit_adjustment_source_payload_hash,
                    source_payload_hash=stored.source_payload_hash,
                    currency_code=stored.currency_code,
                    tax_exclusive_amount=stored.tax_exclusive_amount,
                    tax_amount=stored.tax_amount,
                    tax_inclusive_amount=stored.tax_inclusive_amount,
                    issued_credit_note_status=stored.issued_credit_note_status,
                    issued_at=stored.issued_at,
                )
            )
        other_credit = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE,
            insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("5.00")),
            Decimal("5.00"),
            "goodwill",
        )
        with self.assertRaises(ValueError):
            ledger.insert_issued_credit_note(
                stored.__class__(
                    issued_credit_note_id=generate_record_id(),
                    tenant_account_id=stored.tenant_account_id,
                    credit_adjustment_id=other_credit.credit_adjustment_id,
                    invoice_draft_id=other_credit.invoice_draft_id,
                    issued_invoice_id=None,
                    issued_credit_note_contract_version=stored.issued_credit_note_contract_version,
                    credit_adjustment_contract_version=stored.credit_adjustment_contract_version,
                    credit_reason_code=stored.credit_reason_code,
                    credit_adjustment_source_payload_hash=stored.credit_adjustment_source_payload_hash,
                    source_payload_hash=stored.source_payload_hash,
                    currency_code=stored.currency_code,
                    tax_exclusive_amount=stored.tax_exclusive_amount,
                    tax_amount=stored.tax_amount,
                    tax_inclusive_amount=stored.tax_inclusive_amount,
                    issued_credit_note_status="draft",
                    issued_at=stored.issued_at,
                )
            )
        with self.assertRaises(ValueError):
            ledger.insert_issued_credit_note(
                stored.__class__(
                    issued_credit_note_id=generate_record_id(),
                    tenant_account_id=stored.tenant_account_id,
                    credit_adjustment_id=other_credit.credit_adjustment_id,
                    invoice_draft_id=other_credit.invoice_draft_id,
                    issued_invoice_id=None,
                    issued_credit_note_contract_version=stored.issued_credit_note_contract_version,
                    credit_adjustment_contract_version=stored.credit_adjustment_contract_version,
                    credit_reason_code=stored.credit_reason_code,
                    credit_adjustment_source_payload_hash=stored.credit_adjustment_source_payload_hash,
                    source_payload_hash=stored.source_payload_hash,
                    currency_code=stored.currency_code,
                    tax_exclusive_amount=Decimal("5.00"),
                    tax_amount=Decimal("1.00"),
                    tax_inclusive_amount=Decimal("999.00"),
                    issued_credit_note_status=stored.issued_credit_note_status,
                    issued_at=stored.issued_at,
                )
            )
        unknown_outcome = issued.__class__(
            issued_credit_note_outcome_code="posted",  # type: ignore[arg-type]
            issued_credit_note_contract_version=1,
            issued_credit_note_id=issued.issued_credit_note_id,
            credit_adjustment_id=issued.credit_adjustment_id,
            invoice_draft_id=issued.invoice_draft_id,
            issued_invoice_id=None,
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            tax_exclusive_amount=KNOWN_MORNING_TOTAL,
            tax_amount=Decimal("0"),
            tax_inclusive_amount=KNOWN_MORNING_TOTAL,
            issued_credit_note_status="issued",
            issued_at=ISSUED_MORNING,
            source_payload_hash=issued.source_payload_hash,
            credit_adjustment_source_payload_hash=issued.credit_adjustment_source_payload_hash,
            credit_adjustment_contract_version=1,
            credit_reason_code="rating_correction",
            idempotency_key=issued.idempotency_key,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaises(ValueError):
            unknown_outcome.as_contract_dict()
        rejected = IssuedCreditNoteService(ledger).issue_credit_note("", credit.credit_adjustment_id)
        self.assertEqual(validate_issued_credit_note(rejected.as_contract_dict()), ())
        none_reason = rejected.__class__(
            issued_credit_note_outcome_code=IssuedCreditNoteOutcomeCode.REJECTED,
            issued_credit_note_contract_version=1,
            issued_credit_note_id=None,
            credit_adjustment_id=None,
            invoice_draft_id=None,
            issued_invoice_id=None,
            tenant_reference=None,
            currency_code=None,
            tax_exclusive_amount=None,
            tax_amount=None,
            tax_inclusive_amount=None,
            issued_credit_note_status=None,
            issued_at=None,
            source_payload_hash=None,
            credit_adjustment_source_payload_hash=None,
            credit_adjustment_contract_version=1,
            credit_reason_code=None,
            idempotency_key=None,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        self.assertEqual(
            none_reason.as_contract_dict()["rejection_reason_code"],
            "credit_adjustment_not_found",
        )
        presented = IssuedCreditNotePresentmentService(ledger).present_issued_credit_note(
            TENANT_ONE, issued.issued_credit_note_id
        )
        self.assertNotIn("issued_invoice_id", presented.as_contract_dict())
        self.assertEqual(presented.as_summary_dict()["next_operator_action"], "wait")
