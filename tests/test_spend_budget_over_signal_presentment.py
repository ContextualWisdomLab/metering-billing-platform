"""Spend-budget over-signal GET tests for live observation plus outbox rows."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    SpendBudgetEvaluationPresentmentService,
    SpendBudgetOverSignalPresentmentService,
    SpendBudgetOverSignalService,
    create_http_app,
    format_exact_decimal,
    validate_spend_budget_over_signal,
    validate_spend_budget_over_signal_presentment,
    validate_webhook_outbox_event_presentment,
)
from metering_billing.errors import (
    ExactDecimalError,
    SpendBudgetEvaluationPresentmentQueryError,
    SpendBudgetOverSignalPresentmentQueryError,
)
from metering_billing.spend_budget_evaluation_presentment import (
    SpendBudgetEvaluationPresentmentResult,
    UTILIZATION_AT,
    UTILIZATION_OVER,
    UTILIZATION_UNDER,
)
from metering_billing.usage_ledger import StoredSpendBudget, generate_record_id
from test_http_app import invoke_http
from test_spend_budget_evaluation_presentment import OVER_BUDGET_AMOUNT, _publish_on_rated_ledger
from test_spend_budget_over_signal import _over_events, _over_signal_path
from test_usage_ingestion import ACCOUNT_TWO, TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL, MORNING_WINDOW


AS_OF = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)


class SpendBudgetOverSignalPresentmentTests(unittest.TestCase):
    """Verify GET presents live over-signal and stored outbox without writing."""

    def test_under_and_at_present_zero_outbox_rows_without_writing(self) -> None:
        """Same-tenant under and at observations stay accepted with zero over rows."""
        ledger, account_id, accepted, _ = _publish_on_rated_ledger()
        assert accepted.spend_budget_id is not None
        prior_outbox = len(ledger.webhook_outbox_events)
        under = SpendBudgetOverSignalPresentmentService(ledger).present_spend_budget_over_signal(
            TENANT_ONE, accepted.spend_budget_id
        )
        replay = SpendBudgetOverSignalPresentmentService(ledger).present_spend_budget_over_signal(
            TENANT_ONE, accepted.spend_budget_id
        )
        self.assertEqual(under.as_contract_dict(), replay.as_contract_dict())
        payload = under.as_contract_dict()
        self.assertEqual(validate_spend_budget_over_signal_presentment(payload), ())
        self.assertEqual(validate_spend_budget_over_signal(payload["over_signal"]), ())
        self.assertEqual(payload["over_signal"]["spend_budget_over_signal_outcome_code"], "accepted")
        self.assertEqual(payload["over_signal"]["spend_budget_id"], str(accepted.spend_budget_id))
        self.assertEqual(payload["over_signal"]["billing_account_id"], str(account_id))
        self.assertEqual(payload["over_signal"]["utilization_status"], UTILIZATION_UNDER)
        self.assertEqual(payload["over_signal"]["over_amount"], "0")
        self.assertEqual(payload["over_signal"]["next_operator_action"], "wait")
        self.assertEqual(payload["webhook_outbox_events"], [])
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        self.assertEqual(len(_over_events(ledger)), 0)
        at_ledger, _, at_accepted, _ = _publish_on_rated_ledger(KNOWN_MORNING_TOTAL)
        assert at_accepted.spend_budget_id is not None
        at_result = SpendBudgetOverSignalPresentmentService(
            at_ledger
        ).present_spend_budget_over_signal(TENANT_ONE, at_accepted.spend_budget_id)
        self.assertEqual(at_result.over_signal.utilization_status, UTILIZATION_AT)
        self.assertEqual(at_result.over_signal.over_amount, Decimal("0"))
        self.assertEqual(at_result.webhook_outbox_events, ())
        self.assertEqual(validate_spend_budget_over_signal_presentment(at_result.as_contract_dict()), ())
        self.assertEqual(len(_over_events(at_ledger)), 0)

    def test_over_without_write_stays_live_and_does_not_enqueue(self) -> None:
        """Live over observation is accepted with zero outbox rows and no GET write."""
        ledger, _, accepted, _ = _publish_on_rated_ledger(OVER_BUDGET_AMOUNT)
        assert accepted.spend_budget_id is not None
        prior_outbox = len(ledger.webhook_outbox_events)
        observed = SpendBudgetOverSignalPresentmentService(ledger).present_spend_budget_over_signal(
            TENANT_ONE, accepted.spend_budget_id
        )
        payload = observed.as_contract_dict()
        self.assertEqual(validate_spend_budget_over_signal_presentment(payload), ())
        self.assertEqual(payload["over_signal"]["utilization_status"], UTILIZATION_OVER)
        self.assertEqual(
            payload["over_signal"]["over_amount"],
            format_exact_decimal(KNOWN_MORNING_TOTAL - OVER_BUDGET_AMOUNT),
        )
        self.assertEqual(payload["webhook_outbox_events"], [])
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        self.assertEqual(len(_over_events(ledger)), 0)

    def test_first_over_write_then_get_presents_one_outbox_row(self) -> None:
        """GET after first-over enqueue reuses the live over-signal and outbox envelopes."""
        ledger, _, accepted, _ = _publish_on_rated_ledger(OVER_BUDGET_AMOUNT)
        assert accepted.spend_budget_id is not None
        written = SpendBudgetOverSignalService(ledger, clock=lambda: AS_OF).observe_spend_budget_over(
            TENANT_ONE, accepted.spend_budget_id
        )
        self.assertEqual(written.utilization_status, UTILIZATION_OVER)
        self.assertEqual(len(_over_events(ledger)), 1)
        prior_outbox = len(ledger.webhook_outbox_events)
        observed = SpendBudgetOverSignalPresentmentService(ledger).present_spend_budget_over_signal(
            TENANT_ONE, accepted.spend_budget_id
        )
        payload = observed.as_contract_dict()
        self.assertEqual(validate_spend_budget_over_signal_presentment(payload), ())
        self.assertEqual(validate_spend_budget_over_signal(payload["over_signal"]), ())
        self.assertEqual(payload["over_signal"]["spend_budget_over_signal_outcome_code"], "accepted")
        self.assertEqual(payload["over_signal"]["utilization_status"], UTILIZATION_OVER)
        self.assertEqual(len(payload["webhook_outbox_events"]), 1)
        outbox = payload["webhook_outbox_events"][0]
        self.assertEqual(validate_webhook_outbox_event_presentment(outbox), ())
        self.assertEqual(outbox["event_type_code"], "spend_budget.over")
        self.assertEqual(outbox["source_id"], str(accepted.spend_budget_id))
        self.assertEqual(outbox["delivery_status"], "pending")
        self.assertEqual(outbox["next_operator_action"], "run_deliveries")
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        self.assertEqual(len(_over_events(ledger)), 1)
        replay_write = SpendBudgetOverSignalService(
            ledger, clock=lambda: AS_OF
        ).observe_spend_budget_over(TENANT_ONE, accepted.spend_budget_id)
        self.assertEqual(replay_write.spend_budget_over_signal_outcome_code.value, "duplicate_replay")
        replay_get = SpendBudgetOverSignalPresentmentService(ledger).present_spend_budget_over_signal(
            TENANT_ONE, accepted.spend_budget_id
        )
        self.assertEqual(len(replay_get.webhook_outbox_events), 1)
        self.assertEqual(
            replay_get.as_contract_dict()["webhook_outbox_events"][0]["outbox_event_id"],
            outbox["outbox_event_id"],
        )
        self.assertEqual(len(_over_events(ledger)), 1)

    def test_unknown_cross_tenant_and_pin_mismatch_fail_closed(self) -> None:
        """Unknown and cross-tenant budgets 404; foreign billing accounts 403."""
        ledger, _, accepted, _ = _publish_on_rated_ledger()
        assert accepted.spend_budget_id is not None
        service = SpendBudgetOverSignalPresentmentService(ledger)
        with self.assertRaises(SpendBudgetOverSignalPresentmentQueryError) as missing:
            service.present_spend_budget_over_signal("", accepted.spend_budget_id)
        self.assertEqual(missing.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(SpendBudgetOverSignalPresentmentQueryError) as unknown_tenant:
            service.present_spend_budget_over_signal("urn:cwl:missing", accepted.spend_budget_id)
        self.assertEqual(unknown_tenant.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(SpendBudgetOverSignalPresentmentQueryError) as unknown_budget:
            service.present_spend_budget_over_signal(TENANT_ONE, uuid4())
        self.assertEqual(unknown_budget.exception.rejection_reason_code, "spend_budget_not_found")
        ledger.register_tenant(TENANT_TWO)
        with self.assertRaises(SpendBudgetOverSignalPresentmentQueryError) as crossed:
            service.present_spend_budget_over_signal(TENANT_TWO, accepted.spend_budget_id)
        self.assertEqual(crossed.exception.rejection_reason_code, "spend_budget_not_found")
        rated = ledger
        tenant_one, _ = rated.resolve_tenant(TENANT_ONE)
        assert tenant_one is not None
        if ACCOUNT_TWO not in rated.billing_accounts:
            rated.register_billing_account(TENANT_TWO, ACCOUNT_TWO)
        foreign = rated.billing_accounts[ACCOUNT_TWO]
        mismatched = rated.insert_spend_budget(
            StoredSpendBudget(
                spend_budget_id=generate_record_id(),
                tenant_account_id=tenant_one.tenant_account_id,
                billing_account_id=foreign.billing_account_id,
                spend_budget_contract_version=1,
                currency_code="USD",
                budget_amount=Decimal("100.00"),
                window_started_at=MORNING_WINDOW.window_started_at,
                window_ended_at=MORNING_WINDOW.window_ended_at,
                source_payload_hash="sha256:" + "b" * 64,
                published_at=AS_OF,
            )
        )
        with self.assertRaises(SpendBudgetOverSignalPresentmentQueryError) as forbidden:
            SpendBudgetOverSignalPresentmentService(rated).present_spend_budget_over_signal(
                TENANT_ONE, mismatched.spend_budget_id
            )
        self.assertEqual(forbidden.exception.rejection_reason_code, "billing_account_forbidden")
        empty = SpendBudgetOverSignalPresentmentService()
        with self.assertRaises(SpendBudgetOverSignalPresentmentQueryError) as hollow:
            empty.present_spend_budget_over_signal(TENANT_ONE, uuid4())
        self.assertEqual(hollow.exception.rejection_reason_code, "tenant_not_found")

    def test_missing_budget_and_unsupported_utilization_fail_closed(self) -> None:
        """A vanished budget or unknown utilization cannot invent an observation."""
        ledger, _, accepted, _ = _publish_on_rated_ledger()
        assert accepted.spend_budget_id is not None
        evaluation = SpendBudgetEvaluationPresentmentService(ledger).present_spend_budget_evaluation(
            TENANT_ONE, accepted.spend_budget_id
        )
        with mock.patch.object(
            SpendBudgetEvaluationPresentmentService,
            "present_spend_budget_evaluation",
            return_value=evaluation,
        ), mock.patch.object(ledger, "get_spend_budget", return_value=None):
            with self.assertRaises(ValueError):
                SpendBudgetOverSignalPresentmentService(ledger).present_spend_budget_over_signal(
                    TENANT_ONE, accepted.spend_budget_id
                )
        unsupported = SpendBudgetEvaluationPresentmentResult(
            spend_budget_id=evaluation.spend_budget_id,
            tenant_reference=evaluation.tenant_reference,
            billing_account_id=evaluation.billing_account_id,
            currency_code=evaluation.currency_code,
            budget_amount=evaluation.budget_amount,
            rated_amount=evaluation.rated_amount,
            remaining_amount=evaluation.remaining_amount,
            over_amount=evaluation.over_amount,
            utilization_status="posted",
            window_started_at=evaluation.window_started_at,
            window_ended_at=evaluation.window_ended_at,
            spend_budget_status=evaluation.spend_budget_status,
            next_operator_action=evaluation.next_operator_action,
        )
        with mock.patch.object(
            SpendBudgetEvaluationPresentmentService,
            "present_spend_budget_evaluation",
            return_value=unsupported,
        ):
            with self.assertRaises(ValueError):
                SpendBudgetOverSignalPresentmentService(ledger).present_spend_budget_over_signal(
                    TENANT_ONE, accepted.spend_budget_id
                )
        with mock.patch.object(
            SpendBudgetEvaluationPresentmentService,
            "present_spend_budget_evaluation",
            side_effect=SpendBudgetEvaluationPresentmentQueryError("tax_exempt"),
        ):
            with self.assertRaises(SpendBudgetOverSignalPresentmentQueryError) as nested:
                SpendBudgetOverSignalPresentmentService(ledger).present_spend_budget_over_signal(
                    TENANT_ONE, accepted.spend_budget_id
                )
        self.assertEqual(nested.exception.rejection_reason_code, "tax_exempt")

    def test_http_get_is_safe_and_pins_the_tenant_header(self) -> None:
        """GET presents same-tenant observations; missing pin is 422; leaks stay closed."""
        ledger, account_id, accepted, _ = _publish_on_rated_ledger()
        assert accepted.spend_budget_id is not None
        app = create_http_app(ledger)
        path = _over_signal_path(accepted.spend_budget_id)
        status, body = invoke_http(
            app,
            "GET",
            path,
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(validate_spend_budget_over_signal_presentment(body), ())
        self.assertEqual(body["over_signal"]["spend_budget_id"], str(accepted.spend_budget_id))
        self.assertEqual(body["over_signal"]["billing_account_id"], str(account_id))
        self.assertEqual(body["over_signal"]["utilization_status"], UTILIZATION_UNDER)
        self.assertEqual(body["over_signal"]["over_amount"], "0")
        self.assertEqual(body["webhook_outbox_events"], [])
        query_status, query_body = invoke_http(
            app, "GET", path, query={"tenant_reference": TENANT_ONE}
        )
        self.assertEqual(query_status, 200)
        self.assertEqual(query_body, body)
        over_ledger, _, over_accepted, _ = _publish_on_rated_ledger(OVER_BUDGET_AMOUNT)
        assert over_accepted.spend_budget_id is not None
        SpendBudgetOverSignalService(over_ledger, clock=lambda: AS_OF).observe_spend_budget_over(
            TENANT_ONE, over_accepted.spend_budget_id
        )
        over_status, over_body = invoke_http(
            create_http_app(over_ledger),
            "GET",
            _over_signal_path(over_accepted.spend_budget_id),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(over_status, 200)
        self.assertEqual(over_body["over_signal"]["utilization_status"], UTILIZATION_OVER)
        self.assertEqual(len(over_body["webhook_outbox_events"]), 1)
        self.assertEqual(
            over_body["webhook_outbox_events"][0]["event_type_code"], "spend_budget.over"
        )
        live_over_ledger, _, live_over, _ = _publish_on_rated_ledger(OVER_BUDGET_AMOUNT)
        assert live_over.spend_budget_id is not None
        live_status, live_body = invoke_http(
            create_http_app(live_over_ledger),
            "GET",
            _over_signal_path(live_over.spend_budget_id),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(live_status, 200)
        self.assertEqual(live_body["over_signal"]["utilization_status"], UTILIZATION_OVER)
        self.assertEqual(live_body["webhook_outbox_events"], [])
        self.assertEqual(len(_over_events(live_over_ledger)), 0)
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
        self.assertNotIn("over_signal", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            _over_signal_path(uuid4()),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "spend_budget_not_found")
        method_status, method_body = invoke_http(
            app,
            "PUT",
            path,
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        tenant_one, _ = ledger.resolve_tenant(TENANT_ONE)
        assert tenant_one is not None
        if ACCOUNT_TWO not in ledger.billing_accounts:
            ledger.register_billing_account(TENANT_TWO, ACCOUNT_TWO)
        foreign = ledger.billing_accounts[ACCOUNT_TWO]
        forbidden_row = ledger.insert_spend_budget(
            StoredSpendBudget(
                spend_budget_id=generate_record_id(),
                tenant_account_id=tenant_one.tenant_account_id,
                billing_account_id=foreign.billing_account_id,
                spend_budget_contract_version=1,
                currency_code="USD",
                budget_amount=Decimal("100.00"),
                window_started_at=MORNING_WINDOW.window_started_at,
                window_ended_at=MORNING_WINDOW.window_ended_at,
                source_payload_hash="sha256:" + "b" * 64,
                published_at=AS_OF,
            )
        )
        forbidden_status, forbidden_body = invoke_http(
            create_http_app(ledger),
            "GET",
            _over_signal_path(forbidden_row.spend_budget_id),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(forbidden_status, 403)
        self.assertEqual(forbidden_body["rejection_reason_code"], "billing_account_forbidden")
        with mock.patch(
            "metering_billing.http_app.SpendBudgetOverSignalPresentmentService.present_spend_budget_over_signal",
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
            "metering_billing.http_app.SpendBudgetOverSignalPresentmentService.present_spend_budget_over_signal",
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
            "metering_billing.http_app.SpendBudgetOverSignalPresentmentService.present_spend_budget_over_signal",
            side_effect=SpendBudgetOverSignalPresentmentQueryError("billing_account_not_found"),
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
            "metering_billing.http_app.SpendBudgetOverSignalPresentmentService.present_spend_budget_over_signal",
            side_effect=SpendBudgetOverSignalPresentmentQueryError("request_invalid"),
        ):
            invalid_status, invalid_body = invoke_http(
                create_http_app(ledger),
                "GET",
                path,
                headers={"X-CWL-Tenant-Reference": TENANT_ONE},
            )
        self.assertEqual(invalid_status, 422)
        self.assertEqual(invalid_body["rejection_reason_code"], "request_invalid")


if __name__ == "__main__":
    unittest.main()
