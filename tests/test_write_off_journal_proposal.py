"""Write-off journal tests for leftover remaining, replay, and AIS pull."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import UUID

from metering_billing import (
    AccountingExportService,
    CollectionWriteOffService,
    MemoryUsageLedger,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.accounting_export import parse_proposal_amount
from metering_billing.contracts import validate_journal_proposal
from metering_billing.errors import (
    ExactDecimalError,
    JournalProposalOutcomeCode,
    JournalProposalRejectionReasonCode,
)
from metering_billing.usage_ledger import generate_record_id
from test_collection_write_off import open_morning_case_with_outstanding
from test_http_app import invoke_http
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL


PROPOSED_AT = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
LEFTOVER = Decimal("0.001")


def record_morning_write_off(
    outstanding: Decimal | None = None,
) -> tuple[MemoryUsageLedger, UUID, UUID]:
    """Persist one leftover write-off without composing a journal."""
    ledger, collection = open_morning_case_with_outstanding(outstanding)
    written = CollectionWriteOffService(ledger).write_off_collection_case(
        TENANT_ONE, collection.collection_case_id
    )
    if written.collection_write_off_id is None:
        raise AssertionError("known morning path must persist a collection write-off")
    return ledger, written.collection_write_off_id, collection.collection_case_id


class WriteOffJournalProposalTests(unittest.TestCase):
    """Verify write-off exports stay balanced, exact, and proposal-only."""

    def test_known_write_off_emits_balanced_expense_and_receivable_proposal(self) -> None:
        """A stored write-off becomes one balanced write-off-expense/AR proposal."""
        ledger, write_off_id, collection_case_id = record_morning_write_off()
        prior_receipts = len(ledger.payment_receipts)
        prior_settlements = len(ledger.collection_case_settlements)
        outstanding_before = ledger.collection_cases[collection_case_id].outstanding_amount
        result = AccountingExportService(
            ledger, clock=lambda: PROPOSED_AT
        ).propose_write_off_journal(TENANT_ONE, write_off_id)
        self.assertEqual(result.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertIsInstance(result.proposal_id, UUID)
        self.assertEqual(result.proposal_status, "validated")
        self.assertNotEqual(result.proposal_status, "posted")
        self.assertEqual(result.transaction_currency, "USD")
        self.assertEqual(result.intended_book_role_code, "primary_statutory")
        self.assertEqual(result.collection_write_off_id, write_off_id)
        self.assertEqual(
            result.source_event_references,
            (f"{TENANT_ONE}:collection_write_off:{write_off_id}",),
        )
        self.assertEqual(
            result.idempotency_key,
            (
                f"{TENANT_ONE}:collection_write_off:{write_off_id}:"
                f"{result.source_payload_hash}:v{result.proposal_contract_version}"
            ),
        )
        self.assertEqual(len(result.proposal_lines), 2)
        debit, credit = result.proposal_lines
        self.assertEqual(debit.account_role_code, "write_off_expense")
        self.assertEqual(debit.debit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(debit.credit_amount, Decimal("0"))
        self.assertEqual(credit.account_role_code, "accounts_receivable")
        self.assertEqual(credit.debit_amount, Decimal("0"))
        self.assertEqual(credit.credit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(debit.debit_amount + credit.debit_amount, credit.credit_amount)
        self.assertNotIsInstance(debit.debit_amount, float)
        payload = result.as_contract_dict()
        self.assertEqual(validate_journal_proposal(payload), ())
        self.assertNotIn("journal_proposal_outcome_code", payload)
        self.assertNotIn("statutory_account_id", payload)
        self.assertNotIn("chart_account_id", payload)
        self.assertNotIn("card_pan", payload)
        self.assertEqual(ledger.collection_cases[collection_case_id].outstanding_amount, outstanding_before)
        self.assertEqual(len(ledger.journal_proposals), 1)
        self.assertEqual(len(ledger.payment_receipts), prior_receipts)
        self.assertEqual(len(ledger.collection_case_settlements), prior_settlements)
        self.assertEqual(len(ledger.accounting_export_records), 0)

    def test_write_off_accept_does_not_compose_a_journal(self) -> None:
        """#49 write-off stays without a journal until the explicit compose command."""
        ledger, write_off_id, _collection_case_id = record_morning_write_off()
        self.assertEqual(len(ledger.journal_proposals), 0)
        first = AccountingExportService(ledger).propose_write_off_journal(TENANT_ONE, write_off_id)
        self.assertEqual(first.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(len(ledger.journal_proposals), 1)

    def test_second_propose_of_the_same_write_off_is_a_replay(self) -> None:
        """The same tenant and write-off reuse proposal_id and do not grow the store."""
        ledger, write_off_id, _collection_case_id = record_morning_write_off()
        service = AccountingExportService(ledger)
        first = service.propose_write_off_journal(TENANT_ONE, write_off_id)
        second = service.propose_write_off_journal(TENANT_ONE, write_off_id)
        self.assertEqual(first.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(second.journal_proposal_outcome_code, JournalProposalOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(second.proposal_id, first.proposal_id)
        self.assertEqual(second.source_payload_hash, first.source_payload_hash)
        self.assertEqual(second.proposal_lines[0].debit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(len(ledger.journal_proposals), 1)
        self.assertEqual(validate_journal_proposal(second.as_contract_dict()), ())

    def test_invoice_and_cash_journals_stay_separate_identities(self) -> None:
        """A write-off journal sits beside the existing draft AR/revenue proposal."""
        ledger, write_off_id, collection_case_id = record_morning_write_off()
        invoice_draft_id = ledger.collection_cases[collection_case_id].invoice_draft_id
        write_off = AccountingExportService(ledger).propose_write_off_journal(
            TENANT_ONE, write_off_id
        )
        draft = AccountingExportService(ledger).propose_journal(TENANT_ONE, invoice_draft_id)
        self.assertEqual(write_off.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(draft.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertNotEqual(write_off.proposal_id, draft.proposal_id)
        self.assertEqual(write_off.proposal_lines[0].account_role_code, "write_off_expense")
        self.assertEqual(draft.proposal_lines[0].account_role_code, "accounts_receivable")
        self.assertEqual(len(ledger.journal_proposals), 2)

    def test_other_tenant_cannot_see_or_propose_the_first_write_off(self) -> None:
        """A tenant cannot propose or list another tenant's write-off journal."""
        ledger, write_off_id, _collection_case_id = record_morning_write_off()
        service = AccountingExportService(ledger)
        owned = service.propose_write_off_journal(TENANT_ONE, write_off_id)
        crossed = service.propose_write_off_journal(TENANT_TWO, write_off_id)
        self.assertEqual(owned.proposal_lines[0].debit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(crossed.journal_proposal_outcome_code, JournalProposalOutcomeCode.REJECTED)
        self.assertEqual(
            crossed.rejection_reason_code,
            JournalProposalRejectionReasonCode.COLLECTION_WRITE_OFF_NOT_FOUND,
        )
        self.assertNotIn("proposal_id", crossed.as_contract_dict())
        one_rows = ledger.list_journal_proposals(ledger.require_tenant(TENANT_ONE).tenant_account_id)
        two_rows = ledger.list_journal_proposals(ledger.require_tenant(TENANT_TWO).tenant_account_id)
        self.assertEqual(len(one_rows), 1)
        self.assertEqual(one_rows[0].journal_proposal_id, owned.proposal_id)
        self.assertEqual(len(two_rows), 0)

    def test_missing_write_off_and_tenant_fail_closed(self) -> None:
        """A write-off journal cannot invent money without a stored tenant write-off."""
        ledger, _write_off_id, _collection_case_id = record_morning_write_off()
        service = AccountingExportService(ledger)
        missing_write_off = service.propose_write_off_journal(TENANT_ONE, generate_record_id())
        self.assertEqual(
            missing_write_off.journal_proposal_outcome_code,
            JournalProposalOutcomeCode.REJECTED,
        )
        self.assertEqual(
            missing_write_off.rejection_reason_code,
            JournalProposalRejectionReasonCode.COLLECTION_WRITE_OFF_NOT_FOUND,
        )
        missing_tenant = service.propose_write_off_journal(
            "urn:cwl:missing_tenant", generate_record_id()
        )
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            JournalProposalRejectionReasonCode.TENANT_NOT_FOUND,
        )
        self.assertEqual(len(ledger.journal_proposals), 0)

    def test_currency_mismatch_and_invalid_amount_fail_closed(self) -> None:
        """Optional currency must match; zero, negative, and IEEE floats refuse."""
        ledger, write_off_id, _collection_case_id = record_morning_write_off(LEFTOVER)
        service = AccountingExportService(ledger)
        mismatch = service.propose_write_off_journal(
            TENANT_ONE, write_off_id, currency_code="EUR"
        )
        self.assertEqual(
            mismatch.rejection_reason_code,
            JournalProposalRejectionReasonCode.CURRENCY_MISMATCH,
        )
        matched = service.propose_write_off_journal(
            TENANT_ONE, write_off_id, currency_code="USD"
        )
        self.assertEqual(matched.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        stored = ledger.get_collection_write_off(write_off_id)
        ledger.collection_write_offs[write_off_id] = replace(
            stored, write_off_amount=Decimal("0")
        )
        zero = AccountingExportService(ledger).propose_write_off_journal(TENANT_ONE, write_off_id)
        self.assertEqual(zero.journal_proposal_outcome_code, JournalProposalOutcomeCode.DUPLICATE_REPLAY)
        leftover_ledger, leftover_id, _leftover_case = record_morning_write_off(LEFTOVER)
        leftover_stored = leftover_ledger.get_collection_write_off(leftover_id)
        leftover_ledger.collection_write_offs[leftover_id] = replace(
            leftover_stored, write_off_amount=Decimal("0")
        )
        rejected_zero = AccountingExportService(leftover_ledger).propose_write_off_journal(
            TENANT_ONE, leftover_id
        )
        self.assertEqual(
            rejected_zero.rejection_reason_code,
            JournalProposalRejectionReasonCode.WRITE_OFF_AMOUNT_INVALID,
        )
        leftover_ledger.collection_write_offs[leftover_id] = replace(
            leftover_stored, write_off_amount=Decimal("-1")
        )
        rejected_negative = AccountingExportService(leftover_ledger).propose_write_off_journal(
            TENANT_ONE, leftover_id
        )
        self.assertEqual(
            rejected_negative.rejection_reason_code,
            JournalProposalRejectionReasonCode.WRITE_OFF_AMOUNT_INVALID,
        )
        leftover_ledger.collection_write_offs[leftover_id] = replace(
            leftover_stored, write_off_amount=0.003705  # type: ignore[arg-type]
        )
        floated = AccountingExportService(leftover_ledger).propose_write_off_journal(
            TENANT_ONE, leftover_id
        )
        self.assertEqual(
            floated.rejection_reason_code,
            JournalProposalRejectionReasonCode.WRITE_OFF_AMOUNT_INVALID,
        )
        with self.assertRaises(ExactDecimalError):
            parse_proposal_amount(0.003705)
        self.assertEqual(len(leftover_ledger.journal_proposals), 0)
        self.assertEqual(len(ledger.journal_proposals), 1)

    def test_http_compose_lists_write_off_journal_without_a_second_write(self) -> None:
        """POST compose then GET /v1/journal-proposals leaves one validated proposal."""
        ledger, write_off_id, _collection_case_id = record_morning_write_off(LEFTOVER)
        app = create_http_app(ledger)
        refused_status, refused_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-write-offs/{write_off_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "card_pan": "4111111111111111"},
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.journal_proposals), 0)
        mismatch_status, mismatch_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-write-offs/{write_off_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "currency_code": "EUR"},
        )
        self.assertEqual(mismatch_status, 422)
        self.assertEqual(mismatch_body["rejection_reason_code"], "currency_mismatch")
        self.assertEqual(len(ledger.journal_proposals), 0)
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-write-offs/{write_off_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "currency_code": "USD"},
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["proposal_status"], "validated")
        self.assertNotEqual(accepted_body["proposal_status"], "posted")
        self.assertNotIn("journal_proposal_outcome_code", accepted_body)
        self.assertEqual(accepted_body["lines"][0]["account_role_code"], "write_off_expense")
        self.assertEqual(accepted_body["lines"][0]["debit_amount"], format_exact_decimal(LEFTOVER))
        self.assertEqual(accepted_body["lines"][1]["account_role_code"], "accounts_receivable")
        self.assertEqual(accepted_body["lines"][1]["credit_amount"], format_exact_decimal(LEFTOVER))
        self.assertEqual(validate_journal_proposal(accepted_body), ())
        proposal_id = accepted_body["proposal_id"]
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-write-offs/{write_off_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["proposal_id"], proposal_id)
        self.assertEqual(replay_body["proposal_status"], "validated")
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(list_status, 200)
        write_off_items = [
            item
            for item in list_body["journal_proposals"]
            if item["lines"][0]["account_role_code"] == "write_off_expense"
        ]
        self.assertEqual(len(write_off_items), 1)
        self.assertEqual(write_off_items[0]["proposal_id"], proposal_id)
        self.assertTrue(
            write_off_items[0]["idempotency_key"].startswith(
                f"{TENANT_ONE}:collection_write_off:{write_off_id}:"
            )
        )
        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/journal-proposals/{proposal_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["proposal_id"], proposal_id)
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/journal-proposals/{proposal_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        crossed_status, crossed_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-write-offs/{write_off_id}/journal-proposals",
            {"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(crossed_status, 422)
        self.assertEqual(crossed_body["rejection_reason_code"], "collection_write_off_not_found")
        method_status, _method_body = invoke_http(
            app,
            "PUT",
            f"/v1/collection-write-offs/{write_off_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 405)
        self.assertEqual(len(ledger.journal_proposals), 1)
        self.assertEqual(len(ledger.payment_receipts), 0)
        self.assertEqual(len(ledger.collection_case_settlements), 0)

    def test_resolver_and_ledger_guards_cover_identity(self) -> None:
        """Hollow tenant resolve raises; write-off journal rows stay append-only."""
        ledger, write_off_id, _collection_case_id = record_morning_write_off(LEFTOVER)
        service = AccountingExportService(ledger)
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.propose_write_off_journal(TENANT_ONE, write_off_id)
        first = service.propose_write_off_journal(TENANT_ONE, write_off_id)
        stored = ledger.journal_proposals[first.proposal_id]
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(stored, stored.proposal_lines)
        colliding = replace(stored, journal_proposal_id=generate_record_id())
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(colliding, colliding.proposal_lines)
        write_off_collision = replace(
            stored,
            journal_proposal_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            source_payload_hash="sha256:" + "a" * 64,
        )
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(write_off_collision, write_off_collision.proposal_lines)
        self.assertEqual(stored.collection_write_off_id, write_off_id)
        self.assertIsNone(
            ledger.find_journal_proposal_for_write_off(
                stored.tenant_account_id,
                generate_record_id(),
            )
        )
        found = ledger.find_journal_proposal_for_write_off(
            stored.tenant_account_id,
            write_off_id,
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.journal_proposal_id, first.proposal_id)
        empty = AccountingExportService()
        rejected = empty.propose_write_off_journal(TENANT_ONE, generate_record_id())
        self.assertEqual(
            rejected.rejection_reason_code,
            JournalProposalRejectionReasonCode.TENANT_NOT_FOUND,
        )
        currency_type_status, currency_type_body = invoke_http(
            create_http_app(ledger),
            "POST",
            f"/v1/collection-write-offs/{write_off_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "currency_code": 1},
        )
        self.assertEqual(currency_type_status, 422)
        self.assertEqual(currency_type_body["rejection_reason_code"], "request_invalid")


if __name__ == "__main__":
    unittest.main()
