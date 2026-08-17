"""Realistic credit-adjustment tests for remaining, settlement, replay, and journals."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import UUID, uuid4

from metering_billing import (
    AccountingExportService,
    CollectionCaseService,
    CreditAdjustmentService,
    InvoiceDraftService,
    MemoryUsageLedger,
    PaymentIntentService,
    PaymentSettlementService,
    UsageIngestionService,
    UsageRatingService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.http_app import HttpRequestError, _dispatch_write
from metering_billing.contracts import validate_credit_adjustment, validate_journal_proposal
from metering_billing.errors import (
    CreditAdjustmentOutcomeCode,
    CreditAdjustmentQueryError,
    CreditAdjustmentRejectionReasonCode,
    ExactDecimalError,
)
from metering_billing.credit_adjustment import (
    CREDIT_ADJUSTMENT_CONTRACT_VERSION,
    CreditAdjustmentResult,
    compute_credit_payload_hash,
    parse_credit_amount,
)
from metering_billing.usage_ledger import StoredCreditAdjustment, generate_record_id
from test_cash_journal_proposal import record_known_morning_receipt
from test_collection_case import draft_known_morning
from test_http_app import invoke_http
from test_payment_intent import open_known_morning_case
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL


PARTIAL_CREDIT_AMOUNT = Decimal("0.001000")


def _invoice_draft_id_for_case(ledger: MemoryUsageLedger, collection_case_id: UUID) -> UUID:
    """Return the draft identity stored on one collection case."""
    return ledger.collection_cases[collection_case_id].invoice_draft_id


class CreditAdjustmentTests(unittest.TestCase):
    """Verify commercial credits stay exact, tenant-scoped, and proposal-only."""

    def test_full_credit_settles_the_case_and_emits_revenue_debit(self) -> None:
        """A known full credit must settle outstanding and reverse AR/revenue."""
        ledger, collection_case_id = open_known_morning_case()
        invoice_draft_id = _invoice_draft_id_for_case(ledger, collection_case_id)
        result = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, KNOWN_MORNING_TOTAL, "rating_correction"
        )
        self.assertEqual(result.credit_adjustment_outcome_code, CreditAdjustmentOutcomeCode.ACCEPTED)
        self.assertIsInstance(result.credit_adjustment_id, UUID)
        self.assertIsInstance(result.proposal_id, UUID)
        self.assertEqual(result.credit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(result.credit_reason_code, "rating_correction")
        self.assertEqual(result.remaining_adjustable_amount, Decimal("0"))
        self.assertEqual(result.remaining_outstanding_amount, Decimal("0"))
        self.assertEqual(result.collection_case_status, "settled")
        self.assertEqual(result.proposal_status, "validated")
        self.assertEqual(
            result.idempotency_key,
            (
                f"{TENANT_ONE}:credit_adjustment:{result.credit_adjustment_id}:"
                f"{result.source_payload_hash}:v{result.credit_adjustment_contract_version}"
            ),
        )
        self.assertEqual(validate_credit_adjustment(result.as_contract_dict()), ())
        stored_case = ledger.collection_cases[collection_case_id]
        self.assertEqual(stored_case.outstanding_amount, Decimal("0"))
        self.assertEqual(stored_case.collection_case_status, "settled")
        proposal = ledger.get_journal_proposal(result.proposal_id)
        assert proposal is not None
        self.assertEqual(proposal.proposal_status, "validated")
        self.assertEqual(proposal.intended_book_role_code, "primary_statutory")
        debit, credit = proposal.proposal_lines
        self.assertEqual(debit.account_role_code, "usage_revenue")
        self.assertEqual(debit.debit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(debit.credit_amount, Decimal("0"))
        self.assertEqual(credit.account_role_code, "accounts_receivable")
        self.assertEqual(credit.debit_amount, Decimal("0"))
        self.assertEqual(credit.credit_amount, KNOWN_MORNING_TOTAL)
        exported = AccountingExportService(ledger).get_journal_proposal(
            TENANT_ONE, result.proposal_id
        )
        self.assertEqual(validate_journal_proposal(exported.as_contract_dict()), ())
        self.assertNotEqual(exported.as_contract_dict()["proposal_status"], "posted")

    def test_partial_credit_leaves_residual_outstanding(self) -> None:
        """A partial credit must reduce outstanding without settling the case."""
        ledger, collection_case_id = open_known_morning_case()
        invoice_draft_id = _invoice_draft_id_for_case(ledger, collection_case_id)
        result = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, PARTIAL_CREDIT_AMOUNT, "goodwill"
        )
        remaining = KNOWN_MORNING_TOTAL - PARTIAL_CREDIT_AMOUNT
        self.assertEqual(result.credit_adjustment_outcome_code, CreditAdjustmentOutcomeCode.ACCEPTED)
        self.assertEqual(result.remaining_adjustable_amount, remaining)
        self.assertEqual(result.remaining_outstanding_amount, remaining)
        self.assertEqual(result.collection_case_status, "open")
        self.assertEqual(ledger.collection_cases[collection_case_id].outstanding_amount, remaining)
        self.assertEqual(len(ledger.credit_adjustments), 1)

    def test_second_credit_of_the_same_identity_is_a_replay(self) -> None:
        """The same tenant, draft, amount, reason, hash, and version reuse IDs."""
        ledger, collection_case_id = open_known_morning_case()
        invoice_draft_id = _invoice_draft_id_for_case(ledger, collection_case_id)
        service = CreditAdjustmentService(ledger)
        first = service.record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, PARTIAL_CREDIT_AMOUNT, "billing_error"
        )
        second = service.record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, PARTIAL_CREDIT_AMOUNT, "billing_error"
        )
        self.assertEqual(first.credit_adjustment_outcome_code, CreditAdjustmentOutcomeCode.ACCEPTED)
        self.assertEqual(second.credit_adjustment_outcome_code, CreditAdjustmentOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(second.credit_adjustment_id, first.credit_adjustment_id)
        self.assertEqual(second.proposal_id, first.proposal_id)
        self.assertEqual(second.source_payload_hash, first.source_payload_hash)
        remaining = KNOWN_MORNING_TOTAL - PARTIAL_CREDIT_AMOUNT
        self.assertEqual(ledger.collection_cases[collection_case_id].outstanding_amount, remaining)
        self.assertEqual(len(ledger.credit_adjustments), 1)
        self.assertEqual(len(ledger.journal_proposals), 1)
        self.assertEqual(validate_credit_adjustment(second.as_contract_dict()), ())

    def test_other_tenant_cannot_credit_or_fetch_the_first_draft(self) -> None:
        """A tenant cannot credit or GET another tenant's draft or adjustment."""
        ledger, collection_case_id = open_known_morning_case()
        invoice_draft_id = _invoice_draft_id_for_case(ledger, collection_case_id)
        ledger.register_tenant(TENANT_TWO)
        service = CreditAdjustmentService(ledger)
        first = service.record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, PARTIAL_CREDIT_AMOUNT, "goodwill"
        )
        other = service.record_credit_adjustment(
            TENANT_TWO, invoice_draft_id, PARTIAL_CREDIT_AMOUNT, "goodwill"
        )
        self.assertEqual(other.credit_adjustment_outcome_code, CreditAdjustmentOutcomeCode.REJECTED)
        self.assertEqual(
            other.rejection_reason_code,
            CreditAdjustmentRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND,
        )
        self.assertEqual(len(ledger.credit_adjustments), 1)
        with self.assertRaises(CreditAdjustmentQueryError) as error:
            service.get_credit_adjustment(TENANT_TWO, first.credit_adjustment_id)
        self.assertEqual(error.exception.rejection_reason_code, "credit_adjustment_not_found")

    def test_credit_after_settlement_rejects_without_writing(self) -> None:
        """A settled case cannot accept another commercial credit."""
        ledger, collection_case_id = open_known_morning_case()
        invoice_draft_id = _invoice_draft_id_for_case(ledger, collection_case_id)
        service = CreditAdjustmentService(ledger)
        first = service.record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, KNOWN_MORNING_TOTAL, "rating_correction"
        )
        self.assertEqual(first.collection_case_status, "settled")
        second = service.record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, PARTIAL_CREDIT_AMOUNT, "goodwill"
        )
        self.assertEqual(second.credit_adjustment_outcome_code, CreditAdjustmentOutcomeCode.REJECTED)
        self.assertEqual(
            second.rejection_reason_code,
            CreditAdjustmentRejectionReasonCode.CREDIT_EXCEEDS_REMAINING,
        )
        self.assertEqual(len(ledger.credit_adjustments), 1)
        self.assertEqual(ledger.collection_cases[collection_case_id].outstanding_amount, Decimal("0"))

        paid_ledger, payment_receipt_id, paid_case_id = record_known_morning_receipt()
        paid_draft_id = paid_ledger.collection_cases[paid_case_id].invoice_draft_id
        self.assertEqual(paid_ledger.collection_cases[paid_case_id].collection_case_status, "settled")
        after_payment = CreditAdjustmentService(paid_ledger).record_credit_adjustment(
            TENANT_ONE, paid_draft_id, PARTIAL_CREDIT_AMOUNT, "goodwill"
        )
        self.assertEqual(
            after_payment.rejection_reason_code,
            CreditAdjustmentRejectionReasonCode.CREDIT_EXCEEDS_OUTSTANDING,
        )
        self.assertEqual(len(paid_ledger.credit_adjustments), 0)
        del payment_receipt_id

    def test_fail_closed_inputs_and_remaining_limit(self) -> None:
        """Missing draft, unknown reason, floats, and over-remaining amounts fail closed."""
        ledger, invoice_draft_id = draft_known_morning()
        service = CreditAdjustmentService(ledger)
        missing_tenant = service.record_credit_adjustment(
            "", invoice_draft_id, PARTIAL_CREDIT_AMOUNT, "goodwill"
        )
        unknown_tenant = service.record_credit_adjustment(
            "urn:cwl:missing_tenant", invoice_draft_id, PARTIAL_CREDIT_AMOUNT, "goodwill"
        )
        missing_draft = service.record_credit_adjustment(
            TENANT_ONE, generate_record_id(), PARTIAL_CREDIT_AMOUNT, "goodwill"
        )
        unknown_reason = service.record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, PARTIAL_CREDIT_AMOUNT, "tax_refund"
        )
        zero = service.record_credit_adjustment(TENANT_ONE, invoice_draft_id, Decimal("0"), "goodwill")
        negative = service.record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, Decimal("-1"), "goodwill"
        )
        floated = service.record_credit_adjustment(TENANT_ONE, invoice_draft_id, 0.001, "goodwill")
        too_much = service.record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, KNOWN_MORNING_TOTAL + Decimal("0.001"), "goodwill"
        )
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            CreditAdjustmentRejectionReasonCode.TENANT_NOT_FOUND,
        )
        self.assertEqual(
            unknown_tenant.rejection_reason_code,
            CreditAdjustmentRejectionReasonCode.TENANT_NOT_FOUND,
        )
        self.assertEqual(
            missing_draft.rejection_reason_code,
            CreditAdjustmentRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND,
        )
        self.assertEqual(
            unknown_reason.rejection_reason_code,
            CreditAdjustmentRejectionReasonCode.CREDIT_REASON_INVALID,
        )
        self.assertEqual(
            zero.rejection_reason_code,
            CreditAdjustmentRejectionReasonCode.CREDIT_AMOUNT_INVALID,
        )
        self.assertEqual(
            negative.rejection_reason_code,
            CreditAdjustmentRejectionReasonCode.CREDIT_AMOUNT_INVALID,
        )
        self.assertEqual(
            floated.rejection_reason_code,
            CreditAdjustmentRejectionReasonCode.CREDIT_AMOUNT_INVALID,
        )
        self.assertEqual(
            too_much.rejection_reason_code,
            CreditAdjustmentRejectionReasonCode.CREDIT_EXCEEDS_REMAINING,
        )
        self.assertEqual(len(ledger.credit_adjustments), 0)
        self.assertEqual(len(ledger.journal_proposals), 0)
        with self.assertRaises(ExactDecimalError):
            parse_credit_amount(0.25)

    def test_credit_without_a_collection_case_still_emits_a_proposal(self) -> None:
        """A draft-only credit records remaining adjustable and a validated proposal."""
        ledger, invoice_draft_id = draft_known_morning()
        result = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, PARTIAL_CREDIT_AMOUNT, "goodwill"
        )
        self.assertEqual(result.credit_adjustment_outcome_code, CreditAdjustmentOutcomeCode.ACCEPTED)
        self.assertIsNone(result.collection_case_id)
        self.assertIsNone(result.collection_case_status)
        self.assertIsNone(result.remaining_outstanding_amount)
        self.assertEqual(
            result.remaining_adjustable_amount, KNOWN_MORNING_TOTAL - PARTIAL_CREDIT_AMOUNT
        )
        self.assertEqual(result.proposal_status, "validated")
        self.assertEqual(len(ledger.journal_proposals), 1)
        self.assertNotIn("collection_case_id", result.as_contract_dict())
        self.assertEqual(validate_credit_adjustment(result.as_contract_dict()), ())

    def test_http_post_and_get_include_credit_proposal_on_journal_list(self) -> None:
        """Operators POST a credit; AIS GET list includes the validated proposal."""
        ledger, collection_case_id = open_known_morning_case()
        invoice_draft_id = _invoice_draft_id_for_case(ledger, collection_case_id)
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "POST",
            "/v1/credit-adjustments",
            {
                "tenant_reference": TENANT_ONE,
                "invoice_draft_id": str(invoice_draft_id),
                "credit_amount": format_exact_decimal(PARTIAL_CREDIT_AMOUNT),
                "credit_reason_code": "rating_correction",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["credit_adjustment_outcome_code"], "accepted")
        self.assertIn("proposal_id", body)
        self.assertEqual(body["proposal_status"], "validated")
        self.assertEqual(validate_credit_adjustment(body), ())
        credit_adjustment_id = body["credit_adjustment_id"]
        proposal_id = body["proposal_id"]

        replay_status, replay_body = invoke_http(
            app,
            "POST",
            "/v1/credit-adjustments",
            {
                "invoice_draft_id": str(invoice_draft_id),
                "credit_amount": format_exact_decimal(PARTIAL_CREDIT_AMOUNT),
                "credit_reason_code": "rating_correction",
            },
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["credit_adjustment_outcome_code"], "duplicate_replay")
        self.assertEqual(replay_body["credit_adjustment_id"], credit_adjustment_id)
        self.assertEqual(replay_body["proposal_id"], proposal_id)

        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(list_status, 200)
        proposal_ids = [item["proposal_id"] for item in list_body["journal_proposals"]]
        self.assertIn(proposal_id, proposal_ids)
        matched = next(
            item for item in list_body["journal_proposals"] if item["proposal_id"] == proposal_id
        )
        self.assertEqual(matched["proposal_status"], "validated")
        self.assertEqual(matched["lines"][0]["account_role_code"], "usage_revenue")
        self.assertEqual(matched["lines"][1]["account_role_code"], "accounts_receivable")

        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/credit-adjustments/{credit_adjustment_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["credit_adjustment_id"], credit_adjustment_id)
        self.assertEqual(get_body["invoice_draft_id"], str(invoice_draft_id))
        self.assertEqual(get_body["next_operator_action"], "wait")
        self.assertNotIn("proposal_id", get_body)

        ledger.register_tenant(TENANT_TWO)
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/credit-adjustments/{credit_adjustment_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "credit_adjustment_not_found")

        missing_status, missing_body = invoke_http(
            app,
            "POST",
            "/v1/credit-adjustments",
            {
                "credit_amount": format_exact_decimal(PARTIAL_CREDIT_AMOUNT),
                "credit_reason_code": "goodwill",
            },
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")

        pin_status, pin_body = invoke_http(
            app,
            "POST",
            "/v1/credit-adjustments",
            {
                "tenant_reference": TENANT_ONE,
                "invoice_draft_id": str(invoice_draft_id),
                "credit_amount": format_exact_decimal(PARTIAL_CREDIT_AMOUNT),
                "credit_reason_code": "goodwill",
            },
            headers={"X-CWL-Tenant-Reference": TENANT_TWO},
        )
        self.assertEqual(pin_status, 422)
        self.assertEqual(pin_body["rejection_reason_code"], "request_invalid")

        get_missing_tenant, get_missing_body = invoke_http(
            app,
            "GET",
            f"/v1/credit-adjustments/{credit_adjustment_id}",
        )
        self.assertEqual(get_missing_tenant, 422)
        self.assertEqual(get_missing_body["rejection_reason_code"], "tenant_not_found")

        method_status, method_body = invoke_http(app, "PUT", "/v1/credit-adjustments")
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")

        post_item_status, post_item_body = invoke_http(
            app,
            "POST",
            f"/v1/credit-adjustments/{credit_adjustment_id}",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(post_item_status, 422)
        self.assertEqual(post_item_body["rejection_reason_code"], "request_invalid")

    def test_result_helpers_and_ledger_identity_are_append_only(self) -> None:
        """Helpers fail closed and the ledger rejects conflicting credit identities."""
        rejected = CreditAdjustmentResult(
            credit_adjustment_outcome_code=CreditAdjustmentOutcomeCode.REJECTED,
            credit_adjustment_contract_version=1,
            credit_adjustment_id=None,
            invoice_draft_id=None,
            tenant_reference=None,
            currency_code=None,
            credit_amount=None,
            credit_reason_code=None,
            remaining_adjustable_amount=None,
            remaining_outstanding_amount=None,
            collection_case_id=None,
            collection_case_status=None,
            proposal_id=None,
            proposal_status=None,
            source_payload_hash=None,
            idempotency_key=None,
            recorded_at=None,
            next_operator_action="Let AIS pull the validated credit journal proposal.",
            rejection_reason_code=None,
        )
        rejected_body = rejected.as_contract_dict()
        self.assertEqual(rejected_body["credit_adjustment_outcome_code"], "rejected")
        self.assertEqual(rejected_body["rejection_reason_code"], "invoice_draft_not_found")
        with self.assertRaises(ValueError):
            CreditAdjustmentResult(
                credit_adjustment_outcome_code="nope",  # type: ignore[arg-type]
                credit_adjustment_contract_version=1,
                credit_adjustment_id=None,
                invoice_draft_id=None,
                tenant_reference=None,
                currency_code=None,
                credit_amount=None,
                credit_reason_code=None,
                remaining_adjustable_amount=None,
                remaining_outstanding_amount=None,
                collection_case_id=None,
                collection_case_status=None,
                proposal_id=None,
                proposal_status=None,
                source_payload_hash=None,
                idempotency_key=None,
                recorded_at=None,
                next_operator_action="Let AIS pull the validated credit journal proposal.",
                rejection_reason_code=None,
            ).as_contract_dict()
        with self.assertRaises(ValueError):
            CreditAdjustmentResult(
                credit_adjustment_outcome_code=CreditAdjustmentOutcomeCode.ACCEPTED,
                credit_adjustment_contract_version=1,
                credit_adjustment_id=None,
                invoice_draft_id=None,
                tenant_reference=None,
                currency_code=None,
                credit_amount=None,
                credit_reason_code=None,
                remaining_adjustable_amount=None,
                remaining_outstanding_amount=None,
                collection_case_id=None,
                collection_case_status=None,
                proposal_id=None,
                proposal_status=None,
                source_payload_hash=None,
                idempotency_key=None,
                recorded_at=None,
                next_operator_action="Let AIS pull the validated credit journal proposal.",
                rejection_reason_code=None,
            ).as_contract_dict()

        ledger, invoice_draft_id = draft_known_morning()
        tenant = ledger.require_tenant(TENANT_ONE)
        invoice_draft = ledger.get_invoice_draft(invoice_draft_id)
        assert invoice_draft is not None
        first = StoredCreditAdjustment(
            credit_adjustment_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            invoice_draft_id=invoice_draft_id,
            credit_adjustment_contract_version=CREDIT_ADJUSTMENT_CONTRACT_VERSION,
            credit_reason_code="goodwill",
            currency_code=invoice_draft.currency_code,
            credit_amount=PARTIAL_CREDIT_AMOUNT,
            tax_exclusive_amount=PARTIAL_CREDIT_AMOUNT,
            tax_amount=Decimal("0"),
            source_payload_hash=compute_credit_payload_hash(
                {
                    "invoice_draft_id": str(invoice_draft_id),
                    "credit_amount": format_exact_decimal(PARTIAL_CREDIT_AMOUNT),
                    "credit_reason_code": "goodwill",
                    "currency_code": invoice_draft.currency_code,
                    "credit_adjustment_contract_version": CREDIT_ADJUSTMENT_CONTRACT_VERSION,
                }
            ),
            recorded_at=datetime(2026, 8, 17, 19, 0, tzinfo=UTC),
        )
        stored = ledger.insert_credit_adjustment(first)
        replay = ledger.insert_credit_adjustment(
            replace(first, credit_adjustment_id=generate_record_id())
        )
        self.assertEqual(stored.credit_adjustment_id, first.credit_adjustment_id)
        self.assertEqual(replay.credit_adjustment_id, first.credit_adjustment_id)
        with self.assertRaises(ValueError):
            ledger.insert_credit_adjustment(
                replace(
                    first,
                    invoice_draft_id=uuid4(),
                    source_payload_hash="sha256:" + ("d" * 64),
                )
            )
        with self.assertRaises(ValueError):
            ledger.insert_credit_adjustment(replace(first, credit_reason_code="tax_refund"))
        with self.assertRaises(ValueError):
            ledger.insert_credit_adjustment(
                replace(first, tax_exclusive_amount=Decimal("1"), tax_amount=Decimal("1"))
            )
        with self.assertRaises(ValueError):
            ledger.insert_credit_adjustment(
                replace(
                    first,
                    credit_adjustment_id=generate_record_id(),
                    invoice_draft_id=uuid4(),
                    source_payload_hash="sha256:" + ("e" * 64),
                    credit_amount=Decimal("0"),
                )
            )
        self.assertEqual(
            ledger.find_credit_adjustment(
                tenant.tenant_account_id,
                invoice_draft_id,
                first.source_payload_hash,
                CREDIT_ADJUSTMENT_CONTRACT_VERSION,
            ),
            first,
        )
        self.assertIsNone(
            ledger.find_credit_adjustment(tenant.tenant_account_id, invoice_draft_id, "missing", 1)
        )
        self.assertEqual(ledger.list_credit_adjustments(tenant.tenant_account_id), (first,))
        self.assertEqual(ledger.list_credit_adjustments(), (first,))
        with self.assertRaises(CreditAdjustmentQueryError) as missing_key:
            CreditAdjustmentService(ledger).get_credit_adjustment(TENANT_ONE, None)  # type: ignore[arg-type]
        self.assertEqual(missing_key.exception.rejection_reason_code, "credit_adjustment_not_found")
        with self.assertRaises(CreditAdjustmentQueryError) as missing_tenant:
            CreditAdjustmentService(ledger).get_credit_adjustment("", first.credit_adjustment_id)
        self.assertEqual(missing_tenant.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(CreditAdjustmentQueryError) as unknown_tenant:
            CreditAdjustmentService(ledger).get_credit_adjustment(
                "urn:cwl:missing_tenant", first.credit_adjustment_id
            )
        self.assertEqual(unknown_tenant.exception.rejection_reason_code, "tenant_not_found")
        same_row = ledger.insert_credit_adjustment(first)
        self.assertEqual(same_row.credit_adjustment_id, first.credit_adjustment_id)
        with self.assertRaises(CreditAdjustmentQueryError) as missing_proposal:
            CreditAdjustmentService(ledger).get_credit_adjustment(
                TENANT_ONE, first.credit_adjustment_id
            )
        self.assertEqual(missing_proposal.exception.rejection_reason_code, "credit_adjustment_not_found")
        replay_without_journal = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, PARTIAL_CREDIT_AMOUNT, "goodwill"
        )
        self.assertEqual(
            replay_without_journal.rejection_reason_code,
            CreditAdjustmentRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND,
        )

        accepted = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, PARTIAL_CREDIT_AMOUNT, "billing_error"
        )
        assert accepted.credit_adjustment_id is not None
        assert accepted.proposal_id is not None
        proposal = ledger.get_journal_proposal(accepted.proposal_id)
        assert proposal is not None
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(
                replace(
                    proposal,
                    journal_proposal_id=generate_record_id(),
                    invoice_draft_id=uuid4(),
                ),
                proposal.proposal_lines,
            )
        del ledger.invoice_drafts[invoice_draft_id]
        with self.assertRaises(CreditAdjustmentQueryError) as missing_draft:
            CreditAdjustmentService(ledger).get_credit_adjustment(
                TENANT_ONE, accepted.credit_adjustment_id
            )
        self.assertEqual(missing_draft.exception.rejection_reason_code, "credit_adjustment_not_found")

        unknown_get_status, unknown_get_body = invoke_http(
            create_http_app(ledger),
            "GET",
            f"/v1/credit-adjustments/{accepted.credit_adjustment_id}",
            query={"tenant_reference": "urn:cwl:missing_tenant"},
        )
        self.assertEqual(unknown_get_status, 422)
        self.assertEqual(unknown_get_body["rejection_reason_code"], "tenant_not_found")
        with mock.patch(
            "metering_billing.http_app.CreditAdjustmentPresentmentService.present_credit_adjustment",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                f"/v1/credit-adjustments/{accepted.credit_adjustment_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        invalid_uuid_status, invalid_uuid_body = invoke_http(
            create_http_app(ledger),
            "GET",
            "/v1/credit-adjustments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(invalid_uuid_status, 422)
        self.assertEqual(invalid_uuid_body["rejection_reason_code"], "request_invalid")
        self.assertTrue(validate_credit_adjustment(["not a mapping"]))
        rejected_missing_reason = validate_credit_adjustment(
            {"credit_adjustment_contract_version": 1, "credit_adjustment_outcome_code": "rejected"}
        )
        self.assertIn(
            "$: rejected credit adjustments must include rejection_reason_code",
            rejected_missing_reason,
        )
        accepted_missing_id = validate_credit_adjustment(
            {
                "credit_adjustment_contract_version": 1,
                "credit_adjustment_outcome_code": "accepted",
            }
        )
        self.assertTrue(
            any("credit_adjustment_id" in error for error in accepted_missing_id)
        )
        self.assertTrue(
            validate_credit_adjustment({"credit_adjustment_contract_version": 1})
        )
        self.assertEqual(
            validate_credit_adjustment(
                {
                    "credit_adjustment_contract_version": 1,
                    "credit_adjustment_outcome_code": "rejected",
                    "rejection_reason_code": "tenant_not_found",
                }
            ),
            (),
        )
        with self.assertRaises(HttpRequestError) as missing_credits:
            _dispatch_write(
                "credit_adjustments",
                {},
                TENANT_ONE,
                {
                    "invoice_draft_id": str(invoice_draft_id),
                    "credit_amount": format_exact_decimal(PARTIAL_CREDIT_AMOUNT),
                    "credit_reason_code": "goodwill",
                },
                UsageIngestionService(ledger),
                UsageRatingService(ledger),
                InvoiceDraftService(ledger),
                AccountingExportService(ledger),
                CollectionCaseService(ledger),
                PaymentIntentService(ledger),
                PaymentSettlementService(ledger),
            )
        self.assertEqual(missing_credits.exception.rejection_reason_code, "request_invalid")
        missing_reason_status, missing_reason_body = invoke_http(
            create_http_app(ledger),
            "POST",
            "/v1/credit-adjustments",
            {
                "tenant_reference": TENANT_ONE,
                "invoice_draft_id": str(invoice_draft_id),
                "credit_amount": format_exact_decimal(PARTIAL_CREDIT_AMOUNT),
            },
        )
        self.assertEqual(missing_reason_status, 422)
        self.assertEqual(missing_reason_body["rejection_reason_code"], "request_invalid")


if __name__ == "__main__":
    unittest.main()
