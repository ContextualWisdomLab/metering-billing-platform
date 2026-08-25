"""Cash-journal compose tests for receipt accept and duplicate replay."""

from __future__ import annotations

import unittest
from decimal import Decimal
from uuid import UUID

from metering_billing import (
    AccountingExportService,
    PaymentSettlementService,
    create_http_app,
)
from metering_billing.contracts import validate_journal_proposal
from metering_billing.errors import (
    JournalProposalOutcomeCode,
    PaymentSettlementOutcomeCode,
    PaymentSettlementRejectionReasonCode,
)
from metering_billing.webhook_outbox import (
    EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
    EVENT_TYPE_PAYMENT_RECEIPT_APPLIED,
)
from test_http_app import invoke_http
from test_payment_settlement import PARTIAL_RECEIPT_AMOUNT, project_known_morning_intent
from test_usage_ingestion import TENANT_ONE
from test_usage_rating import KNOWN_MORNING_TOTAL


class CashJournalOnReceiptTests(unittest.TestCase):
    """Verify #12 accept composes the existing #13 cash journal."""

    def test_accepted_receipt_emits_one_validated_cash_journal(self) -> None:
        """A full known-morning receipt must persist one cash/AR proposal."""
        ledger, payment_intent_id, _collection_case_id = project_known_morning_intent()
        result = PaymentSettlementService(ledger).record_payment_receipt(
            TENANT_ONE, payment_intent_id, KNOWN_MORNING_TOTAL
        )
        self.assertEqual(result.payment_settlement_outcome_code, PaymentSettlementOutcomeCode.ACCEPTED)
        self.assertIsInstance(result.payment_receipt_id, UUID)
        self.assertEqual(len(ledger.journal_proposals), 1)
        cash = AccountingExportService(ledger).propose_cash_journal(
            TENANT_ONE, result.payment_receipt_id
        )
        self.assertEqual(cash.journal_proposal_outcome_code, JournalProposalOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(cash.proposal_status, "validated")
        self.assertNotEqual(cash.proposal_status, "posted")
        self.assertEqual(cash.payment_receipt_id, result.payment_receipt_id)
        self.assertEqual(cash.transaction_currency, "USD")
        self.assertEqual(
            cash.idempotency_key,
            (
                f"{TENANT_ONE}:cash_receipt:{result.payment_receipt_id}:"
                f"{cash.source_payload_hash}:v{cash.proposal_contract_version}"
            ),
        )
        debit, credit = cash.proposal_lines
        self.assertEqual(debit.account_role_code, "cash_receipt")
        self.assertEqual(debit.debit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(debit.credit_amount, Decimal("0"))
        self.assertEqual(credit.account_role_code, "accounts_receivable")
        self.assertEqual(credit.credit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(credit.debit_amount, Decimal("0"))
        self.assertEqual(validate_journal_proposal(cash.as_contract_dict()), ())
        self.assertEqual(len(ledger.journal_proposals), 1)
        self.assertEqual(
            result.next_operator_action,
            "The cash journal is already validated for AIS to pull.",
        )
        event_codes = [event.event_type_code for event in ledger.webhook_outbox_events.values()]
        self.assertEqual(event_codes.count(EVENT_TYPE_PAYMENT_RECEIPT_APPLIED), 1)
        self.assertEqual(event_codes.count(EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED), 1)

    def test_receipt_replay_does_not_write_a_second_cash_journal(self) -> None:
        """Duplicate receipt replay must reuse the stored cash proposal_id."""
        ledger, payment_intent_id, _collection_case_id = project_known_morning_intent()
        service = PaymentSettlementService(ledger)
        first = service.record_payment_receipt(TENANT_ONE, payment_intent_id, PARTIAL_RECEIPT_AMOUNT)
        first_cash = AccountingExportService(ledger).propose_cash_journal(
            TENANT_ONE, first.payment_receipt_id
        )
        second = service.record_payment_receipt(TENANT_ONE, payment_intent_id, PARTIAL_RECEIPT_AMOUNT)
        second_cash = AccountingExportService(ledger).propose_cash_journal(
            TENANT_ONE, second.payment_receipt_id
        )
        self.assertEqual(second.payment_settlement_outcome_code, PaymentSettlementOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(second.payment_receipt_id, first.payment_receipt_id)
        self.assertEqual(second_cash.proposal_id, first_cash.proposal_id)
        self.assertEqual(len(ledger.journal_proposals), 1)
        self.assertEqual(
            [
                event.event_type_code
                for event in ledger.webhook_outbox_events.values()
            ].count(EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED),
            1,
        )
        self.assertEqual(
            second.next_operator_action,
            "The cash journal is already validated for AIS to pull, or record another partial receipt.",
        )

    def test_rejected_receipt_and_cancel_do_not_emit_a_cash_journal(self) -> None:
        """A refused receipt or cancel must not invent a cash proposal."""
        ledger, payment_intent_id, _collection_case_id = project_known_morning_intent()
        service = PaymentSettlementService(ledger)
        rejected = service.record_payment_receipt(TENANT_ONE, payment_intent_id, Decimal("0"))
        self.assertEqual(
            rejected.rejection_reason_code,
            PaymentSettlementRejectionReasonCode.PAYMENT_AMOUNT_INVALID,
        )
        self.assertEqual(len(ledger.journal_proposals), 0)
        cancelled = service.cancel_payment_intent(TENANT_ONE, payment_intent_id)
        self.assertEqual(cancelled.payment_settlement_outcome_code, PaymentSettlementOutcomeCode.ACCEPTED)
        self.assertEqual(len(ledger.journal_proposals), 0)

    def test_http_receipt_accept_lists_cash_journal_without_a_second_write(self) -> None:
        """POST /v1/payment-receipts must leave a validated cash proposal for AIS."""
        ledger, payment_intent_id, _collection_case_id = project_known_morning_intent()
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "POST",
            "/v1/payment-receipts",
            {
                "tenant_reference": TENANT_ONE,
                "payment_intent_id": str(payment_intent_id),
                "received_amount": str(KNOWN_MORNING_TOTAL),
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["payment_settlement_outcome_code"], "accepted")
        payment_receipt_id = body["payment_receipt_id"]
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(list_status, 200)
        cash_items = [
            item
            for item in list_body["journal_proposals"]
            if item["lines"][0]["account_role_code"] == "cash_receipt"
        ]
        self.assertEqual(len(cash_items), 1)
        cash_item = cash_items[0]
        self.assertEqual(cash_item["proposal_status"], "validated")
        self.assertNotEqual(cash_item["proposal_status"], "posted")
        self.assertTrue(
            cash_item["idempotency_key"].startswith(f"{TENANT_ONE}:cash_receipt:{payment_receipt_id}:")
        )
        self.assertEqual(cash_item["lines"][0]["debit_amount"], str(KNOWN_MORNING_TOTAL))
        self.assertEqual(cash_item["lines"][1]["account_role_code"], "accounts_receivable")
        self.assertEqual(validate_journal_proposal(cash_item), ())
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            "/v1/cash-journal-proposals",
            {"tenant_reference": TENANT_ONE, "payment_receipt_id": payment_receipt_id},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["proposal_id"], cash_item["proposal_id"])
        self.assertEqual(replay_body["proposal_status"], "validated")
        self.assertEqual(len(ledger.journal_proposals), 1)
