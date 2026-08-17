"""Realistic rating tests for exact totals, replay identity, and quality filters."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing import (
    IngestionOutcomeCode,
    MemoryUsageLedger,
    TimeWindow,
    UsageIngestionService,
    UsageRatingService,
)
from metering_billing.contracts import validate_rating_run, validate_schema_instance
from metering_billing.errors import ExactDecimalError, RatingError
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.rating import utc_now
from test_usage_ingestion import (
    ACCOUNT_TWO,
    CREDENTIAL_TWO,
    PRINCIPAL_TWO,
    TENANT_ONE,
    TENANT_TWO,
    known_event_batch,
    make_event,
    seed_ledger,
)


RATING_WINDOW = TimeWindow.from_iso8601(
    "2026-08-16T10:00:00Z", "2026-08-16T12:00:00Z"
)
RATE_CARD_CODE = "orchestrator_standard"
UNIT_PRICE = Decimal("1")
EXPECTED_BILLABLE_TOTAL = Decimal("1853.500000000001")


def seed_rating_ledger() -> MemoryUsageLedger:
    """Register isolated tenants, quality rules, and one versioned rate card."""
    ledger = seed_ledger()
    meter = next(
        definition
        for definition in ledger.meter_definitions
        if definition.meter_code == "gen_ai_output_token"
    )
    ledger.register_meter_quality_rule(
        meter.meter_definition_id, "reconstructed", "manual_review"
    )
    ledger.register_rate_card(RATE_CARD_CODE, 1, "USD", datetime(2026, 1, 1, tzinfo=UTC))
    ledger.register_rate_card_price(RATE_CARD_CODE, 1, "gen_ai_output_token", UNIT_PRICE)
    return ledger


def ingest_known_rating_facts(service: UsageIngestionService) -> None:
    """Store the buyer fixture plus equivalent and excluded-quality events."""
    batch = known_event_batch()
    first = service.ingest_usage_batch(batch)
    if first.accepted_event_count != 3:
        raise AssertionError("known batch must accept three billable events")

    one_point_zero = make_event(
        event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf801",
        source_event_key="workflow_390:unit_one",
        occurred_at="2026-08-16T10:29:00.000Z",
        measurements=[
            {
                "meter_code": "gen_ai_output_token",
                "quantity": "1.0",
                "unit_code": "token",
                "quality_code": "provider_reported",
            }
        ],
    )
    one_plain = make_event(
        event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf802",
        source_event_key="workflow_390:unit_one",
        occurred_at="2026-08-16T10:29:00.000+00:00",
        measurements=[
            {
                "meter_code": "gen_ai_output_token",
                "quantity": "1",
                "unit_code": "token",
                "quality_code": "provider_reported",
            }
        ],
    )
    if one_point_zero["source_payload_hash"] != one_plain["source_payload_hash"]:
        raise AssertionError("1 and 1.0 with Z and +00:00 must be one commercial fact")
    accepted = service.ingest_usage_event(one_point_zero)
    replay = service.ingest_usage_event(one_plain)
    if accepted.ingestion_outcome_code != IngestionOutcomeCode.ACCEPTED:
        raise AssertionError("the unit-one event must store once")
    if replay.ingestion_outcome_code != IngestionOutcomeCode.DUPLICATE_REPLAY:
        raise AssertionError("the equivalent unit-one spelling must replay")

    estimated = make_event(
        event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf803",
        source_event_key="workflow_391:estimated",
        occurred_at="2026-08-16T10:40:00.000Z",
        measurements=[
            {
                "meter_code": "gen_ai_output_token",
                "quantity": "100",
                "unit_code": "token",
                "quality_code": "estimated",
            }
        ],
    )
    reconstructed = make_event(
        event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf804",
        source_event_key="workflow_391:reconstructed",
        occurred_at="2026-08-16T10:41:00.000Z",
        measurements=[
            {
                "meter_code": "gen_ai_output_token",
                "quantity": "50",
                "unit_code": "token",
                "quality_code": "reconstructed",
            }
        ],
    )
    if (
        service.ingest_usage_event(estimated).ingestion_outcome_code
        != IngestionOutcomeCode.ACCEPTED
    ):
        raise AssertionError("estimated usage must store for analytics")
    if (
        service.ingest_usage_event(reconstructed).ingestion_outcome_code
        != IngestionOutcomeCode.ACCEPTED
    ):
        raise AssertionError("reconstructed usage must store for manual review")

    foreign = make_event(
        event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf805",
        source_event_key="tenant_two:rating_noise",
        tenant_reference=TENANT_TWO,
        billing_account_reference=ACCOUNT_TWO,
        billing_principal_reference=PRINCIPAL_TWO,
        credential_reference=CREDENTIAL_TWO,
        occurred_at="2026-08-16T10:27:42.482Z",
        measurements=[
            {
                "meter_code": "gen_ai_output_token",
                "quantity": "9999",
                "unit_code": "token",
                "quality_code": "provider_reported",
            }
        ],
    )
    if service.ingest_usage_event(foreign).ingestion_outcome_code != IngestionOutcomeCode.ACCEPTED:
        raise AssertionError("the other tenant's usage must store in its own ledger")


class UsageRatingTests(unittest.TestCase):
    """Verify deterministic invoice-intent totals from stored usage."""

    def test_known_window_rates_to_exact_total_and_replays(self) -> None:
        """Known usage in a window produces a known money total; a second rate is a replay."""
        ledger = seed_rating_ledger()
        ingest = UsageIngestionService(ledger)
        ingest_known_rating_facts(ingest)
        rating = UsageRatingService(ledger)

        first = rating.rate_usage_window(TENANT_ONE, RATING_WINDOW, RATE_CARD_CODE, 1)
        self.assertEqual(first.rating_outcome_code.value, "accepted")
        self.assertEqual(first.invoice_intent_total, EXPECTED_BILLABLE_TOTAL)
        self.assertEqual(first.currency_code, "USD")
        self.assertEqual(len(first.rating_lines), 1)
        line = first.rating_lines[0]
        self.assertEqual(line.meter_code, "gen_ai_output_token")
        self.assertEqual(line.billed_quantity, EXPECTED_BILLABLE_TOTAL)
        self.assertEqual(line.unit_price, UNIT_PRICE)
        self.assertEqual(line.line_amount, EXPECTED_BILLABLE_TOTAL)
        self.assertEqual(validate_rating_run(first.as_contract_dict()), ())
        self.assertEqual(len(ledger.accounting_export_records), 0)

        second = rating.rate_usage_window(TENANT_ONE, RATING_WINDOW, RATE_CARD_CODE, 1)
        self.assertEqual(second.rating_outcome_code.value, "duplicate_replay")
        self.assertEqual(second.rating_run_id, first.rating_run_id)
        self.assertEqual(second.invoice_intent_total, first.invoice_intent_total)
        self.assertEqual(second.usage_snapshot_hash, first.usage_snapshot_hash)
        self.assertEqual(len(ledger.rating_runs), 1)
        self.assertEqual(len(ledger.rating_lines), 1)

    def test_other_tenant_usage_is_invisible_and_quality_filters_apply(self) -> None:
        """Another tenant's 9999 tokens and excluded qualities cannot enter invoice intent."""
        ledger = seed_rating_ledger()
        ingest = UsageIngestionService(ledger)
        ingest_known_rating_facts(ingest)
        rating = UsageRatingService(ledger)

        tenant_one = rating.rate_usage_window(TENANT_ONE, RATING_WINDOW, RATE_CARD_CODE, 1)
        tenant_two = rating.rate_usage_window(TENANT_TWO, RATING_WINDOW, RATE_CARD_CODE, 1)
        self.assertEqual(tenant_one.invoice_intent_total, EXPECTED_BILLABLE_TOTAL)
        self.assertEqual(tenant_two.invoice_intent_total, Decimal("9999"))
        self.assertNotEqual(tenant_one.rating_run_id, tenant_two.rating_run_id)
        self.assertNotEqual(tenant_one.usage_snapshot_hash, tenant_two.usage_snapshot_hash)
        self.assertEqual(
            {line.billed_quantity for line in tenant_one.rating_lines},
            {EXPECTED_BILLABLE_TOTAL},
        )

    def test_empty_window_and_missing_catalog_fail_or_zero(self) -> None:
        """An empty window is a zero total; missing tenant or rate card fail closed."""
        ledger = seed_rating_ledger()
        rating = UsageRatingService(ledger)
        empty = rating.rate_usage_window(TENANT_ONE, RATING_WINDOW, RATE_CARD_CODE, 1)
        self.assertEqual(empty.invoice_intent_total, Decimal("0"))
        self.assertEqual(empty.rating_lines, ())
        replay = rating.rate_usage_window(TENANT_ONE, RATING_WINDOW, RATE_CARD_CODE, 1)
        self.assertEqual(replay.rating_run_id, empty.rating_run_id)
        self.assertEqual(replay.rating_outcome_code.value, "duplicate_replay")
        with self.assertRaises(RatingError):
            rating.rate_usage_window("urn:cwl:missing_tenant", RATING_WINDOW, RATE_CARD_CODE, 1)
        with self.assertRaises(RatingError):
            MemoryUsageLedger().register_rate_card_price(
                RATE_CARD_CODE, 1, "gen_ai_output_token", UNIT_PRICE
            )
        with self.assertRaises(RatingError):
            rating.rate_usage_window(TENANT_ONE, RATING_WINDOW, "missing_rate_card", 1)
        with self.assertRaises(RatingError):
            rating.rate_usage_window(TENANT_ONE, RATING_WINDOW, RATE_CARD_CODE, 9)

    def test_billable_meter_without_price_fails_closed(self) -> None:
        """Billable usage cannot silently drop when the rate card has no unit price."""
        ledger = seed_ledger()
        ledger.register_rate_card(RATE_CARD_CODE, 1, "USD", datetime(2026, 1, 1, tzinfo=UTC))
        ingest = UsageIngestionService(ledger)
        self.assertEqual(
            ingest.ingest_usage_event(make_event()).ingestion_outcome_code,
            IngestionOutcomeCode.ACCEPTED,
        )
        rating = UsageRatingService(ledger)
        with self.assertRaises(RatingError):
            rating.rate_usage_window(TENANT_ONE, RATING_WINDOW, RATE_CARD_CODE, 1)

    def test_rate_card_rejects_binary_float_and_unknown_disposition(self) -> None:
        """Unit prices stay exact, and unknown quality dispositions cannot be rated."""
        ledger = seed_rating_ledger()
        with self.assertRaises(ExactDecimalError):
            ledger.register_rate_card_price(RATE_CARD_CODE, 1, "other_meter", 0.1)
        ingest = UsageIngestionService(ledger)
        ingest.ingest_usage_event(make_event())
        stored = next(iter(ledger.usage_events.values()))
        measurement = stored.measurements[0]
        ledger.meter_quality_rules[
            (measurement.meter_definition_id, measurement.quality_code)
        ] = type(next(iter(ledger.meter_quality_rules.values())))(
            meter_quality_rule_id=UUID("019d7b92-1aa0-7a7f-b61c-962c0f4bf900"),
            meter_definition_id=measurement.meter_definition_id,
            quality_code=measurement.quality_code,
            billing_disposition_code="not_a_disposition",
        )
        rating = UsageRatingService(ledger)
        with self.assertRaises(RatingError):
            rating.rate_usage_window(TENANT_ONE, RATING_WINDOW, RATE_CARD_CODE, 1)
        del ledger.meter_quality_rules[
            (measurement.meter_definition_id, measurement.quality_code)
        ]
        with self.assertRaises(RatingError):
            rating.rate_usage_window(TENANT_ONE, RATING_WINDOW, RATE_CARD_CODE, 1)

    def test_rating_run_contract_requires_matching_line_total(self) -> None:
        """Semantic rating validation rejects a total that does not equal its lines."""
        valid = {
            "rating_run_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf910",
            "rating_contract_version": 1,
            "tenant_reference": TENANT_ONE,
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T12:00:00Z",
            "rate_card_code": RATE_CARD_CODE,
            "rate_card_version": 1,
            "usage_snapshot_hash": "sha256:" + "a" * 64,
            "currency_code": "USD",
            "invoice_intent_total": "1853.500000000001",
            "rating_outcome_code": "accepted",
            "rating_lines": [
                {
                    "meter_code": "gen_ai_output_token",
                    "billed_quantity": "1853.500000000001",
                    "unit_price": "1",
                    "line_amount": "1853.500000000001",
                }
            ],
        }
        self.assertEqual(validate_rating_run(valid), ())
        mismatched = json.loads(json.dumps(valid))
        mismatched["invoice_intent_total"] = "1"
        self.assertIn(
            "$: invoice_intent_total must equal the sum of rating_lines",
            validate_rating_run(mismatched),
        )
        self.assertTrue(validate_rating_run({"rating_run_id": 1}))
        self.assertTrue(validate_rating_run(["not-an-object"]))
        self.assertTrue(
            validate_rating_run(
                {
                    "rating_run_id": 1,
                    "invoice_intent_total": "0",
                    "rating_lines": ["not-an-object"],
                }
            )
        )
        self.assertTrue(
            validate_rating_run(
                {
                    "rating_run_id": 1,
                    "invoice_intent_total": "not-a-decimal",
                    "rating_lines": [],
                }
            )
        )
        self.assertEqual(validate_schema_instance({"type": "object"}, {}), ())

    def test_default_rating_service_and_immutable_insert(self) -> None:
        """The zero-argument service constructs a ledger, and rating rows are append-only."""
        rating = UsageRatingService()
        self.assertIsInstance(rating.ledger, MemoryUsageLedger)
        self.assertEqual(utc_now().tzinfo, UTC)
        with self.assertRaises(RatingError):
            rating.rate_usage_window(TENANT_ONE, RATING_WINDOW, RATE_CARD_CODE, 1)
        ledger = seed_rating_ledger()
        ingest = UsageIngestionService(ledger)
        ingest.ingest_usage_event(make_event())
        service = UsageRatingService(ledger)
        first = service.rate_usage_window(TENANT_ONE, RATING_WINDOW, RATE_CARD_CODE, 1)
        with self.assertRaises(ValueError):
            ledger.insert_rating_run(ledger.rating_runs[first.rating_run_id])
        replay_card = ledger.register_rate_card(
            RATE_CARD_CODE, 1, "USD", datetime(2026, 1, 1, tzinfo=UTC)
        )
        self.assertEqual(replay_card.rate_card_version, 1)
        replay_price = ledger.register_rate_card_price(
            RATE_CARD_CODE, 1, "gen_ai_output_token", UNIT_PRICE
        )
        self.assertEqual(replay_price.unit_price, UNIT_PRICE)
        self.assertEqual(format_exact_decimal(first.invoice_intent_total), "1810")


if __name__ == "__main__":
    unittest.main()
