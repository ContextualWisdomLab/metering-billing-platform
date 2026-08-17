"""Realistic collection-case tests for exact outstanding, replay, and dunning."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing import (
    CollectionCaseService,
    InvoiceDraftService,
    MemoryUsageLedger,
    TimeWindow,
    UsageIngestionService,
    UsageRatingService,
    format_exact_decimal,
)
from metering_billing.collection_case import CollectionCaseResult, parse_collection_amount
from metering_billing.contracts import validate_collection_case
from metering_billing.errors import (
    CollectionCaseOutcomeCode,
    CollectionCaseRejectionReasonCode,
    ExactDecimalError,
)
from metering_billing.usage_ledger import generate_record_id
from test_usage_ingestion import ACCOUNT_TWO, TENANT_ONE, TENANT_TWO
from test_usage_rating import (
    KNOWN_MORNING_TOTAL,
    MORNING_WINDOW,
    ingest_known_batch,
    make_event,
)


def draft_known_morning(
    clock: datetime | None = None,
) -> tuple[MemoryUsageLedger, UUID]:
    """Ingest known usage, rate the morning window, and persist one invoice draft."""
    ingest = ingest_known_batch()
    rating = UsageRatingService(ingest.ledger).rate_usage_window(TENANT_ONE, MORNING_WINDOW, 1)
    drafter = (
        InvoiceDraftService(ingest.ledger)
        if clock is None
        else InvoiceDraftService(ingest.ledger, clock=lambda: clock)
    )
    draft = drafter.draft_invoice(TENANT_ONE, rating.rating_run_id)
    if draft.invoice_draft_id is None:
        raise AssertionError("known morning path must persist an invoice draft")
    return ingest.ledger, draft.invoice_draft_id


class CollectionCaseTests(unittest.TestCase):
    """Verify collection cases copy draft totals without capturing payment."""

    def test_known_invoice_draft_opens_exact_outstanding_case(self) -> None:
        """A known draft total must become the collection-case outstanding amount."""
        ledger, invoice_draft_id = draft_known_morning()
        result = CollectionCaseService(ledger).open_collection_case(TENANT_ONE, invoice_draft_id)
        self.assertEqual(result.collection_case_outcome_code, CollectionCaseOutcomeCode.ACCEPTED)
        self.assertIsInstance(result.collection_case_id, UUID)
        self.assertEqual(result.collection_case_status, "open")
        self.assertEqual(result.outstanding_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(result.currency_code, "USD")
        self.assertEqual(result.invoice_draft_id, invoice_draft_id)
        self.assertNotIsInstance(result.outstanding_amount, float)
        self.assertEqual(result.dunning_events, ())
        self.assertEqual(validate_collection_case(result.as_contract_dict()), ())
        self.assertNotIn(result.collection_case_status, {"paid", "written_off", "posted"})
        self.assertEqual(len(ledger.collection_cases), 1)
        self.assertEqual(len(ledger.collection_dunning_events), 0)
        self.assertEqual(len(ledger.accounting_export_records), 0)

    def test_second_open_of_the_same_draft_is_a_replay(self) -> None:
        """The same tenant and invoice_draft_id reuse collection_case_id."""
        ledger, invoice_draft_id = draft_known_morning()
        service = CollectionCaseService(ledger)
        first = service.open_collection_case(TENANT_ONE, invoice_draft_id)
        second = service.open_collection_case(TENANT_ONE, invoice_draft_id)
        self.assertEqual(first.collection_case_outcome_code, CollectionCaseOutcomeCode.ACCEPTED)
        self.assertEqual(second.collection_case_outcome_code, CollectionCaseOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(second.collection_case_id, first.collection_case_id)
        self.assertEqual(second.outstanding_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(len(ledger.collection_cases), 1)

    def test_other_tenant_cannot_see_or_collect_the_first_case(self) -> None:
        """A tenant cannot open or list another tenant's collection case."""
        ledger, one_draft_id = draft_known_morning()
        foreign = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf701",
            source_event_key="tenant_two:step_01",
            tenant_reference=TENANT_TWO,
            billing_account_reference=ACCOUNT_TWO,
            billing_principal_reference="urn:cwl:tenant_002:billing_principal:019d8002",
            credential_reference="urn:cwl:tenant_002:credential_record:019d8003",
            occurred_at="2026-08-16T10:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "10",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(ledger).ingest_usage_event(foreign)
        two_rate = UsageRatingService(ledger).rate_usage_window(TENANT_TWO, MORNING_WINDOW, 1)
        two_draft = InvoiceDraftService(ledger).draft_invoice(TENANT_TWO, two_rate.rating_run_id)
        service = CollectionCaseService(ledger)
        one_case = service.open_collection_case(TENANT_ONE, one_draft_id)
        two_case = service.open_collection_case(TENANT_TWO, two_draft.invoice_draft_id)
        crossed = service.open_collection_case(TENANT_TWO, one_draft_id)
        self.assertEqual(one_case.outstanding_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(two_case.outstanding_amount, Decimal("10") * Decimal("0.000002"))
        self.assertNotEqual(one_case.collection_case_id, two_case.collection_case_id)
        self.assertEqual(crossed.collection_case_outcome_code, CollectionCaseOutcomeCode.REJECTED)
        self.assertEqual(
            crossed.rejection_reason_code,
            CollectionCaseRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND,
        )
        crossed_dunning = service.record_dunning_event(
            TENANT_TWO, one_case.collection_case_id, "first_notice"
        )
        self.assertEqual(
            crossed_dunning.rejection_reason_code,
            CollectionCaseRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )
        one_rows = ledger.list_collection_cases(ledger.require_tenant(TENANT_ONE).tenant_account_id)
        two_rows = ledger.list_collection_cases(ledger.require_tenant(TENANT_TWO).tenant_account_id)
        self.assertEqual(len(one_rows), 1)
        self.assertEqual(one_rows[0].collection_case_id, one_case.collection_case_id)
        self.assertEqual(len(two_rows), 1)

    def test_dunning_notice_appends_without_capturing_money(self) -> None:
        """A first notice then an overdue notice append commercial reminders only."""
        ledger, invoice_draft_id = draft_known_morning()
        service = CollectionCaseService(ledger)
        opened = service.open_collection_case(TENANT_ONE, invoice_draft_id)
        first = service.record_dunning_event(TENANT_ONE, opened.collection_case_id, "first_notice")
        overdue = service.record_dunning_event(TENANT_ONE, opened.collection_case_id, "overdue_notice")
        replay = service.record_dunning_event(TENANT_ONE, opened.collection_case_id, "first_notice")
        self.assertEqual(first.collection_case_outcome_code, CollectionCaseOutcomeCode.ACCEPTED)
        self.assertEqual(first.collection_case_status, "dunning")
        self.assertEqual(first.outstanding_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(len(first.dunning_events), 1)
        self.assertEqual(first.dunning_events[0].dunning_notice_code, "first_notice")
        self.assertEqual(overdue.collection_case_status, "dunning")
        self.assertEqual(
            [event.dunning_notice_code for event in overdue.dunning_events],
            ["first_notice", "overdue_notice"],
        )
        self.assertEqual(replay.collection_case_outcome_code, CollectionCaseOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(replay.dunning_events[0].dunning_event_id, first.dunning_events[0].dunning_event_id)
        self.assertEqual(len(ledger.collection_dunning_events), 2)
        self.assertEqual(overdue.outstanding_amount, opened.outstanding_amount)
        self.assertEqual(validate_collection_case(overdue.as_contract_dict()), ())
        self.assertEqual(len(ledger.collection_cases), 1)

    def test_missing_draft_case_and_tenant_fail_closed(self) -> None:
        """A case cannot invent outstanding without a stored tenant invoice draft."""
        ledger, _invoice_draft_id = draft_known_morning()
        service = CollectionCaseService(ledger)
        missing_draft = service.open_collection_case(TENANT_ONE, generate_record_id())
        self.assertEqual(
            missing_draft.collection_case_outcome_code,
            CollectionCaseOutcomeCode.REJECTED,
        )
        self.assertEqual(
            missing_draft.rejection_reason_code,
            CollectionCaseRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND,
        )
        missing_tenant = service.open_collection_case("urn:cwl:missing_tenant", generate_record_id())
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            CollectionCaseRejectionReasonCode.TENANT_NOT_FOUND,
        )
        missing_case = service.record_dunning_event(TENANT_ONE, generate_record_id(), "first_notice")
        self.assertEqual(
            missing_case.rejection_reason_code,
            CollectionCaseRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )
        missing_dunning_tenant = service.record_dunning_event(
            "urn:cwl:missing_tenant", generate_record_id(), "first_notice"
        )
        self.assertEqual(
            missing_dunning_tenant.rejection_reason_code,
            CollectionCaseRejectionReasonCode.TENANT_NOT_FOUND,
        )
        invalid_notice = service.record_dunning_event(
            TENANT_ONE, generate_record_id(), "mystery_notice"
        )
        self.assertEqual(
            invalid_notice.rejection_reason_code,
            CollectionCaseRejectionReasonCode.DUNNING_NOTICE_INVALID,
        )
        self.assertEqual(len(ledger.collection_cases), 0)

    def test_zero_outstanding_fails_closed(self) -> None:
        """A zero invoice-intent total cannot open a collection case."""
        ingest = ingest_known_batch()
        empty = TimeWindow.from_iso8601("2026-08-15T00:00:00Z", "2026-08-15T01:00:00Z")
        rating = UsageRatingService(ingest.ledger).rate_usage_window(TENANT_ONE, empty, 1)
        draft = InvoiceDraftService(ingest.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        self.assertEqual(draft.drafted_total_amount, Decimal("0"))
        rejected = CollectionCaseService(ingest.ledger).open_collection_case(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(
            rejected.rejection_reason_code,
            CollectionCaseRejectionReasonCode.OUTSTANDING_AMOUNT_INVALID,
        )
        self.assertEqual(len(ingest.ledger.collection_cases), 0)

    def test_binary_float_money_is_rejected_at_the_collection_boundary(self) -> None:
        """Outstanding amounts must be exact decimals, never IEEE binary floats."""
        with self.assertRaises(ExactDecimalError):
            parse_collection_amount(0.003705)
        self.assertEqual(parse_collection_amount("0.003705"), Decimal("0.003705"))
        self.assertEqual(parse_collection_amount(Decimal("0.003705")), Decimal("0.003705"))
        self.assertEqual(format_exact_decimal(parse_collection_amount("0.003705")), "0.003705")

    def test_default_service_and_rejected_contract_stay_sparse(self) -> None:
        """The zero-argument service constructs a ledger and rejected cases omit money."""
        empty = CollectionCaseService()
        self.assertIsInstance(empty.ledger, MemoryUsageLedger)
        rejected = empty.open_collection_case(TENANT_ONE, generate_record_id())
        payload = rejected.as_contract_dict()
        self.assertEqual(payload["collection_case_outcome_code"], "rejected")
        self.assertNotIn("collection_case_id", payload)
        self.assertNotIn("outstanding_amount", payload)
        self.assertNotIn("dunning_events", payload)
        invalid_notice = empty.record_dunning_event(TENANT_ONE, generate_record_id(), "mystery_notice")
        self.assertEqual(
            invalid_notice.rejection_reason_code,
            CollectionCaseRejectionReasonCode.DUNNING_NOTICE_INVALID,
        )


class CollectionCaseCatalogAndContractTests(unittest.TestCase):
    """Cover collection persistence edges and commercial-status contract semantics."""

    def test_collection_case_insert_is_immutable_and_commercial_only(self) -> None:
        """A second insert or paid status cannot replace or settle history."""
        ledger, invoice_draft_id = draft_known_morning()
        first = CollectionCaseService(ledger).open_collection_case(TENANT_ONE, invoice_draft_id)
        stored = ledger.collection_cases[first.collection_case_id]
        with self.assertRaises(ValueError):
            ledger.insert_collection_case(stored)
        colliding = replace(stored, collection_case_id=generate_record_id())
        with self.assertRaises(ValueError):
            ledger.insert_collection_case(colliding)
        paid = replace(
            stored,
            collection_case_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            collection_case_status="paid",
        )
        with self.assertRaises(ValueError):
            ledger.insert_collection_case(paid)
        zero = replace(
            stored,
            collection_case_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            outstanding_amount=Decimal("0"),
        )
        with self.assertRaises(ValueError):
            ledger.insert_collection_case(zero)
        self.assertIsNone(ledger.get_collection_case(generate_record_id()))
        stored_case = ledger.get_collection_case(invoice_draft_id)
        self.assertIsNone(stored_case)
        found = ledger.get_collection_case(first.collection_case_id)
        self.assertIsNotNone(found)
        self.assertEqual(found.collection_case_id, first.collection_case_id)

    def test_dunning_insert_rejects_unknown_notice_and_duplicate_identity(self) -> None:
        """Dunning events stay commercial reminders with unique notice codes."""
        ledger, invoice_draft_id = draft_known_morning()
        opened = CollectionCaseService(ledger).open_collection_case(TENANT_ONE, invoice_draft_id)
        first = CollectionCaseService(ledger).record_dunning_event(
            TENANT_ONE, opened.collection_case_id, "first_notice"
        )
        stored = ledger.collection_dunning_events[0]
        with self.assertRaises(ValueError):
            ledger.insert_collection_dunning_event(stored)
        unknown = replace(
            stored,
            collection_dunning_event_id=generate_record_id(),
            dunning_notice_code="mystery_notice",
        )
        with self.assertRaises(ValueError):
            ledger.insert_collection_dunning_event(unknown)
        orphan = replace(
            stored,
            collection_dunning_event_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            dunning_notice_code="overdue_notice",
        )
        with self.assertRaises(ValueError):
            ledger.insert_collection_dunning_event(orphan)
        duplicate_number = replace(
            stored,
            collection_dunning_event_id=generate_record_id(),
            dunning_notice_code="overdue_notice",
        )
        with self.assertRaises(ValueError):
            ledger.insert_collection_dunning_event(duplicate_number)
        self.assertEqual(first.dunning_events[0].dunning_notice_code, "first_notice")

    def test_unknown_outcome_and_missing_reason_stay_fail_closed(self) -> None:
        """Unsupported outcome text cannot be serialized as a collection case."""
        bogus = CollectionCaseResult(
            collection_case_outcome_code="mystery",  # type: ignore[arg-type]
            collection_case_contract_version=1,
            collection_case_id=None,
            invoice_draft_id=None,
            tenant_reference=None,
            currency_code=None,
            collection_case_status=None,
            outstanding_amount=None,
            rejection_reason_code=None,
            dunning_events=(),
        )
        with self.assertRaises(ValueError):
            bogus.as_contract_dict()
        rejected_without_reason = CollectionCaseResult(
            collection_case_outcome_code=CollectionCaseOutcomeCode.REJECTED,
            collection_case_contract_version=1,
            collection_case_id=None,
            invoice_draft_id=None,
            tenant_reference=None,
            currency_code=None,
            collection_case_status=None,
            outstanding_amount=None,
            rejection_reason_code=None,
            dunning_events=(),
        )
        self.assertEqual(
            rejected_without_reason.as_contract_dict()["rejection_reason_code"],
            "invoice_draft_not_found",
        )

    def test_collection_case_semantics_require_identity_and_commercial_status(self) -> None:
        """Accepted cases need identity; rejected cases need a reason; paid is forbidden."""
        valid = {
            "collection_case_contract_version": 1,
            "collection_case_outcome_code": "accepted",
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf630",
            "tenant_reference": TENANT_ONE,
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "currency_code": "USD",
            "collection_case_status": "open",
            "outstanding_amount": "0.003705",
            "dunning_events": [],
        }
        self.assertEqual(validate_collection_case(valid), ())
        missing_id = json.loads(json.dumps(valid))
        del missing_id["collection_case_id"]
        self.assertIn(
            "$: accepted collection cases must include collection_case_id",
            validate_collection_case(missing_id),
        )
        replay = json.loads(json.dumps(valid))
        replay["collection_case_outcome_code"] = "duplicate_replay"
        del replay["invoice_draft_id"]
        self.assertIn(
            "$: duplicate_replay collection cases must include invoice_draft_id",
            validate_collection_case(replay),
        )
        rejected = {
            "collection_case_contract_version": 1,
            "collection_case_outcome_code": "rejected",
            "rejection_reason_code": "invoice_draft_not_found",
        }
        self.assertEqual(validate_collection_case(rejected), ())
        missing_reason = {
            "collection_case_contract_version": 1,
            "collection_case_outcome_code": "rejected",
        }
        self.assertIn(
            "$: rejected collection cases must include rejection_reason_code",
            validate_collection_case(missing_reason),
        )
        self.assertTrue(validate_collection_case({"collection_case_contract_version": 1}))
        self.assertTrue(validate_collection_case(["not-an-object"]))
        unknown = json.loads(json.dumps(valid))
        unknown["collection_case_outcome_code"] = "mystery"
        self.assertTrue(validate_collection_case(unknown))
        paid = json.loads(json.dumps(valid))
        paid["collection_case_status"] = "paid"
        self.assertTrue(validate_collection_case(paid))

    def test_clock_stamps_opened_at_and_dunning_occurred_at(self) -> None:
        """A supplied clock stamps case opening and dunning occurred_at."""
        opened_at = datetime(2026, 8, 17, 18, 0, tzinfo=UTC)
        noticed_at = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)
        ledger, invoice_draft_id = draft_known_morning()
        opened = CollectionCaseService(ledger, clock=lambda: opened_at).open_collection_case(
            TENANT_ONE, invoice_draft_id
        )
        noticed = CollectionCaseService(ledger, clock=lambda: noticed_at).record_dunning_event(
            TENANT_ONE, opened.collection_case_id, "first_notice"
        )
        self.assertEqual(ledger.collection_cases[opened.collection_case_id].opened_at, opened_at)
        self.assertEqual(noticed.dunning_events[0].occurred_at, noticed_at)
        self.assertEqual(validate_collection_case(noticed.as_contract_dict()), ())


if __name__ == "__main__":
    unittest.main()
