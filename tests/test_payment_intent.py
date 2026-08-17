"""Realistic payment-intent tests for exact amounts, replay, and tenant isolation."""

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
    PaymentIntentService,
    UsageIngestionService,
    UsageRatingService,
    format_exact_decimal,
)
from metering_billing.contracts import validate_payment_intent
from metering_billing.errors import (
    ExactDecimalError,
    PaymentIntentOutcomeCode,
    PaymentIntentRejectionReasonCode,
)
from metering_billing.payment_intent import PaymentIntentResult, parse_payment_amount
from metering_billing.usage_ledger import generate_record_id
from test_collection_case import draft_known_morning
from test_usage_ingestion import ACCOUNT_TWO, TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL, MORNING_WINDOW, make_event


def open_known_morning_case() -> tuple[MemoryUsageLedger, UUID]:
    """Persist the known morning draft and open its collection case."""
    ledger, invoice_draft_id = draft_known_morning()
    opened = CollectionCaseService(ledger).open_collection_case(TENANT_ONE, invoice_draft_id)
    if opened.collection_case_id is None:
        raise AssertionError("known morning path must persist a collection case")
    return ledger, opened.collection_case_id


class PaymentIntentTests(unittest.TestCase):
    """Verify payment intents copy case outstanding without capturing money."""

    def test_known_collection_case_projects_exact_payment_intent_amount(self) -> None:
        """A known draft total and case outstanding must become the intent amount."""
        ledger, collection_case_id = open_known_morning_case()
        outstanding = ledger.collection_cases[collection_case_id].outstanding_amount
        self.assertEqual(outstanding, KNOWN_MORNING_TOTAL)
        result = PaymentIntentService(ledger).project_payment_intent(TENANT_ONE, collection_case_id)
        self.assertEqual(result.payment_intent_outcome_code, PaymentIntentOutcomeCode.ACCEPTED)
        self.assertIsInstance(result.payment_intent_id, UUID)
        self.assertEqual(result.payment_intent_status, "projected")
        self.assertEqual(result.payment_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(result.payment_amount, outstanding)
        self.assertEqual(result.currency_code, "USD")
        self.assertEqual(result.collection_case_id, collection_case_id)
        self.assertTrue(result.source_payload_hash.startswith("sha256:"))
        self.assertNotIsInstance(result.payment_amount, float)
        self.assertEqual(validate_payment_intent(result.as_contract_dict()), ())
        self.assertNotIn(result.payment_intent_status, {"captured", "settled", "posted"})
        self.assertEqual(len(ledger.payment_intents), 1)
        self.assertEqual(ledger.collection_cases[collection_case_id].outstanding_amount, outstanding)
        self.assertEqual(len(ledger.accounting_export_records), 0)
        self.assertEqual(len(ledger.journal_proposals), 0)

    def test_second_project_of_the_same_case_snapshot_is_a_replay(self) -> None:
        """The same tenant, case, hash, and contract version reuse payment_intent_id."""
        ledger, collection_case_id = open_known_morning_case()
        service = PaymentIntentService(ledger)
        first = service.project_payment_intent(TENANT_ONE, collection_case_id)
        second = service.project_payment_intent(TENANT_ONE, collection_case_id)
        self.assertEqual(first.payment_intent_outcome_code, PaymentIntentOutcomeCode.ACCEPTED)
        self.assertEqual(second.payment_intent_outcome_code, PaymentIntentOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(second.payment_intent_id, first.payment_intent_id)
        self.assertEqual(second.source_payload_hash, first.source_payload_hash)
        self.assertEqual(second.payment_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(len(ledger.payment_intents), 1)
        self.assertEqual(validate_payment_intent(second.as_contract_dict()), ())

    def test_other_tenant_cannot_see_or_project_the_first_case(self) -> None:
        """A tenant cannot project or list another tenant's collection case."""
        ledger, one_case_id = open_known_morning_case()
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
        two_case = CollectionCaseService(ledger).open_collection_case(
            TENANT_TWO, two_draft.invoice_draft_id
        )
        service = PaymentIntentService(ledger)
        one_intent = service.project_payment_intent(TENANT_ONE, one_case_id)
        two_intent = service.project_payment_intent(TENANT_TWO, two_case.collection_case_id)
        crossed = service.project_payment_intent(TENANT_TWO, one_case_id)
        self.assertEqual(one_intent.payment_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(two_intent.payment_amount, Decimal("10") * Decimal("0.000002"))
        self.assertNotEqual(one_intent.payment_intent_id, two_intent.payment_intent_id)
        self.assertEqual(crossed.payment_intent_outcome_code, PaymentIntentOutcomeCode.REJECTED)
        self.assertEqual(
            crossed.rejection_reason_code,
            PaymentIntentRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )
        self.assertNotIn("payment_intent_id", crossed.as_contract_dict())
        one_rows = ledger.list_payment_intents(ledger.require_tenant(TENANT_ONE).tenant_account_id)
        two_rows = ledger.list_payment_intents(ledger.require_tenant(TENANT_TWO).tenant_account_id)
        self.assertEqual(len(one_rows), 1)
        self.assertEqual(one_rows[0].payment_intent_id, one_intent.payment_intent_id)
        self.assertEqual(len(two_rows), 1)

    def test_missing_case_and_tenant_fail_closed(self) -> None:
        """An intent cannot invent money without a stored tenant collection case."""
        ledger, _collection_case_id = open_known_morning_case()
        service = PaymentIntentService(ledger)
        missing_case = service.project_payment_intent(TENANT_ONE, generate_record_id())
        self.assertEqual(missing_case.payment_intent_outcome_code, PaymentIntentOutcomeCode.REJECTED)
        self.assertEqual(
            missing_case.rejection_reason_code,
            PaymentIntentRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )
        missing_tenant = service.project_payment_intent("urn:cwl:missing_tenant", generate_record_id())
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            PaymentIntentRejectionReasonCode.TENANT_NOT_FOUND,
        )
        self.assertEqual(len(ledger.payment_intents), 0)

    def test_zero_outstanding_snapshot_fails_closed(self) -> None:
        """A zero case outstanding cannot become a payment intent."""
        ledger, collection_case_id = open_known_morning_case()
        stored = ledger.collection_cases[collection_case_id]
        ledger.collection_cases[collection_case_id] = replace(
            stored, outstanding_amount=Decimal("0")
        )
        rejected = PaymentIntentService(ledger).project_payment_intent(TENANT_ONE, collection_case_id)
        self.assertEqual(
            rejected.rejection_reason_code,
            PaymentIntentRejectionReasonCode.PAYMENT_AMOUNT_INVALID,
        )
        self.assertEqual(len(ledger.payment_intents), 0)

    def test_binary_float_money_is_rejected_at_the_payment_boundary(self) -> None:
        """Payment-intent amounts must be exact decimals, never IEEE binary floats."""
        with self.assertRaises(ExactDecimalError):
            parse_payment_amount(0.003705)
        self.assertEqual(parse_payment_amount("0.003705"), Decimal("0.003705"))
        self.assertEqual(parse_payment_amount(Decimal("0.003705")), Decimal("0.003705"))
        self.assertEqual(format_exact_decimal(parse_payment_amount("0.003705")), "0.003705")

    def test_default_service_and_rejected_contract_stay_sparse(self) -> None:
        """The zero-argument service constructs a ledger and rejected intents omit money."""
        empty = PaymentIntentService()
        self.assertIsInstance(empty.ledger, MemoryUsageLedger)
        rejected = empty.project_payment_intent(TENANT_ONE, generate_record_id())
        payload = rejected.as_contract_dict()
        self.assertEqual(payload["payment_intent_outcome_code"], "rejected")
        self.assertNotIn("payment_intent_id", payload)
        self.assertNotIn("payment_amount", payload)
        self.assertNotIn("source_payload_hash", payload)


class PaymentIntentCatalogAndContractTests(unittest.TestCase):
    """Cover payment-intent persistence edges and projected-only contract semantics."""

    def test_payment_intent_insert_is_immutable_and_projected_only(self) -> None:
        """A second insert or captured status cannot replace or settle history."""
        ledger, collection_case_id = open_known_morning_case()
        first = PaymentIntentService(ledger).project_payment_intent(TENANT_ONE, collection_case_id)
        stored = ledger.payment_intents[first.payment_intent_id]
        with self.assertRaises(ValueError):
            ledger.insert_payment_intent(stored)
        colliding = replace(stored, payment_intent_id=generate_record_id())
        with self.assertRaises(ValueError):
            ledger.insert_payment_intent(colliding)
        captured = replace(
            stored,
            payment_intent_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            source_payload_hash="sha256:" + "a" * 64,
            payment_intent_status="captured",
        )
        with self.assertRaises(ValueError):
            ledger.insert_payment_intent(captured)
        settled = replace(
            stored,
            payment_intent_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            source_payload_hash="sha256:" + "b" * 64,
            payment_intent_status="settled",
        )
        with self.assertRaises(ValueError):
            ledger.insert_payment_intent(settled)
        posted = replace(
            stored,
            payment_intent_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            source_payload_hash="sha256:" + "c" * 64,
            payment_intent_status="posted",
        )
        with self.assertRaises(ValueError):
            ledger.insert_payment_intent(posted)
        zero = replace(
            stored,
            payment_intent_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            source_payload_hash="sha256:" + "d" * 64,
            payment_amount=Decimal("0"),
        )
        with self.assertRaises(ValueError):
            ledger.insert_payment_intent(zero)
        cancelled = replace(
            stored,
            payment_intent_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            source_payload_hash="sha256:" + "e" * 64,
            payment_intent_status="cancelled",
        )
        persisted_cancelled = ledger.insert_payment_intent(cancelled)
        self.assertEqual(persisted_cancelled.payment_intent_status, "cancelled")
        self.assertIsNone(ledger.get_payment_intent(generate_record_id()))
        found = ledger.get_payment_intent(first.payment_intent_id)
        self.assertIsNotNone(found)
        self.assertEqual(found.payment_intent_id, first.payment_intent_id)

    def test_unknown_outcome_and_missing_reason_stay_fail_closed(self) -> None:
        """Unsupported outcome text cannot be serialized as a payment intent."""
        bogus = PaymentIntentResult(
            payment_intent_outcome_code="mystery",  # type: ignore[arg-type]
            payment_intent_contract_version=1,
            payment_intent_id=None,
            collection_case_id=None,
            tenant_reference=None,
            currency_code=None,
            payment_intent_status=None,
            payment_amount=None,
            source_payload_hash=None,
            projected_at=None,
            rejection_reason_code=None,
        )
        with self.assertRaises(ValueError):
            bogus.as_contract_dict()
        rejected_without_reason = PaymentIntentResult(
            payment_intent_outcome_code=PaymentIntentOutcomeCode.REJECTED,
            payment_intent_contract_version=1,
            payment_intent_id=None,
            collection_case_id=None,
            tenant_reference=None,
            currency_code=None,
            payment_intent_status=None,
            payment_amount=None,
            source_payload_hash=None,
            projected_at=None,
            rejection_reason_code=None,
        )
        self.assertEqual(
            rejected_without_reason.as_contract_dict()["rejection_reason_code"],
            "collection_case_not_found",
        )
        accepted_without_time = PaymentIntentResult(
            payment_intent_outcome_code=PaymentIntentOutcomeCode.ACCEPTED,
            payment_intent_contract_version=1,
            payment_intent_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            payment_intent_status="projected",
            payment_amount=KNOWN_MORNING_TOTAL,
            source_payload_hash="sha256:" + "f" * 64,
            projected_at=None,
            rejection_reason_code=None,
        )
        with self.assertRaises(ValueError):
            accepted_without_time.as_contract_dict()

    def test_payment_intent_semantics_require_identity_and_projected_status(self) -> None:
        """Accepted intents need identity; rejected intents need a reason; captured is forbidden."""
        valid = {
            "payment_intent_contract_version": 1,
            "payment_intent_outcome_code": "accepted",
            "payment_intent_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf640",
            "tenant_reference": TENANT_ONE,
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf630",
            "currency_code": "USD",
            "payment_intent_status": "projected",
            "payment_amount": "0.003705",
            "source_payload_hash": "sha256:" + "1" * 64,
            "projected_at": "2026-08-17T19:30:00Z",
        }
        self.assertEqual(validate_payment_intent(valid), ())
        missing_id = json.loads(json.dumps(valid))
        del missing_id["payment_intent_id"]
        self.assertIn(
            "$: accepted payment intents must include payment_intent_id",
            validate_payment_intent(missing_id),
        )
        replay = json.loads(json.dumps(valid))
        replay["payment_intent_outcome_code"] = "duplicate_replay"
        del replay["collection_case_id"]
        self.assertIn(
            "$: duplicate_replay payment intents must include collection_case_id",
            validate_payment_intent(replay),
        )
        rejected = {
            "payment_intent_contract_version": 1,
            "payment_intent_outcome_code": "rejected",
            "rejection_reason_code": "collection_case_not_found",
        }
        self.assertEqual(validate_payment_intent(rejected), ())
        missing_reason = {
            "payment_intent_contract_version": 1,
            "payment_intent_outcome_code": "rejected",
        }
        self.assertIn(
            "$: rejected payment intents must include rejection_reason_code",
            validate_payment_intent(missing_reason),
        )
        self.assertTrue(validate_payment_intent({"payment_intent_contract_version": 1}))
        self.assertTrue(validate_payment_intent(["not-an-object"]))
        unknown = json.loads(json.dumps(valid))
        unknown["payment_intent_outcome_code"] = "mystery"
        self.assertTrue(validate_payment_intent(unknown))
        captured = json.loads(json.dumps(valid))
        captured["payment_intent_status"] = "captured"
        self.assertTrue(validate_payment_intent(captured))

    def test_clock_stamps_projected_at(self) -> None:
        """A supplied clock stamps projected_at on the append-only intent."""
        projected_at = datetime(2026, 8, 17, 19, 30, tzinfo=UTC)
        ledger, collection_case_id = open_known_morning_case()
        result = PaymentIntentService(ledger, clock=lambda: projected_at).project_payment_intent(
            TENANT_ONE, collection_case_id
        )
        self.assertEqual(ledger.payment_intents[result.payment_intent_id].projected_at, projected_at)
        self.assertEqual(validate_payment_intent(result.as_contract_dict()), ())


if __name__ == "__main__":
    unittest.main()
