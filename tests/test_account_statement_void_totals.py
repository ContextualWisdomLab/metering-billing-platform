"""Account-statement tests for unused issued-invoice and issued-credit-note voids."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    AccountStatementPresentmentService,
    CollectionCaseService,
    CreditAdjustmentService,
    CreditNoteApplicationService,
    IssuedCreditNoteService,
    IssuedCreditNoteVoidService,
    IssuedInvoiceService,
    IssuedInvoiceVoidService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.contracts import validate_account_statement_presentment
from metering_billing.errors import AccountStatementPresentmentQueryError, ExactDecimalError
from metering_billing.usage_ledger import StoredInvoiceDraft, StoredInvoiceDraftLine, generate_record_id
from test_account_statement_presentment import ACCOUNT_THREE, _account_id, _insert_account_draft, _statement_path
from test_credit_note_application import issue_morning_credit_then_open_case
from test_http_app import invoke_http
from test_issued_credit_note_void import issue_known_morning_credit_note
from test_issued_invoice_void import issue_known_morning_invoice
from test_tax_assessment import HUNDRED_INT
from test_usage_ingestion import ACCOUNT_ONE, TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL


AS_OF = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
VOIDED_AT = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)


def _void_morning_invoice(open_case: bool = True):
    """Issue then void the known-morning unused invoice."""
    ledger, issued, collection = issue_known_morning_invoice(open_case=open_case)
    voided = IssuedInvoiceVoidService(ledger, clock=lambda: VOIDED_AT).void_issued_invoice(
        TENANT_ONE, issued.issued_invoice_id
    )
    return ledger, issued, voided, collection


def _void_morning_credit_note(open_case: bool = False):
    """Issue then void the known-morning unused credit note."""
    ledger, issued, collection = issue_known_morning_credit_note(open_case=open_case)
    voided = IssuedCreditNoteVoidService(ledger, clock=lambda: VOIDED_AT).void_issued_credit_note(
        TENANT_ONE, issued.issued_credit_note_id
    )
    return ledger, issued, voided, collection


class AccountStatementVoidTotalTests(unittest.TestCase):
    """Verify unused voids roll up separately from issued and applied totals."""

    def test_unused_invoice_void_is_reported_without_rewriting_issued(self) -> None:
        """issued_invoice_total stays the snapshot; voided_invoice_total is the unused void."""
        ledger, issued, voided, collection = _void_morning_invoice()
        prior_voids = len(ledger.issued_invoice_voids)
        prior_journals = len(ledger.journal_proposals)
        prior_receipts = len(ledger.payment_receipts)
        case_status_before = ledger.collection_cases[collection.collection_case_id].collection_case_status
        presented = AccountStatementPresentmentService(
            ledger, clock=lambda: AS_OF
        ).present_account_statement(TENANT_ONE, _account_id(ledger))
        replay = AccountStatementPresentmentService(
            ledger, clock=lambda: AS_OF
        ).present_account_statement(TENANT_ONE, _account_id(ledger))
        self.assertEqual(presented.as_contract_dict(), replay.as_contract_dict())
        usd = presented.currencies[0]
        self.assertEqual(usd.issued_invoice_total, issued.tax_inclusive_amount)
        self.assertEqual(usd.issued_invoice_total, KNOWN_MORNING_TOTAL)
        self.assertEqual(usd.voided_invoice_total, voided.voided_amount)
        self.assertEqual(usd.voided_invoice_total, KNOWN_MORNING_TOTAL)
        self.assertEqual(usd.open_collection_remaining, Decimal("0"))
        self.assertEqual(usd.applied_credit_total, Decimal("0"))
        self.assertEqual(usd.voided_credit_total, Decimal("0"))
        payload = presented.as_contract_dict()
        self.assertEqual(validate_account_statement_presentment(payload), ())
        self.assertEqual(
            payload["currencies"][0]["voided_invoice_total"],
            format_exact_decimal(KNOWN_MORNING_TOTAL),
        )
        self.assertEqual(payload["currencies"][0]["voided_credit_total"], "0")
        self.assertIsInstance(payload["currencies"][0]["voided_invoice_total"], str)
        self.assertNotIsInstance(payload["currencies"][0]["voided_invoice_total"], float)
        self.assertNotIn("statutory_account_id", payload)
        self.assertNotIn("journal_entry_id", payload)
        self.assertEqual(
            ledger.collection_cases[collection.collection_case_id].collection_case_status,
            case_status_before,
        )
        self.assertEqual(len(ledger.issued_invoice_voids), prior_voids)
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        self.assertEqual(len(ledger.payment_receipts), prior_receipts)

    def test_unused_credit_void_is_not_counted_as_applied_credit(self) -> None:
        """applied_credit_total stays applied-only; unused voided notes are voided_credit_total."""
        ledger, issued, voided, _collection = _void_morning_credit_note()
        prior_applications = len(ledger.credit_note_applications)
        prior_voids = len(ledger.issued_credit_note_voids)
        presented = AccountStatementPresentmentService(
            ledger, clock=lambda: AS_OF
        ).present_account_statement(TENANT_ONE, _account_id(ledger))
        usd = presented.currencies[0]
        self.assertEqual(usd.applied_credit_total, Decimal("0"))
        self.assertEqual(usd.voided_credit_total, issued.tax_inclusive_amount)
        self.assertEqual(usd.voided_credit_total, voided.voided_amount)
        self.assertEqual(usd.voided_credit_total, KNOWN_MORNING_TOTAL)
        self.assertEqual(usd.voided_invoice_total, Decimal("0"))
        self.assertEqual(usd.issued_invoice_total, Decimal("0"))
        applied_ledger, applied_note, applied_case = issue_morning_credit_then_open_case()
        CreditNoteApplicationService(applied_ledger).apply_credit_note(
            TENANT_ONE, applied_note.issued_credit_note_id, applied_case.collection_case_id
        )
        applied = AccountStatementPresentmentService(
            applied_ledger, clock=lambda: AS_OF
        ).present_account_statement(TENANT_ONE, _account_id(applied_ledger))
        self.assertEqual(applied.currencies[0].applied_credit_total, KNOWN_MORNING_TOTAL)
        self.assertEqual(applied.currencies[0].voided_credit_total, Decimal("0"))
        self.assertEqual(len(ledger.credit_note_applications), prior_applications)
        self.assertEqual(len(ledger.issued_credit_note_voids), prior_voids)
        self.assertEqual(len(applied_ledger.issued_credit_note_voids), 0)

    def test_void_currencies_stay_separate_and_mixed_drafts_are_omitted(self) -> None:
        """KRW voids never mix into USD; mixed and lineless drafts cannot invent totals."""
        ledger, issued, voided, _collection = _void_morning_invoice()
        krw_draft_id = _insert_account_draft(ledger, TENANT_ONE, "KRW", HUNDRED_INT)
        krw_issued = IssuedInvoiceService(ledger).issue_invoice(TENANT_ONE, krw_draft_id)
        CollectionCaseService(ledger).open_collection_case(TENANT_ONE, krw_draft_id)
        krw_voided = IssuedInvoiceVoidService(ledger).void_issued_invoice(
            TENANT_ONE, krw_issued.issued_invoice_id
        )
        ledger.register_billing_account(TENANT_ONE, ACCOUNT_THREE)
        other = ledger.billing_accounts[ACCOUNT_THREE]
        tenant = ledger.require_tenant(TENANT_ONE)
        account = ledger.billing_accounts[ACCOUNT_ONE]
        mixed_id = generate_record_id()
        own_line = StoredInvoiceDraftLine(
            invoice_draft_line_id=generate_record_id(),
            invoice_draft_id=mixed_id,
            tenant_account_id=tenant.tenant_account_id,
            billing_account_id=account.billing_account_id,
            billing_account_reference=account.billing_account_reference,
            meter_definition_id=ledger.meter_definitions[0].meter_definition_id,
            meter_code="gen_ai_output_token",
            unit_code="token",
            rated_quantity=Decimal("1"),
            unit_price_amount=Decimal("10"),
            line_total_amount=Decimal("10"),
            line_number=1,
        )
        other_line = replace(
            own_line,
            invoice_draft_line_id=generate_record_id(),
            billing_account_id=other.billing_account_id,
            billing_account_reference=other.billing_account_reference,
            line_total_amount=Decimal("5"),
            unit_price_amount=Decimal("5"),
            line_number=2,
        )
        ledger.insert_invoice_draft(
            StoredInvoiceDraft(
                invoice_draft_id=mixed_id,
                tenant_account_id=tenant.tenant_account_id,
                rating_run_id=generate_record_id(),
                usage_snapshot_hash="sha256:" + ("d" * 64),
                currency_code="USD",
                invoice_draft_status="draft",
                drafted_total_amount=Decimal("15"),
                recorded_at=AS_OF,
                invoice_draft_lines=(own_line, other_line),
            ),
            (own_line, other_line),
        )
        mixed_issued = IssuedInvoiceService(ledger).issue_invoice(TENANT_ONE, mixed_id)
        CollectionCaseService(ledger).open_collection_case(TENANT_ONE, mixed_id)
        IssuedInvoiceVoidService(ledger).void_issued_invoice(
            TENANT_ONE, mixed_issued.issued_invoice_id
        )
        three_draft = _insert_account_draft(
            ledger, TENANT_ONE, "USD", Decimal("10"), ACCOUNT_THREE
        )
        three_credit = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, three_draft, Decimal("10"), "rating_correction"
        )
        three_note = IssuedCreditNoteService(ledger).issue_credit_note(
            TENANT_ONE, three_credit.credit_adjustment_id
        )
        IssuedCreditNoteVoidService(ledger).void_issued_credit_note(
            TENANT_ONE, three_note.issued_credit_note_id
        )
        presented = AccountStatementPresentmentService(
            ledger, clock=lambda: AS_OF
        ).present_account_statement(TENANT_ONE, account.billing_account_id)
        self.assertEqual([item.currency_code for item in presented.currencies], ["KRW", "USD"])
        krw, usd = presented.currencies
        self.assertEqual(krw.issued_invoice_total, HUNDRED_INT)
        self.assertEqual(krw.voided_invoice_total, krw_voided.voided_amount)
        self.assertEqual(krw.voided_credit_total, Decimal("0"))
        self.assertEqual(usd.issued_invoice_total, issued.tax_inclusive_amount)
        self.assertEqual(usd.voided_invoice_total, voided.voided_amount)
        self.assertEqual(usd.voided_credit_total, Decimal("0"))
        other_statement = AccountStatementPresentmentService(
            ledger, clock=lambda: AS_OF
        ).present_account_statement(TENANT_ONE, other.billing_account_id)
        self.assertEqual(other_statement.currencies[0].voided_credit_total, Decimal("10"))
        self.assertEqual(other_statement.currencies[0].voided_invoice_total, Decimal("0"))
        self.assertEqual(other_statement.currencies[0].issued_invoice_total, Decimal("0"))
        self.assertEqual(other_statement.currencies[0].applied_credit_total, Decimal("0"))

    def test_http_statement_includes_void_totals_without_writing(self) -> None:
        """GET statement publishes both void totals and does not grow money stores."""
        ledger, _issued, voided, _collection = _void_morning_invoice()
        credit_ledger, credit_issued, credit_voided, _credit_case = _void_morning_credit_note()
        billing_account_id = _account_id(ledger)
        app = create_http_app(ledger, clock=lambda: AS_OF)
        status, body = invoke_http(
            app,
            "GET",
            _statement_path(billing_account_id),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(validate_account_statement_presentment(body), ())
        self.assertEqual(
            body["currencies"][0]["issued_invoice_total"],
            format_exact_decimal(KNOWN_MORNING_TOTAL),
        )
        self.assertEqual(
            body["currencies"][0]["voided_invoice_total"],
            format_exact_decimal(voided.voided_amount),
        )
        self.assertEqual(body["currencies"][0]["voided_credit_total"], "0")
        credit_status, credit_body = invoke_http(
            create_http_app(credit_ledger, clock=lambda: AS_OF),
            "GET",
            _statement_path(_account_id(credit_ledger)),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(credit_status, 200)
        self.assertEqual(
            credit_body["currencies"][0]["voided_credit_total"],
            format_exact_decimal(credit_voided.voided_amount),
        )
        self.assertEqual(credit_body["currencies"][0]["applied_credit_total"], "0")
        self.assertEqual(credit_body["currencies"][0]["voided_invoice_total"], "0")
        self.assertEqual(len(ledger.issued_invoice_voids), 1)
        self.assertEqual(len(credit_ledger.issued_credit_note_voids), 1)
        self.assertEqual(len(ledger.payment_receipts), 0)
        self.assertIsNotNone(credit_issued.issued_credit_note_id)

    def test_other_tenant_and_ieee_void_amounts_fail_closed(self) -> None:
        """Cross-tenant stays 403; IEEE voided amounts cannot become statement dollars."""
        ledger, _issued, voided, _collection = _void_morning_invoice()
        tenant_two = ledger.require_tenant(TENANT_TWO)
        two_account = next(
            account
            for account in ledger.billing_accounts.values()
            if account.tenant_account_id == tenant_two.tenant_account_id
        )
        service = AccountStatementPresentmentService(ledger, clock=lambda: AS_OF)
        one = service.present_account_statement(TENANT_ONE, _account_id(ledger))
        self.assertEqual(one.currencies[0].voided_invoice_total, voided.voided_amount)
        two = service.present_account_statement(TENANT_TWO, two_account.billing_account_id)
        self.assertEqual(two.currencies, ())
        with self.assertRaises(AccountStatementPresentmentQueryError) as forbidden:
            service.present_account_statement(TENANT_ONE, two_account.billing_account_id)
        self.assertEqual(forbidden.exception.rejection_reason_code, "billing_account_forbidden")
        stored_void = ledger.get_issued_invoice_void(voided.issued_invoice_void_id)
        ledger.issued_invoice_voids[voided.issued_invoice_void_id] = replace(
            stored_void, voided_amount=0.003705  # type: ignore[arg-type]
        )
        with self.assertRaises(ExactDecimalError):
            service.present_account_statement(TENANT_ONE, _account_id(ledger))
        credit_ledger, _credit_issued, credit_voided, _credit_case = _void_morning_credit_note()
        stored_credit_void = credit_ledger.get_issued_credit_note_void(
            credit_voided.issued_credit_note_void_id
        )
        credit_ledger.issued_credit_note_voids[credit_voided.issued_credit_note_void_id] = replace(
            stored_credit_void, voided_amount=0.003705  # type: ignore[arg-type]
        )
        with self.assertRaises(ExactDecimalError):
            AccountStatementPresentmentService(
                credit_ledger, clock=lambda: AS_OF
            ).present_account_statement(TENANT_ONE, _account_id(credit_ledger))
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.present_account_statement(TENANT_ONE, _account_id(ledger))
        empty = AccountStatementPresentmentService()
        with self.assertRaises(AccountStatementPresentmentQueryError) as missing_tenant:
            empty.present_account_statement(TENANT_ONE, uuid4())
        self.assertEqual(missing_tenant.exception.rejection_reason_code, "tenant_not_found")


if __name__ == "__main__":
    unittest.main()
