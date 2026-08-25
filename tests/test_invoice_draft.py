"""Realistic invoice-draft tests for exact money, replay, and tenant isolation."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing import (
    MemoryUsageLedger,
    UsageRatingService,
    format_exact_decimal,
)
from metering_billing.errors import ExactDecimalError
from metering_billing.usage_ledger import generate_record_id
from test_usage_ingestion import ACCOUNT_ONE, ACCOUNT_TWO, TENANT_ONE, TENANT_TWO
from test_usage_rating import (
    KNOWN_MORNING_TOTAL,
    MORNING_WINDOW,
    ingest_known_batch,
    make_event,
)


class InvoiceDraftTests(unittest.TestCase):
    """Verify invoice-intent drafts copy rated totals without issuing or posting."""

    def test_known_rating_run_drafts_exact_invoice_intent_totals(self) -> None:
        """A known rating run must produce a draft whose money equals the rated total."""
        from metering_billing import InvoiceDraftService
        from metering_billing.contracts import validate_invoice_draft
        from metering_billing.errors import InvoiceDraftOutcomeCode

        ingest = ingest_known_batch()
        rating = UsageRatingService(ingest.ledger).rate_usage_window(TENANT_ONE, MORNING_WINDOW, 1)
        self.assertIsInstance(rating.rating_run_id, UUID)
        draft = InvoiceDraftService(ingest.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        self.assertEqual(draft.invoice_draft_outcome_code, InvoiceDraftOutcomeCode.ACCEPTED)
        self.assertIsInstance(draft.invoice_draft_id, UUID)
        self.assertEqual(draft.invoice_draft_status, "draft")
        self.assertEqual(draft.drafted_total_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(draft.drafted_total_amount, rating.rated_total_amount)
        self.assertEqual(draft.currency_code, "USD")
        self.assertEqual(draft.rating_run_id, rating.rating_run_id)
        self.assertEqual(draft.usage_snapshot_hash, rating.usage_snapshot_hash)
        self.assertNotIsInstance(draft.drafted_total_amount, float)
        self.assertEqual(len(draft.invoice_draft_lines), 1)
        line = draft.invoice_draft_lines[0]
        self.assertEqual(line.line_total_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(line.billing_account_reference, ACCOUNT_ONE)
        self.assertEqual(validate_invoice_draft(draft.as_contract_dict()), ())
        self.assertEqual(len(ingest.ledger.invoice_drafts), 1)
        self.assertEqual(len(ingest.ledger.invoice_draft_lines), 1)
        self.assertEqual(len(ingest.ledger.accounting_export_records), 0)
        self.assertNotIn("issued", draft.as_contract_dict().get("invoice_draft_status", ""))

    def test_second_draft_of_the_same_rating_run_is_a_replay(self) -> None:
        """The same tenant and rating_run_id reuse invoice_draft_id and totals."""
        from metering_billing import InvoiceDraftService
        from metering_billing.errors import InvoiceDraftOutcomeCode

        ingest = ingest_known_batch()
        rating = UsageRatingService(ingest.ledger).rate_usage_window(TENANT_ONE, MORNING_WINDOW, 1)
        service = InvoiceDraftService(ingest.ledger)
        first = service.draft_invoice(TENANT_ONE, rating.rating_run_id)
        second = service.draft_invoice(TENANT_ONE, rating.rating_run_id)
        self.assertEqual(first.invoice_draft_outcome_code, InvoiceDraftOutcomeCode.ACCEPTED)
        self.assertEqual(second.invoice_draft_outcome_code, InvoiceDraftOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(second.invoice_draft_id, first.invoice_draft_id)
        self.assertEqual(second.drafted_total_amount, first.drafted_total_amount)
        self.assertEqual(second.drafted_total_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(len(ingest.ledger.invoice_drafts), 1)

    def test_other_tenant_cannot_see_or_total_the_first_draft(self) -> None:
        """A tenant cannot draft or list another tenant's rating run."""
        from metering_billing import InvoiceDraftService
        from metering_billing.errors import (
            InvoiceDraftOutcomeCode,
            InvoiceDraftRejectionReasonCode,
        )

        ingest = ingest_known_batch()
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
        ingest.ingest_usage_event(foreign)
        rater = UsageRatingService(ingest.ledger)
        one_rate = rater.rate_usage_window(TENANT_ONE, MORNING_WINDOW, 1)
        two_rate = rater.rate_usage_window(TENANT_TWO, MORNING_WINDOW, 1)
        service = InvoiceDraftService(ingest.ledger)
        one_draft = service.draft_invoice(TENANT_ONE, one_rate.rating_run_id)
        two_draft = service.draft_invoice(TENANT_TWO, two_rate.rating_run_id)
        crossed = service.draft_invoice(TENANT_TWO, one_rate.rating_run_id)
        self.assertEqual(one_draft.drafted_total_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(two_draft.drafted_total_amount, Decimal("10") * Decimal("0.000002"))
        self.assertNotEqual(one_draft.invoice_draft_id, two_draft.invoice_draft_id)
        self.assertEqual(crossed.invoice_draft_outcome_code, InvoiceDraftOutcomeCode.REJECTED)
        self.assertEqual(
            crossed.rejection_reason_code,
            InvoiceDraftRejectionReasonCode.RATING_RUN_NOT_FOUND,
        )
        one_rows = ingest.ledger.list_invoice_drafts(
            ingest.ledger.require_tenant(TENANT_ONE).tenant_account_id
        )
        self.assertEqual(len(one_rows), 1)
        self.assertEqual(one_rows[0].invoice_draft_id, one_draft.invoice_draft_id)

    def test_missing_rating_run_and_tenant_fail_closed(self) -> None:
        """A draft cannot invent money without a stored tenant rating run."""
        from metering_billing import InvoiceDraftService
        from metering_billing.errors import (
            InvoiceDraftOutcomeCode,
            InvoiceDraftRejectionReasonCode,
        )

        ingest = ingest_known_batch()
        service = InvoiceDraftService(ingest.ledger)
        missing_run = service.draft_invoice(TENANT_ONE, generate_record_id())
        self.assertEqual(missing_run.invoice_draft_outcome_code, InvoiceDraftOutcomeCode.REJECTED)
        self.assertEqual(
            missing_run.rejection_reason_code,
            InvoiceDraftRejectionReasonCode.RATING_RUN_NOT_FOUND,
        )
        missing_tenant = service.draft_invoice("urn:cwl:missing_tenant", generate_record_id())
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            InvoiceDraftRejectionReasonCode.TENANT_NOT_FOUND,
        )
        self.assertEqual(len(ingest.ledger.invoice_drafts), 0)

    def test_binary_float_money_is_rejected_at_the_draft_boundary(self) -> None:
        """Invoice-intent amounts must be exact decimals, never IEEE binary floats."""
        from metering_billing.invoice_draft import parse_invoice_amount

        with self.assertRaises(ExactDecimalError):
            parse_invoice_amount(0.003705)
        self.assertEqual(parse_invoice_amount("0.003705"), Decimal("0.003705"))
        self.assertEqual(parse_invoice_amount(Decimal("0.003705")), Decimal("0.003705"))
        self.assertEqual(format_exact_decimal(parse_invoice_amount("0.003705")), "0.003705")

    def test_default_service_and_rejected_contract_stay_sparse(self) -> None:
        """The zero-argument service constructs a ledger and rejected drafts omit money."""
        from metering_billing import InvoiceDraftService

        empty = InvoiceDraftService()
        self.assertIsInstance(empty.ledger, MemoryUsageLedger)
        rejected = empty.draft_invoice(TENANT_ONE, generate_record_id())
        payload = rejected.as_contract_dict()
        self.assertEqual(payload["invoice_draft_outcome_code"], "rejected")
        self.assertNotIn("invoice_draft_id", payload)
        self.assertNotIn("drafted_total_amount", payload)
        self.assertNotIn("invoice_draft_lines", payload)


class InvoiceDraftCatalogAndContractTests(unittest.TestCase):
    """Cover draft persistence edges and invoice-draft contract semantics."""

    def test_invoice_draft_insert_is_immutable(self) -> None:
        """A second insert of the same tenant and rating run cannot replace history."""
        from metering_billing import InvoiceDraftService

        ingest = ingest_known_batch()
        rating = UsageRatingService(ingest.ledger).rate_usage_window(TENANT_ONE, MORNING_WINDOW, 1)
        first = InvoiceDraftService(ingest.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        stored = ingest.ledger.invoice_drafts[first.invoice_draft_id]
        with self.assertRaises(ValueError):
            ingest.ledger.insert_invoice_draft(stored, stored.invoice_draft_lines)
        colliding = replace(stored, invoice_draft_id=generate_record_id())
        with self.assertRaises(ValueError):
            ingest.ledger.insert_invoice_draft(colliding, colliding.invoice_draft_lines)

    def test_invoice_draft_semantics_require_identity_totals_and_draft_status(self) -> None:
        """Accepted drafts need identity and balancing lines; rejected drafts need a reason."""
        from metering_billing.contracts import validate_invoice_draft
        from metering_billing.errors import InvoiceDraftOutcomeCode
        from metering_billing.invoice_draft import InvoiceDraftResult

        valid = {
            "invoice_draft_contract_version": 1,
            "invoice_draft_outcome_code": "accepted",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "tenant_reference": TENANT_ONE,
            "rating_run_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf621",
            "usage_snapshot_hash": "sha256:" + "d" * 64,
            "currency_code": "USD",
            "invoice_draft_status": "draft",
            "drafted_total_amount": "0.003705",
            "invoice_draft_lines": [
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
        self.assertEqual(validate_invoice_draft(valid), ())
        missing_id = json.loads(json.dumps(valid))
        del missing_id["invoice_draft_id"]
        self.assertIn(
            "$: accepted invoice drafts must include invoice_draft_id",
            validate_invoice_draft(missing_id),
        )
        replay = json.loads(json.dumps(valid))
        replay["invoice_draft_outcome_code"] = "duplicate_replay"
        del replay["rating_run_id"]
        self.assertIn(
            "$: duplicate_replay invoice drafts must include rating_run_id",
            validate_invoice_draft(replay),
        )
        unbalanced = json.loads(json.dumps(valid))
        unbalanced["invoice_draft_lines"][0]["line_total_amount"] = "0.003704"
        self.assertIn(
            "$: invoice draft line totals must equal drafted_total_amount",
            validate_invoice_draft(unbalanced),
        )
        rejected = {
            "invoice_draft_contract_version": 1,
            "invoice_draft_outcome_code": "rejected",
            "rejection_reason_code": "rating_run_not_found",
        }
        self.assertEqual(validate_invoice_draft(rejected), ())
        missing_reason = {
            "invoice_draft_contract_version": 1,
            "invoice_draft_outcome_code": "rejected",
        }
        self.assertIn(
            "$: rejected invoice drafts must include rejection_reason_code",
            validate_invoice_draft(missing_reason),
        )
        self.assertTrue(validate_invoice_draft({"invoice_draft_contract_version": 1}))
        self.assertTrue(validate_invoice_draft(["not-an-object"]))
        unknown = json.loads(json.dumps(valid))
        unknown["invoice_draft_outcome_code"] = "mystery"
        self.assertTrue(validate_invoice_draft(unknown))
        malformed_lines = json.loads(json.dumps(valid))
        malformed_lines["invoice_draft_lines"] = ["not-an-object"]
        self.assertTrue(validate_invoice_draft(malformed_lines))
        non_string_total = json.loads(json.dumps(valid))
        non_string_total["drafted_total_amount"] = 1
        self.assertTrue(validate_invoice_draft(non_string_total))
        non_string_line = json.loads(json.dumps(valid))
        non_string_line["invoice_draft_lines"][0]["line_total_amount"] = 1
        self.assertTrue(validate_invoice_draft(non_string_line))
        missing_line_amount = json.loads(json.dumps(valid))
        del missing_line_amount["invoice_draft_lines"][0]["line_total_amount"]
        self.assertTrue(validate_invoice_draft(missing_line_amount))
        bogus = InvoiceDraftResult(
            invoice_draft_outcome_code="mystery",  # type: ignore[arg-type]
            invoice_draft_contract_version=1,
            invoice_draft_id=None,
            tenant_reference=None,
            rating_run_id=None,
            usage_snapshot_hash=None,
            currency_code=None,
            invoice_draft_status=None,
            drafted_total_amount=None,
            rejection_reason_code=None,
            invoice_draft_lines=(),
        )
        with self.assertRaises(ValueError):
            bogus.as_contract_dict()
        rejected_without_reason = InvoiceDraftResult(
            invoice_draft_outcome_code=InvoiceDraftOutcomeCode.REJECTED,
            invoice_draft_contract_version=1,
            invoice_draft_id=None,
            tenant_reference=None,
            rating_run_id=None,
            usage_snapshot_hash=None,
            currency_code=None,
            invoice_draft_status=None,
            drafted_total_amount=None,
            rejection_reason_code=None,
            invoice_draft_lines=(),
        )
        self.assertEqual(
            rejected_without_reason.as_contract_dict()["rejection_reason_code"],
            "rating_run_not_found",
        )

    def test_clock_and_explicit_rating_run_are_recorded(self) -> None:
        """A supplied clock stamps recorded_at on the append-only draft."""
        from metering_billing import InvoiceDraftService

        ingest = ingest_known_batch()
        rating = UsageRatingService(ingest.ledger).rate_usage_window(TENANT_ONE, MORNING_WINDOW, 1)
        fixed = datetime(2026, 8, 17, 18, 0, tzinfo=UTC)
        draft = InvoiceDraftService(ingest.ledger, clock=lambda: fixed).draft_invoice(
            TENANT_ONE, rating.rating_run_id
        )
        self.assertEqual(
            ingest.ledger.invoice_drafts[draft.invoice_draft_id].recorded_at,
            fixed,
        )


if __name__ == "__main__":
    unittest.main()
