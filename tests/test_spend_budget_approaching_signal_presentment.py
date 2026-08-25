"""Spend-budget approaching-signal GET tests for live observation plus outbox rows."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    SpendBudgetEvaluationPresentmentService,
    SpendBudgetApproachingSignalPresentmentService,
    SpendBudgetApproachingSignalService,
    create_http_app,
    format_exact_decimal,
    validate_spend_budget_approaching_signal,
    validate_spend_budget_approaching_signal_presentment,
    validate_webhook_outbox_event_presentment,
)
from metering_billing.errors import (
    ExactDecimalError,
    SpendBudgetEvaluationPresentmentQueryError,
    SpendBudgetApproachingSignalPresentmentQueryError,
)
from metering_billing.spend_budget_evaluation_presentment import (
    SpendBudgetEvaluationPresentmentResult,
    UTILIZATION_AT,
    UTILIZATION_OVER,
    UTILIZATION_UNDER,
)
from metering_billing.usage_ledger import StoredSpendBudget, generate_record_id
from test_http_app import invoke_http
from test_spend_budget import BUDGET_AMOUNT
from test_spend_budget_evaluation_presentment import OVER_BUDGET_AMOUNT, _publish_on_rated_ledger
from test_spend_budget_approaching_signal import _approaching_events, _approaching_signal_path
from test_usage_ingestion import ACCOUNT_TWO, TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL, MORNING_WINDOW


AS_OF = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)


class SpendBudgetApproachingSignalPresentmentTests(unittest.TestCase):
    """Verify GET presents live approaching-signal and stored outbox without writing."""

    def test_under_and_over_present_zero_outbox_rows_without_writing(self) -> None:
        """Same-tenant under and over observations stay accepted with zero approaching rows."""
        ledger, account_id, accepted, _ = _publish_on_rated_ledger()
        assert accepted.spend_budget_id is not None
        prior_outbox = len(ledger.webhook_outbox_events)
        under = SpendBudgetApproachingSignalPresentmentService(
            ledger
        ).present_spend_budget_approaching_signal(TENANT_ONE, accepted.spend_budget_id)
        replay = SpendBudgetApproachingSignalPresentmentService(
            ledger
        ).present_spend_budget_approaching_signal(TENANT_ONE, accepted.spend_budget_id)
        self.assertEqual(under.as_contract_dict(), replay.as_contract_dict())
        payload = under.as_contract_dict()
        self.assertEqual(validate_spend_budget_approaching_signal_presentment(payload), ())
        self.assertEqual(validate_spend_budget_approaching_signal(payload["approaching_signal"]), ())
        self.assertEqual(
            payload["approaching_signal"]["spend_budget_approaching_signal_outcome_code"],
            "accepted",
        )
        self.assertEqual(
            payload["approaching_signal"]["spend_budget_id"], str(accepted.spend_budget_id)
        )
        self.assertEqual(payload["approaching_signal"]["billing_account_id"], str(account_id))
        self.assertEqual(payload["approaching_signal"]["utilization_status"], UTILIZATION_UNDER)
        self.assertEqual(
            payload["approaching_signal"]["remaining_amount"],
            format_exact_decimal(BUDGET_AMOUNT - KNOWN_MORNING_TOTAL),
        )
        self.assertEqual(payload["approaching_signal"]["next_operator_action"], "wait")
        self.assertEqual(payload["webhook_outbox_events"], [])
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        self.assertEqual(len(_approaching_events(ledger)), 0)
        over_ledger, _, over_accepted, _ = _publish_on_rated_ledger(OVER_BUDGET_AMOUNT)
        assert over_accepted.spend_budget_id is not None
        over_result = SpendBudgetApproachingSignalPresentmentService(
            over_ledger
        ).present_spend_budget_approaching_signal(TENANT_ONE, over_accepted.spend_budget_id)
        self.assertEqual(over_result.approaching_signal.utilization_status, UTILIZATION_OVER)
        self.assertEqual(over_result.approaching_signal.remaining_amount, Decimal("0"))
        self.assertEqual(over_result.webhook_outbox_events, ())
        self.assertEqual(
            validate_spend_budget_approaching_signal_presentment(over_result.as_contract_dict()),
            (),
        )
        self.assertEqual(len(_approaching_events(over_ledger)), 0)

    def test_at_without_write_stays_live_and_does_not_enqueue(self) -> None:
        """Live at observation is accepted with zero outbox rows and no GET write."""
        ledger, _, accepted, _ = _publish_on_rated_ledger(KNOWN_MORNING_TOTAL)
        assert accepted.spend_budget_id is not None
        prior_outbox = len(ledger.webhook_outbox_events)
        observed = SpendBudgetApproachingSignalPresentmentService(
            ledger
        ).present_spend_budget_approaching_signal(TENANT_ONE, accepted.spend_budget_id)
        payload = observed.as_contract_dict()
        self.assertEqual(validate_spend_budget_approaching_signal_presentment(payload), ())
        self.assertEqual(payload["approaching_signal"]["utilization_status"], UTILIZATION_AT)
        self.assertEqual(payload["approaching_signal"]["remaining_amount"], "0")
        self.assertEqual(payload["webhook_outbox_events"], [])
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        self.assertEqual(len(_approaching_events(ledger)), 0)

    def test_first_at_write_then_get_presents_one_outbox_row(self) -> None:
        """GET after first-at enqueue reuses the live approaching-signal and outbox envelopes."""
        ledger, _, accepted, _ = _publish_on_rated_ledger(KNOWN_MORNING_TOTAL)
        assert accepted.spend_budget_id is not None
        written = SpendBudgetApproachingSignalService(
            ledger, clock=lambda: AS_OF
        ).observe_spend_budget_approaching(TENANT_ONE, accepted.spend_budget_id)
        self.assertEqual(written.utilization_status, UTILIZATION_AT)
        self.assertEqual(len(_approaching_events(ledger)), 1)
        prior_outbox = len(ledger.webhook_outbox_events)
        observed = SpendBudgetApproachingSignalPresentmentService(
            ledger
        ).present_spend_budget_approaching_signal(TENANT_ONE, accepted.spend_budget_id)
        payload = observed.as_contract_dict()
        self.assertEqual(validate_spend_budget_approaching_signal_presentment(payload), ())
        self.assertEqual(validate_spend_budget_approaching_signal(payload["approaching_signal"]), ())
        self.assertEqual(
            payload["approaching_signal"]["spend_budget_approaching_signal_outcome_code"],
            "accepted",
        )
        self.assertEqual(payload["approaching_signal"]["utilization_status"], UTILIZATION_AT)
        self.assertEqual(len(payload["webhook_outbox_events"]), 1)
        outbox = payload["webhook_outbox_events"][0]
        self.assertEqual(validate_webhook_outbox_event_presentment(outbox), ())
        self.assertEqual(outbox["event_type_code"], "spend_budget.approaching")
        self.assertEqual(outbox["source_id"], str(accepted.spend_budget_id))
        self.assertEqual(outbox["delivery_status"], "pending")
        self.assertEqual(outbox["next_operator_action"], "run_deliveries")
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        self.assertEqual(len(_approaching_events(ledger)), 1)
        replay_write = SpendBudgetApproachingSignalService(
            ledger, clock=lambda: AS_OF
        ).observe_spend_budget_approaching(TENANT_ONE, accepted.spend_budget_id)
        self.assertEqual(
            replay_write.spend_budget_approaching_signal_outcome_code.value, "duplicate_replay"
        )
        replay_get = SpendBudgetApproachingSignalPresentmentService(
            ledger
        ).present_spend_budget_approaching_signal(TENANT_ONE, accepted.spend_budget_id)
        self.assertEqual(len(replay_get.webhook_outbox_events), 1)
        self.assertEqual(
            replay_get.as_contract_dict()["webhook_outbox_events"][0]["outbox_event_id"],
            outbox["outbox_event_id"],
        )
        self.assertEqual(len(_approaching_events(ledger)), 1)

    def test_unknown_cross_tenant_and_pin_mismatch_fail_closed(self) -> None:
        """Unknown and cross-tenant budgets 404; foreign billing accounts 403."""
        ledger, _, accepted, _ = _publish_on_rated_ledger()
        assert accepted.spend_budget_id is not None
        service = SpendBudgetApproachingSignalPresentmentService(ledger)
        with self.assertRaises(SpendBudgetApproachingSignalPresentmentQueryError) as missing:
            service.present_spend_budget_approaching_signal("", accepted.spend_budget_id)
        self.assertEqual(missing.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(SpendBudgetApproachingSignalPresentmentQueryError) as unknown_tenant:
            service.present_spend_budget_approaching_signal(
                "urn:cwl:missing", accepted.spend_budget_id
            )
        self.assertEqual(unknown_tenant.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(SpendBudgetApproachingSignalPresentmentQueryError) as unknown_budget:
            service.present_spend_budget_approaching_signal(TENANT_ONE, uuid4())
        self.assertEqual(unknown_budget.exception.rejection_reason_code, "spend_budget_not_found")
        ledger.register_tenant(TENANT_TWO)
        with self.assertRaises(SpendBudgetApproachingSignalPresentmentQueryError) as crossed:
            service.present_spend_budget_approaching_signal(TENANT_TWO, accepted.spend_budget_id)
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
        with self.assertRaises(SpendBudgetApproachingSignalPresentmentQueryError) as forbidden:
            SpendBudgetApproachingSignalPresentmentService(rated).present_spend_budget_approaching_signal(
                TENANT_ONE, mismatched.spend_budget_id
            )
        self.assertEqual(forbidden.exception.rejection_reason_code, "billing_account_forbidden")
        empty = SpendBudgetApproachingSignalPresentmentService()
        with self.assertRaises(SpendBudgetApproachingSignalPresentmentQueryError) as hollow:
            empty.present_spend_budget_approaching_signal(TENANT_ONE, uuid4())
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
                SpendBudgetApproachingSignalPresentmentService(
                    ledger
                ).present_spend_budget_approaching_signal(TENANT_ONE, accepted.spend_budget_id)
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
                SpendBudgetApproachingSignalPresentmentService(
                    ledger
                ).present_spend_budget_approaching_signal(TENANT_ONE, accepted.spend_budget_id)
        with mock.patch.object(
            SpendBudgetEvaluationPresentmentService,
            "present_spend_budget_evaluation",
            side_effect=SpendBudgetEvaluationPresentmentQueryError("tax_exempt"),
        ):
            with self.assertRaises(SpendBudgetApproachingSignalPresentmentQueryError) as nested:
                SpendBudgetApproachingSignalPresentmentService(
                    ledger
                ).present_spend_budget_approaching_signal(TENANT_ONE, accepted.spend_budget_id)
        self.assertEqual(nested.exception.rejection_reason_code, "tax_exempt")

    def test_http_get_is_safe_and_pins_the_tenant_header(self) -> None:
        """GET presents same-tenant observations; missing pin is 422; leaks stay closed."""
        ledger, account_id, accepted, _ = _publish_on_rated_ledger()
        assert accepted.spend_budget_id is not None
        app = create_http_app(ledger)
        path = _approaching_signal_path(accepted.spend_budget_id)
        status, body = invoke_http(
            app,
            "GET",
            path,
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(validate_spend_budget_approaching_signal_presentment(body), ())
        self.assertEqual(body["approaching_signal"]["spend_budget_id"], str(accepted.spend_budget_id))
        self.assertEqual(body["approaching_signal"]["billing_account_id"], str(account_id))
        self.assertEqual(body["approaching_signal"]["utilization_status"], UTILIZATION_UNDER)
        self.assertEqual(
            body["approaching_signal"]["remaining_amount"],
            format_exact_decimal(BUDGET_AMOUNT - KNOWN_MORNING_TOTAL),
        )
        self.assertEqual(body["webhook_outbox_events"], [])
        query_status, query_body = invoke_http(
            app, "GET", path, query={"tenant_reference": TENANT_ONE}
        )
        self.assertEqual(query_status, 200)
        self.assertEqual(query_body, body)
        at_ledger, _, at_accepted, _ = _publish_on_rated_ledger(KNOWN_MORNING_TOTAL)
        assert at_accepted.spend_budget_id is not None
        SpendBudgetApproachingSignalService(
            at_ledger, clock=lambda: AS_OF
        ).observe_spend_budget_approaching(TENANT_ONE, at_accepted.spend_budget_id)
        at_status, at_body = invoke_http(
            create_http_app(at_ledger),
            "GET",
            _approaching_signal_path(at_accepted.spend_budget_id),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(at_status, 200)
        self.assertEqual(at_body["approaching_signal"]["utilization_status"], UTILIZATION_AT)
        self.assertEqual(len(at_body["webhook_outbox_events"]), 1)
        self.assertEqual(
            at_body["webhook_outbox_events"][0]["event_type_code"], "spend_budget.approaching"
        )
        live_at_ledger, _, live_at, _ = _publish_on_rated_ledger(KNOWN_MORNING_TOTAL)
        assert live_at.spend_budget_id is not None
        live_status, live_body = invoke_http(
            create_http_app(live_at_ledger),
            "GET",
            _approaching_signal_path(live_at.spend_budget_id),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(live_status, 200)
        self.assertEqual(live_body["approaching_signal"]["utilization_status"], UTILIZATION_AT)
        self.assertEqual(live_body["webhook_outbox_events"], [])
        self.assertEqual(len(_approaching_events(live_at_ledger)), 0)
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
        self.assertNotIn("approaching_signal", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            _approaching_signal_path(uuid4()),
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
            _approaching_signal_path(forbidden_row.spend_budget_id),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(forbidden_status, 403)
        self.assertEqual(forbidden_body["rejection_reason_code"], "billing_account_forbidden")
        with mock.patch(
            "metering_billing.http_app.SpendBudgetApproachingSignalPresentmentService.present_spend_budget_approaching_signal",
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
            "metering_billing.http_app.SpendBudgetApproachingSignalPresentmentService.present_spend_budget_approaching_signal",
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
            "metering_billing.http_app.SpendBudgetApproachingSignalPresentmentService.present_spend_budget_approaching_signal",
            side_effect=SpendBudgetApproachingSignalPresentmentQueryError(
                "billing_account_not_found"
            ),
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
            "metering_billing.http_app.SpendBudgetApproachingSignalPresentmentService.present_spend_budget_approaching_signal",
            side_effect=SpendBudgetApproachingSignalPresentmentQueryError("request_invalid"),
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
