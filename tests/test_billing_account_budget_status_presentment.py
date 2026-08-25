"""Billing-account budget-status tests for exact remaining, paging, and isolation."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import mock
from uuid import UUID, uuid4

from metering_billing import (
    MemoryUsageLedger,
    RatedSpendPresentmentService,
    SpendBudgetEvaluationPresentmentService,
    SpendBudgetService,
    UsageRatingService,
    create_http_app,
    format_exact_decimal,
    validate_billing_account_budget_status_presentment,
)
from metering_billing.errors import (
    ExactDecimalError,
    RatedSpendPresentmentQueryError,
    SpendBudgetEvaluationPresentmentQueryError,
)
from metering_billing.spend_budget_evaluation_presentment import next_operator_action
from metering_billing.time_window import TimeWindow
from metering_billing.usage_ledger import StoredSpendBudget, generate_record_id
from test_account_statement_presentment import ACCOUNT_THREE
from test_http_app import invoke_http
from test_rated_spend_presentment import _rate_known_morning
from test_spend_budget import BUDGET_AMOUNT, publish_known_budget
from test_spend_budget_evaluation_presentment import OVER_BUDGET_AMOUNT, _publish_on_rated_ledger
from test_usage_ingestion import ACCOUNT_ONE, ACCOUNT_TWO, TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL, MORNING_WINDOW, seed_rated_ledger


AS_OF = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
LATER_AS_OF = AS_OF + timedelta(hours=1)
AFTERNOON_WINDOW = TimeWindow.from_iso8601("2026-08-16T11:00:00Z", "2026-08-16T12:00:00Z")
WINDOW_STARTED = "2026-08-16T10:00:00Z"
WINDOW_ENDED = "2026-08-16T11:00:00Z"
AFTERNOON_AMOUNT = Decimal("50.00")
KRW_AMOUNT = Decimal("1000")


def _budget_status_path(billing_account_id) -> str:
    """Return the nested budget-status path for one billing account."""
    return f"/v1/billing-accounts/{billing_account_id}/budget-status"


def _insert_budget(
    ledger: MemoryUsageLedger,
    tenant_account_id: UUID,
    billing_account_id: UUID,
    *,
    spend_budget_id: UUID | None = None,
    currency_code: str = "USD",
    budget_amount: Decimal = BUDGET_AMOUNT,
    published_at: datetime = AS_OF,
    window: TimeWindow = MORNING_WINDOW,
    hash_suffix: str = "a",
) -> StoredSpendBudget:
    """Insert one stored spend budget without going through publish."""
    return ledger.insert_spend_budget(
        StoredSpendBudget(
            spend_budget_id=spend_budget_id or generate_record_id(),
            tenant_account_id=tenant_account_id,
            billing_account_id=billing_account_id,
            spend_budget_contract_version=1,
            currency_code=currency_code,
            budget_amount=budget_amount,
            window_started_at=window.window_started_at,
            window_ended_at=window.window_ended_at,
            source_payload_hash="sha256:" + hash_suffix * 64,
            published_at=published_at,
        )
    )


class BillingAccountBudgetStatusPresentmentTests(unittest.TestCase):
    """Verify account-level budget status stays exact, paged, and tenant-scoped."""

    def test_lists_published_budgets_with_exact_remaining_and_over(self) -> None:
        """Same-account published budgets stay exact decimals and never mix currency."""
        ledger, account_id, accepted, rated = _publish_on_rated_ledger()
        assert accepted.spend_budget_id is not None
        later = SpendBudgetService(ledger, clock=lambda: LATER_AS_OF).publish_spend_budget(
            TENANT_ONE, account_id, "USD", AFTERNOON_AMOUNT, AFTERNOON_WINDOW
        )
        krw = SpendBudgetService(
            ledger, clock=lambda: LATER_AS_OF + timedelta(hours=1)
        ).publish_spend_budget(TENANT_ONE, account_id, "KRW", KRW_AMOUNT, MORNING_WINDOW)
        assert later.spend_budget_id is not None
        assert krw.spend_budget_id is not None
        prior_budgets = len(ledger.spend_budgets)
        prior_runs = len(ledger.rating_runs)
        prior_drafts = len(ledger.invoice_drafts)
        prior_journals = len(ledger.journal_proposals)
        prior_outbox = len(ledger.webhook_outbox_events)
        service = SpendBudgetEvaluationPresentmentService(ledger)
        page = service.list_billing_account_budget_statuses(TENANT_ONE, account_id)
        replay = service.list_billing_account_budget_statuses(TENANT_ONE, account_id)
        self.assertEqual(page.as_contract_dict(), replay.as_contract_dict())
        self.assertEqual(len(page.budget_statuses), 3)
        self.assertIsNone(page.next_cursor)
        first, second, third = page.budget_statuses
        self.assertEqual(first.spend_budget_id, accepted.spend_budget_id)
        self.assertEqual(first.currency_code, "USD")
        self.assertEqual(first.budget_amount, BUDGET_AMOUNT)
        self.assertEqual(first.rated_amount, rated.rated_total_amount)
        self.assertEqual(first.remaining_amount, BUDGET_AMOUNT - KNOWN_MORNING_TOTAL)
        self.assertEqual(first.over_amount, Decimal("0"))
        self.assertEqual(first.utilization_status, "under")
        self.assertEqual(first.spend_budget_status, "published")
        self.assertEqual(first.next_operator_action, "wait")
        self.assertEqual(second.spend_budget_id, later.spend_budget_id)
        self.assertEqual(second.rated_amount, Decimal("0"))
        self.assertEqual(second.remaining_amount, AFTERNOON_AMOUNT)
        self.assertEqual(third.spend_budget_id, krw.spend_budget_id)
        self.assertEqual(third.currency_code, "KRW")
        self.assertEqual(third.rated_amount, Decimal("0"))
        self.assertEqual(third.remaining_amount, KRW_AMOUNT)
        self.assertEqual(third.utilization_status, "under")
        payload = page.as_contract_dict()
        self.assertEqual(set(payload), {"budget_statuses", "next_cursor"})
        self.assertNotIn("items", payload)
        self.assertNotIn("cursor", payload)
        self.assertNotIn("rated_amount", payload)
        self.assertEqual(validate_billing_account_budget_status_presentment(payload), ())
        row = payload["budget_statuses"][0]
        self.assertEqual(
            set(row),
            {
                "spend_budget_id",
                "currency_code",
                "budget_amount",
                "rated_amount",
                "remaining_amount",
                "over_amount",
                "utilization_status",
                "window_started_at",
                "window_ended_at",
                "spend_budget_status",
                "next_operator_action",
            },
        )
        self.assertIsInstance(row["remaining_amount"], str)
        self.assertNotIsInstance(row["remaining_amount"], float)
        self.assertNotIn("proposal_status", row)
        self.assertNotIn("retained_earnings", row)
        self.assertNotIn("card_pan", row)
        self.assertEqual(len(ledger.spend_budgets), prior_budgets)
        self.assertEqual(len(ledger.rating_runs), prior_runs)
        self.assertEqual(len(ledger.invoice_drafts), prior_drafts)
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        at_ledger, at_account, at_accepted, _ = _publish_on_rated_ledger(KNOWN_MORNING_TOTAL)
        assert at_accepted.spend_budget_id is not None
        at_page = SpendBudgetEvaluationPresentmentService(
            at_ledger
        ).list_billing_account_budget_statuses(TENANT_ONE, at_account)
        self.assertEqual(at_page.budget_statuses[0].utilization_status, "at")
        self.assertEqual(at_page.budget_statuses[0].remaining_amount, Decimal("0"))
        self.assertEqual(at_page.budget_statuses[0].over_amount, Decimal("0"))
        over_ledger, over_account, over_accepted, _ = _publish_on_rated_ledger(OVER_BUDGET_AMOUNT)
        assert over_accepted.spend_budget_id is not None
        over_page = SpendBudgetEvaluationPresentmentService(
            over_ledger
        ).list_billing_account_budget_statuses(TENANT_ONE, over_account)
        self.assertEqual(over_page.budget_statuses[0].utilization_status, "over")
        self.assertEqual(over_page.budget_statuses[0].remaining_amount, Decimal("0"))
        self.assertEqual(
            over_page.budget_statuses[0].over_amount, KNOWN_MORNING_TOTAL - OVER_BUDGET_AMOUNT
        )
        empty_ledger, empty_account, _ = publish_known_budget()
        empty = SpendBudgetEvaluationPresentmentService(
            empty_ledger
        ).list_billing_account_budget_statuses(TENANT_ONE, empty_account)
        self.assertEqual(empty.budget_statuses[0].rated_amount, Decimal("0"))
        self.assertEqual(empty.budget_statuses[0].utilization_status, "under")
        self.assertEqual(next_operator_action(), "wait")

    def test_pages_by_published_at_then_spend_budget_id(self) -> None:
        """Keyset cursor is published_at then spend_budget_id; page_limit is bounded."""
        ledger, account_id, first, _ = _publish_on_rated_ledger()
        assert first.spend_budget_id is not None
        tenant_one, _ = ledger.resolve_tenant(TENANT_ONE)
        assert tenant_one is not None
        earlier_id = UUID("019d7001-0000-7000-8000-000000000001")
        later_id = UUID("019d7001-0000-7000-8000-000000000002")
        _insert_budget(
            ledger,
            tenant_one.tenant_account_id,
            account_id,
            spend_budget_id=later_id,
            published_at=LATER_AS_OF,
            window=AFTERNOON_WINDOW,
            hash_suffix="b",
        )
        _insert_budget(
            ledger,
            tenant_one.tenant_account_id,
            account_id,
            spend_budget_id=earlier_id,
            published_at=AS_OF - timedelta(hours=1),
            currency_code="EUR",
            hash_suffix="c",
        )
        service = SpendBudgetEvaluationPresentmentService(ledger)
        first_page = service.list_billing_account_budget_statuses(
            TENANT_ONE, account_id, page_limit=1
        )
        self.assertEqual(len(first_page.budget_statuses), 1)
        self.assertEqual(first_page.budget_statuses[0].spend_budget_id, earlier_id)
        self.assertIsNotNone(first_page.next_cursor)
        second_page = service.list_billing_account_budget_statuses(
            TENANT_ONE, account_id, cursor=first_page.next_cursor, page_limit="2"
        )
        self.assertEqual(
            [row.spend_budget_id for row in second_page.budget_statuses],
            [first.spend_budget_id, later_id],
        )
        self.assertIsNone(second_page.next_cursor)
        default_page = service.list_billing_account_budget_statuses(
            TENANT_ONE, account_id, cursor="", page_limit=""
        )
        self.assertEqual(len(default_page.budget_statuses), 3)
        self.assertEqual(len(service.list_billing_account_budget_statuses(
            TENANT_ONE, account_id, page_limit=None
        ).budget_statuses), 3)
        self.assertEqual(len(service.list_billing_account_budget_statuses(
            TENANT_ONE, account_id, page_limit=50
        ).budget_statuses), 3)
        for invalid in (0, 101, True, 1.5, "abc"):
            with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as error:
                service.list_billing_account_budget_statuses(
                    TENANT_ONE, account_id, page_limit=invalid
                )
            self.assertEqual(error.exception.rejection_reason_code, "request_invalid")
        with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as cursor:
            service.list_billing_account_budget_statuses(
                TENANT_ONE, account_id, cursor="not-a-cursor"
            )
        self.assertEqual(cursor.exception.rejection_reason_code, "request_invalid")
        for invalid_cursor in (True, 1.5, b"2026-08-18T15:00:00Z|019d7001-0000-7000-8000-000000000001"):
            with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as typed:
                service.list_billing_account_budget_statuses(
                    TENANT_ONE, account_id, cursor=invalid_cursor
                )
            self.assertEqual(typed.exception.rejection_reason_code, "request_invalid")
        with mock.patch(
            "metering_billing.spend_budget_evaluation_presentment.parse_iso8601_datetime",
            side_effect=TypeError("closed"),
        ):
            with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as typed_parse:
                service.list_billing_account_budget_statuses(
                    TENANT_ONE,
                    account_id,
                    cursor="2026-08-18T14:00:00Z|019d7001-0000-7000-8000-000000000001",
                )
            self.assertEqual(typed_parse.exception.rejection_reason_code, "request_invalid")

    def test_omits_unknown_and_cross_tenant_budgets_without_leak(self) -> None:
        """Other-account and foreign-tenant budgets are omitted; empty account is 200."""
        ledger, account_id, accepted, _ = _publish_on_rated_ledger()
        assert accepted.spend_budget_id is not None
        other = ledger.register_billing_account(TENANT_ONE, ACCOUNT_THREE)
        SpendBudgetService(ledger, clock=lambda: AS_OF).publish_spend_budget(
            TENANT_ONE, other.billing_account_id, "USD", AFTERNOON_AMOUNT, AFTERNOON_WINDOW
        )
        tenant_one, _ = ledger.resolve_tenant(TENANT_ONE)
        assert tenant_one is not None
        foreign = ledger.billing_accounts[ACCOUNT_TWO]
        _insert_budget(
            ledger,
            tenant_one.tenant_account_id,
            foreign.billing_account_id,
            hash_suffix="d",
        )
        ledger.register_tenant(TENANT_TWO)
        tenant_two, _ = ledger.resolve_tenant(TENANT_TWO)
        assert tenant_two is not None
        _insert_budget(
            ledger,
            tenant_two.tenant_account_id,
            account_id,
            hash_suffix="e",
        )
        page = SpendBudgetEvaluationPresentmentService(ledger).list_billing_account_budget_statuses(
            TENANT_ONE, account_id
        )
        self.assertEqual([row.spend_budget_id for row in page.budget_statuses], [accepted.spend_budget_id])
        empty_account = SpendBudgetEvaluationPresentmentService(
            ledger
        ).list_billing_account_budget_statuses(TENANT_ONE, other.billing_account_id)
        self.assertEqual(len(empty_account.budget_statuses), 1)
        self.assertEqual(empty_account.budget_statuses[0].billing_account_id, other.billing_account_id)
        skipped = _insert_budget(
            ledger,
            tenant_one.tenant_account_id,
            account_id,
            window=AFTERNOON_WINDOW,
            hash_suffix="f",
        )
        with mock.patch.object(
            SpendBudgetEvaluationPresentmentService,
            "present_spend_budget_evaluation",
            side_effect=[
                SpendBudgetEvaluationPresentmentQueryError("spend_budget_not_found"),
                SpendBudgetEvaluationPresentmentService(ledger).present_spend_budget_evaluation(
                    TENANT_ONE, skipped.spend_budget_id
                ),
            ],
        ):
            omitted = SpendBudgetEvaluationPresentmentService(
                ledger
            ).list_billing_account_budget_statuses(TENANT_ONE, account_id)
        self.assertEqual(
            [row.spend_budget_id for row in omitted.budget_statuses], [skipped.spend_budget_id]
        )
        with mock.patch.object(
            SpendBudgetEvaluationPresentmentService,
            "present_spend_budget_evaluation",
            side_effect=SpendBudgetEvaluationPresentmentQueryError("billing_account_forbidden"),
        ):
            with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as forbidden:
                SpendBudgetEvaluationPresentmentService(ledger).list_billing_account_budget_statuses(
                    TENANT_ONE, account_id
                )
        self.assertEqual(forbidden.exception.rejection_reason_code, "billing_account_forbidden")
        with mock.patch.object(
            SpendBudgetEvaluationPresentmentService,
            "present_spend_budget_evaluation",
            side_effect=SpendBudgetEvaluationPresentmentQueryError("billing_account_not_found"),
        ):
            with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as gone:
                SpendBudgetEvaluationPresentmentService(ledger).list_billing_account_budget_statuses(
                    TENANT_ONE, account_id
                )
        self.assertEqual(gone.exception.rejection_reason_code, "billing_account_not_found")

    def test_unknown_account_cross_tenant_and_pin_mismatch_fail_closed(self) -> None:
        """Unknown account 404s; foreign account 403s; missing tenant 422s."""
        ledger, account_id, _, _ = _publish_on_rated_ledger()
        service = SpendBudgetEvaluationPresentmentService(ledger)
        with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as missing:
            service.list_billing_account_budget_statuses("", account_id)
        self.assertEqual(missing.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as unknown_tenant:
            service.list_billing_account_budget_statuses("urn:cwl:missing", account_id)
        self.assertEqual(unknown_tenant.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as unknown_account:
            service.list_billing_account_budget_statuses(TENANT_ONE, uuid4())
        self.assertEqual(unknown_account.exception.rejection_reason_code, "billing_account_not_found")
        ledger.register_tenant(TENANT_TWO)
        with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as crossed:
            service.list_billing_account_budget_statuses(TENANT_TWO, account_id)
        self.assertEqual(crossed.exception.rejection_reason_code, "billing_account_forbidden")
        empty = SpendBudgetEvaluationPresentmentService()
        with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as hollow:
            empty.list_billing_account_budget_statuses(TENANT_ONE, uuid4())
        self.assertEqual(hollow.exception.rejection_reason_code, "tenant_not_found")
        bare = ledger.register_billing_account(
            TENANT_ONE, "urn:cwl:tenant_001:billing_account:019da001"
        )
        empty = service.list_billing_account_budget_statuses(TENANT_ONE, bare.billing_account_id)
        self.assertEqual(empty.budget_statuses, ())
        self.assertIsNone(empty.next_cursor)
        self.assertEqual(
            validate_billing_account_budget_status_presentment(empty.as_contract_dict()),
            (),
        )
        with mock.patch.object(ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaises(ValueError):
                service.list_billing_account_budget_statuses(TENANT_ONE, account_id)
        with mock.patch.object(
            SpendBudgetEvaluationPresentmentService,
            "present_spend_budget_evaluation",
            side_effect=SpendBudgetEvaluationPresentmentQueryError("request_invalid"),
        ):
            with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as invalid:
                service.list_billing_account_budget_statuses(TENANT_ONE, account_id)
        self.assertEqual(invalid.exception.rejection_reason_code, "request_invalid")

    def test_float_money_and_rated_spend_errors_fail_closed(self) -> None:
        """IEEE money and nested rated-spend failures stay rejected."""
        ledger, account_id, accepted, _ = _publish_on_rated_ledger()
        assert accepted.spend_budget_id is not None
        stored = ledger.get_spend_budget(accepted.spend_budget_id)
        assert stored is not None
        ledger.spend_budgets[accepted.spend_budget_id] = replace(stored, budget_amount=1.25)
        with self.assertRaises(ExactDecimalError):
            SpendBudgetEvaluationPresentmentService(ledger).list_billing_account_budget_statuses(
                TENANT_ONE, account_id
            )
        ledger.spend_budgets[accepted.spend_budget_id] = stored
        with mock.patch.object(
            RatedSpendPresentmentService,
            "present_rated_spend",
            side_effect=RatedSpendPresentmentQueryError("billing_account_forbidden"),
        ):
            with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as forbidden:
                SpendBudgetEvaluationPresentmentService(ledger).list_billing_account_budget_statuses(
                    TENANT_ONE, account_id
                )
        self.assertEqual(forbidden.exception.rejection_reason_code, "billing_account_forbidden")

    def test_http_get_is_safe_and_pins_the_tenant_header(self) -> None:
        """GET lists same-tenant statuses; missing pin is 422; leaks stay closed."""
        ledger, account_id, accepted, _ = _publish_on_rated_ledger()
        assert accepted.spend_budget_id is not None
        later = SpendBudgetService(ledger, clock=lambda: LATER_AS_OF).publish_spend_budget(
            TENANT_ONE, account_id, "USD", AFTERNOON_AMOUNT, AFTERNOON_WINDOW
        )
        assert later.spend_budget_id is not None
        app = create_http_app(ledger)
        path = _budget_status_path(account_id)
        status, body = invoke_http(
            app,
            "GET",
            path,
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"budget_statuses", "next_cursor"})
        self.assertEqual(len(body["budget_statuses"]), 2)
        self.assertEqual(body["budget_statuses"][0]["spend_budget_id"], str(accepted.spend_budget_id))
        self.assertEqual(body["budget_statuses"][0]["budget_amount"], "100.00")
        self.assertEqual(
            body["budget_statuses"][0]["rated_amount"], format_exact_decimal(KNOWN_MORNING_TOTAL)
        )
        self.assertEqual(body["budget_statuses"][0]["utilization_status"], "under")
        self.assertEqual(body["budget_statuses"][0]["next_operator_action"], "wait")
        self.assertEqual(validate_billing_account_budget_status_presentment(body), ())
        paged_status, paged_body = invoke_http(
            app,
            "GET",
            path,
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(paged_status, 200)
        self.assertEqual(len(paged_body["budget_statuses"]), 1)
        self.assertIsNotNone(paged_body["next_cursor"])
        second_status, second_body = invoke_http(
            app,
            "GET",
            path,
            query={
                "tenant_reference": TENANT_ONE,
                "cursor": str(paged_body["next_cursor"]),
            },
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second_body["budget_statuses"]), 1)
        self.assertEqual(second_body["budget_statuses"][0]["spend_budget_id"], str(later.spend_budget_id))
        self.assertIsNone(second_body["next_cursor"])
        query_status, query_body = invoke_http(
            app, "GET", path, query={"tenant_reference": TENANT_ONE}
        )
        self.assertEqual(query_status, 200)
        self.assertEqual(query_body, body)
        missing_status, missing_body = invoke_http(app, "GET", path)
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        mismatch_status, mismatch_body = invoke_http(
            app,
            "GET",
            path,
            query={"tenant_reference": TENANT_TWO},
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(mismatch_status, 422)
        self.assertEqual(mismatch_body["rejection_reason_code"], "request_invalid")
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            _budget_status_path(uuid4()),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "billing_account_not_found")
        self.assertNotIn("budget_statuses", unknown_body)
        ledger.register_tenant(TENANT_TWO)
        other_status, other_body = invoke_http(
            app,
            "GET",
            path,
            headers={"X-CWL-Tenant-Reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 403)
        self.assertEqual(other_body["rejection_reason_code"], "billing_account_forbidden")
        self.assertNotIn("budget_statuses", other_body)
        method_status, method_body = invoke_http(
            app,
            "POST",
            path,
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        illegal_status, illegal_body = invoke_http(
            app,
            "GET",
            path,
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(illegal_status, 422)
        self.assertEqual(illegal_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.SpendBudgetEvaluationPresentmentService.list_billing_account_budget_statuses",
            side_effect=ExactDecimalError("budget amount must be an exact decimal"),
        ):
            float_status, float_body = invoke_http(
                create_http_app(ledger),
                "GET",
                path,
                headers={"X-CWL-Tenant-Reference": TENANT_ONE},
            )
        self.assertEqual(float_status, 422)
        self.assertEqual(float_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.SpendBudgetEvaluationPresentmentService.list_billing_account_budget_statuses",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                path,
                headers={"X-CWL-Tenant-Reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.SpendBudgetEvaluationPresentmentService.list_billing_account_budget_statuses",
            side_effect=SpendBudgetEvaluationPresentmentQueryError("billing_account_not_found"),
        ):
            gone_status, gone_body = invoke_http(
                create_http_app(ledger),
                "GET",
                path,
                headers={"X-CWL-Tenant-Reference": TENANT_ONE},
            )
        self.assertEqual(gone_status, 404)
        self.assertEqual(gone_body["rejection_reason_code"], "billing_account_not_found")
        with mock.patch(
            "metering_billing.http_app.SpendBudgetEvaluationPresentmentService.list_billing_account_budget_statuses",
            side_effect=SpendBudgetEvaluationPresentmentQueryError("request_invalid"),
        ):
            invalid_status, invalid_body = invoke_http(
                create_http_app(ledger),
                "GET",
                path,
                headers={"X-CWL-Tenant-Reference": TENANT_ONE},
            )
        self.assertEqual(invalid_status, 422)
        self.assertEqual(invalid_body["rejection_reason_code"], "request_invalid")
        spend = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, account_id, MORNING_WINDOW
        )
        self.assertEqual(spend.products[0].rated_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(len(ledger.rating_runs), 1)
        self.assertEqual(UsageRatingService(ledger).ledger, ledger)
        evaluation_status, evaluation_body = invoke_http(
            create_http_app(ledger),
            "GET",
            f"/v1/spend-budgets/{accepted.spend_budget_id}/evaluation",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(evaluation_status, 200)
        self.assertEqual(evaluation_body["spend_budget_id"], str(accepted.spend_budget_id))


if __name__ == "__main__":
    unittest.main()
