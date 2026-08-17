"""Rating-run HTTP presentment tests for tenant-scoped rating-window reads."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest import mock
from uuid import uuid4

from metering_billing import (
    RatingRunPresentmentService,
    UsageRatingService,
    create_http_app,
)
from metering_billing.contracts import validate_rating_run_presentment
from metering_billing.errors import RatingRunPresentmentQueryError
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.rating_run_presentment import next_operator_action
from test_http_app import invoke_http
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import (
    DAY_WINDOW,
    KNOWN_MORNING_TOTAL,
    MORNING_WINDOW,
    ingest_known_batch,
    seed_rated_ledger,
)


def rate_named_window(ledger, time_window, clock=None):
    """Rate one #7 window and return the stored run."""
    service = (
        UsageRatingService(ledger, clock=clock)
        if clock is not None
        else UsageRatingService(ledger)
    )
    result = service.rate_usage_window(TENANT_ONE, time_window, 1)
    assert result.rating_run_id is not None
    return result


class RatingRunPresentmentTests(unittest.TestCase):
    """Verify stored-rating GET, list envelope, and fail-closed isolation."""

    def test_stored_run_projects_total_and_draft_invoice(self) -> None:
        """A stored morning run shows exact total and draft_invoice."""
        ledger = seed_rated_ledger()
        ingest_known_batch(ledger)
        rated = rate_named_window(ledger, MORNING_WINDOW)
        first = RatingRunPresentmentService(ledger).present_rating_run(
            TENANT_ONE, rated.rating_run_id
        )
        second = RatingRunPresentmentService(ledger).present_rating_run(
            TENANT_ONE, rated.rating_run_id
        )
        self.assertEqual(first.as_contract_dict(), second.as_contract_dict())
        self.assertEqual(first.rating_run_id, rated.rating_run_id)
        self.assertEqual(first.tenant_reference, TENANT_ONE)
        self.assertEqual(first.rate_card_code, "cwl_standard")
        self.assertEqual(first.rate_card_version, 1)
        self.assertEqual(first.rated_total_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(first.rating_lines[0].meter_code, "gen_ai_output_token")
        self.assertEqual(first.next_operator_action, "draft_invoice")
        payload = first.as_contract_dict()
        self.assertEqual(validate_rating_run_presentment(payload), ())
        self.assertIsInstance(payload["rated_total_amount"], str)
        self.assertNotIsInstance(payload["rated_total_amount"], float)
        self.assertIsInstance(payload["rating_lines"][0]["line_total_amount"], str)
        self.assertNotIn("items", payload)
        self.assertNotIn("cursor", payload)
        self.assertNotIn("rating_outcome_code", payload)
        self.assertNotIn("card_pan", payload)
        self.assertNotIn("rating_run_status", payload)

    def test_http_get_returns_presentment_and_list_envelope(self) -> None:
        """GET is 200 presentment; list uses {rating_runs, next_cursor}."""
        ledger = seed_rated_ledger()
        ingest_known_batch(ledger)
        first = rate_named_window(
            ledger,
            MORNING_WINDOW,
            clock=lambda: datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
        )
        second = rate_named_window(
            ledger,
            DAY_WINDOW,
            clock=lambda: datetime(2026, 8, 17, 22, 0, tzinfo=UTC),
        )
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "GET",
            f"/v1/rating-runs/{first.rating_run_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["rating_run_id"], str(first.rating_run_id))
        self.assertEqual(body["rated_total_amount"], format_exact_decimal(KNOWN_MORNING_TOTAL))
        self.assertEqual(body["next_operator_action"], "draft_invoice")
        self.assertNotIn("rating_outcome_code", body)
        self.assertEqual(validate_rating_run_presentment(body), ())
        replay_status, replay_body = invoke_http(
            app,
            "GET",
            f"/v1/rating-runs/{first.rating_run_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body, body)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/rating-runs",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"rating_runs", "next_cursor"})
        self.assertNotIn("items", list_body)
        self.assertNotIn("cursor", list_body)
        self.assertEqual(len(list_body["rating_runs"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        first_summary = list_body["rating_runs"][0]
        self.assertEqual(
            set(first_summary),
            {
                "rating_run_id",
                "rated_total_amount",
                "recorded_at",
                "next_operator_action",
            },
        )
        second_status, second_body = invoke_http(
            app,
            "GET",
            "/v1/rating-runs",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second_body["rating_runs"]), 1)
        self.assertIsNone(second_body["next_cursor"])
        listed_ids = {
            first_summary["rating_run_id"],
            second_body["rating_runs"][0]["rating_run_id"],
        }
        self.assertEqual(listed_ids, {str(first.rating_run_id), str(second.rating_run_id)})
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/rating-runs",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["rating_runs"], [])
        self.assertIsNone(empty_body["next_cursor"])

    def test_http_post_stays_the_existing_rate_command_and_refuses_card_data(self) -> None:
        """POST stays the #7 rate-a-window command; PAN and secrets are refused."""
        ledger = seed_rated_ledger()
        ingest_known_batch(ledger)
        app = create_http_app(ledger)
        payload = {
            "tenant_reference": TENANT_ONE,
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "rate_card_version": 1,
        }
        status, body = invoke_http(
            app,
            "POST",
            "/v1/rating-runs",
            {**payload, "card_pan": "4111111111111111"},
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.rating_runs), 0)
        accepted_status, accepted_body = invoke_http(app, "POST", "/v1/rating-runs", payload)
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["rating_outcome_code"], "accepted")
        self.assertIn("rating_run_id", accepted_body)

    def test_cross_tenant_and_unknown_reads_are_404_without_leak(self) -> None:
        """Missing tenant is 422; unknown or cross-tenant ids stay 404 with no money."""
        ledger = seed_rated_ledger()
        ingest_known_batch(ledger)
        rated = rate_named_window(ledger, MORNING_WINDOW)
        app = create_http_app(ledger)
        missing_status, missing_body = invoke_http(
            app, "GET", f"/v1/rating-runs/{rated.rating_run_id}"
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/rating-runs/{rated.rating_run_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "rating_run_not_found")
        self.assertNotIn("rated_total_amount", other_body)
        self.assertNotIn("rating_lines", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/rating-runs/{uuid4()}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "rating_run_not_found")
        with self.assertRaises(RatingRunPresentmentQueryError) as crossed:
            RatingRunPresentmentService(ledger).present_rating_run(
                TENANT_TWO, rated.rating_run_id
            )
        self.assertEqual(crossed.exception.rejection_reason_code, "rating_run_not_found")

    def test_list_filters_and_helpers_fail_closed(self) -> None:
        """Illegal list filters stay 422; helpers cover page bounds and draft_invoice."""
        self.assertEqual(next_operator_action(), "draft_invoice")
        ledger = seed_rated_ledger()
        ingest_known_batch(ledger)
        rated = rate_named_window(ledger, MORNING_WINDOW)
        app = create_http_app(ledger)
        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/rating-runs",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")
        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/rating-runs",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")
        method_status, method_body = invoke_http(
            app, "PUT", f"/v1/rating-runs/{rated.rating_run_id}"
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.RatingRunPresentmentService.present_rating_run",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                f"/v1/rating-runs/{rated.rating_run_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        empty = RatingRunPresentmentService()
        self.assertIsNotNone(empty.ledger)
        with self.assertRaises(RatingRunPresentmentQueryError):
            empty.list_rating_runs(TENANT_ONE)
        service = RatingRunPresentmentService(ledger)
        listed = service.list_rating_runs(TENANT_ONE, cursor="")
        self.assertEqual(len(listed.rating_runs), 1)
        self.assertEqual(
            ledger.get_rating_run(rated.rating_run_id).rating_run_id, rated.rating_run_id
        )
        self.assertEqual(
            len(ledger.list_rating_runs(ledger.require_tenant(TENANT_ONE).tenant_account_id)),
            1,
        )
        self.assertEqual(len(ledger.list_rating_runs()), 1)
        with self.assertRaises(RatingRunPresentmentQueryError):
            service.list_rating_runs(TENANT_ONE, page_limit=True)
        with self.assertRaises(RatingRunPresentmentQueryError):
            service.list_rating_runs(TENANT_ONE, page_limit=101)
        with self.assertRaises(RatingRunPresentmentQueryError):
            service.list_rating_runs(TENANT_ONE, page_limit=1.5)
        with self.assertRaises(RatingRunPresentmentQueryError):
            service.list_rating_runs(TENANT_ONE, page_limit="abc")
        with self.assertRaises(RatingRunPresentmentQueryError):
            service.list_rating_runs(TENANT_ONE, page_limit="0")
        list_missing_status, list_missing_body = invoke_http(app, "GET", "/v1/rating-runs")
        self.assertEqual(list_missing_status, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "tenant_not_found")
        with mock.patch(
            "metering_billing.http_app.RatingRunPresentmentService.list_rating_runs",
            side_effect=ValueError("closed"),
        ):
            decimal_status, decimal_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/rating-runs",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(decimal_status, 422)
        self.assertEqual(decimal_body["rejection_reason_code"], "request_invalid")
        default_limit = service.list_rating_runs(TENANT_ONE, page_limit=None)
        self.assertEqual(len(default_limit.rating_runs), 1)
        empty_limit = service.list_rating_runs(TENANT_ONE, page_limit="")
        self.assertEqual(len(empty_limit.rating_runs), 1)
        self.assertEqual(service.list_rating_runs(TENANT_ONE, page_limit=50).next_cursor, None)
        with self.assertRaises(RatingRunPresentmentQueryError):
            service.present_rating_run(TENANT_ONE, uuid4())
        with self.assertRaises(RatingRunPresentmentQueryError):
            service.present_rating_run("", rated.rating_run_id)
        self.assertIsNone(ledger.get_rating_run(uuid4()))
