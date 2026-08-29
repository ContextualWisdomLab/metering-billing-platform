"""Issued-invoice tests for immutable commercial snapshots from invoice drafts."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import UUID, uuid4

from metering_billing import (
    AccountingExportService,
    CollectionCaseService,
    CollectionCaseSettlementService,
    CollectionWriteOffService,
    CreditAdjustmentService,
    InvoiceDraftService,
    IssuedInvoicePresentmentService,
    IssuedInvoiceService,
    LateAdjustmentInvoiceAdjustmentService,
    MemoryUsageLedger,
    PaymentSettlementService,
    RateCardService,
    TaxAssessmentService,
    TaxRateService,
    UsageRatingService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.contracts import (
    validate_issued_invoice,
    validate_issued_invoice_presentment,
)
from metering_billing.errors import (
    ExactDecimalError,
    IssuedInvoiceOutcomeCode,
    IssuedInvoicePresentmentQueryError,
    IssuedInvoiceRejectionReasonCode,
)
from metering_billing.issued_invoice import next_operator_action
from metering_billing.issued_invoice_presentment import next_operator_action as presentment_action
from metering_billing.usage_ledger import generate_record_id
from metering_billing.webhook_outbox import EVENT_TYPE_INVOICE_ISSUED
from test_collection_case import draft_known_morning
from test_http_app import invoke_http
from test_tax_assessment import HUNDRED, STANDARD_TAX_RATE, insert_commercial_draft
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import (
    KNOWN_MORNING_QUANTITY,
    KNOWN_MORNING_TOTAL,
    MORNING_WINDOW,
    TOKEN_UNIT_PRICE,
    seed_rated_ledger,
)


ISSUED_MORNING = datetime(2026, 8, 17, 21, 0, tzinfo=UTC)
ISSUED_EVENING = datetime(2026, 8, 17, 22, 0, tzinfo=UTC)
DUE_AT = datetime(2026, 9, 16, 21, 0, tzinfo=UTC)


class IssuedInvoiceTests(unittest.TestCase):
    """Verify idempotent issue, immutable totals, and metadata-only GET."""

    def test_memory_commands_support_a_nontransactional_adapter(self) -> None:
        """Command wrappers retain compatibility with duck-typed adapters."""
        ledger = MemoryUsageLedger()
        ledger.transaction = None  # type: ignore[method-assign]
        commands = (
            lambda: AccountingExportService(ledger).propose_journal("", uuid4()),
            lambda: CollectionCaseService(ledger).open_collection_case("", uuid4()),
            lambda: CollectionCaseSettlementService(ledger).settle_collection_case("", uuid4()),
            lambda: CollectionWriteOffService(ledger).write_off_collection_case("", uuid4()),
            lambda: CreditAdjustmentService(ledger).record_credit_adjustment(
                "", uuid4(), "0.001", "rating_correction"
            ),
            lambda: InvoiceDraftService(ledger).draft_invoice("", uuid4()),
            lambda: IssuedInvoiceService(ledger).issue_invoice("", uuid4()),
            lambda: LateAdjustmentInvoiceAdjustmentService(ledger).record_invoice_adjustment(
                "", uuid4(), uuid4(), recorded_by="operator:test", authorization_reference="approval:test"
            ),
            lambda: PaymentSettlementService(ledger).record_payment_receipt("", uuid4(), "1"),
            lambda: RateCardService(ledger).publish_rate_card("", "test", "USD", ()),
            lambda: TaxAssessmentService(ledger).assess_tax("", uuid4(), 1),
            lambda: TaxRateService(ledger).publish_tax_rate("", "vat", "0.10"),
            lambda: UsageRatingService(ledger).rate_usage_window("", MORNING_WINDOW, 1),
        )
        for command in commands:
            command()

    def test_known_draft_issues_immutable_untaxed_snapshot(self) -> None:
        """A known morning draft freezes exact line and tax-zero totals."""
        ledger, invoice_draft_id = draft_known_morning()
        draft = ledger.get_invoice_draft(invoice_draft_id)
        assert draft is not None
        first = IssuedInvoiceService(ledger, clock=lambda: ISSUED_MORNING).issue_invoice(
            TENANT_ONE, invoice_draft_id
        )
        second = IssuedInvoiceService(ledger, clock=lambda: ISSUED_EVENING).issue_invoice(
            TENANT_ONE, invoice_draft_id
        )
        self.assertEqual(first.issued_invoice_outcome_code, IssuedInvoiceOutcomeCode.ACCEPTED)
        self.assertEqual(second.issued_invoice_outcome_code, IssuedInvoiceOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(first.issued_invoice_id, second.issued_invoice_id)
        self.assertEqual(first.invoice_draft_id, invoice_draft_id)
        self.assertEqual(first.rating_run_id, draft.rating_run_id)
        self.assertEqual(first.usage_snapshot_hash, draft.usage_snapshot_hash)
        self.assertEqual(first.currency_code, "USD")
        self.assertEqual(first.tax_exclusive_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(first.tax_amount, Decimal("0"))
        self.assertEqual(first.tax_inclusive_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(first.issued_invoice_status, "issued")
        self.assertEqual(first.issued_at, ISSUED_MORNING)
        self.assertIsNone(first.due_at)
        self.assertEqual(first.next_operator_action, "collect")
        self.assertTrue(first.source_payload_hash.startswith("sha256:"))
        self.assertEqual(
            first.idempotency_key,
            (
                f"{TENANT_ONE}:issued_invoice:{first.issued_invoice_id}:"
                f"{first.source_payload_hash}:v2"
            ),
        )
        self.assertEqual(len(first.issued_invoice_lines), 1)
        line = first.issued_invoice_lines[0]
        self.assertEqual(line.meter_code, "gen_ai_output_token")
        self.assertEqual(line.rated_quantity, KNOWN_MORNING_QUANTITY)
        self.assertEqual(line.unit_price_amount, TOKEN_UNIT_PRICE)
        self.assertEqual(line.line_total_amount, KNOWN_MORNING_TOTAL)
        payload = first.as_contract_dict()
        self.assertEqual(validate_issued_invoice(payload), ())
        self.assertNotIn("due_at", payload)
        self.assertNotIn("invoice_number", payload)
        self.assertNotIn("legal_invoice_number", payload)
        self.assertNotIn("card_pan", payload)
        self.assertEqual(draft.invoice_draft_status, "draft")
        self.assertEqual(len(ledger.issued_invoices), 1)
        issued_events = [
            event
            for event in ledger.webhook_outbox_events.values()
            if event.event_type_code == EVENT_TYPE_INVOICE_ISSUED
        ]
        self.assertEqual(len(issued_events), 1)
        self.assertEqual(issued_events[0].source_id, first.issued_invoice_id)
        envelope = json.loads(issued_events[0].payload_json)
        self.assertEqual(envelope["event_type_code"], EVENT_TYPE_INVOICE_ISSUED)
        self.assertEqual(envelope["tenant_reference"], TENANT_ONE)
        self.assertEqual(envelope["data"]["issued_invoice_id"], str(first.issued_invoice_id))
        self.assertEqual(envelope["data"]["invoice_draft_id"], str(invoice_draft_id))
        self.assertEqual(envelope["data"]["source_payload_hash"], first.source_payload_hash)
        self.assertEqual(envelope["data"]["issued_invoice_contract_version"], 2)
        self.assertEqual(envelope["data"]["currency_code"], "USD")
        self.assertEqual(
            envelope["data"]["tax_exclusive_amount"],
            format_exact_decimal(KNOWN_MORNING_TOTAL),
        )
        self.assertEqual(envelope["data"]["tax_amount"], "0")
        self.assertEqual(
            envelope["data"]["tax_inclusive_amount"],
            format_exact_decimal(KNOWN_MORNING_TOTAL),
        )
        self.assertEqual(envelope["data"]["issued_invoice_status"], "issued")
        self.assertEqual(envelope["data"]["issued_at"], "2026-08-17T21:00:00Z")
        self.assertEqual(envelope["data"]["rating_run_id"], str(first.rating_run_id))
        self.assertEqual(envelope["data"]["usage_snapshot_hash"], first.usage_snapshot_hash)
        self.assertNotIn("issued_invoice_lines", envelope["data"])
        self.assertNotIn("billing_account_reference", json.dumps(envelope))
        self.assertNotIn("meter_code", json.dumps(envelope["data"]))
        self.assertNotIn("invoice_number", envelope["data"])
        self.assertNotIn("legal_invoice_number", envelope["data"])
        self.assertNotIn("card_pan", json.dumps(envelope))
        self.assertNotIn("webhook_secret", json.dumps(envelope))
        self.assertNotIn("api_credential_secret", json.dumps(envelope))
        self.assertEqual(len(ledger.journal_proposals), 0)

    def test_presentment_upgrades_historical_v1_snapshot_to_v2(self) -> None:
        """Historical stored versions use the current presentment envelope."""
        ledger, invoice_draft_id = draft_known_morning()
        issued = IssuedInvoiceService(ledger, clock=lambda: ISSUED_MORNING).issue_invoice(
            TENANT_ONE, invoice_draft_id
        )
        stored = ledger.get_issued_invoice(issued.issued_invoice_id)
        assert stored is not None
        ledger.issued_invoices[issued.issued_invoice_id] = replace(
            stored, issued_invoice_contract_version=1
        )
        presented = IssuedInvoicePresentmentService(ledger).present_issued_invoice(
            TENANT_ONE, issued.issued_invoice_id
        )
        payload = presented.as_contract_dict()
        self.assertEqual(payload["issued_invoice_presentment_contract_version"], 2)
        self.assertEqual(payload["issued_invoice_contract_version"], 2)
        self.assertEqual(validate_issued_invoice_presentment(payload), ())
        replayed = IssuedInvoiceService(ledger).issue_invoice(
            TENANT_ONE, issued.invoice_draft_id
        )
        self.assertEqual(replayed.issued_invoice_outcome_code.value, "duplicate_replay")
        self.assertEqual(replayed.issued_invoice_contract_version, 2)
        self.assertEqual(validate_issued_invoice(replayed.as_contract_dict()), ())

    def test_taxed_draft_freezes_assessment_totals_and_optional_due_at(self) -> None:
        """A taxed draft copies exclusive/tax/inclusive and stores caller due_at."""
        ledger = seed_rated_ledger()
        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        invoice_draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        TaxAssessmentService(ledger).assess_tax(TENANT_ONE, invoice_draft_id, 1)
        issued = IssuedInvoiceService(ledger, clock=lambda: ISSUED_MORNING).issue_invoice(
            TENANT_ONE, invoice_draft_id, due_at=DUE_AT
        )
        self.assertEqual(issued.tax_exclusive_amount, HUNDRED)
        self.assertEqual(issued.tax_amount, Decimal("10.00"))
        self.assertEqual(issued.tax_inclusive_amount, Decimal("110.00"))
        self.assertEqual(issued.due_at, DUE_AT)
        payload = issued.as_contract_dict()
        self.assertEqual(payload["due_at"], "2026-09-16T21:00:00Z")
        self.assertEqual(validate_issued_invoice(payload), ())
        replay = IssuedInvoiceService(ledger, clock=lambda: ISSUED_EVENING).issue_invoice(
            TENANT_ONE, invoice_draft_id, due_at=ISSUED_EVENING
        )
        self.assertEqual(replay.issued_invoice_outcome_code, IssuedInvoiceOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(replay.due_at, DUE_AT)
        self.assertEqual(replay.tax_inclusive_amount, Decimal("110.00"))
        taxed_events = [
            event
            for event in ledger.webhook_outbox_events.values()
            if event.event_type_code == EVENT_TYPE_INVOICE_ISSUED
        ]
        self.assertEqual(len(taxed_events), 1)
        taxed_envelope = json.loads(taxed_events[0].payload_json)
        self.assertEqual(taxed_envelope["data"]["due_at"], "2026-09-16T21:00:00Z")
        self.assertEqual(taxed_envelope["data"]["tax_inclusive_amount"], "110.00")
        self.assertNotIn("issued_invoice_lines", taxed_envelope["data"])

    def test_presentment_exposes_stored_tax_assessment_id_without_inventing_amounts(
        self,
    ) -> None:
        """Item GET links a matching stored assessment; list and later assess omit it."""
        untaxed_ledger, untaxed_draft_id = draft_known_morning()
        untaxed = IssuedInvoiceService(
            untaxed_ledger, clock=lambda: ISSUED_MORNING
        ).issue_invoice(TENANT_ONE, untaxed_draft_id)
        untaxed_presented = IssuedInvoicePresentmentService(
            untaxed_ledger
        ).present_issued_invoice(TENANT_ONE, untaxed.issued_invoice_id)
        untaxed_payload = untaxed_presented.as_contract_dict()
        self.assertEqual(validate_issued_invoice_presentment(untaxed_payload), ())
        self.assertNotIn("tax_assessment_id", untaxed_payload)
        self.assertNotIn("legal_invoice_number", untaxed_payload)
        self.assertNotIn("vat_register_id", untaxed_payload)
        self.assertNotIn("tax_invoice_number", untaxed_payload)
        self.assertNotIn("nts_approval_number", untaxed_payload)
        self.assertNotIn("hometax_document_id", untaxed_payload)
        self.assertNotIn("세금계산서", json.dumps(untaxed_payload))
        later_assess_ledger = seed_rated_ledger()
        later_draft_id = insert_commercial_draft(
            later_assess_ledger, TENANT_ONE, "USD", HUNDRED
        )
        later_issued = IssuedInvoiceService(
            later_assess_ledger, clock=lambda: ISSUED_MORNING
        ).issue_invoice(TENANT_ONE, later_draft_id)
        TaxRateService(later_assess_ledger).publish_tax_rate(
            TENANT_ONE, "vat", STANDARD_TAX_RATE
        )
        later_assessed = TaxAssessmentService(later_assess_ledger).assess_tax(
            TENANT_ONE, later_draft_id, 1
        )
        self.assertIsNotNone(later_assessed.tax_assessment_id)
        later_presented = IssuedInvoicePresentmentService(
            later_assess_ledger
        ).present_issued_invoice(TENANT_ONE, later_issued.issued_invoice_id)
        self.assertEqual(later_presented.tax_exclusive_amount, HUNDRED)
        self.assertEqual(later_presented.tax_amount, Decimal("0"))
        self.assertNotIn("tax_assessment_id", later_presented.as_contract_dict())
        taxed_ledger = seed_rated_ledger()
        TaxRateService(taxed_ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        taxed_draft_id = insert_commercial_draft(taxed_ledger, TENANT_ONE, "USD", HUNDRED)
        assessed = TaxAssessmentService(taxed_ledger).assess_tax(
            TENANT_ONE, taxed_draft_id, 1
        )
        taxed = IssuedInvoiceService(
            taxed_ledger, clock=lambda: ISSUED_MORNING
        ).issue_invoice(TENANT_ONE, taxed_draft_id, due_at=DUE_AT)
        presented = IssuedInvoicePresentmentService(taxed_ledger).present_issued_invoice(
            TENANT_ONE, taxed.issued_invoice_id
        )
        self.assertEqual(presented.tax_assessment_id, assessed.tax_assessment_id)
        self.assertEqual(presented.tax_exclusive_amount, HUNDRED)
        self.assertEqual(presented.tax_amount, Decimal("10.00"))
        self.assertEqual(presented.tax_inclusive_amount, Decimal("110.00"))
        payload = presented.as_contract_dict()
        self.assertEqual(payload["tax_assessment_id"], str(assessed.tax_assessment_id))
        self.assertEqual(payload["tax_inclusive_amount"], "110.00")
        self.assertEqual(validate_issued_invoice_presentment(payload), ())
        self.assertNotIn("legal_invoice_number", payload)
        self.assertNotIn("vat_register_id", payload)
        self.assertNotIn("tax_invoice_number", payload)
        self.assertNotIn("nts_approval_number", payload)
        self.assertNotIn("hometax_document_id", payload)
        app = create_http_app(taxed_ledger)
        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/issued-invoices/{taxed.issued_invoice_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["tax_assessment_id"], str(assessed.tax_assessment_id))
        self.assertEqual(get_body["tax_inclusive_amount"], "110.00")
        self.assertEqual(validate_issued_invoice_presentment(get_body), ())
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/issued-invoices",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(len(list_body["issued_invoices"]), 1)
        self.assertNotIn("tax_assessment_id", list_body["issued_invoices"][0])
        self.assertEqual(len(taxed_ledger.journal_proposals), 0)

    def test_http_issue_get_and_paged_list_without_capture(self) -> None:
        """POST issues; GET item and list page metadata and never capture payment."""
        ledger, first_draft_id = draft_known_morning()
        second_draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("20.00"))
        app = create_http_app(ledger)
        refused_status, refused_body = invoke_http(
            app,
            "POST",
            f"/v1/invoice-drafts/{first_draft_id}/issued-invoices",
            {"tenant_reference": TENANT_ONE, "card_pan": "4111111111111111"},
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.issued_invoices), 0)
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            f"/v1/invoice-drafts/{first_draft_id}/issued-invoices",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["issued_invoice_outcome_code"], "accepted")
        self.assertEqual(accepted_body["tax_inclusive_amount"], format_exact_decimal(KNOWN_MORNING_TOTAL))
        self.assertEqual(accepted_body["next_operator_action"], "collect")
        issued_invoice_id = accepted_body["issued_invoice_id"]
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            f"/v1/invoice-drafts/{first_draft_id}/issued-invoices",
            {"tenant_reference": TENANT_ONE, "due_at": "2026-09-16T21:00:00Z"},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["issued_invoice_outcome_code"], "duplicate_replay")
        self.assertEqual(replay_body["issued_invoice_id"], issued_invoice_id)
        self.assertNotIn("due_at", replay_body)
        second_status, second_issue = invoke_http(
            app,
            "POST",
            f"/v1/invoice-drafts/{second_draft_id}/issued-invoices",
            {"tenant_reference": TENANT_ONE, "due_at": "2026-09-16T21:00:00Z"},
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(second_issue["issued_invoice_outcome_code"], "accepted")
        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/issued-invoices/{issued_invoice_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["issued_invoice_id"], issued_invoice_id)
        self.assertEqual(get_body["invoice_draft_id"], str(first_draft_id))
        self.assertEqual(get_body["next_operator_action"], "collect")
        self.assertNotIn("issued_invoice_outcome_code", get_body)
        self.assertNotIn("tax_assessment_id", get_body)
        self.assertEqual(validate_issued_invoice_presentment(get_body), ())
        header_status, header_body = invoke_http(
            app,
            "GET",
            f"/v1/issued-invoices/{issued_invoice_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(header_status, 200)
        self.assertEqual(header_body, get_body)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/issued-invoices",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"issued_invoices", "next_cursor"})
        self.assertNotIn("items", list_body)
        self.assertNotIn("cursor", list_body)
        self.assertEqual(len(list_body["issued_invoices"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        first_summary = list_body["issued_invoices"][0]
        self.assertEqual(
            set(first_summary),
            {
                "issued_invoice_id",
                "invoice_draft_id",
                "currency_code",
                "tax_inclusive_amount",
                "issued_at",
                "next_operator_action",
            },
        )
        page_two_status, page_two = invoke_http(
            app,
            "GET",
            "/v1/issued-invoices",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(page_two_status, 200)
        self.assertEqual(len(page_two["issued_invoices"]), 1)
        self.assertIsNone(page_two["next_cursor"])
        listed_ids = {
            first_summary["issued_invoice_id"],
            page_two["issued_invoices"][0]["issued_invoice_id"],
        }
        self.assertEqual(listed_ids, {issued_invoice_id, second_issue["issued_invoice_id"]})
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/issued-invoices",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["issued_invoices"], [])
        self.assertIsNone(empty_body["next_cursor"])
        issued_events = [
            event
            for event in ledger.webhook_outbox_events.values()
            if event.event_type_code == EVENT_TYPE_INVOICE_ISSUED
        ]
        self.assertEqual(len(issued_events), 2)
        self.assertEqual(
            {str(event.source_id) for event in issued_events},
            {issued_invoice_id, second_issue["issued_invoice_id"]},
        )
        self.assertEqual(len(ledger.journal_proposals), 0)
        self.assertEqual(len(ledger.payment_intents), 0)

    def test_cross_tenant_and_unknown_reads_are_404_without_leak(self) -> None:
        """Missing tenant is 422; unknown or cross-tenant ids stay 404."""
        ledger, invoice_draft_id = draft_known_morning()
        issued = IssuedInvoiceService(ledger).issue_invoice(TENANT_ONE, invoice_draft_id)
        app = create_http_app(ledger)
        missing_status, missing_body = invoke_http(
            app,
            "GET",
            f"/v1/issued-invoices/{issued.issued_invoice_id}",
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/issued-invoices/{issued.issued_invoice_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "issued_invoice_not_found")
        self.assertNotIn("tax_inclusive_amount", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/issued-invoices/{uuid4()}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "issued_invoice_not_found")
        with self.assertRaises(IssuedInvoicePresentmentQueryError) as crossed:
            IssuedInvoicePresentmentService(ledger).present_issued_invoice(
                TENANT_TWO, issued.issued_invoice_id
            )
        self.assertEqual(crossed.exception.rejection_reason_code, "issued_invoice_not_found")
        unknown_issue_status, unknown_issue = invoke_http(
            app,
            "POST",
            f"/v1/invoice-drafts/{uuid4()}/issued-invoices",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_issue_status, 422)
        self.assertEqual(unknown_issue["rejection_reason_code"], "invoice_draft_not_found")
        crossed_issue = IssuedInvoiceService(ledger).issue_invoice(TENANT_TWO, invoice_draft_id)
        self.assertEqual(crossed_issue.issued_invoice_outcome_code, IssuedInvoiceOutcomeCode.REJECTED)
        self.assertEqual(
            crossed_issue.rejection_reason_code,
            IssuedInvoiceRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND,
        )

    def test_list_filters_and_helpers_fail_closed(self) -> None:
        """Illegal filters stay 422; helpers cover page bounds and actions."""
        self.assertEqual(next_operator_action(), "collect")
        self.assertEqual(presentment_action(), "collect")
        ledger, invoice_draft_id = draft_known_morning()
        issued = IssuedInvoiceService(ledger, clock=lambda: ISSUED_MORNING).issue_invoice(
            TENANT_ONE, invoice_draft_id
        )
        app = create_http_app(ledger)
        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/issued-invoices",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")
        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/issued-invoices",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")
        method_status, method_body = invoke_http(
            app,
            "PUT",
            f"/v1/issued-invoices/{issued.issued_invoice_id}",
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        collection_method_status, collection_method_body = invoke_http(
            app,
            "PUT",
            "/v1/issued-invoices",
        )
        self.assertEqual(collection_method_status, 422)
        self.assertEqual(collection_method_body["rejection_reason_code"], "request_invalid")
        nested_method_status, nested_method_body = invoke_http(
            app,
            "PUT",
            f"/v1/invoice-drafts/{invoice_draft_id}/issued-invoices",
        )
        self.assertEqual(nested_method_status, 422)
        self.assertEqual(nested_method_body["rejection_reason_code"], "request_invalid")
        due_status, due_body = invoke_http(
            app,
            "POST",
            f"/v1/invoice-drafts/{invoice_draft_id}/issued-invoices",
            {"tenant_reference": TENANT_ONE, "due_at": "not-a-date"},
        )
        self.assertEqual(due_status, 422)
        self.assertEqual(due_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.IssuedInvoicePresentmentService.list_issued_invoices",
            side_effect=IssuedInvoicePresentmentQueryError("issued_invoice_not_found"),
        ):
            not_found_status, not_found_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/issued-invoices",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(not_found_status, 404)
        self.assertEqual(not_found_body["rejection_reason_code"], "issued_invoice_not_found")
        with mock.patch(
            "metering_billing.http_app.IssuedInvoiceService.issue_invoice",
            side_effect=ValueError("closed"),
        ):
            issue_value_status, issue_value_body = invoke_http(
                create_http_app(ledger),
                "POST",
                f"/v1/invoice-drafts/{invoice_draft_id}/issued-invoices",
                {"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(issue_value_status, 422)
        self.assertEqual(issue_value_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.IssuedInvoicePresentmentService.list_issued_invoices",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/issued-invoices",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        empty = IssuedInvoicePresentmentService()
        self.assertIsNotNone(empty.ledger)
        with self.assertRaises(IssuedInvoicePresentmentQueryError):
            empty.list_issued_invoices(TENANT_ONE)
        service = IssuedInvoicePresentmentService(ledger)
        listed = service.list_issued_invoices(TENANT_ONE, cursor="")
        self.assertEqual(len(listed.issued_invoices), 1)
        with self.assertRaises(IssuedInvoicePresentmentQueryError):
            service.list_issued_invoices(TENANT_ONE, page_limit=True)
        with self.assertRaises(IssuedInvoicePresentmentQueryError):
            service.list_issued_invoices(TENANT_ONE, page_limit=101)
        with self.assertRaises(IssuedInvoicePresentmentQueryError):
            service.list_issued_invoices(TENANT_ONE, page_limit=1.5)
        with self.assertRaises(IssuedInvoicePresentmentQueryError):
            service.list_issued_invoices(TENANT_ONE, page_limit="abc")
        with self.assertRaises(IssuedInvoicePresentmentQueryError):
            service.list_issued_invoices(TENANT_ONE, page_limit="0")
        list_missing_status, list_missing_body = invoke_http(app, "GET", "/v1/issued-invoices")
        self.assertEqual(list_missing_status, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "tenant_not_found")
        default_limit = service.list_issued_invoices(TENANT_ONE, page_limit=None)
        self.assertEqual(len(default_limit.issued_invoices), 1)
        empty_limit = service.list_issued_invoices(TENANT_ONE, page_limit="")
        self.assertEqual(len(empty_limit.issued_invoices), 1)
        self.assertEqual(
            service.list_issued_invoices(TENANT_ONE, page_limit=50).next_cursor,
            None,
        )
        with self.assertRaises(IssuedInvoicePresentmentQueryError):
            service.present_issued_invoice(TENANT_ONE, uuid4())
        with self.assertRaises(IssuedInvoicePresentmentQueryError):
            service.present_issued_invoice("", issued.issued_invoice_id)
        self.assertEqual(listed.issued_invoices[0].issued_at, ISSUED_MORNING)
        self.assertIsInstance(issued.issued_invoice_id, UUID)
        writer = IssuedInvoiceService()
        self.assertIsNotNone(writer.ledger)
        missing_tenant = writer.issue_invoice(TENANT_ONE, invoice_draft_id)
        self.assertEqual(missing_tenant.issued_invoice_outcome_code, IssuedInvoiceOutcomeCode.REJECTED)
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            IssuedInvoiceRejectionReasonCode.TENANT_NOT_FOUND,
        )
        invalid_due = IssuedInvoiceService(ledger).issue_invoice(
            TENANT_ONE, invoice_draft_id, due_at="yesterday"
        )
        self.assertEqual(invalid_due.issued_invoice_outcome_code, IssuedInvoiceOutcomeCode.REJECTED)
        self.assertEqual(
            invalid_due.rejection_reason_code,
            IssuedInvoiceRejectionReasonCode.REQUEST_INVALID,
        )
        stored = next(iter(ledger.issued_invoices.values()))
        with self.assertRaises(ValueError):
            ledger.insert_issued_invoice(stored, stored.issued_invoice_lines)
        with self.assertRaises(ValueError):
            ledger.insert_issued_invoice(
                stored.__class__(
                    issued_invoice_id=generate_record_id(),
                    tenant_account_id=stored.tenant_account_id,
                    invoice_draft_id=stored.invoice_draft_id,
                    issued_invoice_contract_version=stored.issued_invoice_contract_version,
                    rating_run_id=stored.rating_run_id,
                    usage_snapshot_hash=stored.usage_snapshot_hash,
                    source_payload_hash=stored.source_payload_hash,
                    currency_code=stored.currency_code,
                    tax_exclusive_amount=stored.tax_exclusive_amount,
                    tax_amount=stored.tax_amount,
                    tax_inclusive_amount=stored.tax_inclusive_amount,
                    issued_invoice_status=stored.issued_invoice_status,
                    issued_at=stored.issued_at,
                    due_at=stored.due_at,
                    issued_invoice_lines=stored.issued_invoice_lines,
                ),
                stored.issued_invoice_lines,
            )
        unknown_outcome = issued.__class__(
            issued_invoice_outcome_code="posted",  # type: ignore[arg-type]
            issued_invoice_contract_version=1,
            issued_invoice_id=issued.issued_invoice_id,
            invoice_draft_id=issued.invoice_draft_id,
            tenant_reference=TENANT_ONE,
            rating_run_id=issued.rating_run_id,
            usage_snapshot_hash=issued.usage_snapshot_hash,
            currency_code="USD",
            tax_exclusive_amount=KNOWN_MORNING_TOTAL,
            tax_amount=Decimal("0"),
            tax_inclusive_amount=KNOWN_MORNING_TOTAL,
            issued_invoice_status="issued",
            issued_at=ISSUED_MORNING,
            due_at=None,
            source_payload_hash=issued.source_payload_hash,
            idempotency_key=issued.idempotency_key,
            next_operator_action="collect",
            rejection_reason_code=None,
            issued_invoice_lines=issued.issued_invoice_lines,
        )
        with self.assertRaises(ValueError):
            unknown_outcome.as_contract_dict()
        rejected = IssuedInvoiceService(ledger).issue_invoice("", invoice_draft_id)
        self.assertEqual(validate_issued_invoice(rejected.as_contract_dict()), ())
        self.assertEqual(generate_record_id().__class__, UUID)
        empty_due = IssuedInvoiceService(ledger).issue_invoice(
            TENANT_ONE, invoice_draft_id, due_at=""
        )
        self.assertEqual(empty_due.issued_invoice_outcome_code, IssuedInvoiceOutcomeCode.DUPLICATE_REPLAY)
        self.assertIsNone(empty_due.due_at)
        naive_due = IssuedInvoiceService(ledger).issue_invoice(
            TENANT_ONE, invoice_draft_id, due_at=datetime(2026, 9, 16, 21, 0)
        )
        self.assertEqual(
            naive_due.rejection_reason_code,
            IssuedInvoiceRejectionReasonCode.REQUEST_INVALID,
        )
        typed_due = IssuedInvoiceService(ledger).issue_invoice(
            TENANT_ONE, invoice_draft_id, due_at=1
        )
        self.assertEqual(
            typed_due.rejection_reason_code,
            IssuedInvoiceRejectionReasonCode.REQUEST_INVALID,
        )
        other_draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("30.00"))
        with mock.patch(
            "metering_billing.issued_invoice.parse_invoice_amount",
            side_effect=ExactDecimalError("bad"),
        ):
            invalid_amount = IssuedInvoiceService(ledger).issue_invoice(
                TENANT_ONE, other_draft_id
            )
        self.assertEqual(
            invalid_amount.rejection_reason_code,
            IssuedInvoiceRejectionReasonCode.REQUEST_INVALID,
        )
        fake_tax = mock.Mock(
            tax_exclusive_amount=Decimal("30.00"),
            tax_amount=Decimal("3.00"),
            tax_inclusive_amount=Decimal("999.00"),
        )
        with mock.patch.object(
            ledger, "find_tax_assessment_for_draft", return_value=fake_tax
        ):
            unbalanced = IssuedInvoiceService(ledger).issue_invoice(
                TENANT_ONE, other_draft_id
            )
        self.assertEqual(
            unbalanced.rejection_reason_code,
            IssuedInvoiceRejectionReasonCode.REQUEST_INVALID,
        )
        with self.assertRaises(ValueError):
            ledger.insert_issued_invoice(
                stored.__class__(
                    issued_invoice_id=generate_record_id(),
                    tenant_account_id=stored.tenant_account_id,
                    invoice_draft_id=other_draft_id,
                    issued_invoice_contract_version=stored.issued_invoice_contract_version,
                    rating_run_id=stored.rating_run_id,
                    usage_snapshot_hash=stored.usage_snapshot_hash,
                    source_payload_hash=stored.source_payload_hash,
                    currency_code=stored.currency_code,
                    tax_exclusive_amount=stored.tax_exclusive_amount,
                    tax_amount=stored.tax_amount,
                    tax_inclusive_amount=stored.tax_inclusive_amount,
                    issued_invoice_status="draft",
                    issued_at=stored.issued_at,
                    due_at=stored.due_at,
                    issued_invoice_lines=stored.issued_invoice_lines,
                ),
                stored.issued_invoice_lines,
            )
        with self.assertRaises(ValueError):
            ledger.insert_issued_invoice(
                stored.__class__(
                    issued_invoice_id=generate_record_id(),
                    tenant_account_id=stored.tenant_account_id,
                    invoice_draft_id=other_draft_id,
                    issued_invoice_contract_version=stored.issued_invoice_contract_version,
                    rating_run_id=stored.rating_run_id,
                    usage_snapshot_hash=stored.usage_snapshot_hash,
                    source_payload_hash=stored.source_payload_hash,
                    currency_code=stored.currency_code,
                    tax_exclusive_amount=Decimal("30.00"),
                    tax_amount=Decimal("3.00"),
                    tax_inclusive_amount=Decimal("999.00"),
                    issued_invoice_status=stored.issued_invoice_status,
                    issued_at=stored.issued_at,
                    due_at=stored.due_at,
                    issued_invoice_lines=stored.issued_invoice_lines,
                ),
                stored.issued_invoice_lines,
            )
        taxed_ledger = seed_rated_ledger()
        TaxRateService(taxed_ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        taxed_draft_id = insert_commercial_draft(taxed_ledger, TENANT_ONE, "USD", HUNDRED)
        TaxAssessmentService(taxed_ledger).assess_tax(TENANT_ONE, taxed_draft_id, 1)
        taxed = IssuedInvoiceService(taxed_ledger, clock=lambda: ISSUED_MORNING).issue_invoice(
            TENANT_ONE, taxed_draft_id, due_at=DUE_AT
        )
        presented = IssuedInvoicePresentmentService(taxed_ledger).present_issued_invoice(
            TENANT_ONE, taxed.issued_invoice_id
        )
        self.assertEqual(presented.due_at, DUE_AT)
        self.assertEqual(presented.as_contract_dict()["due_at"], "2026-09-16T21:00:00Z")
        assessed = taxed_ledger.find_tax_assessment_for_draft(
            taxed_ledger.resolve_tenant(TENANT_ONE)[0].tenant_account_id,
            taxed_draft_id,
        )
        assert assessed is not None
        self.assertEqual(
            presented.as_contract_dict()["tax_assessment_id"],
            str(assessed.tax_assessment_id),
        )
        none_reason = rejected.__class__(
            issued_invoice_outcome_code=IssuedInvoiceOutcomeCode.REJECTED,
            issued_invoice_contract_version=1,
            issued_invoice_id=None,
            invoice_draft_id=None,
            tenant_reference=None,
            rating_run_id=None,
            usage_snapshot_hash=None,
            currency_code=None,
            tax_exclusive_amount=None,
            tax_amount=None,
            tax_inclusive_amount=None,
            issued_invoice_status=None,
            issued_at=None,
            due_at=None,
            source_payload_hash=None,
            idempotency_key=None,
            next_operator_action="collect",
            rejection_reason_code=None,
            issued_invoice_lines=(),
        )
        self.assertEqual(
            none_reason.as_contract_dict()["rejection_reason_code"],
            "invoice_draft_not_found",
        )
        with self.assertRaises(ValueError):
            none_reason.as_webhook_event_data()
