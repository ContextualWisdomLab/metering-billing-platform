"""Spend-budget write and presentment tests for one billing-account window."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    MemoryUsageLedger,
    RatedSpendPresentmentService,
    SpendBudgetPresentmentService,
    SpendBudgetService,
    create_http_app,
    format_exact_decimal,
    validate_spend_budget,
    validate_spend_budget_presentment,
)
from metering_billing.errors import (
    ExactDecimalError,
    SpendBudgetOutcomeCode,
    SpendBudgetPresentmentQueryError,
    SpendBudgetQueryError,
    SpendBudgetRejectionReasonCode,
)
from metering_billing.spend_budget import (
    SPEND_BUDGET_CONTRACT_VERSION,
    SpendBudgetResult,
    compute_spend_budget_payload_hash,
    parse_budget_amount,
)
from metering_billing.spend_budget_presentment import next_operator_action
from metering_billing.time_window import TimeWindow
from metering_billing.usage_ledger import StoredSpendBudget, generate_record_id
from test_http_app import invoke_http
from test_usage_ingestion import ACCOUNT_ONE, TENANT_ONE, TENANT_TWO
from test_usage_rating import MORNING_WINDOW, seed_rated_ledger


AS_OF = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
BUDGET_AMOUNT = Decimal("100.00")
LATER_AMOUNT = Decimal("250.00")
WINDOW_STARTED = "2026-08-16T10:00:00Z"
WINDOW_ENDED = "2026-08-16T11:00:00Z"


def seed_account_ledger() -> tuple[MemoryUsageLedger, object]:
    """Register one tenant billing account with no rated spend."""
    ledger = MemoryUsageLedger()
    ledger.register_tenant(TENANT_ONE)
    account = ledger.register_billing_account(TENANT_ONE, ACCOUNT_ONE)
    return ledger, account.billing_account_id


def publish_known_budget(
    ledger: MemoryUsageLedger | None = None,
    billing_account_id=None,
    amount: Decimal = BUDGET_AMOUNT,
    currency_code: str = "USD",
    time_window: TimeWindow = MORNING_WINDOW,
    clock=None,
    source_payload_hash: str | None = None,
):
    """Publish one known-morning commercial spend budget."""
    if ledger is None:
        ledger, billing_account_id = seed_account_ledger()
    service = SpendBudgetService(ledger, clock=clock or (lambda: AS_OF))
    result = service.publish_spend_budget(
        TENANT_ONE,
        billing_account_id,
        currency_code,
        amount,
        time_window,
        source_payload_hash=source_payload_hash,
    )
    return ledger, billing_account_id, result


class SpendBudgetTests(unittest.TestCase):
    """Verify commercial spend budgets stay exact, append-only, and tenant-scoped."""

    def test_publish_morning_usd_budget_replays_without_growing_the_store(self) -> None:
        """A known morning USD budget persists once and replays the same id."""
        ledger, billing_account_id, accepted = publish_known_budget()
        self.assertEqual(accepted.spend_budget_outcome_code, SpendBudgetOutcomeCode.ACCEPTED)
        self.assertIsNotNone(accepted.spend_budget_id)
        self.assertEqual(accepted.tenant_reference, TENANT_ONE)
        self.assertEqual(accepted.billing_account_id, billing_account_id)
        self.assertEqual(accepted.currency_code, "USD")
        self.assertEqual(accepted.budget_amount, BUDGET_AMOUNT)
        self.assertEqual(accepted.spend_budget_status, "published")
        self.assertEqual(accepted.next_operator_action, "wait")
        self.assertEqual(accepted.published_at, AS_OF)
        self.assertEqual(accepted.window_started_at, MORNING_WINDOW.window_started_at)
        self.assertEqual(accepted.window_ended_at, MORNING_WINDOW.window_ended_at)
        payload = accepted.as_contract_dict()
        self.assertEqual(validate_spend_budget(payload), ())
        self.assertIsInstance(payload["budget_amount"], str)
        self.assertNotIsInstance(payload["budget_amount"], float)
        self.assertNotIn("items", payload)
        self.assertNotIn("cursor", payload)
        self.assertNotIn("proposal_status", payload)
        self.assertNotIn("retained_earnings", payload)
        self.assertNotIn("group_by", payload)
        self.assertEqual(len(ledger.spend_budgets), 1)
        replay = SpendBudgetService(ledger, clock=lambda: AS_OF).publish_spend_budget(
            TENANT_ONE, billing_account_id, "USD", BUDGET_AMOUNT, MORNING_WINDOW
        )
        self.assertEqual(replay.spend_budget_outcome_code, SpendBudgetOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(replay.spend_budget_id, accepted.spend_budget_id)
        self.assertEqual(len(ledger.spend_budgets), 1)
        self.assertEqual(validate_spend_budget(replay.as_contract_dict()), ())
        self.assertEqual(len(ledger.rating_runs), 0)
        self.assertEqual(len(ledger.invoice_drafts), 0)
        self.assertEqual(len(ledger.journal_proposals), 0)
        self.assertEqual(len(ledger.webhook_outbox_events), 0)

    def test_distinct_amount_or_currency_appends_a_new_row(self) -> None:
        """A later distinct amount or currency is a new append-only row."""
        ledger, billing_account_id, first = publish_known_budget()
        later = SpendBudgetService(
            ledger, clock=lambda: datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
        ).publish_spend_budget(
            TENANT_ONE, billing_account_id, "USD", LATER_AMOUNT, MORNING_WINDOW
        )
        self.assertEqual(later.spend_budget_outcome_code, SpendBudgetOutcomeCode.ACCEPTED)
        self.assertNotEqual(later.spend_budget_id, first.spend_budget_id)
        self.assertEqual(later.budget_amount, LATER_AMOUNT)
        krw = SpendBudgetService(
            ledger, clock=lambda: datetime(2026, 8, 18, 17, 0, tzinfo=UTC)
        ).publish_spend_budget(
            TENANT_ONE, billing_account_id, "KRW", Decimal("1000"), MORNING_WINDOW
        )
        self.assertEqual(krw.spend_budget_outcome_code, SpendBudgetOutcomeCode.ACCEPTED)
        self.assertEqual(len(ledger.spend_budgets), 3)
        matching_hash = compute_spend_budget_payload_hash(
            {
                "billing_account_id": str(billing_account_id),
                "currency_code": "USD",
                "budget_amount": format_exact_decimal(BUDGET_AMOUNT),
                "window_started_at": WINDOW_STARTED,
                "window_ended_at": WINDOW_ENDED,
                "spend_budget_contract_version": SPEND_BUDGET_CONTRACT_VERSION,
            }
        )
        hashed = SpendBudgetService(ledger, clock=lambda: AS_OF).publish_spend_budget(
            TENANT_ONE,
            billing_account_id,
            "USD",
            BUDGET_AMOUNT,
            MORNING_WINDOW,
            source_payload_hash=matching_hash,
        )
        self.assertEqual(hashed.spend_budget_outcome_code, SpendBudgetOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(hashed.spend_budget_id, first.spend_budget_id)
        mismatched = SpendBudgetService(ledger).publish_spend_budget(
            TENANT_ONE,
            billing_account_id,
            "USD",
            BUDGET_AMOUNT,
            MORNING_WINDOW,
            source_payload_hash="sha256:" + "a" * 64,
        )
        self.assertEqual(mismatched.spend_budget_outcome_code, SpendBudgetOutcomeCode.REJECTED)
        self.assertEqual(
            mismatched.rejection_reason_code, SpendBudgetRejectionReasonCode.REQUEST_INVALID
        )

    def test_fail_closed_tenant_account_amount_window_and_currency(self) -> None:
        """Missing tenant, unknown account, IEEE money, and illegal currency fail closed."""
        ledger, billing_account_id = seed_account_ledger()
        service = SpendBudgetService(ledger, clock=lambda: AS_OF)
        missing_tenant = service.publish_spend_budget(
            "", billing_account_id, "USD", BUDGET_AMOUNT, MORNING_WINDOW
        )
        self.assertEqual(missing_tenant.spend_budget_outcome_code, SpendBudgetOutcomeCode.REJECTED)
        self.assertEqual(
            missing_tenant.rejection_reason_code, SpendBudgetRejectionReasonCode.TENANT_NOT_FOUND
        )
        unknown_account = service.publish_spend_budget(
            TENANT_ONE, uuid4(), "USD", BUDGET_AMOUNT, MORNING_WINDOW
        )
        self.assertEqual(
            unknown_account.rejection_reason_code,
            SpendBudgetRejectionReasonCode.BILLING_ACCOUNT_NOT_FOUND,
        )
        ledger.register_tenant(TENANT_TWO)
        other_account = ledger.register_billing_account(
            TENANT_TWO, "urn:cwl:tenant_002:billing_account:019d7002"
        )
        forbidden = service.publish_spend_budget(
            TENANT_TWO, billing_account_id, "USD", BUDGET_AMOUNT, MORNING_WINDOW
        )
        self.assertEqual(
            forbidden.rejection_reason_code,
            SpendBudgetRejectionReasonCode.BILLING_ACCOUNT_FORBIDDEN,
        )
        self.assertNotEqual(
            other_account.tenant_account_id, ledger.require_tenant(TENANT_ONE).tenant_account_id
        )
        floated = service.publish_spend_budget(
            TENANT_ONE, billing_account_id, "USD", 100.00, MORNING_WINDOW
        )
        self.assertEqual(
            floated.rejection_reason_code, SpendBudgetRejectionReasonCode.BUDGET_AMOUNT_INVALID
        )
        zeroed = service.publish_spend_budget(
            TENANT_ONE, billing_account_id, "USD", Decimal("0"), MORNING_WINDOW
        )
        self.assertEqual(
            zeroed.rejection_reason_code, SpendBudgetRejectionReasonCode.BUDGET_AMOUNT_INVALID
        )
        negative = service.publish_spend_budget(
            TENANT_ONE, billing_account_id, "USD", "-1", MORNING_WINDOW
        )
        self.assertEqual(
            negative.rejection_reason_code, SpendBudgetRejectionReasonCode.BUDGET_AMOUNT_INVALID
        )
        bad_currency = service.publish_spend_budget(
            TENANT_ONE, billing_account_id, "usd", BUDGET_AMOUNT, MORNING_WINDOW
        )
        self.assertEqual(
            bad_currency.rejection_reason_code, SpendBudgetRejectionReasonCode.CURRENCY_INVALID
        )
        unknown_tenant = SpendBudgetService(ledger).publish_spend_budget(
            "urn:cwl:missing", billing_account_id, "USD", BUDGET_AMOUNT, MORNING_WINDOW
        )
        self.assertEqual(
            unknown_tenant.rejection_reason_code, SpendBudgetRejectionReasonCode.TENANT_NOT_FOUND
        )
        with self.assertRaises(ExactDecimalError):
            parse_budget_amount(True)
        with self.assertRaises(ExactDecimalError):
            parse_budget_amount(1.25)
        self.assertEqual(parse_budget_amount(BUDGET_AMOUNT), BUDGET_AMOUNT)
        self.assertEqual(parse_budget_amount("100.00"), BUDGET_AMOUNT)
        self.assertEqual(len(ledger.spend_budgets), 0)

    def test_http_post_get_list_and_refuses_secrets(self) -> None:
        """POST publishes; GET presents; PAN and secrets are refused."""
        ledger, billing_account_id = seed_account_ledger()
        app = create_http_app(ledger)
        nested_path = f"/v1/billing-accounts/{billing_account_id}/spend-budgets"
        status, body = invoke_http(
            app,
            "POST",
            nested_path,
            {
                "tenant_reference": TENANT_ONE,
                "currency_code": "USD",
                "budget_amount": format_exact_decimal(BUDGET_AMOUNT),
                "window_started_at": WINDOW_STARTED,
                "window_ended_at": WINDOW_ENDED,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["spend_budget_outcome_code"], "accepted")
        self.assertEqual(body["spend_budget_status"], "published")
        self.assertEqual(body["budget_amount"], "100.00")
        self.assertEqual(body["next_operator_action"], "wait")
        self.assertEqual(validate_spend_budget(body), ())
        spend_budget_id = body["spend_budget_id"]
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            nested_path,
            {
                "currency_code": "USD",
                "budget_amount": "100.00",
                "window_started_at": WINDOW_STARTED,
                "window_ended_at": WINDOW_ENDED,
            },
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["spend_budget_outcome_code"], "duplicate_replay")
        self.assertEqual(replay_body["spend_budget_id"], spend_budget_id)
        pan_status, pan_body = invoke_http(
            app,
            "POST",
            nested_path,
            {
                "tenant_reference": TENANT_ONE,
                "currency_code": "USD",
                "budget_amount": "100.00",
                "window_started_at": WINDOW_STARTED,
                "window_ended_at": WINDOW_ENDED,
                "card_pan": "4111111111111111",
            },
        )
        self.assertEqual(pan_status, 422)
        self.assertEqual(pan_body["rejection_reason_code"], "request_invalid")
        secret_status, secret_body = invoke_http(
            app,
            "POST",
            nested_path,
            {
                "tenant_reference": TENANT_ONE,
                "currency_code": "USD",
                "budget_amount": "100.00",
                "window_started_at": WINDOW_STARTED,
                "window_ended_at": WINDOW_ENDED,
                "cvc": "123",
                "provider_secret": "sk_test",
            },
        )
        self.assertEqual(secret_status, 422)
        self.assertEqual(secret_body["rejection_reason_code"], "request_invalid")
        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/spend-budgets/{spend_budget_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["spend_budget_id"], spend_budget_id)
        self.assertEqual(get_body["billing_account_id"], str(billing_account_id))
        self.assertEqual(get_body["budget_amount"], "100.00")
        self.assertEqual(get_body["spend_budget_status"], "published")
        self.assertEqual(get_body["next_operator_action"], "wait")
        self.assertNotIn("spend_budget_outcome_code", get_body)
        self.assertEqual(validate_spend_budget_presentment(get_body), ())
        later_status, later_body = invoke_http(
            app,
            "POST",
            nested_path,
            {
                "tenant_reference": TENANT_ONE,
                "currency_code": "USD",
                "budget_amount": format_exact_decimal(LATER_AMOUNT),
                "window_started_at": WINDOW_STARTED,
                "window_ended_at": WINDOW_ENDED,
            },
        )
        self.assertEqual(later_status, 200)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/spend-budgets",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"spend_budgets", "next_cursor"})
        self.assertNotIn("items", list_body)
        self.assertNotIn("cursor", list_body)
        self.assertEqual(len(list_body["spend_budgets"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        second_status, second_body = invoke_http(
            app,
            "GET",
            "/v1/spend-budgets",
            query={
                "tenant_reference": TENANT_ONE,
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second_body["spend_budgets"]), 1)
        self.assertIsNone(second_body["next_cursor"])
        missing_tenant_status, missing_tenant_body = invoke_http(app, "POST", nested_path, {
            "currency_code": "USD",
            "budget_amount": "100.00",
            "window_started_at": WINDOW_STARTED,
            "window_ended_at": WINDOW_ENDED,
        })
        self.assertEqual(missing_tenant_status, 422)
        self.assertEqual(missing_tenant_body["rejection_reason_code"], "tenant_not_found")
        unknown_status, unknown_body = invoke_http(
            app,
            "POST",
            f"/v1/billing-accounts/{uuid4()}/spend-budgets",
            {
                "tenant_reference": TENANT_ONE,
                "currency_code": "USD",
                "budget_amount": "100.00",
                "window_started_at": WINDOW_STARTED,
                "window_ended_at": WINDOW_ENDED,
            },
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "billing_account_not_found")
        ledger.register_tenant(TENANT_TWO)
        forbidden_status, forbidden_body = invoke_http(
            app,
            "POST",
            nested_path,
            {
                "tenant_reference": TENANT_TWO,
                "currency_code": "USD",
                "budget_amount": "100.00",
                "window_started_at": WINDOW_STARTED,
                "window_ended_at": WINDOW_ENDED,
            },
        )
        self.assertEqual(forbidden_status, 403)
        self.assertEqual(forbidden_body["rejection_reason_code"], "billing_account_forbidden")
        float_status, float_body = invoke_http(
            app,
            "POST",
            nested_path,
            {
                "tenant_reference": TENANT_ONE,
                "currency_code": "USD",
                "budget_amount": 100.00,
                "window_started_at": WINDOW_STARTED,
                "window_ended_at": WINDOW_ENDED,
            },
        )
        self.assertEqual(float_status, 422)
        self.assertEqual(float_body["rejection_reason_code"], "budget_amount_invalid")
        window_status, window_body = invoke_http(
            app,
            "POST",
            nested_path,
            {
                "tenant_reference": TENANT_ONE,
                "currency_code": "USD",
                "budget_amount": "100.00",
                "window_started_at": WINDOW_ENDED,
                "window_ended_at": WINDOW_STARTED,
            },
        )
        self.assertEqual(window_status, 422)
        self.assertEqual(window_body["rejection_reason_code"], "request_invalid")
        currency_status, currency_body = invoke_http(
            app,
            "POST",
            nested_path,
            {
                "tenant_reference": TENANT_ONE,
                "currency_code": "usd",
                "budget_amount": "100.00",
                "window_started_at": WINDOW_STARTED,
                "window_ended_at": WINDOW_ENDED,
            },
        )
        self.assertEqual(currency_status, 422)
        self.assertEqual(currency_body["rejection_reason_code"], "currency_invalid")
        matching_hash_status, matching_hash_body = invoke_http(
            app,
            "POST",
            nested_path,
            {
                "tenant_reference": TENANT_ONE,
                "currency_code": "USD",
                "budget_amount": "100.00",
                "window_started_at": WINDOW_STARTED,
                "window_ended_at": WINDOW_ENDED,
                "source_payload_hash": body["source_payload_hash"],
            },
        )
        self.assertEqual(matching_hash_status, 200)
        self.assertEqual(matching_hash_body["spend_budget_outcome_code"], "duplicate_replay")
        self.assertEqual(matching_hash_body["spend_budget_id"], spend_budget_id)

    def test_presentment_isolation_and_list_helpers(self) -> None:
        """GET is 200 same tenant; cross-tenant and unknown stay 404 with no leak."""
        ledger, billing_account_id, accepted = publish_known_budget()
        assert accepted.spend_budget_id is not None
        presentment = SpendBudgetPresentmentService(ledger).present_spend_budget(
            TENANT_ONE, accepted.spend_budget_id
        )
        self.assertEqual(presentment.spend_budget_id, accepted.spend_budget_id)
        self.assertEqual(presentment.billing_account_id, billing_account_id)
        self.assertEqual(presentment.budget_amount, BUDGET_AMOUNT)
        self.assertEqual(presentment.next_operator_action, "wait")
        self.assertEqual(validate_spend_budget_presentment(presentment.as_contract_dict()), ())
        app = create_http_app(ledger)
        missing_status, missing_body = invoke_http(
            app, "GET", f"/v1/spend-budgets/{accepted.spend_budget_id}"
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/spend-budgets/{accepted.spend_budget_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "spend_budget_not_found")
        self.assertNotIn("budget_amount", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/spend-budgets/{uuid4()}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "spend_budget_not_found")
        self.assertEqual(next_operator_action(), "wait")
        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/spend-budgets",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")
        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/spend-budgets",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")
        method_status, method_body = invoke_http(
            app, "PUT", f"/v1/spend-budgets/{accepted.spend_budget_id}"
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        nested_get_status, nested_get_body = invoke_http(
            app,
            "GET",
            f"/v1/billing-accounts/{billing_account_id}/spend-budgets",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(nested_get_status, 422)
        self.assertEqual(nested_get_body["rejection_reason_code"], "request_invalid")
        collection_post_status, collection_post_body = invoke_http(
            app,
            "POST",
            "/v1/spend-budgets",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(collection_post_status, 422)
        self.assertEqual(collection_post_body["rejection_reason_code"], "request_invalid")
        service = SpendBudgetPresentmentService(ledger)
        with self.assertRaises(SpendBudgetPresentmentQueryError):
            service.list_spend_budgets(TENANT_ONE, page_limit=True)
        with self.assertRaises(SpendBudgetPresentmentQueryError):
            service.list_spend_budgets(TENANT_ONE, page_limit=101)
        with self.assertRaises(SpendBudgetPresentmentQueryError):
            service.list_spend_budgets(TENANT_ONE, page_limit=1.5)
        with self.assertRaises(SpendBudgetPresentmentQueryError):
            service.list_spend_budgets(TENANT_ONE, page_limit="abc")
        self.assertEqual(len(service.list_spend_budgets(TENANT_ONE, cursor="").spend_budgets), 1)
        self.assertEqual(len(service.list_spend_budgets(TENANT_ONE, page_limit=None).spend_budgets), 1)
        self.assertEqual(len(service.list_spend_budgets(TENANT_ONE, page_limit="").spend_budgets), 1)
        self.assertIsNone(service.list_spend_budgets(TENANT_ONE, page_limit=50).next_cursor)
        with mock.patch(
            "metering_billing.http_app.SpendBudgetPresentmentService.present_spend_budget",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                f"/v1/spend-budgets/{accepted.spend_budget_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.SpendBudgetPresentmentService.list_spend_budgets",
            side_effect=ValueError("closed"),
        ):
            list_value_status, list_value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/spend-budgets",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(list_value_status, 422)
        self.assertEqual(list_value_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.SpendBudgetService.publish_spend_budget",
            side_effect=ValueError("closed"),
        ):
            write_value_status, write_value_body = invoke_http(
                create_http_app(ledger),
                "POST",
                f"/v1/billing-accounts/{billing_account_id}/spend-budgets",
                {
                    "tenant_reference": TENANT_ONE,
                    "currency_code": "USD",
                    "budget_amount": "100.00",
                    "window_started_at": WINDOW_STARTED,
                    "window_ended_at": WINDOW_ENDED,
                },
            )
        self.assertEqual(write_value_status, 422)
        self.assertEqual(write_value_body["rejection_reason_code"], "request_invalid")
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.present_spend_budget(TENANT_ONE, accepted.spend_budget_id)
        SpendBudgetService()
        SpendBudgetPresentmentService()
        empty = SpendBudgetPresentmentService()
        with self.assertRaises(SpendBudgetPresentmentQueryError):
            empty.list_spend_budgets(TENANT_ONE)
        with self.assertRaises(SpendBudgetPresentmentQueryError):
            service.present_spend_budget("", accepted.spend_budget_id)
        list_missing_status, list_missing_body = invoke_http(app, "GET", "/v1/spend-budgets")
        self.assertEqual(list_missing_status, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "tenant_not_found")

    def test_result_contract_and_ledger_identity_guards(self) -> None:
        """Contract projection and ledger identity stay closed."""
        ledger, billing_account_id, accepted = publish_known_budget()
        unsupported = replace(accepted, spend_budget_outcome_code="posted")
        with self.assertRaises(ValueError):
            unsupported.as_contract_dict()
        accepted_without_time = replace(accepted, published_at=None)
        with self.assertRaises(ValueError):
            accepted_without_time.as_contract_dict()
        none_reason = SpendBudgetResult(
            spend_budget_outcome_code=SpendBudgetOutcomeCode.REJECTED,
            spend_budget_contract_version=SPEND_BUDGET_CONTRACT_VERSION,
            spend_budget_id=None,
            tenant_reference=None,
            billing_account_id=None,
            currency_code=None,
            budget_amount=None,
            window_started_at=None,
            window_ended_at=None,
            spend_budget_status=None,
            source_payload_hash=None,
            published_at=None,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        rejected_payload = none_reason.as_contract_dict()
        self.assertEqual(rejected_payload["spend_budget_outcome_code"], "rejected")
        self.assertEqual(rejected_payload["rejection_reason_code"], "spend_budget_not_found")
        stored = next(iter(ledger.spend_budgets.values()))
        replayed = ledger.insert_spend_budget(stored)
        self.assertEqual(replayed.spend_budget_id, stored.spend_budget_id)
        identity_replay = ledger.insert_spend_budget(
            replace(stored, spend_budget_id=generate_record_id())
        )
        self.assertEqual(identity_replay.spend_budget_id, stored.spend_budget_id)
        reused = replace(
            stored,
            currency_code="EUR",
            source_payload_hash="sha256:" + "b" * 64,
        )
        with self.assertRaises(ValueError):
            ledger.insert_spend_budget(reused)
        different_id = replace(
            stored,
            spend_budget_id=generate_record_id(),
            source_payload_hash="sha256:" + "c" * 64,
            currency_code="JPY",
            budget_amount=Decimal("1"),
        )
        inserted = ledger.insert_spend_budget(different_id)
        self.assertEqual(inserted.spend_budget_id, different_id.spend_budget_id)
        with self.assertRaises(ValueError):
            ledger.insert_spend_budget(
                replace(
                    different_id,
                    spend_budget_id=generate_record_id(),
                    currency_code="CAD",
                    source_payload_hash="sha256:" + "e" * 64,
                    budget_amount=Decimal("0"),
                )
            )
        self.assertIsNone(ledger.get_spend_budget(uuid4()))
        self.assertEqual(len(ledger.list_spend_budgets()), 2)
        self.assertEqual(len(ledger.list_spend_budgets(stored.tenant_account_id)), 2)
        self.assertEqual(len(ledger.list_spend_budgets(uuid4())), 0)
        same_pk = replace(
            stored,
            spend_budget_id=different_id.spend_budget_id,
            source_payload_hash="sha256:" + "d" * 64,
        )
        with self.assertRaises(ValueError):
            ledger.insert_spend_budget(same_pk)
        found = ledger.find_spend_budget(
            stored.tenant_account_id,
            stored.billing_account_id,
            stored.window_started_at,
            stored.window_ended_at,
            stored.currency_code,
            stored.source_payload_hash,
            stored.spend_budget_contract_version,
        )
        self.assertEqual(found, stored)
        self.assertIsNone(
            ledger.find_spend_budget(
                stored.tenant_account_id,
                stored.billing_account_id,
                stored.window_started_at,
                stored.window_ended_at,
                "EUR",
                stored.source_payload_hash,
                stored.spend_budget_contract_version,
            )
        )
        with mock.patch.object(SpendBudgetService(ledger).ledger, "resolve_tenant", return_value=(None, None)):
            hollow = SpendBudgetService(ledger)
            with mock.patch.object(hollow.ledger, "resolve_tenant", return_value=(None, None)):
                with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                    hollow.publish_spend_budget(
                        TENANT_ONE, billing_account_id, "USD", BUDGET_AMOUNT, MORNING_WINDOW
                    )
        non_string_hash = SpendBudgetService(ledger).publish_spend_budget(
            TENANT_ONE,
            billing_account_id,
            "USD",
            BUDGET_AMOUNT,
            MORNING_WINDOW,
            source_payload_hash=True,  # type: ignore[arg-type]
        )
        self.assertEqual(
            non_string_hash.rejection_reason_code, SpendBudgetRejectionReasonCode.REQUEST_INVALID
        )
        with self.assertRaises(ValueError):
            ledger.insert_spend_budget(replace(stored, currency_code="usd"))
        with self.assertRaises(ValueError):
            ledger.insert_spend_budget(replace(stored, source_payload_hash="md5:abc"))
        invalid_hash = SpendBudgetService(ledger).publish_spend_budget(
            TENANT_ONE,
            billing_account_id,
            "USD",
            BUDGET_AMOUNT,
            MORNING_WINDOW,
            source_payload_hash="not-a-hash",
        )
        self.assertEqual(
            invalid_hash.rejection_reason_code, SpendBudgetRejectionReasonCode.REQUEST_INVALID
        )
        app = create_http_app(ledger)
        nested_path = f"/v1/billing-accounts/{billing_account_id}/spend-budgets"
        type_status, type_body = invoke_http(
            app,
            "POST",
            nested_path,
            {
                "tenant_reference": TENANT_ONE,
                "currency_code": 1,
                "budget_amount": "100.00",
                "window_started_at": WINDOW_STARTED,
                "window_ended_at": WINDOW_ENDED,
            },
        )
        self.assertEqual(type_status, 422)
        self.assertEqual(type_body["rejection_reason_code"], "request_invalid")
        window_type_status, window_type_body = invoke_http(
            app,
            "POST",
            nested_path,
            {
                "tenant_reference": TENANT_ONE,
                "currency_code": "USD",
                "budget_amount": "100.00",
                "window_started_at": 1,
                "window_ended_at": WINDOW_ENDED,
            },
        )
        self.assertEqual(window_type_status, 422)
        self.assertEqual(window_type_body["rejection_reason_code"], "request_invalid")
        hash_type_status, hash_type_body = invoke_http(
            app,
            "POST",
            nested_path,
            {
                "tenant_reference": TENANT_ONE,
                "currency_code": "USD",
                "budget_amount": "100.00",
                "window_started_at": WINDOW_STARTED,
                "window_ended_at": WINDOW_ENDED,
                "source_payload_hash": 1,
            },
        )
        self.assertEqual(hash_type_status, 422)
        self.assertEqual(hash_type_body["rejection_reason_code"], "request_invalid")
        query_error = SpendBudgetQueryError("spend_budget_not_found")
        self.assertEqual(query_error.rejection_reason_code, "spend_budget_not_found")
        for mutated in (
            replace(stored, tenant_account_id=uuid4()),
            replace(stored, billing_account_id=uuid4()),
            replace(
                stored,
                window_started_at=stored.window_started_at - timedelta(hours=1),
            ),
            replace(
                stored,
                window_ended_at=stored.window_ended_at + timedelta(hours=1),
            ),
            replace(stored, spend_budget_contract_version=2),
        ):
            with self.assertRaises(ValueError):
                ledger.insert_spend_budget(mutated)
        with mock.patch.object(
            SpendBudgetService,
            "publish_spend_budget",
            return_value=SpendBudgetResult(
                spend_budget_outcome_code=SpendBudgetOutcomeCode.REJECTED,
                spend_budget_contract_version=SPEND_BUDGET_CONTRACT_VERSION,
                spend_budget_id=None,
                tenant_reference=None,
                billing_account_id=None,
                currency_code=None,
                budget_amount=None,
                window_started_at=None,
                window_ended_at=None,
                spend_budget_status=None,
                source_payload_hash=None,
                published_at=None,
                next_operator_action="wait",
                rejection_reason_code=None,
            ),
        ):
            hollow_status, hollow_body = invoke_http(
                create_http_app(ledger),
                "POST",
                nested_path,
                {
                    "tenant_reference": TENANT_ONE,
                    "currency_code": "USD",
                    "budget_amount": "100.00",
                    "window_started_at": WINDOW_STARTED,
                    "window_ended_at": WINDOW_ENDED,
                },
            )
        self.assertEqual(hollow_status, 422)
        self.assertEqual(hollow_body["rejection_reason_code"], "spend_budget_not_found")

    def test_publish_does_not_change_rated_spend(self) -> None:
        """A spend-budget write leaves #77 rated-spend rows unchanged."""
        rated_ledger = seed_rated_ledger()
        account_id = rated_ledger.billing_accounts[ACCOUNT_ONE].billing_account_id
        before = RatedSpendPresentmentService(rated_ledger).present_rated_spend(
            TENANT_ONE, account_id, MORNING_WINDOW
        )
        SpendBudgetService(rated_ledger, clock=lambda: AS_OF).publish_spend_budget(
            TENANT_ONE, account_id, "USD", BUDGET_AMOUNT, MORNING_WINDOW
        )
        after = RatedSpendPresentmentService(rated_ledger).present_rated_spend(
            TENANT_ONE, account_id, MORNING_WINDOW
        )
        self.assertEqual(before.as_contract_dict(), after.as_contract_dict())
        self.assertEqual(len(rated_ledger.spend_budgets), 1)


if __name__ == "__main__":
    unittest.main()
