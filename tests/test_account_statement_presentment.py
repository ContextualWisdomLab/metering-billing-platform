"""Account-statement tests for currency rollup, attribution, and fail-closed reads."""

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
    CollectionWriteOffService,
    CreditAdjustmentService,
    CreditNoteApplicationService,
    IssuedCreditNoteService,
    IssuedInvoiceService,
    MemoryUsageLedger,
    PaymentIntentService,
    PaymentSettlementService,
    UnappliedCashApplicationService,
    UnappliedCashRefundService,
    UnappliedCashService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.contracts import validate_account_statement_presentment
from metering_billing.errors import AccountStatementPresentmentQueryError, ExactDecimalError
from metering_billing.usage_ledger import StoredInvoiceDraft, StoredInvoiceDraftLine, generate_record_id
from test_credit_note_application import issue_morning_credit_then_open_case
from test_http_app import invoke_http
from test_payment_intent import open_known_morning_case
from test_payment_receipt_presentment import apply_known_morning_receipt
from test_tax_assessment import HUNDRED_INT, insert_commercial_draft
from test_usage_ingestion import ACCOUNT_ONE, TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL, seed_rated_ledger


AS_OF = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
LEFTOVER = Decimal("0.001")
ACCOUNT_THREE = "urn:cwl:tenant_001:billing_account:019d9001"


def _account_id(ledger: MemoryUsageLedger, billing_account_reference: str = ACCOUNT_ONE):
    """Return the stored billing-account identifier for one catalog URN."""
    return ledger.billing_accounts[billing_account_reference].billing_account_id


def _statement_path(billing_account_id) -> str:
    """Return the nested statement path for one billing account."""
    return f"/v1/billing-accounts/{billing_account_id}/statement"


def _insert_account_draft(
    ledger: MemoryUsageLedger,
    tenant_reference: str,
    currency_code: str,
    drafted_total: Decimal,
    billing_account_reference: str = ACCOUNT_ONE,
):
    """Persist one exclusive-line draft so the statement can attribute money."""
    tenant = ledger.require_tenant(tenant_reference)
    account = ledger.billing_accounts[billing_account_reference]
    draft_id = generate_record_id()
    line = StoredInvoiceDraftLine(
        invoice_draft_line_id=generate_record_id(),
        invoice_draft_id=draft_id,
        tenant_account_id=tenant.tenant_account_id,
        billing_account_id=account.billing_account_id,
        billing_account_reference=account.billing_account_reference,
        meter_definition_id=ledger.meter_definitions[0].meter_definition_id,
        meter_code="gen_ai_output_token",
        unit_code="token",
        rated_quantity=Decimal("1"),
        unit_price_amount=drafted_total,
        line_total_amount=drafted_total,
        line_number=1,
    )
    ledger.insert_invoice_draft(
        StoredInvoiceDraft(
            invoice_draft_id=draft_id,
            tenant_account_id=tenant.tenant_account_id,
            rating_run_id=generate_record_id(),
            usage_snapshot_hash="sha256:" + ("b" * 64),
            currency_code=currency_code,
            invoice_draft_status="draft",
            drafted_total_amount=drafted_total,
            recorded_at=AS_OF,
            invoice_draft_lines=(line,),
        ),
        (line,),
    )
    return draft_id


class AccountStatementPresentmentTests(unittest.TestCase):
    """Verify statement totals stay exact, account-scoped, and read-only."""

    def test_issued_and_open_remaining_roll_up_for_one_account(self) -> None:
        """A morning issued invoice and open case become one USD statement."""
        ledger, collection_case_id = open_known_morning_case()
        stored = ledger.collection_cases[collection_case_id]
        issued = IssuedInvoiceService(ledger).issue_invoice(TENANT_ONE, stored.invoice_draft_id)
        billing_account_id = _account_id(ledger)
        presented = AccountStatementPresentmentService(
            ledger, clock=lambda: AS_OF
        ).present_account_statement(TENANT_ONE, billing_account_id)
        replay = AccountStatementPresentmentService(
            ledger, clock=lambda: AS_OF
        ).present_account_statement(TENANT_ONE, billing_account_id)
        self.assertEqual(presented.as_contract_dict(), replay.as_contract_dict())
        self.assertEqual(presented.tenant_reference, TENANT_ONE)
        self.assertEqual(presented.billing_account_id, billing_account_id)
        self.assertEqual(presented.billing_account_reference, ACCOUNT_ONE)
        self.assertEqual(presented.as_of, AS_OF)
        self.assertEqual(len(presented.currencies), 1)
        usd = presented.currencies[0]
        self.assertEqual(usd.currency_code, "USD")
        self.assertEqual(usd.issued_invoice_total, issued.tax_inclusive_amount)
        self.assertEqual(usd.issued_invoice_total, KNOWN_MORNING_TOTAL)
        self.assertEqual(usd.open_collection_remaining, KNOWN_MORNING_TOTAL)
        self.assertEqual(usd.voided_invoice_total, Decimal("0"))
        self.assertEqual(usd.applied_credit_total, Decimal("0"))
        self.assertEqual(usd.voided_credit_total, Decimal("0"))
        self.assertEqual(usd.write_off_total, Decimal("0"))
        self.assertEqual(usd.parked_unapplied_cash, Decimal("0"))
        self.assertEqual(usd.refunded_unapplied_cash, Decimal("0"))
        payload = presented.as_contract_dict()
        self.assertEqual(validate_account_statement_presentment(payload), ())
        self.assertIsInstance(payload["currencies"][0]["issued_invoice_total"], str)
        self.assertNotIsInstance(payload["currencies"][0]["issued_invoice_total"], float)
        self.assertNotIn("card_pan", payload)
        self.assertNotIn("statutory_account_id", payload)
        self.assertEqual(len(ledger.journal_proposals), 0)
        self.assertEqual(len(ledger.payment_receipts), 0)

    def test_currencies_stay_separate_and_empty_account_has_no_buckets(self) -> None:
        """KRW never mixes into USD; a registered account with no facts is empty."""
        ledger, collection_case_id = open_known_morning_case()
        stored = ledger.collection_cases[collection_case_id]
        IssuedInvoiceService(ledger).issue_invoice(TENANT_ONE, stored.invoice_draft_id)
        krw_draft_id = _insert_account_draft(ledger, TENANT_ONE, "KRW", HUNDRED_INT)
        IssuedInvoiceService(ledger).issue_invoice(TENANT_ONE, krw_draft_id)
        CollectionCaseService(ledger).open_collection_case(TENANT_ONE, krw_draft_id)
        presented = AccountStatementPresentmentService(
            ledger, clock=lambda: AS_OF
        ).present_account_statement(TENANT_ONE, _account_id(ledger))
        self.assertEqual([item.currency_code for item in presented.currencies], ["KRW", "USD"])
        krw, usd = presented.currencies
        self.assertEqual(krw.issued_invoice_total, HUNDRED_INT)
        self.assertEqual(krw.open_collection_remaining, HUNDRED_INT)
        self.assertEqual(usd.issued_invoice_total, KNOWN_MORNING_TOTAL)
        self.assertEqual(usd.open_collection_remaining, KNOWN_MORNING_TOTAL)
        empty = AccountStatementPresentmentService(seed_rated_ledger(), clock=lambda: AS_OF)
        empty_statement = empty.present_account_statement(TENANT_ONE, _account_id(empty.ledger))
        self.assertEqual(empty_statement.currencies, ())
        self.assertEqual(validate_account_statement_presentment(empty_statement.as_contract_dict()), ())

    def test_applied_credits_write_offs_and_leftover_buckets(self) -> None:
        """Applied credits, write-offs, parked leftover, and refunds stay exact."""
        credit_ledger, issued, collection = issue_morning_credit_then_open_case()
        CreditNoteApplicationService(credit_ledger).apply_credit_note(
            TENANT_ONE, issued.issued_credit_note_id, collection.collection_case_id
        )
        credited = AccountStatementPresentmentService(
            credit_ledger, clock=lambda: AS_OF
        ).present_account_statement(TENANT_ONE, _account_id(credit_ledger))
        self.assertEqual(credited.currencies[0].applied_credit_total, KNOWN_MORNING_TOTAL)
        self.assertEqual(credited.currencies[0].voided_credit_total, Decimal("0"))
        self.assertEqual(credited.currencies[0].open_collection_remaining, Decimal("0"))
        write_off_ledger, write_off_case_id = open_known_morning_case()
        written = CollectionWriteOffService(write_off_ledger).write_off_collection_case(
            TENANT_ONE, write_off_case_id
        )
        written_statement = AccountStatementPresentmentService(
            write_off_ledger, clock=lambda: AS_OF
        ).present_account_statement(TENANT_ONE, _account_id(write_off_ledger))
        self.assertEqual(written_statement.currencies[0].write_off_total, written.write_off_amount)
        self.assertEqual(written_statement.currencies[0].open_collection_remaining, Decimal("0"))
        park_ledger, payment_receipt_id, _intent_id, _case_id = apply_known_morning_receipt()
        parked = UnappliedCashService(park_ledger).park_unapplied_cash(
            TENANT_ONE, payment_receipt_id, unapplied_amount=LEFTOVER
        )
        parked_statement = AccountStatementPresentmentService(
            park_ledger, clock=lambda: AS_OF
        ).present_account_statement(TENANT_ONE, _account_id(park_ledger))
        self.assertEqual(parked_statement.currencies[0].parked_unapplied_cash, LEFTOVER)
        self.assertEqual(parked_statement.currencies[0].refunded_unapplied_cash, Decimal("0"))
        refund_ledger, refund_receipt_id, _refund_intent, _refund_case = apply_known_morning_receipt()
        refund_leftover = UnappliedCashService(refund_ledger).park_unapplied_cash(
            TENANT_ONE, refund_receipt_id, unapplied_amount=LEFTOVER
        )
        UnappliedCashRefundService(refund_ledger).refund_unapplied_cash(
            TENANT_ONE, refund_leftover.unapplied_cash_id
        )
        refunded = AccountStatementPresentmentService(
            refund_ledger, clock=lambda: AS_OF
        ).present_account_statement(TENANT_ONE, _account_id(refund_ledger))
        self.assertEqual(refunded.currencies[0].parked_unapplied_cash, Decimal("0"))
        self.assertEqual(refunded.currencies[0].refunded_unapplied_cash, LEFTOVER)
        apply_ledger, apply_receipt_id, _apply_intent, _apply_source = apply_known_morning_receipt()
        apply_leftover = UnappliedCashService(apply_ledger).park_unapplied_cash(
            TENANT_ONE, apply_receipt_id, unapplied_amount=LEFTOVER
        )
        target_draft_id = _insert_account_draft(apply_ledger, TENANT_ONE, "USD", KNOWN_MORNING_TOTAL)
        target = CollectionCaseService(apply_ledger).open_collection_case(
            TENANT_ONE, target_draft_id
        )
        UnappliedCashApplicationService(apply_ledger).apply_unapplied_cash(
            TENANT_ONE, apply_leftover.unapplied_cash_id, target.collection_case_id
        )
        applied = AccountStatementPresentmentService(
            apply_ledger, clock=lambda: AS_OF
        ).present_account_statement(TENANT_ONE, _account_id(apply_ledger))
        self.assertEqual(applied.currencies[0].parked_unapplied_cash, Decimal("0"))
        self.assertEqual(
            applied.currencies[0].open_collection_remaining,
            KNOWN_MORNING_TOTAL - LEFTOVER,
        )
        self.assertIsNotNone(parked.unapplied_cash_id)

    def test_mixed_or_empty_draft_lines_are_not_attributed(self) -> None:
        """A split draft and a lineless draft cannot invent an account balance."""
        ledger, collection_case_id = open_known_morning_case()
        stored = ledger.collection_cases[collection_case_id]
        IssuedInvoiceService(ledger).issue_invoice(TENANT_ONE, stored.invoice_draft_id)
        ledger.register_billing_account(TENANT_ONE, ACCOUNT_THREE)
        other = ledger.billing_accounts[ACCOUNT_THREE]
        mixed_id = generate_record_id()
        tenant = ledger.require_tenant(TENANT_ONE)
        account = ledger.billing_accounts[ACCOUNT_ONE]
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
                usage_snapshot_hash="sha256:" + ("c" * 64),
                currency_code="USD",
                invoice_draft_status="draft",
                drafted_total_amount=Decimal("15"),
                recorded_at=AS_OF,
                invoice_draft_lines=(own_line, other_line),
            ),
            (own_line, other_line),
        )
        IssuedInvoiceService(ledger).issue_invoice(TENANT_ONE, mixed_id)
        mixed_case = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, mixed_id
        )
        mixed_intent = PaymentIntentService(ledger).project_payment_intent(
            TENANT_ONE, mixed_case.collection_case_id
        )
        mixed_receipt = PaymentSettlementService(ledger).record_payment_receipt(
            TENANT_ONE, mixed_intent.payment_intent_id, Decimal("15")
        )
        mixed_leftover = UnappliedCashService(ledger).park_unapplied_cash(
            TENANT_ONE, mixed_receipt.payment_receipt_id, unapplied_amount=LEFTOVER
        )
        UnappliedCashRefundService(ledger).refund_unapplied_cash(
            TENANT_ONE, mixed_leftover.unapplied_cash_id
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
        three_case = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, three_draft
        )
        CreditNoteApplicationService(ledger).apply_credit_note(
            TENANT_ONE, three_note.issued_credit_note_id, three_case.collection_case_id
        )
        lineless = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("9"))
        IssuedInvoiceService(ledger).issue_invoice(TENANT_ONE, lineless)
        lineless_case = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, lineless
        )
        CollectionWriteOffService(ledger).write_off_collection_case(
            TENANT_ONE, lineless_case.collection_case_id
        )
        presented = AccountStatementPresentmentService(
            ledger, clock=lambda: AS_OF
        ).present_account_statement(TENANT_ONE, account.billing_account_id)
        self.assertEqual(presented.currencies[0].issued_invoice_total, KNOWN_MORNING_TOTAL)
        self.assertEqual(presented.currencies[0].open_collection_remaining, KNOWN_MORNING_TOTAL)
        other_statement = AccountStatementPresentmentService(
            ledger, clock=lambda: AS_OF
        ).present_account_statement(TENANT_ONE, other.billing_account_id)
        self.assertEqual(other_statement.currencies[0].applied_credit_total, Decimal("10"))
        self.assertEqual(other_statement.currencies[0].issued_invoice_total, Decimal("0"))
        self.assertEqual(presented.currencies[0].applied_credit_total, Decimal("0"))
        self.assertEqual(presented.currencies[0].parked_unapplied_cash, Decimal("0"))
        self.assertEqual(presented.currencies[0].refunded_unapplied_cash, Decimal("0"))

    def test_other_tenant_missing_account_and_missing_tenant_fail_closed(self) -> None:
        """Cross-tenant accounts are forbidden; unknown accounts and tenants fail closed."""
        ledger, collection_case_id = open_known_morning_case()
        stored = ledger.collection_cases[collection_case_id]
        IssuedInvoiceService(ledger).issue_invoice(TENANT_ONE, stored.invoice_draft_id)
        tenant_two = ledger.require_tenant(TENANT_TWO)
        two_account = next(
            account
            for account in ledger.billing_accounts.values()
            if account.tenant_account_id == tenant_two.tenant_account_id
        )
        service = AccountStatementPresentmentService(ledger, clock=lambda: AS_OF)
        one = service.present_account_statement(TENANT_ONE, _account_id(ledger))
        self.assertEqual(one.currencies[0].issued_invoice_total, KNOWN_MORNING_TOTAL)
        two = service.present_account_statement(TENANT_TWO, two_account.billing_account_id)
        self.assertEqual(two.currencies, ())
        with self.assertRaises(AccountStatementPresentmentQueryError) as forbidden:
            service.present_account_statement(TENANT_ONE, two_account.billing_account_id)
        self.assertEqual(forbidden.exception.rejection_reason_code, "billing_account_forbidden")
        with self.assertRaises(AccountStatementPresentmentQueryError) as missing_account:
            service.present_account_statement(TENANT_ONE, uuid4())
        self.assertEqual(missing_account.exception.rejection_reason_code, "billing_account_not_found")
        empty = AccountStatementPresentmentService()
        with self.assertRaises(AccountStatementPresentmentQueryError) as missing_tenant:
            empty.present_account_statement(TENANT_ONE, uuid4())
        self.assertEqual(missing_tenant.exception.rejection_reason_code, "tenant_not_found")

    def test_http_reads_statement_without_writing_money(self) -> None:
        """GET /v1/billing-accounts/{id}/statement is a tenant-scoped read."""
        ledger, collection_case_id = open_known_morning_case()
        stored = ledger.collection_cases[collection_case_id]
        IssuedInvoiceService(ledger).issue_invoice(TENANT_ONE, stored.invoice_draft_id)
        billing_account_id = _account_id(ledger)
        tenant_two = ledger.require_tenant(TENANT_TWO)
        two_account = next(
            account
            for account in ledger.billing_accounts.values()
            if account.tenant_account_id == tenant_two.tenant_account_id
        )
        app = create_http_app(ledger, clock=lambda: AS_OF)
        status, body = invoke_http(
            app,
            "GET",
            _statement_path(billing_account_id),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(validate_account_statement_presentment(body), ())
        self.assertEqual(body["billing_account_id"], str(billing_account_id))
        self.assertEqual(body["currencies"][0]["currency_code"], "USD")
        self.assertEqual(
            body["currencies"][0]["issued_invoice_total"],
            format_exact_decimal(KNOWN_MORNING_TOTAL),
        )
        forbidden_status, forbidden_body = invoke_http(
            app,
            "GET",
            _statement_path(two_account.billing_account_id),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(forbidden_status, 403)
        self.assertEqual(forbidden_body["rejection_reason_code"], "billing_account_forbidden")
        missing_status, missing_body = invoke_http(
            app,
            "GET",
            _statement_path(uuid4()),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(missing_status, 404)
        self.assertEqual(missing_body["rejection_reason_code"], "billing_account_not_found")
        no_tenant_status, no_tenant_body = invoke_http(
            app,
            "GET",
            _statement_path(billing_account_id),
        )
        self.assertEqual(no_tenant_status, 422)
        self.assertEqual(no_tenant_body["rejection_reason_code"], "tenant_not_found")
        unknown_tenant_status, unknown_tenant_body = invoke_http(
            app,
            "GET",
            _statement_path(billing_account_id),
            headers={"X-CWL-Tenant-Reference": "urn:cwl:missing_tenant"},
        )
        self.assertEqual(unknown_tenant_status, 422)
        self.assertEqual(unknown_tenant_body["rejection_reason_code"], "tenant_not_found")
        method_status, _method_body = invoke_http(
            app,
            "POST",
            _statement_path(billing_account_id),
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(len(ledger.payment_receipts), 0)
        self.assertEqual(len(ledger.collection_write_offs), 0)
        self.assertEqual(len(ledger.journal_proposals), 0)
        _, issue_body = invoke_http(
            app,
            "POST",
            "/v1/tenant-api-credentials",
            {"tenant_reference": TENANT_ONE, "credential_label": "operator_key"},
        )
        gated_status, gated_body = invoke_http(
            app,
            "GET",
            _statement_path(billing_account_id),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(gated_status, 422)
        self.assertEqual(gated_body["rejection_reason_code"], "api_credential_missing")
        keyed_status, keyed_body = invoke_http(
            app,
            "GET",
            _statement_path(billing_account_id),
            headers={
                "X-CWL-Tenant-Reference": TENANT_ONE,
                "Authorization": f"Bearer {issue_body['api_credential_secret']}",
            },
        )
        self.assertEqual(keyed_status, 200)
        self.assertEqual(keyed_body["currencies"][0]["issued_invoice_total"], format_exact_decimal(KNOWN_MORNING_TOTAL))

    def test_resolver_and_corrupt_remaining_fail_closed(self) -> None:
        """Hollow tenant resolve raises; IEEE remaining cannot become statement dollars."""
        ledger, collection_case_id = open_known_morning_case()
        billing_account_id = _account_id(ledger)
        service = AccountStatementPresentmentService(ledger, clock=lambda: AS_OF)
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.present_account_statement(TENANT_ONE, billing_account_id)
        stored = ledger.collection_cases[collection_case_id]
        ledger.collection_cases[collection_case_id] = replace(
            stored, outstanding_amount=0.003705  # type: ignore[arg-type]
        )
        with self.assertRaises(ExactDecimalError):
            service.present_account_statement(TENANT_ONE, billing_account_id)
        corrupt_status, corrupt_body = invoke_http(
            create_http_app(ledger),
            "GET",
            _statement_path(billing_account_id),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(corrupt_status, 422)
        self.assertEqual(corrupt_body["rejection_reason_code"], "request_invalid")


if __name__ == "__main__":
    unittest.main()
