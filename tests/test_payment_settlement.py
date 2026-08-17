"""Realistic payment-settlement tests for receipts, cancel, replay, and isolation."""

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
    PaymentSettlementService,
    UsageIngestionService,
    UsageRatingService,
    format_exact_decimal,
)
from metering_billing.contracts import validate_payment_receipt
from metering_billing.errors import (
    ExactDecimalError,
    PaymentSettlementOutcomeCode,
    PaymentSettlementRejectionReasonCode,
)
from metering_billing.payment_settlement import (
    PaymentSettlementResult,
    parse_settlement_amount,
)
from metering_billing.usage_ledger import generate_record_id
from test_payment_intent import open_known_morning_case
from test_usage_ingestion import ACCOUNT_TWO, TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL, MORNING_WINDOW, make_event


PARTIAL_RECEIPT_AMOUNT = Decimal("0.001000")


def project_known_morning_intent() -> tuple[MemoryUsageLedger, UUID, UUID]:
    """Persist the known morning case and project its payment intent."""
    ledger, collection_case_id = open_known_morning_case()
    projected = PaymentIntentService(ledger).project_payment_intent(TENANT_ONE, collection_case_id)
    if projected.payment_intent_id is None:
        raise AssertionError("known morning path must persist a payment intent")
    return ledger, projected.payment_intent_id, collection_case_id


class PaymentSettlementTests(unittest.TestCase):
    """Verify receipts apply exact amounts without capturing via a provider."""

    def test_full_receipt_zeros_outstanding_and_settles_the_case(self) -> None:
        """A known intent amount must apply in full and settle the collection case."""
        ledger, payment_intent_id, collection_case_id = project_known_morning_intent()
        result = PaymentSettlementService(ledger).record_payment_receipt(
            TENANT_ONE, payment_intent_id, KNOWN_MORNING_TOTAL
        )
        self.assertEqual(result.payment_settlement_outcome_code, PaymentSettlementOutcomeCode.ACCEPTED)
        self.assertIsInstance(result.payment_receipt_id, UUID)
        self.assertEqual(result.payment_receipt_status, "applied")
        self.assertEqual(result.received_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(result.remaining_outstanding_amount, Decimal("0"))
        self.assertEqual(result.collection_case_status, "settled")
        self.assertEqual(result.collection_case_id, collection_case_id)
        self.assertEqual(result.next_operator_action, "Emit a cash journal proposal to AIS.")
        self.assertNotIsInstance(result.received_amount, float)
        self.assertEqual(validate_payment_receipt(result.as_contract_dict()), ())
        self.assertNotIn(result.payment_receipt_status, {"captured", "posted"})
        self.assertEqual(ledger.collection_cases[collection_case_id].outstanding_amount, Decimal("0"))
        self.assertEqual(ledger.collection_cases[collection_case_id].collection_case_status, "settled")
        self.assertEqual(len(ledger.payment_receipts), 1)
        self.assertEqual(len(ledger.accounting_export_records), 0)
        self.assertEqual(len(ledger.journal_proposals), 0)
        replayed_case = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, ledger.collection_cases[collection_case_id].invoice_draft_id
        )
        self.assertEqual(replayed_case.collection_case_status, "settled")
        self.assertEqual(replayed_case.outstanding_amount, Decimal("0"))

    def test_partial_receipt_leaves_residual_and_keeps_the_case_open(self) -> None:
        """A smaller receipt must leave residual outstanding on an open case."""
        ledger, payment_intent_id, collection_case_id = project_known_morning_intent()
        result = PaymentSettlementService(ledger).record_payment_receipt(
            TENANT_ONE, payment_intent_id, PARTIAL_RECEIPT_AMOUNT
        )
        remaining = KNOWN_MORNING_TOTAL - PARTIAL_RECEIPT_AMOUNT
        self.assertEqual(result.payment_settlement_outcome_code, PaymentSettlementOutcomeCode.ACCEPTED)
        self.assertEqual(result.received_amount, PARTIAL_RECEIPT_AMOUNT)
        self.assertEqual(result.remaining_outstanding_amount, remaining)
        self.assertEqual(result.collection_case_status, "open")
        self.assertEqual(
            result.next_operator_action,
            "Emit a cash journal proposal to AIS, or record another partial receipt.",
        )
        self.assertEqual(ledger.collection_cases[collection_case_id].outstanding_amount, remaining)
        self.assertEqual(ledger.collection_cases[collection_case_id].collection_case_status, "open")
        residual = PaymentSettlementService(ledger).record_payment_receipt(
            TENANT_ONE, payment_intent_id, remaining
        )
        self.assertEqual(residual.remaining_outstanding_amount, Decimal("0"))
        self.assertEqual(residual.collection_case_status, "settled")
        self.assertEqual(len(ledger.payment_receipts), 2)
        self.assertEqual(validate_payment_receipt(result.as_contract_dict()), ())

    def test_dunning_case_stays_dunning_after_partial_and_settles_after_full(self) -> None:
        """A dunning case keeps reminder history; settled wins after the residual applies."""
        ledger, collection_case_id = open_known_morning_case()
        CollectionCaseService(ledger).record_dunning_event(TENANT_ONE, collection_case_id, "first_notice")
        projected = PaymentIntentService(ledger).project_payment_intent(TENANT_ONE, collection_case_id)
        partial = PaymentSettlementService(ledger).record_payment_receipt(
            TENANT_ONE, projected.payment_intent_id, PARTIAL_RECEIPT_AMOUNT
        )
        self.assertEqual(partial.collection_case_status, "dunning")
        noticed = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, ledger.collection_cases[collection_case_id].invoice_draft_id
        )
        self.assertEqual(noticed.collection_case_status, "dunning")
        remaining = KNOWN_MORNING_TOTAL - PARTIAL_RECEIPT_AMOUNT
        full = PaymentSettlementService(ledger).record_payment_receipt(
            TENANT_ONE, projected.payment_intent_id, remaining
        )
        self.assertEqual(full.collection_case_status, "settled")
        settled = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, ledger.collection_cases[collection_case_id].invoice_draft_id
        )
        self.assertEqual(settled.collection_case_status, "settled")
        self.assertEqual(len(settled.dunning_events), 1)

    def test_second_receipt_of_the_same_identity_is_a_replay(self) -> None:
        """The same tenant, intent, amount, hash, and contract version reuse payment_receipt_id."""
        ledger, payment_intent_id, collection_case_id = project_known_morning_intent()
        service = PaymentSettlementService(ledger)
        first = service.record_payment_receipt(TENANT_ONE, payment_intent_id, PARTIAL_RECEIPT_AMOUNT)
        second = service.record_payment_receipt(TENANT_ONE, payment_intent_id, PARTIAL_RECEIPT_AMOUNT)
        remaining = KNOWN_MORNING_TOTAL - PARTIAL_RECEIPT_AMOUNT
        self.assertEqual(first.payment_settlement_outcome_code, PaymentSettlementOutcomeCode.ACCEPTED)
        self.assertEqual(second.payment_settlement_outcome_code, PaymentSettlementOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(second.payment_receipt_id, first.payment_receipt_id)
        self.assertEqual(second.source_payload_hash, first.source_payload_hash)
        self.assertEqual(second.received_amount, PARTIAL_RECEIPT_AMOUNT)
        self.assertEqual(second.remaining_outstanding_amount, remaining)
        self.assertEqual(len(ledger.payment_receipts), 1)
        self.assertEqual(ledger.collection_cases[collection_case_id].outstanding_amount, remaining)
        self.assertEqual(validate_payment_receipt(second.as_contract_dict()), ())

    def test_other_tenant_cannot_see_or_settle_the_first_intent(self) -> None:
        """A tenant cannot settle or list another tenant's payment intent."""
        ledger, one_intent_id, _one_case_id = project_known_morning_intent()
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
        two_intent = PaymentIntentService(ledger).project_payment_intent(
            TENANT_TWO, two_case.collection_case_id
        )
        service = PaymentSettlementService(ledger)
        one_receipt = service.record_payment_receipt(TENANT_ONE, one_intent_id, KNOWN_MORNING_TOTAL)
        two_amount = Decimal("10") * Decimal("0.000002")
        two_receipt = service.record_payment_receipt(TENANT_TWO, two_intent.payment_intent_id, two_amount)
        crossed = service.record_payment_receipt(TENANT_TWO, one_intent_id, two_amount)
        self.assertEqual(one_receipt.received_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(two_receipt.received_amount, two_amount)
        self.assertNotEqual(one_receipt.payment_receipt_id, two_receipt.payment_receipt_id)
        self.assertEqual(crossed.payment_settlement_outcome_code, PaymentSettlementOutcomeCode.REJECTED)
        self.assertEqual(
            crossed.rejection_reason_code,
            PaymentSettlementRejectionReasonCode.PAYMENT_INTENT_NOT_FOUND,
        )
        self.assertNotIn("payment_receipt_id", crossed.as_contract_dict())
        one_rows = ledger.list_payment_receipts(ledger.require_tenant(TENANT_ONE).tenant_account_id)
        two_rows = ledger.list_payment_receipts(ledger.require_tenant(TENANT_TWO).tenant_account_id)
        self.assertEqual(len(one_rows), 1)
        self.assertEqual(one_rows[0].payment_receipt_id, one_receipt.payment_receipt_id)
        self.assertEqual(len(two_rows), 1)

    def test_over_apply_zero_and_missing_intent_fail_closed(self) -> None:
        """A receipt cannot invent money or apply more than remaining outstanding."""
        ledger, payment_intent_id, collection_case_id = project_known_morning_intent()
        service = PaymentSettlementService(ledger)
        over_applied = service.record_payment_receipt(TENANT_ONE, payment_intent_id, Decimal("1"))
        self.assertEqual(
            over_applied.rejection_reason_code,
            PaymentSettlementRejectionReasonCode.PAYMENT_AMOUNT_EXCEEDS_OUTSTANDING,
        )
        zero = service.record_payment_receipt(TENANT_ONE, payment_intent_id, Decimal("0"))
        self.assertEqual(
            zero.rejection_reason_code,
            PaymentSettlementRejectionReasonCode.PAYMENT_AMOUNT_INVALID,
        )
        missing_intent = service.record_payment_receipt(TENANT_ONE, generate_record_id(), KNOWN_MORNING_TOTAL)
        self.assertEqual(
            missing_intent.rejection_reason_code,
            PaymentSettlementRejectionReasonCode.PAYMENT_INTENT_NOT_FOUND,
        )
        missing_tenant = service.record_payment_receipt(
            "urn:cwl:missing_tenant", payment_intent_id, KNOWN_MORNING_TOTAL
        )
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            PaymentSettlementRejectionReasonCode.TENANT_NOT_FOUND,
        )
        self.assertEqual(len(ledger.payment_receipts), 0)
        self.assertEqual(
            ledger.collection_cases[collection_case_id].outstanding_amount, KNOWN_MORNING_TOTAL
        )

    def test_binary_float_money_is_rejected_at_the_settlement_boundary(self) -> None:
        """Settlement amounts must be exact decimals, never IEEE binary floats."""
        ledger, payment_intent_id, _collection_case_id = project_known_morning_intent()
        with self.assertRaises(ExactDecimalError):
            parse_settlement_amount(0.003705)
        self.assertEqual(parse_settlement_amount("0.003705"), Decimal("0.003705"))
        self.assertEqual(parse_settlement_amount(Decimal("0.003705")), Decimal("0.003705"))
        self.assertEqual(format_exact_decimal(parse_settlement_amount("0.003705")), "0.003705")
        rejected = PaymentSettlementService(ledger).record_payment_receipt(
            TENANT_ONE, payment_intent_id, 0.003705
        )
        self.assertEqual(
            rejected.rejection_reason_code,
            PaymentSettlementRejectionReasonCode.PAYMENT_AMOUNT_INVALID,
        )
        self.assertEqual(len(ledger.payment_receipts), 0)

    def test_cancel_then_receipt_is_rejected_and_cancel_replays(self) -> None:
        """Cancel flips a projected intent without a receipt; later receipts fail closed."""
        ledger, payment_intent_id, collection_case_id = project_known_morning_intent()
        outstanding = ledger.collection_cases[collection_case_id].outstanding_amount
        service = PaymentSettlementService(ledger)
        cancelled = service.cancel_payment_intent(TENANT_ONE, payment_intent_id)
        replay = service.cancel_payment_intent(TENANT_ONE, payment_intent_id)
        receipt = service.record_payment_receipt(TENANT_ONE, payment_intent_id, KNOWN_MORNING_TOTAL)
        self.assertEqual(cancelled.payment_settlement_outcome_code, PaymentSettlementOutcomeCode.ACCEPTED)
        self.assertIsNone(cancelled.payment_receipt_id)
        self.assertEqual(cancelled.payment_intent_status, "cancelled")
        self.assertEqual(cancelled.remaining_outstanding_amount, outstanding)
        self.assertEqual(ledger.collection_cases[collection_case_id].outstanding_amount, outstanding)
        self.assertEqual(replay.payment_settlement_outcome_code, PaymentSettlementOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(replay.payment_intent_id, cancelled.payment_intent_id)
        self.assertEqual(
            receipt.rejection_reason_code,
            PaymentSettlementRejectionReasonCode.PAYMENT_INTENT_NOT_PROJECTED,
        )
        self.assertEqual(len(ledger.payment_receipts), 0)
        self.assertEqual(validate_payment_receipt(cancelled.as_contract_dict()), ())
        self.assertEqual(validate_payment_receipt(replay.as_contract_dict()), ())

    def test_default_service_and_rejected_contract_stay_sparse(self) -> None:
        """The zero-argument service constructs a ledger and rejected results omit money."""
        empty = PaymentSettlementService()
        self.assertIsInstance(empty.ledger, MemoryUsageLedger)
        rejected = empty.record_payment_receipt(TENANT_ONE, generate_record_id(), KNOWN_MORNING_TOTAL)
        payload = rejected.as_contract_dict()
        self.assertEqual(payload["payment_settlement_outcome_code"], "rejected")
        self.assertNotIn("payment_receipt_id", payload)
        self.assertNotIn("received_amount", payload)
        self.assertNotIn("source_payload_hash", payload)
        cancelled = empty.cancel_payment_intent(TENANT_ONE, generate_record_id())
        self.assertEqual(
            cancelled.rejection_reason_code,
            PaymentSettlementRejectionReasonCode.PAYMENT_INTENT_NOT_FOUND,
        )


class PaymentSettlementCatalogAndContractTests(unittest.TestCase):
    """Cover settlement persistence edges and applied-only contract semantics."""

    def test_payment_receipt_insert_is_immutable_and_applied_only(self) -> None:
        """A second insert or captured status cannot replace or post history."""
        ledger, payment_intent_id, _collection_case_id = project_known_morning_intent()
        first = PaymentSettlementService(ledger).record_payment_receipt(
            TENANT_ONE, payment_intent_id, PARTIAL_RECEIPT_AMOUNT
        )
        stored = ledger.payment_receipts[first.payment_receipt_id]
        with self.assertRaises(ValueError):
            ledger.insert_payment_receipt(stored)
        colliding = replace(stored, payment_receipt_id=generate_record_id())
        with self.assertRaises(ValueError):
            ledger.insert_payment_receipt(colliding)
        captured = replace(
            stored,
            payment_receipt_id=generate_record_id(),
            source_payload_hash="sha256:" + "a" * 64,
            payment_receipt_status="captured",
        )
        with self.assertRaises(ValueError):
            ledger.insert_payment_receipt(captured)
        posted = replace(
            stored,
            payment_receipt_id=generate_record_id(),
            source_payload_hash="sha256:" + "b" * 64,
            payment_receipt_status="posted",
        )
        with self.assertRaises(ValueError):
            ledger.insert_payment_receipt(posted)
        zero = replace(
            stored,
            payment_receipt_id=generate_record_id(),
            source_payload_hash="sha256:" + "c" * 64,
            received_amount=Decimal("0"),
        )
        with self.assertRaises(ValueError):
            ledger.insert_payment_receipt(zero)
        self.assertIsNone(ledger.get_payment_receipt(generate_record_id()))
        found = ledger.get_payment_receipt(first.payment_receipt_id)
        self.assertIsNotNone(found)
        self.assertEqual(found.payment_receipt_id, first.payment_receipt_id)
        self.assertIsNone(
            ledger.find_payment_receipt(
                stored.tenant_account_id,
                stored.payment_intent_id,
                "sha256:" + "d" * 64,
                stored.settlement_contract_version,
            )
        )
        self.assertEqual(ledger.list_payment_receipts(generate_record_id()), ())

    def test_settlement_and_cancel_ledger_edges_fail_closed(self) -> None:
        """Balance updates and cancels reject missing rows and illegal amounts."""
        ledger, payment_intent_id, collection_case_id = project_known_morning_intent()
        with self.assertRaises(ValueError):
            ledger.apply_collection_settlement(generate_record_id(), PARTIAL_RECEIPT_AMOUNT)
        with self.assertRaises(ValueError):
            ledger.apply_collection_settlement(collection_case_id, Decimal("0"))
        with self.assertRaises(ValueError):
            ledger.apply_collection_settlement(collection_case_id, Decimal("1"))
        with self.assertRaises(ValueError):
            ledger.cancel_stored_payment_intent(generate_record_id())
        stored_intent = ledger.payment_intents[payment_intent_id]
        rejected_intent = replace(
            stored_intent,
            payment_intent_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            source_payload_hash="sha256:" + "e" * 64,
            payment_intent_status="rejected",
        )
        ledger.insert_payment_intent(rejected_intent)
        with self.assertRaises(ValueError):
            ledger.cancel_stored_payment_intent(rejected_intent.payment_intent_id)
        already = ledger.cancel_stored_payment_intent(payment_intent_id)
        again = ledger.cancel_stored_payment_intent(payment_intent_id)
        self.assertEqual(already.payment_intent_status, "cancelled")
        self.assertEqual(again.payment_intent_id, already.payment_intent_id)
        rejected_cancel = PaymentSettlementService(ledger).cancel_payment_intent(
            TENANT_ONE, rejected_intent.payment_intent_id
        )
        self.assertEqual(
            rejected_cancel.rejection_reason_code,
            PaymentSettlementRejectionReasonCode.PAYMENT_INTENT_NOT_PROJECTED,
        )
        missing_cancel_tenant = PaymentSettlementService(ledger).cancel_payment_intent(
            "urn:cwl:missing_tenant", payment_intent_id
        )
        self.assertEqual(
            missing_cancel_tenant.rejection_reason_code,
            PaymentSettlementRejectionReasonCode.TENANT_NOT_FOUND,
        )
        ledger.register_tenant(TENANT_TWO)
        crossed_cancel = PaymentSettlementService(ledger).cancel_payment_intent(
            TENANT_TWO, payment_intent_id
        )
        self.assertEqual(
            crossed_cancel.rejection_reason_code,
            PaymentSettlementRejectionReasonCode.PAYMENT_INTENT_NOT_FOUND,
        )

    def test_orphan_intent_and_replay_without_case_fail_closed(self) -> None:
        """A receipt or cancel cannot proceed when the linked case row is gone."""
        ledger, payment_intent_id, collection_case_id = project_known_morning_intent()
        first = PaymentSettlementService(ledger).record_payment_receipt(
            TENANT_ONE, payment_intent_id, PARTIAL_RECEIPT_AMOUNT
        )
        del ledger.collection_cases[collection_case_id]
        replay = PaymentSettlementService(ledger).record_payment_receipt(
            TENANT_ONE, payment_intent_id, PARTIAL_RECEIPT_AMOUNT
        )
        self.assertEqual(
            replay.rejection_reason_code,
            PaymentSettlementRejectionReasonCode.PAYMENT_INTENT_NOT_FOUND,
        )
        self.assertEqual(first.payment_receipt_status, "applied")
        ledger_two, orphan_intent_id, orphan_case_id = project_known_morning_intent()
        del ledger_two.collection_cases[orphan_case_id]
        orphan_receipt = PaymentSettlementService(ledger_two).record_payment_receipt(
            TENANT_ONE, orphan_intent_id, KNOWN_MORNING_TOTAL
        )
        self.assertEqual(
            orphan_receipt.rejection_reason_code,
            PaymentSettlementRejectionReasonCode.PAYMENT_INTENT_NOT_FOUND,
        )
        orphan_cancel = PaymentSettlementService(ledger_two).cancel_payment_intent(
            TENANT_ONE, orphan_intent_id
        )
        self.assertEqual(
            orphan_cancel.rejection_reason_code,
            PaymentSettlementRejectionReasonCode.PAYMENT_INTENT_NOT_FOUND,
        )
        ledger_three, cancel_intent_id, cancel_case_id = project_known_morning_intent()
        PaymentSettlementService(ledger_three).cancel_payment_intent(TENANT_ONE, cancel_intent_id)
        del ledger_three.collection_cases[cancel_case_id]
        replay_cancel = PaymentSettlementService(ledger_three).cancel_payment_intent(
            TENANT_ONE, cancel_intent_id
        )
        self.assertEqual(
            replay_cancel.rejection_reason_code,
            PaymentSettlementRejectionReasonCode.PAYMENT_INTENT_NOT_FOUND,
        )

    def test_unknown_outcome_and_missing_reason_stay_fail_closed(self) -> None:
        """Unsupported outcome text cannot be serialized as a payment receipt."""
        bogus = PaymentSettlementResult(
            payment_settlement_outcome_code="mystery",  # type: ignore[arg-type]
            settlement_contract_version=1,
            payment_receipt_id=None,
            payment_intent_id=None,
            collection_case_id=None,
            tenant_reference=None,
            currency_code=None,
            payment_receipt_status=None,
            payment_intent_status=None,
            received_amount=None,
            remaining_outstanding_amount=None,
            collection_case_status=None,
            source_payload_hash=None,
            received_at=None,
            next_operator_action="",
            rejection_reason_code=None,
        )
        with self.assertRaises(ValueError):
            bogus.as_contract_dict()
        rejected_without_reason = PaymentSettlementResult(
            payment_settlement_outcome_code=PaymentSettlementOutcomeCode.REJECTED,
            settlement_contract_version=1,
            payment_receipt_id=None,
            payment_intent_id=None,
            collection_case_id=None,
            tenant_reference=None,
            currency_code=None,
            payment_receipt_status=None,
            payment_intent_status=None,
            received_amount=None,
            remaining_outstanding_amount=None,
            collection_case_status=None,
            source_payload_hash=None,
            received_at=None,
            next_operator_action="",
            rejection_reason_code=None,
        )
        self.assertEqual(
            rejected_without_reason.as_contract_dict()["rejection_reason_code"],
            "payment_intent_not_found",
        )
        accepted_without_time = PaymentSettlementResult(
            payment_settlement_outcome_code=PaymentSettlementOutcomeCode.ACCEPTED,
            settlement_contract_version=1,
            payment_receipt_id=generate_record_id(),
            payment_intent_id=generate_record_id(),
            collection_case_id=generate_record_id(),
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            payment_receipt_status="applied",
            payment_intent_status="projected",
            received_amount=KNOWN_MORNING_TOTAL,
            remaining_outstanding_amount=Decimal("0"),
            collection_case_status="settled",
            source_payload_hash="sha256:" + "f" * 64,
            received_at=None,
            next_operator_action="Emit a cash journal proposal to AIS.",
            rejection_reason_code=None,
        )
        with self.assertRaises(ValueError):
            accepted_without_time.as_contract_dict()

    def test_payment_receipt_semantics_require_identity_and_applied_status(self) -> None:
        """Accepted receipts need identity; rejected receipts need a reason; captured is forbidden."""
        valid = {
            "settlement_contract_version": 1,
            "payment_settlement_outcome_code": "accepted",
            "payment_receipt_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf650",
            "tenant_reference": TENANT_ONE,
            "payment_intent_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf640",
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf630",
            "currency_code": "USD",
            "payment_receipt_status": "applied",
            "received_amount": "0.003705",
            "remaining_outstanding_amount": "0",
            "collection_case_status": "settled",
            "source_payload_hash": "sha256:" + "1" * 64,
            "received_at": "2026-08-17T20:15:00Z",
            "next_operator_action": "Emit a cash journal proposal to AIS.",
        }
        self.assertEqual(validate_payment_receipt(valid), ())
        missing_id = json.loads(json.dumps(valid))
        del missing_id["payment_receipt_id"]
        self.assertIn(
            "$: accepted payment receipts must include payment_receipt_id",
            validate_payment_receipt(missing_id),
        )
        replay = json.loads(json.dumps(valid))
        replay["payment_settlement_outcome_code"] = "duplicate_replay"
        del replay["collection_case_id"]
        self.assertIn(
            "$: duplicate_replay payment receipts must include collection_case_id",
            validate_payment_receipt(replay),
        )
        cancelled = {
            "settlement_contract_version": 1,
            "payment_settlement_outcome_code": "accepted",
            "payment_intent_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf640",
            "tenant_reference": TENANT_ONE,
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf630",
            "currency_code": "USD",
            "payment_intent_status": "cancelled",
            "remaining_outstanding_amount": "0.003705",
            "collection_case_status": "open",
            "next_operator_action": "Project a replacement payment_intent if collection should continue.",
        }
        self.assertEqual(validate_payment_receipt(cancelled), ())
        missing_cancel_intent = json.loads(json.dumps(cancelled))
        del missing_cancel_intent["payment_intent_id"]
        self.assertIn(
            "$: accepted payment receipts must include payment_intent_id",
            validate_payment_receipt(missing_cancel_intent),
        )
        rejected = {
            "settlement_contract_version": 1,
            "payment_settlement_outcome_code": "rejected",
            "rejection_reason_code": "payment_intent_not_found",
        }
        self.assertEqual(validate_payment_receipt(rejected), ())
        missing_reason = {
            "settlement_contract_version": 1,
            "payment_settlement_outcome_code": "rejected",
        }
        self.assertIn(
            "$: rejected payment receipts must include rejection_reason_code",
            validate_payment_receipt(missing_reason),
        )
        self.assertTrue(validate_payment_receipt({"settlement_contract_version": 1}))
        self.assertTrue(validate_payment_receipt(["not-an-object"]))
        unknown = json.loads(json.dumps(valid))
        unknown["payment_settlement_outcome_code"] = "mystery"
        self.assertTrue(validate_payment_receipt(unknown))
        captured = json.loads(json.dumps(valid))
        captured["payment_receipt_status"] = "captured"
        self.assertTrue(validate_payment_receipt(captured))

    def test_clock_stamps_received_at(self) -> None:
        """A supplied clock stamps received_at on the append-only receipt."""
        received_at = datetime(2026, 8, 17, 20, 15, tzinfo=UTC)
        ledger, payment_intent_id, _collection_case_id = project_known_morning_intent()
        result = PaymentSettlementService(ledger, clock=lambda: received_at).record_payment_receipt(
            TENANT_ONE, payment_intent_id, KNOWN_MORNING_TOTAL
        )
        self.assertEqual(ledger.payment_receipts[result.payment_receipt_id].received_at, received_at)
        self.assertEqual(validate_payment_receipt(result.as_contract_dict()), ())


if __name__ == "__main__":
    unittest.main()
