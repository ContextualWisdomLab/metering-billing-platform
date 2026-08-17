"""Rate-card HTTP presentment tests for tenant-scoped catalog reads."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    RateCardPresentmentService,
    RateCardService,
    create_http_app,
)
from metering_billing.contracts import validate_rate_card_presentment
from metering_billing.errors import RateCardPresentmentQueryError
from metering_billing.rate_card_presentment import next_operator_action
from metering_billing.usage_ledger import StoredRateCard, generate_record_id
from test_http_app import invoke_http
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import TOKEN_UNIT_PRICE, ingest_known_batch


def publish_named_card(ledger, rate_card_name, unit_amount, clock=None):
    """Publish one flat-price card against the existing #18 catalog write."""
    service = (
        RateCardService(ledger, clock=clock) if clock is not None else RateCardService(ledger)
    )
    result = service.publish_rate_card(
        TENANT_ONE,
        rate_card_name,
        "USD",
        [{"metric_code": "gen_ai_output_token", "unit_amount": unit_amount}],
    )
    assert result.rate_card_id is not None
    return result


class RateCardPresentmentTests(unittest.TestCase):
    """Verify published-card GET, list envelope, and fail-closed isolation."""

    def test_published_card_projects_unit_price_and_rate_window(self) -> None:
        """A published standard card shows exact unit prices and rate_window."""
        ledger = ingest_known_batch().ledger
        published = publish_named_card(ledger, "workflow_standard", TOKEN_UNIT_PRICE)
        first = RateCardPresentmentService(ledger).present_rate_card(
            TENANT_ONE, published.rate_card_id
        )
        second = RateCardPresentmentService(ledger).present_rate_card(
            TENANT_ONE, published.rate_card_id
        )
        self.assertEqual(first.as_contract_dict(), second.as_contract_dict())
        self.assertEqual(first.rate_card_id, published.rate_card_id)
        self.assertEqual(first.tenant_reference, TENANT_ONE)
        self.assertEqual(first.rate_card_name, "workflow_standard")
        self.assertEqual(first.currency_code, "USD")
        self.assertEqual(first.rate_card_version, 1)
        self.assertEqual(first.lines[0].unit_amount, TOKEN_UNIT_PRICE)
        self.assertEqual(first.lines[0].metric_code, "gen_ai_output_token")
        self.assertEqual(first.next_operator_action, "rate_window")
        payload = first.as_contract_dict()
        self.assertEqual(validate_rate_card_presentment(payload), ())
        self.assertIsInstance(payload["lines"][0]["unit_amount"], str)
        self.assertNotIsInstance(payload["lines"][0]["unit_amount"], float)
        self.assertNotIn("items", payload)
        self.assertNotIn("cursor", payload)
        self.assertNotIn("rate_card_outcome_code", payload)
        self.assertNotIn("source_payload_hash", payload)
        self.assertNotIn("rate_card_status", payload)

    def test_http_get_returns_presentment_and_list_envelope(self) -> None:
        """GET is 200 presentment; list uses {rate_cards, next_cursor}."""
        ledger = ingest_known_batch().ledger
        first = RateCardService(
            ledger, clock=lambda: datetime(2026, 8, 17, 21, 0, tzinfo=UTC)
        ).publish_rate_card(
            TENANT_ONE,
            "workflow_standard",
            "USD",
            [{"metric_code": "gen_ai_output_token", "unit_amount": TOKEN_UNIT_PRICE}],
        )
        second = RateCardService(
            ledger, clock=lambda: datetime(2026, 8, 17, 22, 0, tzinfo=UTC)
        ).publish_rate_card(
            TENANT_ONE,
            "workflow_premium",
            "USD",
            [{"metric_code": "gen_ai_output_token", "unit_amount": Decimal("0.000005")}],
        )
        assert first.rate_card_id is not None
        assert second.rate_card_id is not None
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "GET",
            f"/v1/rate-cards/{first.rate_card_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["rate_card_id"], str(first.rate_card_id))
        self.assertEqual(body["rate_card_name"], "workflow_standard")
        self.assertEqual(body["currency_code"], "USD")
        self.assertEqual(body["rate_card_version"], 1)
        self.assertEqual(body["lines"][0]["unit_amount"], "0.000002")
        self.assertEqual(body["next_operator_action"], "rate_window")
        self.assertNotIn("rate_card_outcome_code", body)
        self.assertEqual(validate_rate_card_presentment(body), ())
        replay_status, replay_body = invoke_http(
            app,
            "GET",
            f"/v1/rate-cards/{first.rate_card_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body, body)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/rate-cards",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"rate_cards", "next_cursor"})
        self.assertNotIn("items", list_body)
        self.assertNotIn("cursor", list_body)
        self.assertEqual(len(list_body["rate_cards"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        first_summary = list_body["rate_cards"][0]
        self.assertEqual(
            set(first_summary),
            {
                "rate_card_id",
                "rate_card_name",
                "currency_code",
                "rate_card_version",
                "created_at",
                "next_operator_action",
            },
        )
        second_status, second_body = invoke_http(
            app,
            "GET",
            "/v1/rate-cards",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second_body["rate_cards"]), 1)
        self.assertIsNone(second_body["next_cursor"])
        listed_ids = {
            first_summary["rate_card_id"],
            second_body["rate_cards"][0]["rate_card_id"],
        }
        self.assertEqual(listed_ids, {str(first.rate_card_id), str(second.rate_card_id)})
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/rate-cards",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["rate_cards"], [])
        self.assertIsNone(empty_body["next_cursor"])

    def test_http_post_stays_the_existing_write_and_refuses_card_data(self) -> None:
        """POST stays the #18 catalog write; PAN and secrets are refused."""
        ledger = ingest_known_batch().ledger
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "POST",
            "/v1/rate-cards",
            {
                "tenant_reference": TENANT_ONE,
                "rate_card_name": "workflow_standard",
                "currency_code": "USD",
                "lines": [
                    {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002"}
                ],
                "card_pan": "4111111111111111",
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.rate_cards), 0)
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            "/v1/rate-cards",
            {
                "tenant_reference": TENANT_ONE,
                "rate_card_name": "workflow_standard",
                "currency_code": "USD",
                "lines": [
                    {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002"}
                ],
            },
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["rate_card_outcome_code"], "accepted")
        self.assertEqual(accepted_body["rate_card_version"], 1)

    def test_cross_tenant_and_unknown_reads_are_404_without_leak(self) -> None:
        """Missing tenant is 422; unknown or cross-tenant ids stay 404 with no card."""
        ledger = ingest_known_batch().ledger
        published = publish_named_card(ledger, "workflow_standard", TOKEN_UNIT_PRICE)
        app = create_http_app(ledger)
        missing_status, missing_body = invoke_http(
            app, "GET", f"/v1/rate-cards/{published.rate_card_id}"
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/rate-cards/{published.rate_card_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "rate_card_not_found")
        self.assertNotIn("unit_amount", other_body)
        self.assertNotIn("rate_card_name", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/rate-cards/{uuid4()}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "rate_card_not_found")
        with self.assertRaises(RateCardPresentmentQueryError) as crossed:
            RateCardPresentmentService(ledger).present_rate_card(
                TENANT_TWO, published.rate_card_id
            )
        self.assertEqual(crossed.exception.rejection_reason_code, "rate_card_not_found")

    def test_list_filters_and_helpers_fail_closed(self) -> None:
        """Illegal list filters stay 422; helpers cover page bounds and rate_window."""
        self.assertEqual(next_operator_action(), "rate_window")
        ledger = ingest_known_batch().ledger
        published = publish_named_card(ledger, "workflow_standard", TOKEN_UNIT_PRICE)
        tenant = ledger.require_tenant(TENANT_ONE)
        ledger.insert_rate_card(
            StoredRateCard(
                rate_card_id=generate_record_id(),
                tenant_account_id=tenant.tenant_account_id,
                rate_card_name="header_only_card",
                currency_code="USD",
                created_at=datetime(2026, 8, 17, 20, 0, tzinfo=UTC),
            )
        )
        app = create_http_app(ledger)
        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/rate-cards",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")
        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/rate-cards",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")
        method_status, method_body = invoke_http(
            app, "PUT", f"/v1/rate-cards/{published.rate_card_id}"
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.RateCardPresentmentService.present_rate_card",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                f"/v1/rate-cards/{published.rate_card_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        empty = RateCardPresentmentService()
        self.assertIsNotNone(empty.ledger)
        with self.assertRaises(RateCardPresentmentQueryError):
            empty.list_rate_cards(TENANT_ONE)
        service = RateCardPresentmentService(ledger)
        listed = service.list_rate_cards(TENANT_ONE, cursor="")
        self.assertEqual(len(listed.rate_cards), 1)
        self.assertEqual(listed.rate_cards[0].rate_card_name, "workflow_standard")
        with self.assertRaises(RateCardPresentmentQueryError):
            service.list_rate_cards(TENANT_ONE, page_limit=True)
        with self.assertRaises(RateCardPresentmentQueryError):
            service.list_rate_cards(TENANT_ONE, page_limit=101)
        with self.assertRaises(RateCardPresentmentQueryError):
            service.list_rate_cards(TENANT_ONE, page_limit=1.5)
        with self.assertRaises(RateCardPresentmentQueryError):
            service.list_rate_cards(TENANT_ONE, page_limit="abc")
        with self.assertRaises(RateCardPresentmentQueryError):
            service.list_rate_cards(TENANT_ONE, page_limit="0")
        list_missing_status, list_missing_body = invoke_http(app, "GET", "/v1/rate-cards")
        self.assertEqual(list_missing_status, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "tenant_not_found")
        with mock.patch(
            "metering_billing.http_app.RateCardPresentmentService.list_rate_cards",
            side_effect=ValueError("closed"),
        ):
            decimal_status, decimal_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/rate-cards",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(decimal_status, 422)
        self.assertEqual(decimal_body["rejection_reason_code"], "request_invalid")
        default_limit = service.list_rate_cards(TENANT_ONE, page_limit=None)
        self.assertEqual(len(default_limit.rate_cards), 1)
        empty_limit = service.list_rate_cards(TENANT_ONE, page_limit="")
        self.assertEqual(len(empty_limit.rate_cards), 1)
        self.assertEqual(service.list_rate_cards(TENANT_ONE, page_limit=50).next_cursor, None)
        with self.assertRaises(RateCardPresentmentQueryError):
            service.present_rate_card(TENANT_ONE, uuid4())
        with self.assertRaises(RateCardPresentmentQueryError):
            service.present_rate_card("", published.rate_card_id)
        header_only_id = next(
            card.rate_card_id
            for card in ledger.list_rate_cards(tenant.tenant_account_id)
            if card.rate_card_name == "header_only_card"
        )
        with self.assertRaises(RateCardPresentmentQueryError):
            service.present_rate_card(TENANT_ONE, header_only_id)
