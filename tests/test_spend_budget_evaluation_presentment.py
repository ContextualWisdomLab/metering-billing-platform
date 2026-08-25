"""Spend-budget evaluation tests for exact remaining, over, and tenant isolation."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    MemoryUsageLedger,
    RatedSpendPresentmentService,
    SpendBudgetEvaluationPresentmentService,
    SpendBudgetService,
    UsageRatingService,
    create_http_app,
    format_exact_decimal,
    validate_spend_budget_evaluation_presentment,
)
from metering_billing.errors import (
    ExactDecimalError,
    RatedSpendPresentmentQueryError,
    SpendBudgetEvaluationPresentmentQueryError,
)
from metering_billing.rated_spend_presentment import RatedSpendPresentmentResult, RatedSpendProductResult
from metering_billing.spend_budget import compute_spend_budget_payload_hash
from metering_billing.spend_budget_evaluation_presentment import next_operator_action
from metering_billing.usage_ledger import StoredSpendBudget, generate_record_id
from test_account_statement_presentment import _account_id
from test_http_app import invoke_http
from test_rated_spend_presentment import _rate_known_morning
from test_spend_budget import BUDGET_AMOUNT, publish_known_budget
from test_usage_ingestion import ACCOUNT_ONE, ACCOUNT_TWO, TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL, MORNING_WINDOW, seed_rated_ledger


AS_OF = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
WINDOW_STARTED = "2026-08-16T10:00:00Z"
WINDOW_ENDED = "2026-08-16T11:00:00Z"
OVER_BUDGET_AMOUNT = Decimal("0.001")


def _evaluation_path(spend_budget_id) -> str:
    """Return the nested evaluation path for one spend budget."""
    return f"/v1/spend-budgets/{spend_budget_id}/evaluation"


def _publish_on_rated_ledger(
    amount: Decimal = BUDGET_AMOUNT, currency_code: str = "USD"
):
    """Rate the known morning window, then publish one commercial spend budget."""
    ledger, rated = _rate_known_morning()
    account_id = _account_id(ledger)
    accepted = SpendBudgetService(ledger, clock=lambda: AS_OF).publish_spend_budget(
        TENANT_ONE, account_id, currency_code, amount, MORNING_WINDOW
    )
    return ledger, account_id, accepted, rated


class SpendBudgetEvaluationPresentmentTests(unittest.TestCase):
    """Verify budget evaluation stays exact, read-only, and tenant-scoped."""

    def test_under_at_and_over_use_exact_remaining_and_over(self) -> None:
        """Rated spend below, equal to, and above the budget stay exact decimals."""
        ledger, account_id, accepted, rated = _publish_on_rated_ledger()
        assert accepted.spend_budget_id is not None
        prior_budgets = len(ledger.spend_budgets)
        prior_runs = len(ledger.rating_runs)
        prior_drafts = len(ledger.invoice_drafts)
        prior_journals = len(ledger.journal_proposals)
        prior_outbox = len(ledger.webhook_outbox_events)
        under = SpendBudgetEvaluationPresentmentService(ledger).present_spend_budget_evaluation(
            TENANT_ONE, accepted.spend_budget_id
        )
        replay = SpendBudgetEvaluationPresentmentService(ledger).present_spend_budget_evaluation(
            TENANT_ONE, accepted.spend_budget_id
        )
        self.assertEqual(under.as_contract_dict(), replay.as_contract_dict())
        self.assertEqual(under.spend_budget_id, accepted.spend_budget_id)
        self.assertEqual(under.billing_account_id, account_id)
        self.assertEqual(under.currency_code, "USD")
        self.assertEqual(under.budget_amount, BUDGET_AMOUNT)
        self.assertEqual(under.rated_amount, rated.rated_total_amount)
        self.assertEqual(under.rated_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(under.remaining_amount, BUDGET_AMOUNT - KNOWN_MORNING_TOTAL)
        self.assertEqual(under.over_amount, Decimal("0"))
        self.assertEqual(under.utilization_status, "under")
        self.assertEqual(under.spend_budget_status, "published")
        self.assertEqual(under.next_operator_action, "wait")
        payload = under.as_contract_dict()
        self.assertEqual(validate_spend_budget_evaluation_presentment(payload), ())
        self.assertIsInstance(payload["remaining_amount"], str)
        self.assertNotIsInstance(payload["remaining_amount"], float)
        self.assertIsInstance(payload["over_amount"], str)
        self.assertNotIsInstance(payload["over_amount"], float)
        self.assertNotIn("proposal_status", payload)
        self.assertNotIn("retained_earnings", payload)
        self.assertNotIn("card_pan", payload)
        self.assertNotIn("statutory_account_id", payload)
        self.assertEqual(len(ledger.spend_budgets), prior_budgets)
        self.assertEqual(len(ledger.rating_runs), prior_runs)
        self.assertEqual(len(ledger.invoice_drafts), prior_drafts)
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        at_ledger, _, at_accepted, _ = _publish_on_rated_ledger(KNOWN_MORNING_TOTAL)
        assert at_accepted.spend_budget_id is not None
        at_result = SpendBudgetEvaluationPresentmentService(
            at_ledger
        ).present_spend_budget_evaluation(TENANT_ONE, at_accepted.spend_budget_id)
        self.assertEqual(at_result.utilization_status, "at")
        self.assertEqual(at_result.remaining_amount, Decimal("0"))
        self.assertEqual(at_result.over_amount, Decimal("0"))
        self.assertEqual(validate_spend_budget_evaluation_presentment(at_result.as_contract_dict()), ())
        over_ledger, _, over_accepted, _ = _publish_on_rated_ledger(OVER_BUDGET_AMOUNT)
        assert over_accepted.spend_budget_id is not None
        over_result = SpendBudgetEvaluationPresentmentService(
            over_ledger
        ).present_spend_budget_evaluation(TENANT_ONE, over_accepted.spend_budget_id)
        self.assertEqual(over_result.utilization_status, "over")
        self.assertEqual(over_result.remaining_amount, Decimal("0"))
        self.assertEqual(over_result.over_amount, KNOWN_MORNING_TOTAL - OVER_BUDGET_AMOUNT)
        self.assertEqual(
            validate_spend_budget_evaluation_presentment(over_result.as_contract_dict()), ()
        )
        self.assertEqual(next_operator_action(), "wait")

    def test_other_currency_and_zero_rated_spend_stay_under(self) -> None:
        """A KRW budget ignores USD rated rows; an unpublished window stays under."""
        ledger, _, accepted, _ = _publish_on_rated_ledger(Decimal("1000"), "KRW")
        assert accepted.spend_budget_id is not None
        evaluated = SpendBudgetEvaluationPresentmentService(ledger).present_spend_budget_evaluation(
            TENANT_ONE, accepted.spend_budget_id
        )
        self.assertEqual(evaluated.currency_code, "KRW")
        self.assertEqual(evaluated.rated_amount, Decimal("0"))
        self.assertEqual(evaluated.remaining_amount, Decimal("1000"))
        self.assertEqual(evaluated.over_amount, Decimal("0"))
        self.assertEqual(evaluated.utilization_status, "under")
        empty_ledger, _, empty_accepted = publish_known_budget()
        assert empty_accepted.spend_budget_id is not None
        empty = SpendBudgetEvaluationPresentmentService(
            empty_ledger
        ).present_spend_budget_evaluation(TENANT_ONE, empty_accepted.spend_budget_id)
        self.assertEqual(empty.rated_amount, Decimal("0"))
        self.assertEqual(empty.remaining_amount, BUDGET_AMOUNT)
        self.assertEqual(empty.utilization_status, "under")

    def test_unknown_cross_tenant_and_pin_mismatch_fail_closed(self) -> None:
        """Unknown and cross-tenant budgets 404; foreign billing accounts 403."""
        ledger, _, accepted, _ = _publish_on_rated_ledger()
        assert accepted.spend_budget_id is not None
        service = SpendBudgetEvaluationPresentmentService(ledger)
        with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as missing:
            service.present_spend_budget_evaluation("", accepted.spend_budget_id)
        self.assertEqual(missing.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as unknown_tenant:
            service.present_spend_budget_evaluation("urn:cwl:missing", accepted.spend_budget_id)
        self.assertEqual(unknown_tenant.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as unknown_budget:
            service.present_spend_budget_evaluation(TENANT_ONE, uuid4())
        self.assertEqual(unknown_budget.exception.rejection_reason_code, "spend_budget_not_found")
        ledger.register_tenant(TENANT_TWO)
        with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as crossed:
            service.present_spend_budget_evaluation(TENANT_TWO, accepted.spend_budget_id)
        self.assertEqual(crossed.exception.rejection_reason_code, "spend_budget_not_found")
        rated = seed_rated_ledger()
        tenant_one, _ = rated.resolve_tenant(TENANT_ONE)
        assert tenant_one is not None
        foreign = rated.billing_accounts[ACCOUNT_TWO]
        mismatched = rated.insert_spend_budget(
            StoredSpendBudget(
                spend_budget_id=generate_record_id(),
                tenant_account_id=tenant_one.tenant_account_id,
                billing_account_id=foreign.billing_account_id,
                spend_budget_contract_version=1,
                currency_code="USD",
                budget_amount=BUDGET_AMOUNT,
                window_started_at=MORNING_WINDOW.window_started_at,
                window_ended_at=MORNING_WINDOW.window_ended_at,
                source_payload_hash=compute_spend_budget_payload_hash(
                    {
                        "billing_account_id": str(foreign.billing_account_id),
                        "currency_code": "USD",
                        "budget_amount": format_exact_decimal(BUDGET_AMOUNT),
                        "window_started_at": WINDOW_STARTED,
                        "window_ended_at": WINDOW_ENDED,
                        "spend_budget_contract_version": 1,
                    }
                ),
                published_at=AS_OF,
            )
        )
        with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as forbidden:
            SpendBudgetEvaluationPresentmentService(rated).present_spend_budget_evaluation(
                TENANT_ONE, mismatched.spend_budget_id
            )
        self.assertEqual(forbidden.exception.rejection_reason_code, "billing_account_forbidden")
        missing_account = rated.insert_spend_budget(
            StoredSpendBudget(
                spend_budget_id=generate_record_id(),
                tenant_account_id=tenant_one.tenant_account_id,
                billing_account_id=uuid4(),
                spend_budget_contract_version=1,
                currency_code="USD",
                budget_amount=BUDGET_AMOUNT,
                window_started_at=MORNING_WINDOW.window_started_at,
                window_ended_at=MORNING_WINDOW.window_ended_at,
                source_payload_hash="sha256:" + "a" * 64,
                published_at=AS_OF,
            )
        )
        with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as gone:
            SpendBudgetEvaluationPresentmentService(rated).present_spend_budget_evaluation(
                TENANT_ONE, missing_account.spend_budget_id
            )
        self.assertEqual(gone.exception.rejection_reason_code, "billing_account_not_found")

    def test_float_money_and_rated_spend_errors_fail_closed(self) -> None:
        """IEEE money and nested rated-spend failures stay rejected."""
        ledger, _, accepted, _ = _publish_on_rated_ledger()
        assert accepted.spend_budget_id is not None
        stored = ledger.get_spend_budget(accepted.spend_budget_id)
        assert stored is not None
        ledger.spend_budgets[accepted.spend_budget_id] = replace(stored, budget_amount=1.25)
        with self.assertRaises(ExactDecimalError):
            SpendBudgetEvaluationPresentmentService(ledger).present_spend_budget_evaluation(
                TENANT_ONE, accepted.spend_budget_id
            )
        ledger.spend_budgets[accepted.spend_budget_id] = stored
        floated = RatedSpendPresentmentResult(
            tenant_reference=TENANT_ONE,
            billing_account_id=_account_id(ledger),
            billing_account_reference=ACCOUNT_ONE,
            window_started_at=MORNING_WINDOW.window_started_at,
            window_ended_at=MORNING_WINDOW.window_ended_at,
            products=(
                RatedSpendProductResult(
                    currency_code="USD",
                    product_code="contextual_orchestrator",
                    rated_amount=0.003705,  # type: ignore[arg-type]
                ),
            ),
        )
        with mock.patch.object(
            RatedSpendPresentmentService,
            "present_rated_spend",
            return_value=floated,
        ):
            with self.assertRaises(ExactDecimalError):
                SpendBudgetEvaluationPresentmentService(ledger).present_spend_budget_evaluation(
                    TENANT_ONE, accepted.spend_budget_id
                )
        with mock.patch.object(
            RatedSpendPresentmentService,
            "present_rated_spend",
            side_effect=RatedSpendPresentmentQueryError("billing_account_forbidden"),
        ):
            with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as forbidden:
                SpendBudgetEvaluationPresentmentService(ledger).present_spend_budget_evaluation(
                    TENANT_ONE, accepted.spend_budget_id
                )
        self.assertEqual(forbidden.exception.rejection_reason_code, "billing_account_forbidden")
        with mock.patch.object(
            RatedSpendPresentmentService,
            "present_rated_spend",
            side_effect=RatedSpendPresentmentQueryError("tenant_not_found"),
        ):
            with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as nested:
                SpendBudgetEvaluationPresentmentService(ledger).present_spend_budget_evaluation(
                    TENANT_ONE, accepted.spend_budget_id
                )
        self.assertEqual(nested.exception.rejection_reason_code, "tenant_not_found")
        empty = SpendBudgetEvaluationPresentmentService()
        with self.assertRaises(SpendBudgetEvaluationPresentmentQueryError) as hollow:
            empty.present_spend_budget_evaluation(TENANT_ONE, uuid4())
        self.assertEqual(hollow.exception.rejection_reason_code, "tenant_not_found")
        with mock.patch.object(ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaises(ValueError):
                SpendBudgetEvaluationPresentmentService(ledger).present_spend_budget_evaluation(
                    TENANT_ONE, accepted.spend_budget_id
                )

    def test_http_get_is_safe_and_pins_the_tenant_header(self) -> None:
        """GET evaluates same-tenant budgets; missing pin is 422; leaks stay closed."""
        ledger, account_id, accepted, _ = _publish_on_rated_ledger()
        assert accepted.spend_budget_id is not None
        app = create_http_app(ledger)
        path = _evaluation_path(accepted.spend_budget_id)
        status, body = invoke_http(
            app,
            "GET",
            path,
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["spend_budget_id"], str(accepted.spend_budget_id))
        self.assertEqual(body["billing_account_id"], str(account_id))
        self.assertEqual(body["budget_amount"], "100.00")
        self.assertEqual(body["rated_amount"], format_exact_decimal(KNOWN_MORNING_TOTAL))
        self.assertEqual(
            body["remaining_amount"],
            format_exact_decimal(BUDGET_AMOUNT - KNOWN_MORNING_TOTAL),
        )
        self.assertEqual(body["over_amount"], "0")
        self.assertEqual(body["utilization_status"], "under")
        self.assertEqual(body["next_operator_action"], "wait")
        self.assertEqual(validate_spend_budget_evaluation_presentment(body), ())
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
        ledger.register_tenant(TENANT_TWO)
        other_status, other_body = invoke_http(
            app,
            "GET",
            path,
            headers={"X-CWL-Tenant-Reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "spend_budget_not_found")
        self.assertNotIn("budget_amount", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            _evaluation_path(uuid4()),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "spend_budget_not_found")
        method_status, method_body = invoke_http(
            app,
            "POST",
            path,
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        tenant_one, _ = ledger.resolve_tenant(TENANT_ONE)
        assert tenant_one is not None
        foreign = ledger.billing_accounts[ACCOUNT_TWO] if ACCOUNT_TWO in ledger.billing_accounts else None
        if foreign is None:
            ledger.register_tenant(TENANT_TWO)
            ledger.register_billing_account(TENANT_TWO, ACCOUNT_TWO)
            foreign = ledger.billing_accounts[ACCOUNT_TWO]
        forbidden_row = ledger.insert_spend_budget(
            StoredSpendBudget(
                spend_budget_id=generate_record_id(),
                tenant_account_id=tenant_one.tenant_account_id,
                billing_account_id=foreign.billing_account_id,
                spend_budget_contract_version=1,
                currency_code="USD",
                budget_amount=BUDGET_AMOUNT,
                window_started_at=MORNING_WINDOW.window_started_at,
                window_ended_at=MORNING_WINDOW.window_ended_at,
                source_payload_hash="sha256:" + "b" * 64,
                published_at=AS_OF,
            )
        )
        forbidden_status, forbidden_body = invoke_http(
            create_http_app(ledger),
            "GET",
            _evaluation_path(forbidden_row.spend_budget_id),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(forbidden_status, 403)
        self.assertEqual(forbidden_body["rejection_reason_code"], "billing_account_forbidden")
        with mock.patch(
            "metering_billing.http_app.SpendBudgetEvaluationPresentmentService.present_spend_budget_evaluation",
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
            "metering_billing.http_app.SpendBudgetEvaluationPresentmentService.present_spend_budget_evaluation",
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
            "metering_billing.http_app.SpendBudgetEvaluationPresentmentService.present_spend_budget_evaluation",
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
            "metering_billing.http_app.SpendBudgetEvaluationPresentmentService.present_spend_budget_evaluation",
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


if __name__ == "__main__":
    unittest.main()
