"""Spend-budget evaluation tests against already-rated exclusive product spend."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    RatedSpendPresentmentService,
    SpendBudgetEvaluationService,
    UsageRatingService,
    create_http_app,
    format_exact_decimal,
    validate_spend_budget_evaluation,
    validate_spend_budget_presentment,
)
from metering_billing.errors import (
    RatedSpendPresentmentQueryError,
    SpendBudgetEvaluationQueryError,
)
from metering_billing.rated_spend_presentment import (
    GROUP_BY_PRODUCT,
    RatedSpendPresentmentResult,
    RatedSpendProductResult,
)
from metering_billing.spend_budget_evaluation import (
    remaining_and_over_amounts,
    utilization_status,
)
from test_account_statement_presentment import _account_id
from test_http_app import invoke_http
from test_spend_budget import AS_OF, BUDGET_AMOUNT, publish_known_budget
from test_usage_ingestion import ACCOUNT_ONE, TENANT_ONE, TENANT_TWO
from test_usage_rating import (
    KNOWN_MORNING_TOTAL,
    MORNING_WINDOW,
    ingest_known_batch,
)


SMALL_BUDGET = Decimal("0.001")
AT_BUDGET = KNOWN_MORNING_TOTAL
UNDER_BUDGET = Decimal("1.00")


def _rate_known_morning():
    """Ingest the known morning batch and persist one rating run."""
    ingest = ingest_known_batch()
    UsageRatingService(ingest.ledger, clock=lambda: AS_OF).rate_usage_window(
        TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
    )
    return ingest.ledger


def _publish_on_rated_ledger(amount: Decimal, currency_code: str = "USD"):
    """Publish one budget on the known morning rated ledger."""
    ledger = _rate_known_morning()
    billing_account_id = _account_id(ledger)
    ledger, billing_account_id, accepted = publish_known_budget(
        ledger,
        billing_account_id,
        amount=amount,
        currency_code=currency_code,
    )
    return ledger, billing_account_id, accepted


class SpendBudgetEvaluationTests(unittest.TestCase):
    """Verify evaluation stays a safe read of one stored budget versus rated spend."""

    def test_zero_rated_spend_evaluates_under_without_growing_the_store(self) -> None:
        """A published budget with no matching rated rows is under and rated zero."""
        ledger, billing_account_id, accepted = publish_known_budget()
        spend_budget_count = len(ledger.spend_budgets)
        rating_count = len(ledger.rating_runs)
        evaluation = SpendBudgetEvaluationService(ledger).evaluate_spend_budget(
            TENANT_ONE, accepted.spend_budget_id
        )
        self.assertEqual(evaluation.spend_budget_id, accepted.spend_budget_id)
        self.assertEqual(evaluation.billing_account_id, billing_account_id)
        self.assertEqual(evaluation.currency_code, "USD")
        self.assertEqual(evaluation.budget_amount, BUDGET_AMOUNT)
        self.assertEqual(evaluation.window_started_at, MORNING_WINDOW.window_started_at)
        self.assertEqual(evaluation.window_ended_at, MORNING_WINDOW.window_ended_at)
        self.assertEqual(evaluation.spend_budget_status, "published")
        self.assertEqual(evaluation.rated_amount, Decimal("0"))
        self.assertEqual(evaluation.remaining_amount, BUDGET_AMOUNT)
        self.assertEqual(evaluation.over_amount, Decimal("0"))
        self.assertEqual(evaluation.utilization_status, "under")
        self.assertEqual(evaluation.next_operator_action, "wait")
        payload = evaluation.as_contract_dict()
        self.assertEqual(validate_spend_budget_evaluation(payload), ())
        self.assertIsInstance(payload["rated_amount"], str)
        self.assertNotIsInstance(payload["rated_amount"], float)
        self.assertNotIn("source_payload_hash", payload)
        self.assertNotIn("published_at", payload)
        self.assertNotIn("retained_earnings", payload)
        self.assertEqual(len(ledger.spend_budgets), spend_budget_count)
        self.assertEqual(len(ledger.rating_runs), rating_count)
        self.assertEqual(len(ledger.invoice_drafts), 0)
        self.assertEqual(len(ledger.journal_proposals), 0)
        self.assertEqual(len(ledger.webhook_outbox_events), 0)

    def test_known_morning_rating_is_under_at_or_over_the_published_budget(self) -> None:
        """Real exclusive morning rating compares exactly under, at, and over."""
        ledger, _, under_budget = _publish_on_rated_ledger(UNDER_BUDGET)
        under = SpendBudgetEvaluationService(ledger).evaluate_spend_budget(
            TENANT_ONE, under_budget.spend_budget_id
        )
        self.assertEqual(under.rated_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(under.utilization_status, "under")
        self.assertEqual(under.remaining_amount, UNDER_BUDGET - KNOWN_MORNING_TOTAL)
        self.assertEqual(under.over_amount, Decimal("0"))
        self.assertEqual(validate_spend_budget_evaluation(under.as_contract_dict()), ())

        _, _, at_budget = publish_known_budget(
            ledger, _account_id(ledger), amount=AT_BUDGET
        )
        at_eval = SpendBudgetEvaluationService(ledger).evaluate_spend_budget(
            TENANT_ONE, at_budget.spend_budget_id
        )
        self.assertEqual(at_eval.rated_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(at_eval.utilization_status, "at")
        self.assertEqual(at_eval.remaining_amount, Decimal("0"))
        self.assertEqual(at_eval.over_amount, Decimal("0"))
        self.assertEqual(validate_spend_budget_evaluation(at_eval.as_contract_dict()), ())

        _, _, over_budget = publish_known_budget(
            ledger, _account_id(ledger), amount=SMALL_BUDGET
        )
        over = SpendBudgetEvaluationService(ledger).evaluate_spend_budget(
            TENANT_ONE, over_budget.spend_budget_id
        )
        self.assertEqual(over.rated_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(over.utilization_status, "over")
        self.assertEqual(over.remaining_amount, Decimal("0"))
        self.assertEqual(over.over_amount, KNOWN_MORNING_TOTAL - SMALL_BUDGET)
        self.assertEqual(validate_spend_budget_evaluation(over.as_contract_dict()), ())

    def test_krw_budget_omits_usd_rated_rows_and_does_not_mix_currencies(self) -> None:
        """A KRW budget on a USD-rated ledger evaluates as under with rated zero."""
        ledger, billing_account_id, accepted = _publish_on_rated_ledger(
            Decimal("1000"), currency_code="KRW"
        )
        evaluation = SpendBudgetEvaluationService(ledger).evaluate_spend_budget(
            TENANT_ONE, accepted.spend_budget_id
        )
        self.assertEqual(evaluation.currency_code, "KRW")
        self.assertEqual(evaluation.rated_amount, Decimal("0"))
        self.assertEqual(evaluation.remaining_amount, Decimal("1000"))
        self.assertEqual(evaluation.utilization_status, "under")
        spend = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, billing_account_id, MORNING_WINDOW, group_by=GROUP_BY_PRODUCT
        )
        self.assertTrue(any(row.currency_code == "USD" for row in spend.products))
        self.assertFalse(any(row.currency_code == "KRW" for row in spend.products))

    def test_sums_same_currency_product_rows_and_reuses_group_by_product(self) -> None:
        """USD product rows sum; a KRW row stays omitted; exclusive rules are reused."""
        ledger, billing_account_id, accepted = publish_known_budget()
        spend = RatedSpendPresentmentResult(
            tenant_reference=TENANT_ONE,
            billing_account_id=billing_account_id,
            billing_account_reference=ACCOUNT_ONE,
            window_started_at=MORNING_WINDOW.window_started_at,
            window_ended_at=MORNING_WINDOW.window_ended_at,
            products=(
                RatedSpendProductResult("USD", "contextual_orchestrator", Decimal("20.00")),
                RatedSpendProductResult("USD", "contextual_memory", Decimal("35.00")),
                RatedSpendProductResult("KRW", "contextual_orchestrator", Decimal("1000")),
            ),
        )
        with mock.patch.object(
            RatedSpendPresentmentService,
            "present_rated_spend",
            return_value=spend,
        ) as present:
            evaluation = SpendBudgetEvaluationService(ledger).evaluate_spend_budget(
                TENANT_ONE, accepted.spend_budget_id
            )
        present.assert_called_once()
        _args, kwargs = present.call_args
        self.assertEqual(_args[0], TENANT_ONE)
        self.assertEqual(_args[1], billing_account_id)
        self.assertEqual(_args[2].window_started_at, MORNING_WINDOW.window_started_at)
        self.assertEqual(_args[2].window_ended_at, MORNING_WINDOW.window_ended_at)
        self.assertEqual(kwargs["group_by"], GROUP_BY_PRODUCT)
        self.assertEqual(evaluation.rated_amount, Decimal("55.00"))
        self.assertEqual(evaluation.remaining_amount, Decimal("45.00"))
        self.assertEqual(evaluation.over_amount, Decimal("0"))
        self.assertEqual(evaluation.utilization_status, "under")

    def test_http_get_presents_evaluation_and_leaves_item_get_unchanged(self) -> None:
        """GET evaluation is 200; #82 item GET still has no rated or utilization fields."""
        ledger, _, accepted = publish_known_budget()
        ledger.register_tenant(TENANT_TWO)
        app = create_http_app(ledger)
        path = f"/v1/spend-budgets/{accepted.spend_budget_id}/evaluation"
        status, body = invoke_http(
            app, "GET", path, query={"tenant_reference": TENANT_ONE}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["spend_budget_id"], str(accepted.spend_budget_id))
        self.assertEqual(body["rated_amount"], "0")
        self.assertEqual(body["remaining_amount"], "100.00")
        self.assertEqual(body["over_amount"], "0")
        self.assertEqual(body["utilization_status"], "under")
        self.assertEqual(body["next_operator_action"], "wait")
        self.assertEqual(validate_spend_budget_evaluation(body), ())
        item_status, item_body = invoke_http(
            app,
            "GET",
            f"/v1/spend-budgets/{accepted.spend_budget_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(item_status, 200)
        self.assertNotIn("rated_amount", item_body)
        self.assertNotIn("remaining_amount", item_body)
        self.assertNotIn("over_amount", item_body)
        self.assertNotIn("utilization_status", item_body)
        self.assertEqual(validate_spend_budget_presentment(item_body), ())
        missing_status, missing_body = invoke_http(app, "GET", path)
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        cross_status, cross_body = invoke_http(
            app, "GET", path, query={"tenant_reference": TENANT_TWO}
        )
        self.assertEqual(cross_status, 404)
        self.assertEqual(cross_body["rejection_reason_code"], "spend_budget_not_found")
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/spend-budgets/{uuid4()}/evaluation",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "spend_budget_not_found")
        post_status, post_body = invoke_http(
            app, "POST", path, {"tenant_reference": TENANT_ONE}
        )
        self.assertEqual(post_status, 422)
        self.assertEqual(post_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.SpendBudgetEvaluationService.evaluate_spend_budget",
            side_effect=ValueError("posted"),
        ):
            value_status, value_body = invoke_http(
                app, "GET", path, query={"tenant_reference": TENANT_ONE}
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")

    def test_fail_closed_tenant_identity_and_mapped_rated_spend_errors(self) -> None:
        """Empty tenant, unknown id, hollow resolve, and rated-spend errors fail closed."""
        ledger, _, accepted = publish_known_budget()
        service = SpendBudgetEvaluationService(ledger)
        with self.assertRaises(SpendBudgetEvaluationQueryError) as empty:
            service.evaluate_spend_budget("", accepted.spend_budget_id)
        self.assertEqual(empty.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(SpendBudgetEvaluationQueryError) as missing_tenant:
            service.evaluate_spend_budget("urn:cwl:missing", accepted.spend_budget_id)
        self.assertEqual(missing_tenant.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(SpendBudgetEvaluationQueryError) as unknown:
            service.evaluate_spend_budget(TENANT_ONE, uuid4())
        self.assertEqual(unknown.exception.rejection_reason_code, "spend_budget_not_found")
        with mock.patch.object(ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaises(ValueError):
                service.evaluate_spend_budget(TENANT_ONE, accepted.spend_budget_id)
        self.assertEqual(utilization_status(Decimal("1"), Decimal("2")), "under")
        self.assertEqual(utilization_status(Decimal("2"), Decimal("2")), "at")
        self.assertEqual(utilization_status(Decimal("3"), Decimal("2")), "over")
        with self.assertRaises(ValueError):
            remaining_and_over_amounts("posted", BUDGET_AMOUNT, Decimal("0"))
        for reason, mapped in (
            ("tenant_not_found", "tenant_not_found"),
            ("billing_account_not_found", "request_invalid"),
            ("billing_account_forbidden", "request_invalid"),
            ("request_invalid", "request_invalid"),
            ("posted", "request_invalid"),
        ):
            with mock.patch.object(
                RatedSpendPresentmentService,
                "present_rated_spend",
                side_effect=RatedSpendPresentmentQueryError(reason),
            ):
                with self.assertRaises(SpendBudgetEvaluationQueryError) as raised:
                    service.evaluate_spend_budget(TENANT_ONE, accepted.spend_budget_id)
            self.assertEqual(raised.exception.rejection_reason_code, mapped)


if __name__ == "__main__":
    unittest.main()
