"""Collection aging tests for buckets, currency grouping, and fail-closed reads."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import mock

from metering_billing import (
    CollectionAgingPresentmentService,
    CollectionCaseService,
    CollectionWriteOffService,
    CreditAdjustmentService,
    IssuedInvoiceService,
    MemoryUsageLedger,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.collection_aging_presentment import aging_bucket_code
from metering_billing.contracts import validate_collection_aging_presentment
from metering_billing.errors import CollectionAgingPresentmentQueryError, ExactDecimalError
from test_http_app import invoke_http
from test_payment_intent import open_known_morning_case
from test_tax_assessment import HUNDRED, HUNDRED_INT, insert_commercial_draft
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL, seed_rated_ledger


AS_OF = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
PARTIAL = Decimal("1.25")
KRW_AMOUNT = HUNDRED_INT


def _open_aged_case(
    ledger: MemoryUsageLedger,
    tenant_reference: str,
    currency_code: str,
    amount: Decimal,
    opened_at: datetime,
) -> None:
    """Persist one open case whose opened_at is the aging key."""
    invoice_draft_id = insert_commercial_draft(ledger, tenant_reference, currency_code, amount)
    opened = CollectionCaseService(ledger, clock=lambda: opened_at).open_collection_case(
        tenant_reference, invoice_draft_id
    )
    if opened.collection_case_id is None:
        raise AssertionError("aged path must persist a collection case")


class CollectionAgingPresentmentTests(unittest.TestCase):
    """Verify aging totals stay exact, tenant-scoped, and read-only."""

    def test_known_open_case_lands_in_current_when_due_today(self) -> None:
        """An open morning case due as-of today is current, not aged dollars."""
        ledger, collection_case_id = open_known_morning_case()
        stored = ledger.collection_cases[collection_case_id]
        ledger.collection_cases[collection_case_id] = replace(stored, opened_at=AS_OF)
        presented = CollectionAgingPresentmentService(
            ledger, clock=lambda: AS_OF
        ).present_collection_aging(TENANT_ONE)
        replay = CollectionAgingPresentmentService(
            ledger, clock=lambda: AS_OF
        ).present_collection_aging(TENANT_ONE)
        self.assertEqual(presented.as_contract_dict(), replay.as_contract_dict())
        self.assertEqual(presented.tenant_reference, TENANT_ONE)
        self.assertEqual(presented.as_of, AS_OF)
        self.assertEqual(len(presented.currencies), 1)
        usd = presented.currencies[0]
        self.assertEqual(usd.currency_code, "USD")
        self.assertEqual(usd.current.case_count, 1)
        self.assertEqual(usd.current.outstanding_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(usd.days_1_30.case_count, 0)
        self.assertEqual(usd.days_1_30.outstanding_amount, Decimal("0"))
        payload = presented.as_contract_dict()
        self.assertEqual(validate_collection_aging_presentment(payload), ())
        self.assertIsInstance(payload["currencies"][0]["current"]["outstanding_amount"], str)
        self.assertNotIsInstance(
            payload["currencies"][0]["current"]["outstanding_amount"], float
        )
        self.assertNotIn("collection_case_id", payload)
        self.assertNotIn("card_pan", payload)
        self.assertNotIn("statutory_account_id", payload)
        self.assertEqual(len(ledger.journal_proposals), 0)
        self.assertEqual(len(ledger.payment_receipts), 0)

    def test_five_buckets_and_currency_groups_stay_separate(self) -> None:
        """Boundary days land in the closed buckets; KRW never mixes into USD."""
        ledger = seed_rated_ledger()
        _open_aged_case(ledger, TENANT_ONE, "USD", Decimal("1"), AS_OF)
        _open_aged_case(ledger, TENANT_ONE, "USD", Decimal("2"), AS_OF - timedelta(days=1))
        _open_aged_case(ledger, TENANT_ONE, "USD", Decimal("3"), AS_OF - timedelta(days=30))
        _open_aged_case(ledger, TENANT_ONE, "USD", Decimal("4"), AS_OF - timedelta(days=31))
        _open_aged_case(ledger, TENANT_ONE, "USD", Decimal("5"), AS_OF - timedelta(days=60))
        _open_aged_case(ledger, TENANT_ONE, "USD", Decimal("6"), AS_OF - timedelta(days=61))
        _open_aged_case(ledger, TENANT_ONE, "USD", Decimal("7"), AS_OF - timedelta(days=90))
        _open_aged_case(ledger, TENANT_ONE, "USD", Decimal("8"), AS_OF - timedelta(days=91))
        _open_aged_case(ledger, TENANT_ONE, "KRW", KRW_AMOUNT, AS_OF - timedelta(days=15))
        presented = CollectionAgingPresentmentService(
            ledger, clock=lambda: AS_OF
        ).present_collection_aging(TENANT_ONE)
        self.assertEqual([item.currency_code for item in presented.currencies], ["KRW", "USD"])
        krw, usd = presented.currencies
        self.assertEqual(krw.days_1_30.case_count, 1)
        self.assertEqual(krw.days_1_30.outstanding_amount, KRW_AMOUNT)
        self.assertEqual(usd.current.case_count, 1)
        self.assertEqual(usd.current.outstanding_amount, Decimal("1"))
        self.assertEqual(usd.days_1_30.case_count, 2)
        self.assertEqual(usd.days_1_30.outstanding_amount, Decimal("5"))
        self.assertEqual(usd.days_31_60.case_count, 2)
        self.assertEqual(usd.days_31_60.outstanding_amount, Decimal("9"))
        self.assertEqual(usd.days_61_90.case_count, 2)
        self.assertEqual(usd.days_61_90.outstanding_amount, Decimal("13"))
        self.assertEqual(usd.days_90_plus.case_count, 1)
        self.assertEqual(usd.days_90_plus.outstanding_amount, Decimal("8"))
        self.assertEqual(validate_collection_aging_presentment(presented.as_contract_dict()), ())
        self.assertEqual(aging_bucket_code(0), "current")
        self.assertEqual(aging_bucket_code(-3), "current")
        self.assertEqual(aging_bucket_code(30), "days_1_30")
        self.assertEqual(aging_bucket_code(31), "days_31_60")
        self.assertEqual(aging_bucket_code(90), "days_61_90")
        self.assertEqual(aging_bucket_code(91), "days_90_plus")

    def test_issued_invoice_due_at_overrides_opened_at(self) -> None:
        """A stored invoice due date ages the case even when opened recently."""
        ledger, collection_case_id = open_known_morning_case()
        stored = ledger.collection_cases[collection_case_id]
        ledger.collection_cases[collection_case_id] = replace(stored, opened_at=AS_OF)
        issued = IssuedInvoiceService(ledger).issue_invoice(
            TENANT_ONE, stored.invoice_draft_id, due_at=AS_OF - timedelta(days=45)
        )
        self.assertEqual(issued.due_at, AS_OF - timedelta(days=45))
        presented = CollectionAgingPresentmentService(
            ledger, clock=lambda: AS_OF
        ).present_collection_aging(TENANT_ONE)
        usd = presented.currencies[0]
        self.assertEqual(usd.current.case_count, 0)
        self.assertEqual(usd.days_31_60.case_count, 1)
        self.assertEqual(usd.days_31_60.outstanding_amount, KNOWN_MORNING_TOTAL)
        undated_ledger, undated_case_id = open_known_morning_case()
        undated = undated_ledger.collection_cases[undated_case_id]
        undated_ledger.collection_cases[undated_case_id] = replace(
            undated, opened_at=AS_OF - timedelta(days=10)
        )
        IssuedInvoiceService(undated_ledger).issue_invoice(
            TENANT_ONE, undated.invoice_draft_id
        )
        undated_aging = CollectionAgingPresentmentService(
            undated_ledger, clock=lambda: AS_OF
        ).present_collection_aging(TENANT_ONE)
        self.assertEqual(undated_aging.currencies[0].days_1_30.case_count, 1)
        self.assertEqual(
            undated_aging.currencies[0].days_1_30.outstanding_amount, KNOWN_MORNING_TOTAL
        )

    def test_settled_and_zero_remaining_do_not_inflate_aging(self) -> None:
        """Settled cases and write-off leftover zero are omitted from aged dollars."""
        ledger, settled_id = open_known_morning_case()
        CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE,
            ledger.collection_cases[settled_id].invoice_draft_id,
            KNOWN_MORNING_TOTAL,
            "rating_correction",
        )
        self.assertEqual(ledger.collection_cases[settled_id].collection_case_status, "settled")
        leftover_ledger, leftover_id = open_known_morning_case()
        leftover = leftover_ledger.collection_cases[leftover_id]
        leftover_ledger.collection_cases[leftover_id] = replace(
            leftover, outstanding_amount=PARTIAL, opened_at=AS_OF - timedelta(days=40)
        )
        written = CollectionWriteOffService(leftover_ledger).write_off_collection_case(
            TENANT_ONE, leftover_id
        )
        self.assertEqual(written.remaining_outstanding_amount, Decimal("0"))
        self.assertEqual(
            leftover_ledger.collection_cases[leftover_id].collection_case_status, "open"
        )
        settled_aging = CollectionAgingPresentmentService(
            ledger, clock=lambda: AS_OF
        ).present_collection_aging(TENANT_ONE)
        leftover_aging = CollectionAgingPresentmentService(
            leftover_ledger, clock=lambda: AS_OF
        ).present_collection_aging(TENANT_ONE)
        self.assertEqual(settled_aging.currencies, ())
        self.assertEqual(leftover_aging.currencies, ())
        dunning_ledger, dunning_id = open_known_morning_case()
        CollectionCaseService(dunning_ledger).record_dunning_event(
            TENANT_ONE, dunning_id, "first_notice"
        )
        dunning = dunning_ledger.collection_cases[dunning_id]
        dunning_ledger.collection_cases[dunning_id] = replace(
            dunning, opened_at=AS_OF - timedelta(days=20)
        )
        dunning_aging = CollectionAgingPresentmentService(
            dunning_ledger, clock=lambda: AS_OF
        ).present_collection_aging(TENANT_ONE)
        self.assertEqual(dunning_aging.currencies[0].days_1_30.case_count, 1)

    def test_other_tenant_and_missing_tenant_fail_closed(self) -> None:
        """A tenant cannot see another tenant's aged remaining."""
        ledger = seed_rated_ledger()
        _open_aged_case(ledger, TENANT_ONE, "USD", Decimal("9"), AS_OF - timedelta(days=5))
        _open_aged_case(ledger, TENANT_TWO, "USD", Decimal("11"), AS_OF - timedelta(days=5))
        service = CollectionAgingPresentmentService(ledger, clock=lambda: AS_OF)
        one = service.present_collection_aging(TENANT_ONE)
        two = service.present_collection_aging(TENANT_TWO)
        self.assertEqual(one.currencies[0].days_1_30.outstanding_amount, Decimal("9"))
        self.assertEqual(two.currencies[0].days_1_30.outstanding_amount, Decimal("11"))
        empty = CollectionAgingPresentmentService()
        with self.assertRaises(CollectionAgingPresentmentQueryError) as raised:
            empty.present_collection_aging(TENANT_ONE)
        self.assertEqual(raised.exception.rejection_reason_code, "tenant_not_found")

    def test_http_lists_aging_without_writing_money(self) -> None:
        """GET /v1/collection-aging is a tenant-scoped read of existing remaining."""
        ledger, collection_case_id = open_known_morning_case()
        stored = ledger.collection_cases[collection_case_id]
        ledger.collection_cases[collection_case_id] = replace(
            stored, opened_at=AS_OF - timedelta(days=12)
        )
        app = create_http_app(ledger, clock=lambda: AS_OF)
        status, body = invoke_http(
            app,
            "GET",
            "/v1/collection-aging",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(validate_collection_aging_presentment(body), ())
        self.assertEqual(body["tenant_reference"], TENANT_ONE)
        self.assertEqual(body["currencies"][0]["currency_code"], "USD")
        self.assertEqual(body["currencies"][0]["days_1_30"]["case_count"], 1)
        self.assertEqual(
            body["currencies"][0]["days_1_30"]["outstanding_amount"],
            format_exact_decimal(KNOWN_MORNING_TOTAL),
        )
        other_status, other_body = invoke_http(
            app,
            "GET",
            "/v1/collection-aging",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 200)
        self.assertEqual(other_body["currencies"], [])
        missing_status, missing_body = invoke_http(
            app,
            "GET",
            "/v1/collection-aging",
            query={"tenant_reference": "urn:cwl:missing_tenant"},
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        method_status, _method_body = invoke_http(
            app,
            "POST",
            "/v1/collection-aging",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(len(ledger.payment_receipts), 0)
        self.assertEqual(len(ledger.collection_write_offs), 0)
        self.assertEqual(len(ledger.collection_case_settlements), 0)
        _, issue_body = invoke_http(
            app,
            "POST",
            "/v1/tenant-api-credentials",
            {"tenant_reference": TENANT_ONE, "credential_label": "operator_key"},
        )
        gated_status, gated_body = invoke_http(
            app,
            "GET",
            "/v1/collection-aging",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(gated_status, 422)
        self.assertEqual(gated_body["rejection_reason_code"], "api_credential_missing")
        keyed_status, keyed_body = invoke_http(
            app,
            "GET",
            "/v1/collection-aging",
            query={"tenant_reference": TENANT_ONE},
            headers={"Authorization": f"Bearer {issue_body['api_credential_secret']}"},
        )
        self.assertEqual(keyed_status, 200)
        self.assertEqual(keyed_body["currencies"][0]["days_1_30"]["case_count"], 1)

    def test_resolver_and_corrupt_remaining_fail_closed(self) -> None:
        """Hollow tenant resolve raises; IEEE remaining cannot become aged dollars."""
        ledger, collection_case_id = open_known_morning_case()
        service = CollectionAgingPresentmentService(ledger, clock=lambda: AS_OF)
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.present_collection_aging(TENANT_ONE)
        stored = ledger.collection_cases[collection_case_id]
        ledger.collection_cases[collection_case_id] = replace(
            stored, outstanding_amount=0.003705  # type: ignore[arg-type]
        )
        with self.assertRaises(ExactDecimalError):
            service.present_collection_aging(TENANT_ONE)
        corrupt_status, corrupt_body = invoke_http(
            create_http_app(ledger),
            "GET",
            "/v1/collection-aging",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(corrupt_status, 422)
        self.assertEqual(corrupt_body["rejection_reason_code"], "request_invalid")


if __name__ == "__main__":
    unittest.main()
