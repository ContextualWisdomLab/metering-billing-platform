"""Realistic rate-card catalog tests for publish, replay, rating pin, and HTTP."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import UUID, uuid4

from metering_billing import (
    InvoiceDraftService,
    MemoryUsageLedger,
    RateCardService,
    UsageIngestionService,
    UsageRatingService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.contracts import validate_invoice_draft, validate_rate_card
from metering_billing.errors import (
    ExactDecimalError,
    RateCardOutcomeCode,
    RateCardQueryError,
    RateCardRejectionReasonCode,
    RatingOutcomeCode,
    RatingRejectionReasonCode,
)
from metering_billing.http_app import HttpRequestError, _dispatch_write, _parse_rate_card_version
from metering_billing.rate_card import RateCardListPage, RateCardResult, parse_unit_amount
from metering_billing.usage_ledger import (
    StoredRateCard,
    StoredRateCardLine,
    StoredRateCardVersion,
    generate_record_id,
)
from test_http_app import invoke_http
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import (
    KNOWN_MORNING_TOTAL,
    MORNING_WINDOW,
    RATE_CARD_CODE,
    STANDARD_RATE_CARD_LINES,
    TOKEN_UNIT_PRICE,
    ingest_known_batch,
    publish_standard_rate_card,
    seed_rated_ledger,
)


NEWER_UNIT_AMOUNT = Decimal("0.000005")


class RateCardCatalogTests(unittest.TestCase):
    """Verify published versions stay append-only and pin later rating."""

    def test_publish_then_rate_then_draft_equals_quantity_times_unit_amount(self) -> None:
        """A published card must price known usage as quantity times unit amount."""
        ingest = ingest_known_batch()
        published = publish_standard_rate_card(ingest.ledger, TENANT_ONE)
        self.assertEqual(published.rate_card_outcome_code, RateCardOutcomeCode.DUPLICATE_REPLAY)
        rating = UsageRatingService(ingest.ledger).rate_usage_window(TENANT_ONE, MORNING_WINDOW, 1)
        self.assertEqual(rating.rating_outcome_code, RatingOutcomeCode.ACCEPTED)
        self.assertEqual(rating.rated_total_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(rating.rate_card_version, 1)
        self.assertEqual(rating.rating_lines[0].unit_price_amount, TOKEN_UNIT_PRICE)
        draft = InvoiceDraftService(ingest.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        self.assertEqual(draft.drafted_total_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(validate_rate_card(published.as_contract_dict()), ())
        self.assertEqual(validate_invoice_draft(draft.as_contract_dict()), ())

    def test_second_publish_increments_version_and_old_ratings_pin_the_old_version(self) -> None:
        """A new line set is version 2; rating version 1 still uses the old price."""
        ingest = ingest_known_batch()
        first = publish_standard_rate_card(ingest.ledger, TENANT_ONE)
        second = RateCardService(ingest.ledger).publish_rate_card(
            TENANT_ONE,
            RATE_CARD_CODE,
            "USD",
            [{"metric_code": "gen_ai_output_token", "unit_amount": NEWER_UNIT_AMOUNT}],
        )
        self.assertEqual(first.rate_card_version, 1)
        self.assertEqual(second.rate_card_outcome_code, RateCardOutcomeCode.ACCEPTED)
        self.assertEqual(second.rate_card_id, first.rate_card_id)
        self.assertEqual(second.rate_card_version, 2)
        self.assertNotEqual(second.rate_card_version_id, first.rate_card_version_id)
        old = UsageRatingService(ingest.ledger).rate_usage_window(TENANT_ONE, MORNING_WINDOW, 1)
        new = UsageRatingService(ingest.ledger).rate_usage_window(TENANT_ONE, MORNING_WINDOW, 2)
        self.assertEqual(old.rate_card_version, 1)
        self.assertEqual(old.rated_total_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(new.rate_card_version, 2)
        self.assertEqual(new.rating_lines[0].unit_price_amount, NEWER_UNIT_AMOUNT)
        self.assertNotEqual(new.rating_run_id, old.rating_run_id)

    def test_same_publish_is_a_replay(self) -> None:
        """The same tenant, name, lines, hash, and contract version reuse IDs."""
        ledger = seed_rated_ledger()
        service = RateCardService(ledger)
        first = service.publish_rate_card(
            TENANT_ONE, RATE_CARD_CODE, "USD", STANDARD_RATE_CARD_LINES
        )
        second = service.publish_rate_card(
            TENANT_ONE, RATE_CARD_CODE, "USD", STANDARD_RATE_CARD_LINES
        )
        self.assertEqual(first.rate_card_outcome_code, RateCardOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(second.rate_card_outcome_code, RateCardOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(second.rate_card_id, first.rate_card_id)
        self.assertEqual(second.rate_card_version_id, first.rate_card_version_id)
        self.assertEqual(second.rate_card_version, 1)
        self.assertEqual(len(ledger.rate_card_versions), 2)

    def test_other_tenant_cannot_read_or_rate_the_first_card(self) -> None:
        """A tenant cannot GET or rate another tenant's published version."""
        ledger = seed_rated_ledger()
        first = publish_standard_rate_card(ledger, TENANT_ONE)
        other = RateCardService(ledger)
        with self.assertRaises(RateCardQueryError) as error:
            other.get_rate_card(TENANT_TWO, first.rate_card_id)
        self.assertEqual(error.exception.rejection_reason_code, "rate_card_not_found")
        with self.assertRaises(RateCardQueryError) as version_error:
            other.get_rate_card_version(TENANT_TWO, first.rate_card_version_id)
        self.assertEqual(version_error.exception.rejection_reason_code, "rate_card_not_found")
        missing = UsageRatingService(MemoryUsageLedger())
        missing.ledger.register_tenant(TENANT_TWO)
        rated = missing.rate_usage_window(TENANT_TWO, MORNING_WINDOW, 1)
        self.assertEqual(rated.rejection_reason_code, RatingRejectionReasonCode.RATE_CARD_NOT_FOUND)

    def test_fail_closed_inputs_and_unknown_rating_version(self) -> None:
        """Missing tenant, empty lines, floats, and unknown versions fail closed."""
        ledger = seed_rated_ledger()
        service = RateCardService(ledger)
        missing_tenant = service.publish_rate_card("", RATE_CARD_CODE, "USD", STANDARD_RATE_CARD_LINES)
        unknown_tenant = service.publish_rate_card(
            "urn:cwl:missing_tenant", RATE_CARD_CODE, "USD", STANDARD_RATE_CARD_LINES
        )
        empty_lines = service.publish_rate_card(TENANT_ONE, RATE_CARD_CODE, "USD", [])
        bad_name = service.publish_rate_card(TENANT_ONE, "standard", "USD", STANDARD_RATE_CARD_LINES)
        bad_currency = service.publish_rate_card(
            TENANT_ONE, RATE_CARD_CODE, "usd", STANDARD_RATE_CARD_LINES
        )
        zero = service.publish_rate_card(
            TENANT_ONE,
            RATE_CARD_CODE,
            "USD",
            [{"metric_code": "gen_ai_output_token", "unit_amount": Decimal("0")}],
        )
        negative = service.publish_rate_card(
            TENANT_ONE,
            RATE_CARD_CODE,
            "USD",
            [{"metric_code": "gen_ai_output_token", "unit_amount": Decimal("-1")}],
        )
        floated = service.publish_rate_card(
            TENANT_ONE,
            RATE_CARD_CODE,
            "USD",
            [{"metric_code": "gen_ai_output_token", "unit_amount": 0.000002}],
        )
        mismatch = service.publish_rate_card(
            TENANT_ONE,
            RATE_CARD_CODE,
            "USD",
            [
                {
                    "metric_code": "gen_ai_output_token",
                    "unit_amount": TOKEN_UNIT_PRICE,
                    "currency_code": "EUR",
                }
            ],
        )
        unknown_metric = service.publish_rate_card(
            TENANT_ONE,
            RATE_CARD_CODE,
            "USD",
            [{"metric_code": "tokens", "unit_amount": TOKEN_UNIT_PRICE}],
        )
        self.assertEqual(missing_tenant.rejection_reason_code, RateCardRejectionReasonCode.TENANT_NOT_FOUND)
        self.assertEqual(unknown_tenant.rejection_reason_code, RateCardRejectionReasonCode.TENANT_NOT_FOUND)
        self.assertEqual(empty_lines.rejection_reason_code, RateCardRejectionReasonCode.RATE_CARD_LINES_INVALID)
        self.assertEqual(bad_name.rejection_reason_code, RateCardRejectionReasonCode.RATE_CARD_NAME_INVALID)
        self.assertEqual(bad_currency.rejection_reason_code, RateCardRejectionReasonCode.CURRENCY_CODE_INVALID)
        self.assertEqual(zero.rejection_reason_code, RateCardRejectionReasonCode.UNIT_AMOUNT_INVALID)
        self.assertEqual(negative.rejection_reason_code, RateCardRejectionReasonCode.UNIT_AMOUNT_INVALID)
        self.assertEqual(floated.rejection_reason_code, RateCardRejectionReasonCode.UNIT_AMOUNT_INVALID)
        self.assertEqual(mismatch.rejection_reason_code, RateCardRejectionReasonCode.CURRENCY_MISMATCH)
        self.assertEqual(unknown_metric.rejection_reason_code, RateCardRejectionReasonCode.METRIC_CODE_INVALID)
        unknown_version = UsageRatingService(ledger).rate_usage_window(TENANT_ONE, MORNING_WINDOW, 9)
        self.assertEqual(unknown_version.rejection_reason_code, RatingRejectionReasonCode.RATE_CARD_NOT_FOUND)
        with self.assertRaises(ExactDecimalError):
            parse_unit_amount(0.25)

    def test_http_publish_list_and_get_are_tenant_scoped(self) -> None:
        """Operators POST a card; GET list and version stay on the same tenant."""
        ledger = seed_rated_ledger()
        app = create_http_app(ledger)
        number_status, number_body = invoke_http(
            app,
            "GET",
            "/v1/rate-card-versions/1",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(number_status, 200)
        self.assertEqual(number_body["rate_card_version"], 1)
        status, body = invoke_http(
            app,
            "POST",
            "/v1/rate-cards",
            {
                "tenant_reference": TENANT_ONE,
                "rate_card_name": "workflow_standard",
                "currency_code": "USD",
                "lines": [
                    {
                        "metric_code": "gen_ai_output_token",
                        "unit_amount": format_exact_decimal(TOKEN_UNIT_PRICE),
                    }
                ],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["rate_card_outcome_code"], "accepted")
        self.assertEqual(body["rate_card_version"], 1)
        self.assertEqual(validate_rate_card(body), ())
        rate_card_id = body["rate_card_id"]
        rate_card_version_id = body["rate_card_version_id"]

        replay_status, replay_body = invoke_http(
            app,
            "POST",
            "/v1/rate-cards",
            {
                "rate_card_name": "workflow_standard",
                "currency_code": "USD",
                "lines": [
                    {
                        "metric_code": "gen_ai_output_token",
                        "unit_amount": format_exact_decimal(TOKEN_UNIT_PRICE),
                    }
                ],
            },
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["rate_card_outcome_code"], "duplicate_replay")
        self.assertEqual(replay_body["rate_card_id"], rate_card_id)

        list_status, list_body = invoke_http(
            app, "GET", "/v1/rate-cards", query={"tenant_reference": TENANT_ONE}
        )
        self.assertEqual(list_status, 200)
        names = [item["rate_card_name"] for item in list_body["rate_cards"]]
        self.assertIn("workflow_standard", names)

        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/rate-cards/{rate_card_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["rate_card_id"], rate_card_id)

        versions_status, versions_body = invoke_http(
            app,
            "GET",
            f"/v1/rate-cards/{rate_card_id}/versions",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(versions_status, 200)
        self.assertEqual(len(versions_body["rate_card_versions"]), 1)

        version_status, version_body = invoke_http(
            app,
            "GET",
            f"/v1/rate-card-versions/{rate_card_version_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(version_status, 200)
        self.assertEqual(version_body["rate_card_version_id"], rate_card_version_id)

        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/rate-cards/{rate_card_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "rate_card_not_found")

        missing_status, missing_body = invoke_http(
            app,
            "POST",
            "/v1/rate-cards",
            {"rate_card_name": "workflow_standard", "currency_code": "USD", "lines": []},
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")

        pin_status, pin_body = invoke_http(
            app,
            "POST",
            "/v1/rate-cards",
            {
                "tenant_reference": TENANT_ONE,
                "rate_card_name": "workflow_standard",
                "currency_code": "USD",
                "lines": [
                    {
                        "metric_code": "gen_ai_output_token",
                        "unit_amount": format_exact_decimal(TOKEN_UNIT_PRICE),
                    }
                ],
            },
            headers={"X-CWL-Tenant-Reference": TENANT_TWO},
        )
        self.assertEqual(pin_status, 422)
        self.assertEqual(pin_body["rejection_reason_code"], "request_invalid")

        get_missing_tenant, get_missing_body = invoke_http(
            app, "GET", f"/v1/rate-cards/{rate_card_id}"
        )
        self.assertEqual(get_missing_tenant, 422)
        self.assertEqual(get_missing_body["rejection_reason_code"], "tenant_not_found")

        method_status, method_body = invoke_http(app, "PUT", "/v1/rate-cards")
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")

        post_item_status, post_item_body = invoke_http(
            app, "POST", f"/v1/rate-cards/{rate_card_id}", {"tenant_reference": TENANT_ONE}
        )
        self.assertEqual(post_item_status, 422)
        self.assertEqual(post_item_body["rejection_reason_code"], "request_invalid")

    def test_result_helpers_and_ledger_identity_are_append_only(self) -> None:
        """Helpers fail closed and the ledger rejects conflicting catalog identities."""
        rejected = RateCardResult(
            rate_card_outcome_code=RateCardOutcomeCode.REJECTED,
            rate_card_contract_version=1,
            rate_card_id=None,
            rate_card_version_id=None,
            tenant_reference=None,
            rate_card_name=None,
            rate_card_version=None,
            currency_code=None,
            source_payload_hash=None,
            published_at=None,
            next_operator_action="Publish a rate card, then rate a window against that version.",
            rejection_reason_code=None,
            lines=(),
        )
        rejected_body = rejected.as_contract_dict()
        self.assertEqual(rejected_body["rate_card_outcome_code"], "rejected")
        self.assertEqual(rejected_body["rejection_reason_code"], "rate_card_not_found")
        with self.assertRaises(ValueError):
            RateCardResult(
                rate_card_outcome_code="nope",  # type: ignore[arg-type]
                rate_card_contract_version=1,
                rate_card_id=None,
                rate_card_version_id=None,
                tenant_reference=None,
                rate_card_name=None,
                rate_card_version=None,
                currency_code=None,
                source_payload_hash=None,
                published_at=None,
                next_operator_action="Publish a rate card, then rate a window against that version.",
                rejection_reason_code=None,
                lines=(),
            ).as_contract_dict()
        with self.assertRaises(ValueError):
            RateCardResult(
                rate_card_outcome_code=RateCardOutcomeCode.ACCEPTED,
                rate_card_contract_version=1,
                rate_card_id=None,
                rate_card_version_id=None,
                tenant_reference=None,
                rate_card_name=None,
                rate_card_version=None,
                currency_code=None,
                source_payload_hash=None,
                published_at=None,
                next_operator_action="Publish a rate card, then rate a window against that version.",
                rejection_reason_code=None,
                lines=(),
            ).as_contract_dict()

        ledger = seed_rated_ledger()
        tenant = ledger.require_tenant(TENANT_ONE)
        first = next(iter(ledger.list_rate_cards(tenant.tenant_account_id)))
        replay = ledger.insert_rate_card(first)
        self.assertEqual(replay.rate_card_id, first.rate_card_id)
        with self.assertRaises(ValueError):
            ledger.insert_rate_card(replace(first, currency_code="EUR"))
        with self.assertRaises(ValueError):
            ledger.insert_rate_card(
                StoredRateCard(
                    rate_card_id=first.rate_card_id,
                    tenant_account_id=tenant.tenant_account_id,
                    rate_card_name="other_card",
                    currency_code="USD",
                    created_at=datetime(2026, 8, 17, 20, 0, tzinfo=UTC),
                )
            )
        version = ledger.list_rate_card_versions(tenant.tenant_account_id, first.rate_card_id)[0]
        again = ledger.insert_rate_card_version(version)
        self.assertEqual(again.rate_card_version_id, version.rate_card_version_id)
        with self.assertRaises(ValueError):
            ledger.insert_rate_card_version(replace(version, version_number=0))
        with self.assertRaises(ValueError):
            ledger.insert_rate_card_version(replace(version, rate_card_lines=()))
        with self.assertRaises(ValueError):
            ledger.insert_rate_card_version(
                replace(
                    version,
                    rate_card_version_id=generate_record_id(),
                    source_payload_hash="sha256:" + ("f" * 64),
                    rate_card_lines=(
                        replace(version.rate_card_lines[0], unit_amount=Decimal("0")),
                    ),
                )
            )
        with self.assertRaises(ValueError):
            ledger.insert_rate_card_version(
                replace(
                    version,
                    rate_card_version_id=generate_record_id(),
                    source_payload_hash="sha256:" + ("a" * 64),
                    rate_card_lines=(
                        replace(version.rate_card_lines[0], currency_code="EUR"),
                    ),
                )
            )
        with self.assertRaises(ValueError):
            ledger.insert_rate_card_version(
                replace(
                    version,
                    source_payload_hash="sha256:" + ("b" * 64),
                )
            )
        self.assertIsNone(
            ledger.find_rate_card_version(tenant.tenant_account_id, 9, RATE_CARD_CODE)
        )
        self.assertIsNone(ledger.find_rate_card_line(uuid4(), "gen_ai_output_token"))
        self.assertEqual(
            ledger.find_rate_card_line(version.rate_card_version_id, "gen_ai_output_token"),
            version.rate_card_lines[0],
        )
        self.assertIsNone(ledger.find_rate_card_line(version.rate_card_version_id, "missing_metric"))
        with self.assertRaises(RateCardQueryError) as missing_key:
            RateCardService(ledger).get_rate_card(TENANT_ONE, None)  # type: ignore[arg-type]
        self.assertEqual(missing_key.exception.rejection_reason_code, "rate_card_not_found")
        with self.assertRaises(RateCardQueryError) as missing_tenant:
            RateCardService(ledger).get_rate_card("", first.rate_card_id)
        self.assertEqual(missing_tenant.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(RateCardQueryError) as unknown_tenant:
            RateCardService(ledger).get_rate_card("urn:cwl:missing_tenant", first.rate_card_id)
        self.assertEqual(unknown_tenant.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(RateCardQueryError):
            RateCardService(ledger).list_rate_card_versions(TENANT_ONE, None)  # type: ignore[arg-type]
        with self.assertRaises(RateCardQueryError):
            RateCardService(ledger).get_rate_card_version(TENANT_ONE, True)  # type: ignore[arg-type]
        self.assertTrue(validate_rate_card(["not a mapping"]))
        self.assertTrue(validate_rate_card({"rate_card_contract_version": 1}))
        self.assertEqual(
            validate_rate_card(
                {
                    "rate_card_contract_version": 1,
                    "rate_card_outcome_code": "rejected",
                    "rejection_reason_code": "tenant_not_found",
                }
            ),
            (),
        )
        self.assertTrue(
            any(
                "rate_card_id" in error
                for error in validate_rate_card(
                    {
                        "rate_card_contract_version": 1,
                        "rate_card_outcome_code": "accepted",
                    }
                )
            )
        )
        with self.assertRaises(HttpRequestError) as missing_catalog:
            _dispatch_write(
                "rate_cards",
                {},
                TENANT_ONE,
                {
                    "rate_card_name": RATE_CARD_CODE,
                    "currency_code": "USD",
                    "lines": STANDARD_RATE_CARD_LINES,
                },
                UsageIngestionService(ledger),
                UsageRatingService(ledger),
                InvoiceDraftService(ledger),
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
            )
        self.assertEqual(missing_catalog.exception.rejection_reason_code, "request_invalid")
        with mock.patch(
            "metering_billing.http_app.RateCardService.get_rate_card",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                f"/v1/rate-cards/{first.rate_card_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")

    def test_catalog_edges_cover_unique_versions_and_fail_closed_reads(self) -> None:
        """Header-only cards, colliding versions, and HTTP gaps fail closed."""
        self.assertEqual(parse_unit_amount(Decimal("0.25")), Decimal("0.25"))
        self.assertEqual(parse_unit_amount("0.25"), Decimal("0.25"))
        empty_service = RateCardService()
        self.assertIsInstance(empty_service.ledger, MemoryUsageLedger)
        self.assertEqual(
            RateCardListPage(tenant_reference=TENANT_ONE, rate_cards=()).as_contract_dict(),
            {"tenant_reference": TENANT_ONE, "rate_cards": []},
        )
        self.assertTrue(
            validate_rate_card(
                {
                    "rate_card_contract_version": 1,
                    "rate_card_outcome_code": "rejected",
                }
            )
        )
        with self.assertRaises(HttpRequestError):
            _parse_rate_card_version("")
        with self.assertRaises(HttpRequestError):
            _parse_rate_card_version(1)
        with self.assertRaises(HttpRequestError):
            _parse_rate_card_version("ffffffffffffffffffffffffffffffffffffff")

        ledger = seed_rated_ledger()
        service = RateCardService(ledger)
        tenant = ledger.require_tenant(TENANT_ONE)
        header_only = ledger.insert_rate_card(
            StoredRateCard(
                rate_card_id=generate_record_id(),
                tenant_account_id=tenant.tenant_account_id,
                rate_card_name="header_only_card",
                currency_code="USD",
                created_at=datetime(2026, 8, 17, 20, 0, tzinfo=UTC),
            )
        )
        listed = service.list_rate_cards(TENANT_ONE)
        self.assertIn(
            None,
            [item["latest_rate_card_version"] for item in listed.rate_cards],
        )
        with self.assertRaises(RateCardQueryError):
            service.get_rate_card(TENANT_ONE, header_only.rate_card_id)
        with self.assertRaises(RateCardQueryError):
            service.get_rate_card(TENANT_ONE, uuid4())
        with self.assertRaises(RateCardQueryError):
            service.list_rate_card_versions(TENANT_ONE, uuid4())
        self.assertEqual(
            ledger.list_rate_card_versions(tenant.tenant_account_id, None)[0].version_number,
            1,
        )
        self.assertIsNone(
            ledger.find_rate_card_version(tenant.tenant_account_id, 1, "missing_card_name")
        )
        self.assertIsNone(
            ledger.find_rate_card_version(tenant.tenant_account_id, 9, RATE_CARD_CODE)
        )
        first = next(iter(ledger.list_rate_cards(tenant.tenant_account_id)))
        version = ledger.list_rate_card_versions(tenant.tenant_account_id, first.rate_card_id)[0]
        with self.assertRaises(ValueError):
            ledger.insert_rate_card_version(
                replace(
                    version,
                    rate_card_version_id=generate_record_id(),
                    source_payload_hash="sha256:" + ("c" * 64),
                    rate_card_lines=(
                        version.rate_card_lines[0],
                        replace(
                            version.rate_card_lines[0],
                            rate_card_line_id=generate_record_id(),
                        ),
                    ),
                )
            )
        with self.assertRaises(ValueError):
            ledger.insert_rate_card_version(
                replace(
                    version,
                    source_payload_hash="sha256:" + ("d" * 64),
                    rate_card_lines=(
                        replace(version.rate_card_lines[0], metric_code="other_metric"),
                    ),
                )
            )
        orphan = ledger.insert_rate_card_version(
            StoredRateCardVersion(
                rate_card_version_id=generate_record_id(),
                tenant_account_id=tenant.tenant_account_id,
                rate_card_id=uuid4(),
                version_number=7,
                rate_card_contract_version=1,
                currency_code="USD",
                source_payload_hash="sha256:" + ("e" * 64),
                published_at=datetime(2026, 8, 17, 20, 1, tzinfo=UTC),
                rate_card_lines=(
                    StoredRateCardLine(
                        rate_card_line_id=generate_record_id(),
                        tenant_account_id=tenant.tenant_account_id,
                        rate_card_version_id=generate_record_id(),
                        metric_code="gen_ai_output_token",
                        unit_amount=TOKEN_UNIT_PRICE,
                        currency_code="USD",
                    ),
                ),
            )
        )
        with self.assertRaises(RateCardQueryError):
            service.get_rate_card_version(TENANT_ONE, orphan.rate_card_version_id)
        other_tenant = ledger.require_tenant(TENANT_TWO)
        foreign_version = ledger.insert_rate_card_version(
            StoredRateCardVersion(
                rate_card_version_id=generate_record_id(),
                tenant_account_id=other_tenant.tenant_account_id,
                rate_card_id=first.rate_card_id,
                version_number=9,
                rate_card_contract_version=1,
                currency_code="USD",
                source_payload_hash="sha256:" + ("1" * 64),
                published_at=datetime(2026, 8, 17, 20, 2, tzinfo=UTC),
                rate_card_lines=(
                    StoredRateCardLine(
                        rate_card_line_id=generate_record_id(),
                        tenant_account_id=other_tenant.tenant_account_id,
                        rate_card_version_id=generate_record_id(),
                        metric_code="gen_ai_output_token",
                        unit_amount=TOKEN_UNIT_PRICE,
                        currency_code="USD",
                    ),
                ),
            )
        )
        with self.assertRaises(RateCardQueryError):
            service.get_rate_card_version(TENANT_TWO, foreign_version.rate_card_version_id)
        currency_change = service.publish_rate_card(
            TENANT_ONE, RATE_CARD_CODE, "EUR", STANDARD_RATE_CARD_LINES
        )
        self.assertEqual(
            currency_change.rejection_reason_code,
            RateCardRejectionReasonCode.CURRENCY_MISMATCH,
        )
        duplicate_metrics = service.publish_rate_card(
            TENANT_ONE,
            "workflow_catalog",
            "USD",
            [
                {"metric_code": "gen_ai_output_token", "unit_amount": TOKEN_UNIT_PRICE},
                {"metric_code": "gen_ai_output_token", "unit_amount": Decimal("0.01")},
            ],
        )
        self.assertEqual(
            duplicate_metrics.rejection_reason_code,
            RateCardRejectionReasonCode.RATE_CARD_LINES_INVALID,
        )
        not_a_mapping = service.publish_rate_card(
            TENANT_ONE, "workflow_catalog", "USD", ["not-a-line"]
        )
        self.assertEqual(
            not_a_mapping.rejection_reason_code,
            RateCardRejectionReasonCode.RATE_CARD_LINES_INVALID,
        )
        string_lines = service.publish_rate_card(TENANT_ONE, "workflow_catalog", "USD", "lines")
        self.assertEqual(
            string_lines.rejection_reason_code,
            RateCardRejectionReasonCode.RATE_CARD_LINES_INVALID,
        )
        bool_amount = service.publish_rate_card(
            TENANT_ONE,
            "workflow_catalog",
            "USD",
            [{"metric_code": "gen_ai_output_token", "unit_amount": True}],
        )
        self.assertEqual(
            bool_amount.rejection_reason_code,
            RateCardRejectionReasonCode.UNIT_AMOUNT_INVALID,
        )
        non_string_currency = service.publish_rate_card(
            TENANT_ONE,
            "workflow_catalog",
            "USD",
            [
                {
                    "metric_code": "gen_ai_output_token",
                    "unit_amount": TOKEN_UNIT_PRICE,
                    "currency_code": 1,
                }
            ],
        )
        self.assertEqual(
            non_string_currency.rejection_reason_code,
            RateCardRejectionReasonCode.CURRENCY_MISMATCH,
        )
        bool_version = UsageRatingService(ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, True  # type: ignore[arg-type]
        )
        self.assertEqual(
            bool_version.rejection_reason_code,
            RatingRejectionReasonCode.RATE_CARD_NOT_FOUND,
        )
        second_card = service.publish_rate_card(
            TENANT_ONE,
            "workflow_standard",
            "USD",
            [{"metric_code": "gen_ai_output_token", "unit_amount": TOKEN_UNIT_PRICE}],
        )
        self.assertEqual(second_card.rate_card_outcome_code, RateCardOutcomeCode.ACCEPTED)
        self.assertIsNone(ledger.find_rate_card_version(tenant.tenant_account_id, 1))
        named = UsageRatingService(ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code=RATE_CARD_CODE
        )
        self.assertEqual(named.rating_outcome_code, RatingOutcomeCode.ACCEPTED)
        fallback = UsageRatingService(ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="missing_card_name"
        )
        self.assertEqual(
            fallback.rejection_reason_code,
            RatingRejectionReasonCode.RATE_CARD_NOT_FOUND,
        )
        orphan_rate = UsageRatingService(ledger).rate_usage_window(TENANT_ONE, MORNING_WINDOW, 7)
        self.assertEqual(
            orphan_rate.rejection_reason_code,
            RatingRejectionReasonCode.RATE_CARD_NOT_FOUND,
        )
        foreign_rate = UsageRatingService(ledger).rate_usage_window(TENANT_TWO, MORNING_WINDOW, 9)
        self.assertEqual(
            foreign_rate.rejection_reason_code,
            RatingRejectionReasonCode.RATE_CARD_NOT_FOUND,
        )

        app = create_http_app(ledger)
        missing_name_status, missing_name_body = invoke_http(
            app,
            "POST",
            "/v1/rate-cards",
            {"tenant_reference": TENANT_ONE, "currency_code": "USD", "lines": []},
        )
        self.assertEqual(missing_name_status, 422)
        self.assertEqual(missing_name_body["rejection_reason_code"], "request_invalid")
        versions_404_status, versions_404_body = invoke_http(
            app,
            "GET",
            f"/v1/rate-cards/{uuid4()}/versions",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(versions_404_status, 404)
        self.assertEqual(versions_404_body["rejection_reason_code"], "rate_card_not_found")
        version_404_status, version_404_body = invoke_http(
            app,
            "GET",
            f"/v1/rate-card-versions/{uuid4()}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(version_404_status, 404)
        self.assertEqual(version_404_body["rejection_reason_code"], "rate_card_not_found")
        ambiguous_status, ambiguous_body = invoke_http(
            app,
            "GET",
            "/v1/rate-card-versions/1",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(ambiguous_status, 404)
        self.assertEqual(ambiguous_body["rejection_reason_code"], "rate_card_not_found")
        list_missing_status, list_missing_body = invoke_http(app, "GET", "/v1/rate-cards")
        self.assertEqual(list_missing_status, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "tenant_not_found")
        method_status, method_body = invoke_http(
            app, "PUT", f"/v1/rate-cards/{first.rate_card_id}/versions"
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        invalid_uuid_status, invalid_uuid_body = invoke_http(
            app,
            "GET",
            "/v1/rate-card-versions/ffffffffffffffffffffffffffffffffffffff",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(invalid_uuid_status, 422)
        self.assertEqual(invalid_uuid_body["rejection_reason_code"], "request_invalid")


if __name__ == "__main__":
    unittest.main()
