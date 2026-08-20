"""Realistic rating tests for exact money, replay identity, and quality filters."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import UUID

from metering_billing import (
    IngestionOutcomeCode,
    MemoryUsageLedger,
    RateCardService,
    RatingOutcomeCode,
    RatingRejectionReasonCode,
    RejectionReasonCode,
    TimeWindow,
    UsageIngestionService,
    UsageRatingService,
    format_exact_decimal,
    parse_exact_decimal,
)
from metering_billing.contracts import validate_rating_run
from metering_billing.errors import ExactDecimalError
from metering_billing.usage_ledger import generate_record_id
from metering_billing.usage_rating import RatingRunResult
from test_usage_ingestion import (
    ACCOUNT_ONE,
    ACCOUNT_TWO,
    CATALOG_START,
    CREDENTIAL_TWO,
    PRINCIPAL_TWO,
    TENANT_ONE,
    TENANT_TWO,
    known_event_batch,
    make_event,
    seed_ledger,
)


TOKEN_UNIT_PRICE = Decimal("0.000002")
RATE_CARD_CODE = "cwl_standard"
STANDARD_RATE_CARD_LINES = (
    {"metric_code": "gen_ai_output_token", "unit_amount": TOKEN_UNIT_PRICE},
)
MORNING_WINDOW = TimeWindow.from_iso8601("2026-08-16T10:00:00Z", "2026-08-16T11:00:00Z")
DAY_WINDOW = TimeWindow.from_iso8601("2026-08-16T10:00:00Z", "2026-08-16T12:00:00Z")
KNOWN_MORNING_QUANTITY = Decimal("1810") + Decimal("42.5")
KNOWN_MORNING_TOTAL = KNOWN_MORNING_QUANTITY * TOKEN_UNIT_PRICE
KNOWN_DAY_QUANTITY = KNOWN_MORNING_QUANTITY + Decimal("0.000000000001")
KNOWN_DAY_TOTAL = KNOWN_DAY_QUANTITY * TOKEN_UNIT_PRICE


def publish_standard_rate_card(
    ledger: MemoryUsageLedger, tenant_reference: str = TENANT_ONE
):
    """Publish the known token price list for one tenant."""
    return RateCardService(ledger).publish_rate_card(
        tenant_reference, RATE_CARD_CODE, "USD", STANDARD_RATE_CARD_LINES
    )


def seed_rated_ledger() -> MemoryUsageLedger:
    """Register isolated tenants, billable qualities, and persisted rate cards."""
    ledger = seed_ledger()
    meter = ledger.meter_definitions[0]
    ledger.register_meter_quality_rule(
        meter.meter_definition_id, "reconstructed", "manual_review"
    )
    ledger.register_meter_quality_rule(
        meter.meter_definition_id, "corrected", "manual_review"
    )
    publish_standard_rate_card(ledger, TENANT_ONE)
    publish_standard_rate_card(ledger, TENANT_TWO)
    return ledger


def ingest_known_batch(ledger: MemoryUsageLedger | None = None) -> UsageIngestionService:
    """Persist the known buyer batch on a rated ledger."""
    service = UsageIngestionService(seed_rated_ledger() if ledger is None else ledger)
    receipt = service.ingest_usage_batch(known_event_batch())
    if receipt.accepted_event_count != 3:
        raise AssertionError("known batch must ingest before rating")
    return service


class UsageRatingTests(unittest.TestCase):
    """Verify buyer-facing windowed rating, replay, isolation, and quality filters."""

    def test_rating_fails_closed_when_tenant_resolution_is_hollow(self) -> None:
        """A hollow tenant resolution must raise ValueError instead of using assert."""
        ingest = ingest_known_batch()
        rating = UsageRatingService(ingest.ledger)
        with mock.patch.object(rating.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                rating.rate_usage_window(TENANT_ONE, MORNING_WINDOW, 1)

    def test_known_window_produces_exact_invoice_intent_total(self) -> None:
        """Known stored usage in a half-open window must rate to one exact money total."""
        ingest = ingest_known_batch()
        fixed = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
        rating = UsageRatingService(ingest.ledger, clock=lambda: fixed)
        result = rating.rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        self.assertEqual(
            ingest.ledger.rating_runs[result.rating_run_id].recorded_at,
            fixed,
        )

        self.assertEqual(result.rating_outcome_code, RatingOutcomeCode.ACCEPTED)
        self.assertIsInstance(result.rating_run_id, UUID)
        self.assertEqual(result.rated_total_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(result.currency_code, "USD")
        self.assertEqual(result.rate_card_version, 1)
        self.assertNotIsInstance(result.rated_total_amount, float)
        self.assertEqual(len(result.rating_lines), 1)
        line = result.rating_lines[0]
        self.assertEqual(line.rated_quantity, KNOWN_MORNING_QUANTITY)
        self.assertEqual(line.unit_price_amount, TOKEN_UNIT_PRICE)
        self.assertEqual(line.line_total_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(line.meter_code, "gen_ai_output_token")
        self.assertEqual(line.billing_account_reference, ACCOUNT_ONE)
        self.assertEqual(validate_rating_run(result.as_contract_dict()), ())
        self.assertEqual(len(ingest.ledger.rating_runs), 1)
        self.assertEqual(len(ingest.ledger.rating_lines), 1)
        self.assertEqual(len(ingest.ledger.accounting_export_records), 0)
        self.assertEqual(len(ingest.ledger.invoice_drafts), 0)

    def test_equivalent_decimal_and_utc_spellings_rate_as_one_fact(self) -> None:
        """Ingested ``1``/``1.0`` and ``Z``/``+00:00`` remain one fact and one money total."""
        ingest = UsageIngestionService(seed_rated_ledger())
        first = make_event(
            occurred_at="2026-08-16T10:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "1.0",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        second = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf888",
            source_event_key="workflow_381:step_04:attempt_alt",
            occurred_at="2026-08-16T10:27:42.482+00:00",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "1",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        self.assertEqual(first["source_payload_hash"], second["source_payload_hash"])
        self.assertEqual(
            ingest.ingest_usage_event(first).ingestion_outcome_code,
            IngestionOutcomeCode.ACCEPTED,
        )
        self.assertEqual(
            ingest.ingest_usage_event(second).rejection_reason_code,
            RejectionReasonCode.PAYLOAD_HASH_CONFLICT,
        )

        rating = UsageRatingService(ingest.ledger)
        z_window = TimeWindow.from_iso8601("2026-08-16T10:00:00Z", "2026-08-16T11:00:00Z")
        offset_window = TimeWindow.from_iso8601(
            "2026-08-16T10:00:00+00:00", "2026-08-16T11:00:00+00:00"
        )
        first_rate = rating.rate_usage_window(TENANT_ONE, z_window, 1)
        second_rate = rating.rate_usage_window(TENANT_ONE, offset_window, 1)
        self.assertEqual(first_rate.rated_total_amount, TOKEN_UNIT_PRICE)
        self.assertEqual(first_rate.rating_lines[0].rated_quantity, Decimal("1"))
        self.assertEqual(second_rate.rating_outcome_code, RatingOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(second_rate.rating_run_id, first_rate.rating_run_id)
        self.assertEqual(second_rate.rated_total_amount, first_rate.rated_total_amount)
        self.assertEqual(len(ingest.ledger.rating_runs), 1)

    def test_second_rate_of_the_same_snapshot_is_a_replay(self) -> None:
        """The same tenant, window, rate card, and usage snapshot reuse rating_run_id."""
        ingest = ingest_known_batch()
        rating = UsageRatingService(ingest.ledger)
        first = rating.rate_usage_window(TENANT_ONE, DAY_WINDOW, 1)
        second = rating.rate_usage_window(TENANT_ONE, DAY_WINDOW, 1)
        self.assertEqual(first.rating_outcome_code, RatingOutcomeCode.ACCEPTED)
        self.assertEqual(second.rating_outcome_code, RatingOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(second.rating_run_id, first.rating_run_id)
        self.assertEqual(second.usage_snapshot_hash, first.usage_snapshot_hash)
        self.assertEqual(second.rated_total_amount, KNOWN_DAY_TOTAL)
        self.assertEqual(first.rated_total_amount, KNOWN_DAY_TOTAL)
        self.assertEqual(len(ingest.ledger.rating_runs), 1)
        self.assertEqual(len(ingest.ledger.rating_lines), 1)

    def test_other_tenant_usage_is_invisible_to_invoice_intent_totals(self) -> None:
        """A tenant window cannot see or price another tenant's stored usage."""
        ingest = ingest_known_batch()
        foreign = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf701",
            source_event_key="tenant_two:step_01",
            tenant_reference=TENANT_TWO,
            billing_account_reference=ACCOUNT_TWO,
            billing_principal_reference=PRINCIPAL_TWO,
            credential_reference=CREDENTIAL_TWO,
            occurred_at="2026-08-16T10:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "999999",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        self.assertEqual(
            ingest.ingest_usage_event(foreign).ingestion_outcome_code,
            IngestionOutcomeCode.ACCEPTED,
        )
        rating = UsageRatingService(ingest.ledger)
        tenant_one = rating.rate_usage_window(TENANT_ONE, MORNING_WINDOW, 1)
        tenant_two = rating.rate_usage_window(TENANT_TWO, MORNING_WINDOW, 1)
        self.assertEqual(tenant_one.rated_total_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(tenant_two.rated_total_amount, Decimal("999999") * TOKEN_UNIT_PRICE)
        self.assertNotEqual(tenant_one.rating_run_id, tenant_two.rating_run_id)
        self.assertNotEqual(tenant_one.usage_snapshot_hash, tenant_two.usage_snapshot_hash)
        self.assertEqual(
            [line.billing_account_reference for line in tenant_one.rating_lines],
            [ACCOUNT_ONE],
        )
        self.assertEqual(
            [line.billing_account_reference for line in tenant_two.rating_lines],
            [ACCOUNT_TWO],
        )
        one_runs = ingest.ledger.list_rating_runs(
            ingest.ledger.require_tenant(TENANT_ONE).tenant_account_id
        )
        self.assertEqual(len(one_runs), 1)
        self.assertEqual(one_runs[0].rating_run_id, tenant_one.rating_run_id)

    def test_estimated_and_reconstructed_quality_stay_out_of_invoice_intent(self) -> None:
        """Analytics-only and manual-review measurements remain stored and unpriced."""
        ingest = UsageIngestionService(seed_rated_ledger())
        billable = make_event(
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "100",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ]
        )
        estimated = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf801",
            source_event_key="workflow_381:step_04:estimated",
            occurred_at="2026-08-16T10:28:00Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "1000",
                    "unit_code": "token",
                    "quality_code": "estimated",
                }
            ],
        )
        reconstructed = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf802",
            source_event_key="workflow_381:step_04:reconstructed",
            occurred_at="2026-08-16T10:29:00Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "reconstructed",
                }
            ],
        )
        for event in (billable, estimated, reconstructed):
            self.assertEqual(
                ingest.ingest_usage_event(event).ingestion_outcome_code,
                IngestionOutcomeCode.ACCEPTED,
            )
        rating = UsageRatingService(ingest.ledger)
        result = rating.rate_usage_window(TENANT_ONE, MORNING_WINDOW, 1)
        self.assertEqual(result.rated_total_amount, Decimal("100") * TOKEN_UNIT_PRICE)
        self.assertEqual(result.rating_lines[0].rated_quantity, Decimal("100"))
        stored_qualities = {
            measurement.quality_code
            for event in ingest.ledger.usage_events.values()
            for measurement in event.measurements
        }
        self.assertEqual(stored_qualities, {"provider_reported", "estimated", "reconstructed"})

    def test_missing_quality_rule_is_excluded_from_invoice_intent(self) -> None:
        """A measurement without a live quality rule cannot enter invoice-intent money."""
        ingest = ingest_known_batch()
        meter = ingest.ledger.meter_definitions[0]
        del ingest.ledger.meter_quality_rules[(meter.meter_definition_id, "provider_reported")]
        result = UsageRatingService(ingest.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1
        )
        self.assertEqual(result.rating_outcome_code, RatingOutcomeCode.ACCEPTED)
        self.assertEqual(result.rated_total_amount, Decimal("0"))
        self.assertEqual(result.rating_lines, ())

    def test_unknown_billing_disposition_fails_closed(self) -> None:
        """An unrecognized disposition cannot silently become invoice-intent money."""
        ingest = ingest_known_batch()
        meter = ingest.ledger.meter_definitions[0]
        key = (meter.meter_definition_id, "provider_reported")
        ingest.ledger.meter_quality_rules[key] = replace(
            ingest.ledger.meter_quality_rules[key],
            billing_disposition_code="mystery",
        )
        result = UsageRatingService(ingest.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1
        )
        self.assertEqual(result.rating_outcome_code, RatingOutcomeCode.REJECTED)
        self.assertEqual(
            result.rejection_reason_code,
            RatingRejectionReasonCode.BILLING_DISPOSITION_UNKNOWN,
        )
        self.assertEqual(len(ingest.ledger.rating_runs), 0)

    def test_new_usage_in_the_same_window_opens_a_new_rating_run(self) -> None:
        """A changed usage snapshot is a new append-only run, not a rewrite."""
        ingest = ingest_known_batch()
        rating = UsageRatingService(ingest.ledger)
        first = rating.rate_usage_window(TENANT_ONE, MORNING_WINDOW, 1)
        extra = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf901",
            source_event_key="workflow_381:step_06:attempt_01",
            occurred_at="2026-08-16T10:29:30Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "10",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        self.assertEqual(
            ingest.ingest_usage_event(extra).ingestion_outcome_code,
            IngestionOutcomeCode.ACCEPTED,
        )
        second = rating.rate_usage_window(TENANT_ONE, MORNING_WINDOW, 1)
        self.assertEqual(second.rating_outcome_code, RatingOutcomeCode.ACCEPTED)
        self.assertNotEqual(second.rating_run_id, first.rating_run_id)
        self.assertNotEqual(second.usage_snapshot_hash, first.usage_snapshot_hash)
        self.assertEqual(
            second.rated_total_amount,
            (KNOWN_MORNING_QUANTITY + Decimal("10")) * TOKEN_UNIT_PRICE,
        )
        self.assertEqual(first.rated_total_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(len(ingest.ledger.rating_runs), 2)

    def test_empty_window_rates_to_zero_without_a_journal_or_provider(self) -> None:
        """No usage in the window is an accepted zero total, not a posted journal."""
        ingest = ingest_known_batch()
        rating = UsageRatingService(ingest.ledger)
        empty = TimeWindow.from_iso8601("2026-08-15T00:00:00Z", "2026-08-15T01:00:00Z")
        result = rating.rate_usage_window(TENANT_ONE, empty, 1)
        self.assertEqual(result.rating_outcome_code, RatingOutcomeCode.ACCEPTED)
        self.assertEqual(result.rated_total_amount, Decimal("0"))
        self.assertEqual(result.rating_lines, ())
        self.assertEqual(len(ingest.ledger.accounting_export_records), 0)
        self.assertIsNone(getattr(result, "proposal_status", None))
        self.assertEqual(validate_rating_run(result.as_contract_dict()), ())

    def test_unknown_tenant_and_rate_card_fail_closed(self) -> None:
        """Missing tenant or rate-card version cannot invent invoice-intent money."""
        ingest = ingest_known_batch()
        rating = UsageRatingService(ingest.ledger)
        missing_tenant = rating.rate_usage_window("urn:cwl:missing_tenant", MORNING_WINDOW, 1)
        self.assertEqual(missing_tenant.rating_outcome_code, RatingOutcomeCode.REJECTED)
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            RatingRejectionReasonCode.TENANT_NOT_FOUND,
        )
        self.assertIsNone(missing_tenant.rating_run_id)
        missing_card = rating.rate_usage_window(TENANT_ONE, MORNING_WINDOW, 9)
        self.assertEqual(
            missing_card.rejection_reason_code,
            RatingRejectionReasonCode.RATE_CARD_NOT_FOUND,
        )
        self.assertEqual(len(ingest.ledger.rating_runs), 0)

    def test_billable_meter_without_a_unit_price_fails_closed(self) -> None:
        """Invoice-intent rating cannot invent a price for a billable meter."""
        ledger = seed_rated_ledger()
        extra_meter = ledger.register_meter_definition(
            "workflow_step_count", 1, "request", "sum", CATALOG_START
        )
        ledger.register_meter_quality_rule(
            extra_meter.meter_definition_id, "provider_reported", "billable"
        )
        ingest = UsageIngestionService(ledger)
        event = make_event(
            measurements=[
                {
                    "meter_code": "workflow_step_count",
                    "quantity": "3",
                    "unit_code": "request",
                    "quality_code": "provider_reported",
                }
            ]
        )
        self.assertEqual(
            ingest.ingest_usage_event(event).ingestion_outcome_code,
            IngestionOutcomeCode.ACCEPTED,
        )
        result = UsageRatingService(ledger).rate_usage_window(TENANT_ONE, MORNING_WINDOW, 1)
        self.assertEqual(result.rating_outcome_code, RatingOutcomeCode.REJECTED)
        self.assertEqual(
            result.rejection_reason_code,
            RatingRejectionReasonCode.METER_PRICE_MISSING,
        )
        self.assertEqual(len(ledger.rating_runs), 0)

    def test_unknown_version_and_binary_float_prices_are_rejected(self) -> None:
        """Rating cannot invent a version, and float prices never enter the catalog."""
        ledger = seed_rated_ledger()
        ingest = ingest_known_batch(ledger)
        result = UsageRatingService(ingest.ledger).rate_usage_window(TENANT_ONE, MORNING_WINDOW, 2)
        self.assertEqual(
            result.rejection_reason_code,
            RatingRejectionReasonCode.RATE_CARD_NOT_FOUND,
        )
        with self.assertRaises(ExactDecimalError):
            from metering_billing.rate_card import parse_unit_amount

            parse_unit_amount(0.000003)

    def test_default_rating_service_and_zero_line_contract_shape(self) -> None:
        """The zero-argument service constructs a ledger and rejected results stay sparse."""
        empty = UsageRatingService()
        self.assertIsInstance(empty.ledger, MemoryUsageLedger)
        rejected = empty.rate_usage_window(TENANT_ONE, MORNING_WINDOW, 1)
        payload = rejected.as_contract_dict()
        self.assertEqual(payload["rating_outcome_code"], "rejected")
        self.assertNotIn("rating_run_id", payload)
        self.assertNotIn("rated_total_amount", payload)
        self.assertNotIn("rating_lines", payload)


class RatingCatalogAndContractTests(unittest.TestCase):
    """Cover rate-card catalog edges and rating-run contract semantics."""

    def test_rate_card_publish_replay_reuses_the_same_version(self) -> None:
        """The same tenant, name, line hash, and contract version reuse the version."""
        ledger = seed_rated_ledger()
        first = publish_standard_rate_card(ledger, TENANT_ONE)
        again = publish_standard_rate_card(ledger, TENANT_ONE)
        self.assertEqual(again.rate_card_outcome_code.value, "duplicate_replay")
        self.assertEqual(again.rate_card_id, first.rate_card_id)
        self.assertEqual(again.rate_card_version_id, first.rate_card_version_id)
        self.assertEqual(again.rate_card_version, 1)
        self.assertEqual(again.lines[0].unit_amount, TOKEN_UNIT_PRICE)
        self.assertEqual(parse_exact_decimal("0.000002"), TOKEN_UNIT_PRICE)
        self.assertEqual(format_exact_decimal(TOKEN_UNIT_PRICE), "0.000002")

    def test_rating_run_insert_is_immutable(self) -> None:
        """A second insert of the same rating identity cannot replace history."""
        ingest = ingest_known_batch()
        first = UsageRatingService(ingest.ledger).rate_usage_window(TENANT_ONE, MORNING_WINDOW, 1)
        stored = ingest.ledger.rating_runs[first.rating_run_id]
        with self.assertRaises(ValueError):
            ingest.ledger.insert_rating_run(stored, stored.rating_lines)
        colliding_identity = replace(stored, rating_run_id=generate_record_id())
        with self.assertRaises(ValueError):
            ingest.ledger.insert_rating_run(colliding_identity, colliding_identity.rating_lines)
        with self.assertRaises(KeyError):
            ingest.ledger.billing_account_reference_for(generate_record_id())

    def test_rating_run_semantics_require_identity_totals_and_reasons(self) -> None:
        """Accepted runs need identity and balancing lines; rejected runs need a reason."""
        valid = {
            "rating_contract_version": 1,
            "rating_outcome_code": "accepted",
            "rating_run_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "tenant_reference": TENANT_ONE,
            "rate_card_code": RATE_CARD_CODE,
            "rate_card_version": 1,
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "usage_snapshot_hash": "sha256:" + "d" * 64,
            "currency_code": "USD",
            "rated_total_amount": "0.003705",
            "rating_lines": [
                {
                    "line_number": 1,
                    "billing_account_reference": ACCOUNT_ONE,
                    "meter_code": "gen_ai_output_token",
                    "unit_code": "token",
                    "rated_quantity": "1852.5",
                    "unit_price_amount": "0.000002",
                    "line_total_amount": "0.003705",
                }
            ],
        }
        self.assertEqual(validate_rating_run(valid), ())
        missing_id = json.loads(json.dumps(valid))
        del missing_id["rating_run_id"]
        self.assertIn(
            "$: accepted rating runs must include rating_run_id",
            validate_rating_run(missing_id),
        )
        replay = json.loads(json.dumps(valid))
        replay["rating_outcome_code"] = "duplicate_replay"
        del replay["usage_snapshot_hash"]
        self.assertIn(
            "$: duplicate_replay rating runs must include usage_snapshot_hash",
            validate_rating_run(replay),
        )
        unbalanced = json.loads(json.dumps(valid))
        unbalanced["rating_lines"][0]["line_total_amount"] = "0.003704"
        self.assertIn(
            "$: rating line totals must equal rated_total_amount",
            validate_rating_run(unbalanced),
        )
        rejected = {
            "rating_contract_version": 1,
            "rating_outcome_code": "rejected",
            "rejection_reason_code": "tenant_not_found",
        }
        self.assertEqual(validate_rating_run(rejected), ())
        missing_reason = {"rating_contract_version": 1, "rating_outcome_code": "rejected"}
        self.assertIn(
            "$: rejected rating runs must include rejection_reason_code",
            validate_rating_run(missing_reason),
        )
        self.assertTrue(validate_rating_run({"rating_contract_version": 1}))
        self.assertTrue(validate_rating_run(["not-an-object"]))
        unknown = json.loads(json.dumps(valid))
        unknown["rating_outcome_code"] = "mystery"
        self.assertTrue(validate_rating_run(unknown))
        malformed_lines = json.loads(json.dumps(valid))
        malformed_lines["rating_lines"] = ["not-an-object"]
        self.assertTrue(validate_rating_run(malformed_lines))
        missing_amount = json.loads(json.dumps(valid))
        del missing_amount["rated_total_amount"]
        self.assertIn(
            "$: accepted rating runs must include rated_total_amount",
            validate_rating_run(missing_amount),
        )
        non_string_total = json.loads(json.dumps(valid))
        non_string_total["rated_total_amount"] = 1
        self.assertTrue(validate_rating_run(non_string_total))
        non_string_line = json.loads(json.dumps(valid))
        non_string_line["rating_lines"][0]["line_total_amount"] = 1
        self.assertTrue(validate_rating_run(non_string_line))
        missing_line_amount = json.loads(json.dumps(valid))
        del missing_line_amount["rating_lines"][0]["line_total_amount"]
        self.assertTrue(validate_rating_run(missing_line_amount))
        bogus = RatingRunResult(
            rating_outcome_code="mystery",  # type: ignore[arg-type]
            rating_contract_version=1,
            rating_run_id=None,
            tenant_reference=None,
            rate_card_code=None,
            rate_card_version=None,
            window_started_at=None,
            window_ended_at=None,
            usage_snapshot_hash=None,
            currency_code=None,
            rated_total_amount=None,
            rejection_reason_code=None,
            rating_lines=(),
        )
        with self.assertRaises(ValueError):
            bogus.as_contract_dict()
        rejected_without_reason = RatingRunResult(
            rating_outcome_code=RatingOutcomeCode.REJECTED,
            rating_contract_version=1,
            rating_run_id=None,
            tenant_reference=None,
            rate_card_code=None,
            rate_card_version=None,
            window_started_at=None,
            window_ended_at=None,
            usage_snapshot_hash=None,
            currency_code=None,
            rated_total_amount=None,
            rejection_reason_code=None,
            rating_lines=(),
        )
        self.assertEqual(
            rejected_without_reason.as_contract_dict()["rejection_reason_code"],
            "tenant_not_found",
        )


if __name__ == "__main__":
    unittest.main()
