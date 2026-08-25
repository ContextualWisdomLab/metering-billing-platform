"""Spend-budget approaching-signal write tests for the commercial webhook outbox."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    MemoryUsageLedger,
    SpendBudgetEvaluationPresentmentService,
    SpendBudgetApproachingSignalService,
    WebhookDeliveryService,
    WebhookSubscriptionService,
    create_http_app,
    format_exact_decimal,
    validate_spend_budget_evaluation_presentment,
    validate_spend_budget_approaching_signal,
)
from metering_billing.errors import (
    ExactDecimalError,
    SpendBudgetEvaluationPresentmentQueryError,
    SpendBudgetApproachingSignalOutcomeCode,
    SpendBudgetRejectionReasonCode,
    WebhookSubscriptionOutcomeCode,
)
from metering_billing.spend_budget import SPEND_BUDGET_CONTRACT_VERSION, compute_spend_budget_payload_hash
from metering_billing.spend_budget_evaluation_presentment import (
    SpendBudgetEvaluationPresentmentResult,
    UTILIZATION_AT,
    UTILIZATION_OVER,
    UTILIZATION_UNDER,
)
from metering_billing.spend_budget_approaching_signal import (
    SPEND_BUDGET_APPROACHING_SIGNAL_CONTRACT_VERSION,
    SpendBudgetApproachingSignalResult,
    _enqueue_spend_budget_approaching,
)
from metering_billing.usage_ledger import StoredSpendBudget, generate_record_id
from metering_billing.webhook_outbox import (
    EVENT_TYPE_SPEND_BUDGET_APPROACHING,
    EVENT_TYPE_SPEND_BUDGET_OVER,
    EVENT_TYPE_SPEND_BUDGET_PUBLISHED,
    WEBHOOK_SIGNATURE_HEADER,
    sign_webhook_body,
)
from test_http_app import invoke_http
from test_spend_budget import publish_known_budget
from test_spend_budget_evaluation_presentment import OVER_BUDGET_AMOUNT, _publish_on_rated_ledger
from test_usage_ingestion import ACCOUNT_TWO, TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL, MORNING_WINDOW, seed_rated_ledger
from test_webhook_outbox import ISSUED_AT, _start_recorder


AS_OF = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
WINDOW_STARTED = "2026-08-16T10:00:00Z"
WINDOW_ENDED = "2026-08-16T11:00:00Z"


def _approaching_signal_path(spend_budget_id) -> str:
    """Return the nested approaching-signal write path for one spend budget."""
    return f"/v1/spend-budgets/{spend_budget_id}/approaching-signal"


def _approaching_events(ledger: MemoryUsageLedger):
    """Return spend_budget.approaching outbox rows from one ledger."""
    return [
        event
        for event in ledger.webhook_outbox_events.values()
        if event.event_type_code == EVENT_TYPE_SPEND_BUDGET_APPROACHING
    ]


class SpendBudgetApproachingSignalTests(unittest.TestCase):
    """Verify first-at observation enqueues once and under/over write nothing."""

    def test_at_enqueues_once_and_replay_is_duplicate(self) -> None:
        """First at observation enqueues one row; the same at replays."""
        ledger, account_id, accepted, rated = _publish_on_rated_ledger(KNOWN_MORNING_TOTAL)
        assert accepted.spend_budget_id is not None
        prior_published = len(
            [
                event
                for event in ledger.webhook_outbox_events.values()
                if event.event_type_code == EVENT_TYPE_SPEND_BUDGET_PUBLISHED
            ]
        )
        prior_over = len(
            [
                event
                for event in ledger.webhook_outbox_events.values()
                if event.event_type_code == EVENT_TYPE_SPEND_BUDGET_OVER
            ]
        )
        prior_journals = len(ledger.journal_proposals)
        evaluation = SpendBudgetEvaluationPresentmentService(ledger).present_spend_budget_evaluation(
            TENANT_ONE, accepted.spend_budget_id
        )
        self.assertEqual(evaluation.utilization_status, UTILIZATION_AT)
        self.assertEqual(evaluation.remaining_amount, Decimal("0"))
        self.assertEqual(evaluation.over_amount, Decimal("0"))
        first = SpendBudgetApproachingSignalService(
            ledger, clock=lambda: AS_OF
        ).observe_spend_budget_approaching(TENANT_ONE, accepted.spend_budget_id)
        self.assertEqual(
            first.spend_budget_approaching_signal_outcome_code,
            SpendBudgetApproachingSignalOutcomeCode.ACCEPTED,
        )
        self.assertEqual(first.spend_budget_id, accepted.spend_budget_id)
        self.assertEqual(first.billing_account_id, account_id)
        self.assertEqual(first.utilization_status, UTILIZATION_AT)
        self.assertEqual(first.remaining_amount, evaluation.remaining_amount)
        self.assertEqual(first.budget_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(first.currency_code, "USD")
        self.assertEqual(first.spend_budget_status, "published")
        self.assertEqual(first.next_operator_action, "wait")
        payload = first.as_contract_dict()
        self.assertEqual(validate_spend_budget_approaching_signal(payload), ())
        self.assertIsInstance(payload["remaining_amount"], str)
        self.assertNotIsInstance(payload["remaining_amount"], float)
        self.assertIsInstance(payload["budget_amount"], str)
        self.assertNotIn("over_amount", payload)
        self.assertNotIn("rated_amount", payload)
        self.assertNotIn("card_pan", payload)
        self.assertNotIn("retained_earnings", payload)
        self.assertNotIn("proposal_status", payload)
        approaching_events = _approaching_events(ledger)
        self.assertEqual(len(approaching_events), 1)
        self.assertEqual(approaching_events[0].source_id, accepted.spend_budget_id)
        envelope = json.loads(approaching_events[0].payload_json)
        self.assertEqual(envelope["event_type_code"], EVENT_TYPE_SPEND_BUDGET_APPROACHING)
        data = envelope["data"]
        self.assertEqual(data, first.as_webhook_event_data())
        self.assertEqual(data["spend_budget_id"], str(accepted.spend_budget_id))
        self.assertEqual(data["billing_account_id"], str(account_id))
        self.assertEqual(data["source_payload_hash"], accepted.source_payload_hash)
        self.assertEqual(data["spend_budget_contract_version"], SPEND_BUDGET_CONTRACT_VERSION)
        self.assertEqual(data["currency_code"], "USD")
        self.assertEqual(data["budget_amount"], format_exact_decimal(KNOWN_MORNING_TOTAL))
        self.assertEqual(data["remaining_amount"], format_exact_decimal(evaluation.remaining_amount))
        self.assertEqual(data["window_started_at"], WINDOW_STARTED)
        self.assertEqual(data["window_ended_at"], WINDOW_ENDED)
        self.assertEqual(data["spend_budget_status"], "published")
        self.assertEqual(data["utilization_status"], UTILIZATION_AT)
        self.assertNotIn("over_amount", data)
        self.assertNotIn("rated_amount", data)
        self.assertNotIn("next_operator_action", data)
        self.assertNotIn("card_pan", json.dumps(envelope))
        self.assertNotIn("legal_invoice_number", json.dumps(envelope))
        self.assertNotIn("webhook_secret", json.dumps(envelope))
        replay = SpendBudgetApproachingSignalService(
            ledger, clock=lambda: AS_OF
        ).observe_spend_budget_approaching(TENANT_ONE, accepted.spend_budget_id)
        self.assertEqual(
            replay.spend_budget_approaching_signal_outcome_code,
            SpendBudgetApproachingSignalOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertEqual(replay.spend_budget_id, accepted.spend_budget_id)
        self.assertEqual(validate_spend_budget_approaching_signal(replay.as_contract_dict()), ())
        self.assertEqual(len(_approaching_events(ledger)), 1)
        self.assertEqual(
            len(
                [
                    event
                    for event in ledger.webhook_outbox_events.values()
                    if event.event_type_code == EVENT_TYPE_SPEND_BUDGET_PUBLISHED
                ]
            ),
            prior_published,
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in ledger.webhook_outbox_events.values()
                    if event.event_type_code == EVENT_TYPE_SPEND_BUDGET_OVER
                ]
            ),
            prior_over,
        )
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        later_eval = SpendBudgetEvaluationPresentmentService(ledger).present_spend_budget_evaluation(
            TENANT_ONE, accepted.spend_budget_id
        )
        self.assertEqual(later_eval.as_contract_dict(), evaluation.as_contract_dict())
        self.assertEqual(validate_spend_budget_evaluation_presentment(later_eval.as_contract_dict()), ())
        self.assertEqual(rated.rated_total_amount, KNOWN_MORNING_TOTAL)

    def test_under_and_over_write_zero_approaching_signal_rows(self) -> None:
        """under and over observations stay HTTP-success writes with no approaching row."""
        under_ledger, _, under_accepted, _ = _publish_on_rated_ledger()
        assert under_accepted.spend_budget_id is not None
        under = SpendBudgetApproachingSignalService(under_ledger).observe_spend_budget_approaching(
            TENANT_ONE, under_accepted.spend_budget_id
        )
        self.assertEqual(
            under.spend_budget_approaching_signal_outcome_code,
            SpendBudgetApproachingSignalOutcomeCode.ACCEPTED,
        )
        self.assertEqual(under.utilization_status, UTILIZATION_UNDER)
        self.assertGreater(under.remaining_amount, Decimal("0"))
        self.assertEqual(validate_spend_budget_approaching_signal(under.as_contract_dict()), ())
        self.assertEqual(len(_approaching_events(under_ledger)), 0)
        over_ledger, _, over_accepted, _ = _publish_on_rated_ledger(OVER_BUDGET_AMOUNT)
        assert over_accepted.spend_budget_id is not None
        over_result = SpendBudgetApproachingSignalService(over_ledger).observe_spend_budget_approaching(
            TENANT_ONE, over_accepted.spend_budget_id
        )
        self.assertEqual(
            over_result.spend_budget_approaching_signal_outcome_code,
            SpendBudgetApproachingSignalOutcomeCode.ACCEPTED,
        )
        self.assertEqual(over_result.utilization_status, UTILIZATION_OVER)
        self.assertEqual(over_result.remaining_amount, Decimal("0"))
        self.assertEqual(len(_approaching_events(over_ledger)), 0)
        empty_ledger, _, empty_accepted = publish_known_budget()
        assert empty_accepted.spend_budget_id is not None
        empty = SpendBudgetApproachingSignalService(empty_ledger).observe_spend_budget_approaching(
            TENANT_ONE, empty_accepted.spend_budget_id
        )
        self.assertEqual(empty.utilization_status, UTILIZATION_UNDER)
        self.assertEqual(len(_approaching_events(empty_ledger)), 0)

    def test_rejected_observe_writes_zero_approaching_signal_rows(self) -> None:
        """Unknown, cross-tenant, and forbidden observes write zero approaching rows."""
        ledger, _, accepted, _ = _publish_on_rated_ledger(KNOWN_MORNING_TOTAL)
        assert accepted.spend_budget_id is not None
        service = SpendBudgetApproachingSignalService(ledger)
        missing = service.observe_spend_budget_approaching("", accepted.spend_budget_id)
        self.assertEqual(
            missing.spend_budget_approaching_signal_outcome_code,
            SpendBudgetApproachingSignalOutcomeCode.REJECTED,
        )
        self.assertEqual(missing.rejection_reason_code, SpendBudgetRejectionReasonCode.TENANT_NOT_FOUND)
        unknown_tenant = service.observe_spend_budget_approaching(
            "urn:cwl:missing", accepted.spend_budget_id
        )
        self.assertEqual(
            unknown_tenant.rejection_reason_code, SpendBudgetRejectionReasonCode.TENANT_NOT_FOUND
        )
        unknown_budget = service.observe_spend_budget_approaching(TENANT_ONE, uuid4())
        self.assertEqual(
            unknown_budget.rejection_reason_code, SpendBudgetRejectionReasonCode.SPEND_BUDGET_NOT_FOUND
        )
        ledger.register_tenant(TENANT_TWO)
        crossed = service.observe_spend_budget_approaching(TENANT_TWO, accepted.spend_budget_id)
        self.assertEqual(
            crossed.rejection_reason_code, SpendBudgetRejectionReasonCode.SPEND_BUDGET_NOT_FOUND
        )
        self.assertEqual(len(_approaching_events(ledger)), 0)
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
                budget_amount=KNOWN_MORNING_TOTAL,
                window_started_at=MORNING_WINDOW.window_started_at,
                window_ended_at=MORNING_WINDOW.window_ended_at,
                source_payload_hash=compute_spend_budget_payload_hash(
                    {
                        "billing_account_id": str(foreign.billing_account_id),
                        "currency_code": "USD",
                        "budget_amount": format_exact_decimal(KNOWN_MORNING_TOTAL),
                        "window_started_at": WINDOW_STARTED,
                        "window_ended_at": WINDOW_ENDED,
                        "spend_budget_contract_version": 1,
                    }
                ),
                published_at=AS_OF,
            )
        )
        forbidden = SpendBudgetApproachingSignalService(rated).observe_spend_budget_approaching(
            TENANT_ONE, mismatched.spend_budget_id
        )
        self.assertEqual(
            forbidden.rejection_reason_code, SpendBudgetRejectionReasonCode.BILLING_ACCOUNT_FORBIDDEN
        )
        self.assertEqual(len(_approaching_events(rated)), 0)
        missing_account = rated.insert_spend_budget(
            StoredSpendBudget(
                spend_budget_id=generate_record_id(),
                tenant_account_id=tenant_one.tenant_account_id,
                billing_account_id=uuid4(),
                spend_budget_contract_version=1,
                currency_code="USD",
                budget_amount=KNOWN_MORNING_TOTAL,
                window_started_at=MORNING_WINDOW.window_started_at,
                window_ended_at=MORNING_WINDOW.window_ended_at,
                source_payload_hash="sha256:" + "d" * 64,
                published_at=AS_OF,
            )
        )
        gone = SpendBudgetApproachingSignalService(rated).observe_spend_budget_approaching(
            TENANT_ONE, missing_account.spend_budget_id
        )
        self.assertEqual(
            gone.rejection_reason_code, SpendBudgetRejectionReasonCode.BILLING_ACCOUNT_NOT_FOUND
        )
        self.assertEqual(len(_approaching_events(rated)), 0)

    def test_crash_heal_reenqueues_the_first_approaching_row(self) -> None:
        """A crash after compute and before enqueue is healed on the next observe."""
        ledger, _, accepted, _ = _publish_on_rated_ledger(KNOWN_MORNING_TOTAL)
        assert accepted.spend_budget_id is not None
        first = SpendBudgetApproachingSignalService(
            ledger, clock=lambda: AS_OF
        ).observe_spend_budget_approaching(TENANT_ONE, accepted.spend_budget_id)
        self.assertEqual(
            first.spend_budget_approaching_signal_outcome_code,
            SpendBudgetApproachingSignalOutcomeCode.ACCEPTED,
        )
        orphan = _approaching_events(ledger)[0]
        identity = next(
            key
            for key, stored_id in ledger.webhook_outbox_identity_index.items()
            if stored_id == orphan.outbox_event_id
        )
        del ledger.webhook_outbox_events[orphan.outbox_event_id]
        del ledger.webhook_outbox_identity_index[identity]
        healed = SpendBudgetApproachingSignalService(
            ledger, clock=lambda: AS_OF
        ).observe_spend_budget_approaching(TENANT_ONE, accepted.spend_budget_id)
        self.assertEqual(
            healed.spend_budget_approaching_signal_outcome_code,
            SpendBudgetApproachingSignalOutcomeCode.ACCEPTED,
        )
        healed_events = _approaching_events(ledger)
        self.assertEqual(len(healed_events), 1)
        self.assertEqual(healed_events[0].source_id, accepted.spend_budget_id)

    def test_first_at_wins_when_source_id_already_has_a_row(self) -> None:
        """A later at observation does not enqueue a second source_id row."""
        ledger, _, accepted, _ = _publish_on_rated_ledger(KNOWN_MORNING_TOTAL)
        assert accepted.spend_budget_id is not None
        first = SpendBudgetApproachingSignalService(
            ledger, clock=lambda: AS_OF
        ).observe_spend_budget_approaching(TENANT_ONE, accepted.spend_budget_id)
        existing = _approaching_events(ledger)[0]
        later = SpendBudgetApproachingSignalService(
            ledger, clock=lambda: datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
        ).observe_spend_budget_approaching(TENANT_ONE, accepted.spend_budget_id)
        self.assertEqual(
            later.spend_budget_approaching_signal_outcome_code,
            SpendBudgetApproachingSignalOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertEqual(later.spend_budget_id, first.spend_budget_id)
        self.assertEqual(len(_approaching_events(ledger)), 1)
        self.assertEqual(_approaching_events(ledger)[0].outbox_event_id, existing.outbox_event_id)

    def test_enqueues_and_delivers_signed_approaching_callback(self) -> None:
        """Existing subscriptions opt in and receive HMAC-signed spend_budget.approaching."""
        ledger, _, accepted, _ = _publish_on_rated_ledger(KNOWN_MORNING_TOTAL)
        assert accepted.spend_budget_id is not None
        server, callback_url, received = _start_recorder()
        try:
            subscriptions = WebhookSubscriptionService(ledger, clock=lambda: ISSUED_AT)
            registered = subscriptions.register_subscription(
                TENANT_ONE, callback_url, (EVENT_TYPE_SPEND_BUDGET_APPROACHING,)
            )
            self.assertEqual(
                registered.webhook_subscription_outcome_code,
                WebhookSubscriptionOutcomeCode.ACCEPTED,
            )
            self.assertEqual(registered.event_type_codes, (EVENT_TYPE_SPEND_BUDGET_APPROACHING,))
            SpendBudgetApproachingSignalService(
                ledger, clock=lambda: AS_OF
            ).observe_spend_budget_approaching(TENANT_ONE, accepted.spend_budget_id)
            delivered = WebhookDeliveryService(ledger).deliver_due_events(TENANT_ONE)
            self.assertEqual(delivered.delivered_event_count, 1)
            self.assertEqual(len(received), 1)
            headers, raw_body = received[0]
            posted = json.loads(raw_body.decode("utf-8"))
            self.assertEqual(posted["event_type_code"], EVENT_TYPE_SPEND_BUDGET_APPROACHING)
            expected = sign_webhook_body(registered.webhook_secret or "", raw_body)
            signature = next(
                value
                for key, value in headers.items()
                if key.lower() == WEBHOOK_SIGNATURE_HEADER.lower()
            )
            self.assertEqual(signature, expected)
        finally:
            server.shutdown()
            server.server_close()

    def test_http_post_is_200_and_pins_the_tenant_header(self) -> None:
        """POST observes same-tenant budgets; missing pin is 422; leaks stay closed."""
        ledger, account_id, accepted, _ = _publish_on_rated_ledger(KNOWN_MORNING_TOTAL)
        assert accepted.spend_budget_id is not None
        app = create_http_app(ledger, clock=lambda: AS_OF)
        path = _approaching_signal_path(accepted.spend_budget_id)
        status, body = invoke_http(
            app,
            "POST",
            path,
            {"tenant_reference": TENANT_ONE},
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["spend_budget_approaching_signal_outcome_code"], "accepted")
        self.assertEqual(body["spend_budget_id"], str(accepted.spend_budget_id))
        self.assertEqual(body["billing_account_id"], str(account_id))
        self.assertEqual(body["utilization_status"], UTILIZATION_AT)
        self.assertEqual(body["remaining_amount"], format_exact_decimal(Decimal("0")))
        self.assertEqual(validate_spend_budget_approaching_signal(body), ())
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            path,
            {"tenant_reference": TENANT_ONE},
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["spend_budget_approaching_signal_outcome_code"], "duplicate_replay")
        self.assertEqual(len(_approaching_events(ledger)), 1)
        under_ledger, _, under_accepted, _ = _publish_on_rated_ledger()
        assert under_accepted.spend_budget_id is not None
        under_status, under_body = invoke_http(
            create_http_app(under_ledger),
            "POST",
            _approaching_signal_path(under_accepted.spend_budget_id),
            {"tenant_reference": TENANT_ONE},
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(under_status, 200)
        self.assertEqual(under_body["utilization_status"], UTILIZATION_UNDER)
        self.assertEqual(len(_approaching_events(under_ledger)), 0)
        eval_status, eval_body = invoke_http(
            app,
            "GET",
            f"/v1/spend-budgets/{accepted.spend_budget_id}/evaluation",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(eval_status, 200)
        self.assertEqual(eval_body["utilization_status"], UTILIZATION_AT)
        over_get_status, over_get_body = invoke_http(
            app,
            "GET",
            f"/v1/spend-budgets/{accepted.spend_budget_id}/over-signal",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(over_get_status, 200)
        self.assertEqual(over_get_body["over_signal"]["utilization_status"], UTILIZATION_AT)
        self.assertEqual(over_get_body["webhook_outbox_events"], [])
        missing_status, missing_body = invoke_http(app, "POST", path, {})
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        mismatch_status, mismatch_body = invoke_http(
            app,
            "POST",
            path,
            {"tenant_reference": TENANT_TWO},
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(mismatch_status, 422)
        self.assertEqual(mismatch_body["rejection_reason_code"], "request_invalid")
        ledger.register_tenant(TENANT_TWO)
        other_status, other_body = invoke_http(
            app,
            "POST",
            path,
            {"tenant_reference": TENANT_TWO},
            headers={"X-CWL-Tenant-Reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "spend_budget_not_found")
        self.assertNotIn("remaining_amount", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "POST",
            _approaching_signal_path(uuid4()),
            {"tenant_reference": TENANT_ONE},
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "spend_budget_not_found")
        method_status, method_body = invoke_http(
            app,
            "PUT",
            path,
            {"tenant_reference": TENANT_ONE},
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        get_status, get_body = invoke_http(
            app,
            "GET",
            path,
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["approaching_signal"]["utilization_status"], UTILIZATION_AT)
        self.assertEqual(len(get_body["webhook_outbox_events"]), 1)
        self.assertEqual(len(_approaching_events(ledger)), 1)
        pan_status, pan_body = invoke_http(
            app,
            "POST",
            path,
            {"tenant_reference": TENANT_ONE, "card_pan": "4111111111111111"},
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(pan_status, 422)
        self.assertEqual(pan_body["rejection_reason_code"], "request_invalid")
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
                budget_amount=KNOWN_MORNING_TOTAL,
                window_started_at=MORNING_WINDOW.window_started_at,
                window_ended_at=MORNING_WINDOW.window_ended_at,
                source_payload_hash="sha256:" + "b" * 64,
                published_at=AS_OF,
            )
        )
        forbidden_status, forbidden_body = invoke_http(
            create_http_app(ledger),
            "POST",
            _approaching_signal_path(forbidden_row.spend_budget_id),
            {"tenant_reference": TENANT_ONE},
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(forbidden_status, 403)
        self.assertEqual(forbidden_body["rejection_reason_code"], "billing_account_forbidden")
        with mock.patch(
            "metering_billing.http_app.SpendBudgetApproachingSignalService.observe_spend_budget_approaching",
            side_effect=ExactDecimalError("budget amount must be an exact decimal"),
        ):
            float_status, float_body = invoke_http(
                create_http_app(ledger, clock=lambda: AS_OF),
                "POST",
                path,
                {"tenant_reference": TENANT_ONE},
                headers={"X-CWL-Tenant-Reference": TENANT_ONE},
            )
        self.assertEqual(float_status, 422)
        self.assertEqual(float_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.SpendBudgetApproachingSignalService.observe_spend_budget_approaching",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger, clock=lambda: AS_OF),
                "POST",
                path,
                {"tenant_reference": TENANT_ONE},
                headers={"X-CWL-Tenant-Reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        rejected = SpendBudgetApproachingSignalResult(
            spend_budget_approaching_signal_outcome_code=SpendBudgetApproachingSignalOutcomeCode.REJECTED,
            spend_budget_approaching_signal_contract_version=SPEND_BUDGET_APPROACHING_SIGNAL_CONTRACT_VERSION,
            spend_budget_id=None,
            tenant_reference=None,
            billing_account_id=None,
            currency_code=None,
            budget_amount=None,
            remaining_amount=None,
            utilization_status=None,
            window_started_at=None,
            window_ended_at=None,
            spend_budget_status=None,
            source_payload_hash=None,
            spend_budget_contract_version=SPEND_BUDGET_CONTRACT_VERSION,
            next_operator_action="wait",
            rejection_reason_code=SpendBudgetRejectionReasonCode.BILLING_ACCOUNT_NOT_FOUND,
        )
        with mock.patch(
            "metering_billing.http_app.SpendBudgetApproachingSignalService.observe_spend_budget_approaching",
            return_value=rejected,
        ):
            gone_status, gone_body = invoke_http(
                create_http_app(ledger, clock=lambda: AS_OF),
                "POST",
                path,
                {"tenant_reference": TENANT_ONE},
                headers={"X-CWL-Tenant-Reference": TENANT_ONE},
            )
        self.assertEqual(gone_status, 404)
        self.assertEqual(gone_body["rejection_reason_code"], "billing_account_not_found")
        invalid = replace(rejected, rejection_reason_code=SpendBudgetRejectionReasonCode.REQUEST_INVALID)
        with mock.patch(
            "metering_billing.http_app.SpendBudgetApproachingSignalService.observe_spend_budget_approaching",
            return_value=invalid,
        ):
            invalid_status, invalid_body = invoke_http(
                create_http_app(ledger, clock=lambda: AS_OF),
                "POST",
                path,
                {"tenant_reference": TENANT_ONE},
                headers={"X-CWL-Tenant-Reference": TENANT_ONE},
            )
        self.assertEqual(invalid_status, 422)
        self.assertEqual(invalid_body["rejection_reason_code"], "request_invalid")
        hollow = replace(rejected, rejection_reason_code=None)
        with mock.patch(
            "metering_billing.http_app.SpendBudgetApproachingSignalService.observe_spend_budget_approaching",
            return_value=hollow,
        ):
            hollow_status, hollow_body = invoke_http(
                create_http_app(ledger, clock=lambda: AS_OF),
                "POST",
                path,
                {"tenant_reference": TENANT_ONE},
                headers={"X-CWL-Tenant-Reference": TENANT_ONE},
            )
        self.assertEqual(hollow_status, 404)
        self.assertEqual(hollow_body["rejection_reason_code"], "spend_budget_not_found")

    def test_result_contract_and_enqueue_helpers_fail_closed(self) -> None:
        """Sparse rejected results and incomplete accepted rows fail closed."""
        ledger, account_id, accepted, _ = _publish_on_rated_ledger(KNOWN_MORNING_TOTAL)
        assert accepted.spend_budget_id is not None
        observed = SpendBudgetApproachingSignalService(
            ledger, clock=lambda: AS_OF
        ).observe_spend_budget_approaching(TENANT_ONE, accepted.spend_budget_id)
        unsupported = replace(observed, spend_budget_approaching_signal_outcome_code="posted")
        with self.assertRaises(ValueError):
            unsupported.as_contract_dict()
        accepted_without_id = replace(observed, spend_budget_id=None)
        with self.assertRaises(ValueError):
            accepted_without_id.as_contract_dict()
        accepted_without_tenant = replace(observed, tenant_reference=None)
        with self.assertRaises(ValueError):
            accepted_without_tenant.as_contract_dict()
        accepted_without_account = replace(observed, billing_account_id=None)
        with self.assertRaises(ValueError):
            accepted_without_account.as_contract_dict()
        accepted_without_currency = replace(observed, currency_code=None)
        with self.assertRaises(ValueError):
            accepted_without_currency.as_contract_dict()
        accepted_without_amount = replace(observed, budget_amount=None)
        with self.assertRaises(ValueError):
            accepted_without_amount.as_contract_dict()
        accepted_without_remaining = replace(observed, remaining_amount=None)
        with self.assertRaises(ValueError):
            accepted_without_remaining.as_contract_dict()
        accepted_without_window = replace(observed, window_started_at=None)
        with self.assertRaises(ValueError):
            accepted_without_window.as_contract_dict()
        accepted_without_end = replace(observed, window_ended_at=None)
        with self.assertRaises(ValueError):
            accepted_without_end.as_contract_dict()
        none_reason = SpendBudgetApproachingSignalResult(
            spend_budget_approaching_signal_outcome_code=SpendBudgetApproachingSignalOutcomeCode.REJECTED,
            spend_budget_approaching_signal_contract_version=SPEND_BUDGET_APPROACHING_SIGNAL_CONTRACT_VERSION,
            spend_budget_id=None,
            tenant_reference=None,
            billing_account_id=None,
            currency_code=None,
            budget_amount=None,
            remaining_amount=None,
            utilization_status=None,
            window_started_at=None,
            window_ended_at=None,
            spend_budget_status=None,
            source_payload_hash=None,
            spend_budget_contract_version=SPEND_BUDGET_CONTRACT_VERSION,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        rejected_payload = none_reason.as_contract_dict()
        self.assertEqual(rejected_payload["spend_budget_approaching_signal_outcome_code"], "rejected")
        self.assertEqual(rejected_payload["rejection_reason_code"], "spend_budget_not_found")
        self.assertEqual(none_reason.rejection_reason_text(), "spend_budget_not_found")
        self.assertEqual(
            replace(
                none_reason,
                rejection_reason_code=SpendBudgetRejectionReasonCode.REQUEST_INVALID,
            ).rejection_reason_text(),
            "request_invalid",
        )
        with self.assertRaisesRegex(ValueError, "rejected spend budget approaching signal has no webhook"):
            none_reason.as_webhook_event_data()
        missing_account = replace(observed, billing_account_id=None)
        with self.assertRaisesRegex(ValueError, "rejected spend budget approaching signal has no webhook"):
            missing_account.as_webhook_event_data()
        under_result = replace(observed, utilization_status=UTILIZATION_UNDER)
        with self.assertRaisesRegex(ValueError, "only at observations have webhook event data"):
            under_result.as_webhook_event_data()
        missing_remaining = replace(observed, remaining_amount=None)
        with self.assertRaisesRegex(ValueError, "accepted approaching signals must include remaining_amount"):
            missing_remaining.as_webhook_event_data()
        with self.assertRaisesRegex(ValueError, "accepted approaching signals must include identity"):
            _enqueue_spend_budget_approaching(ledger, TENANT_ONE, accepted_without_id, AS_OF)
        with self.assertRaisesRegex(ValueError, "accepted approaching signals must include identity"):
            _enqueue_spend_budget_approaching(
                ledger, TENANT_ONE, replace(observed, remaining_amount=None), AS_OF
            )
        empty = SpendBudgetApproachingSignalService()
        hollow = empty.observe_spend_budget_approaching(TENANT_ONE, uuid4())
        self.assertEqual(hollow.rejection_reason_code, SpendBudgetRejectionReasonCode.TENANT_NOT_FOUND)
        with mock.patch.object(
            SpendBudgetEvaluationPresentmentService,
            "present_spend_budget_evaluation",
            side_effect=SpendBudgetEvaluationPresentmentQueryError("stale_reason"),
        ):
            invalid = SpendBudgetApproachingSignalService(ledger).observe_spend_budget_approaching(
                TENANT_ONE, accepted.spend_budget_id
            )
        self.assertEqual(invalid.rejection_reason_code, SpendBudgetRejectionReasonCode.REQUEST_INVALID)
        unexpected = SpendBudgetEvaluationPresentmentResult(
            spend_budget_id=accepted.spend_budget_id,
            tenant_reference=TENANT_ONE,
            billing_account_id=account_id,
            currency_code="USD",
            budget_amount=KNOWN_MORNING_TOTAL,
            rated_amount=KNOWN_MORNING_TOTAL,
            remaining_amount=Decimal("0"),
            over_amount=Decimal("0"),
            utilization_status="posted",
            window_started_at=MORNING_WINDOW.window_started_at,
            window_ended_at=MORNING_WINDOW.window_ended_at,
            spend_budget_status="published",
            next_operator_action="wait",
        )
        with mock.patch.object(
            SpendBudgetEvaluationPresentmentService,
            "present_spend_budget_evaluation",
            return_value=unexpected,
        ):
            with self.assertRaisesRegex(ValueError, "unsupported utilization"):
                SpendBudgetApproachingSignalService(ledger).observe_spend_budget_approaching(
                    TENANT_ONE, accepted.spend_budget_id
                )
        real_get = ledger.get_spend_budget

        def _missing_after_eval(spend_budget_id):
            if getattr(_missing_after_eval, "seen", False):
                return None
            _missing_after_eval.seen = True
            return real_get(spend_budget_id)

        with mock.patch.object(ledger, "get_spend_budget", side_effect=_missing_after_eval):
            with self.assertRaisesRegex(ValueError, "accepted approaching signals require a stored budget"):
                SpendBudgetApproachingSignalService(ledger).observe_spend_budget_approaching(
                    TENANT_ONE, accepted.spend_budget_id
                )
        with mock.patch.object(
            SpendBudgetEvaluationPresentmentService,
            "present_spend_budget_evaluation",
            side_effect=SpendBudgetEvaluationPresentmentQueryError("tax_exempt"),
        ):
            unknown_reason = SpendBudgetApproachingSignalService(ledger).observe_spend_budget_approaching(
                TENANT_ONE, accepted.spend_budget_id
            )
        self.assertEqual(
            unknown_reason.rejection_reason_code, SpendBudgetRejectionReasonCode.REQUEST_INVALID
        )
        under_ledger, _, under_accepted, _ = _publish_on_rated_ledger()
        assert under_accepted.spend_budget_id is not None
        under_get = under_ledger.get_spend_budget

        def _missing_under_hash(spend_budget_id):
            if getattr(_missing_under_hash, "seen", False):
                return None
            _missing_under_hash.seen = True
            return under_get(spend_budget_id)

        with mock.patch.object(under_ledger, "get_spend_budget", side_effect=_missing_under_hash):
            with self.assertRaisesRegex(ValueError, "accepted approaching signals require a stored budget"):
                SpendBudgetApproachingSignalService(under_ledger).observe_spend_budget_approaching(
                    TENANT_ONE, under_accepted.spend_budget_id
                )
        self.assertEqual(account_id, accepted.billing_account_id)
        default_clock = SpendBudgetApproachingSignalService(ledger)
        self.assertIsNotNone(default_clock._clock())
