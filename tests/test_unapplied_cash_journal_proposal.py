"""Unapplied-cash journal tests for parked leftover, replay, and AIS pull."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import UUID

from metering_billing import (
    AccountingExportService,
    MemoryUsageLedger,
    UnappliedCashService,
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
from test_http_app import invoke_http
from test_unapplied_cash import LEFTOVER
from test_unapplied_cash_refund import _park_leftover
from test_usage_ingestion import TENANT_ONE, TENANT_TWO


PROPOSED_AT = datetime(2026, 8, 18, 19, 0, tzinfo=UTC)


def park_morning_leftover() -> tuple[MemoryUsageLedger, UUID]:
    """Persist one parked leftover without composing a leftover journal."""
    ledger, parked, _receipt_id = _park_leftover()
    if parked.unapplied_cash_id is None:
        raise AssertionError("known morning path must persist parked leftover")
    return ledger, parked.unapplied_cash_id


class UnappliedCashJournalProposalTests(unittest.TestCase):
    """Verify leftover-park exports stay balanced, exact, and proposal-only."""

    def test_known_parked_leftover_emits_balanced_cash_and_unapplied_cash_proposal(
        self,
    ) -> None:
        """A stored parked leftover becomes one balanced cash/unapplied-cash proposal."""
        ledger, leftover_id = park_morning_leftover()
        prior_receipts = len(ledger.payment_receipts)
        prior_write_offs = len(ledger.collection_write_offs)
        prior_settlements = len(ledger.collection_case_settlements)
        prior_refunds = len(ledger.unapplied_cash_refunds)
        prior_leftovers = len(ledger.unapplied_cash)
        result = AccountingExportService(
            ledger, clock=lambda: PROPOSED_AT
        ).propose_unapplied_cash_journal(TENANT_ONE, leftover_id)
        self.assertEqual(result.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertIsInstance(result.proposal_id, UUID)
        self.assertEqual(result.proposal_status, "validated")
        self.assertNotEqual(result.proposal_status, "posted")
        self.assertEqual(result.transaction_currency, "USD")
        self.assertEqual(result.intended_book_role_code, "primary_statutory")
        self.assertEqual(result.unapplied_cash_id, leftover_id)
        self.assertEqual(
            result.source_event_references,
            (f"{TENANT_ONE}:unapplied_cash:{leftover_id}",),
        )
        self.assertEqual(
            result.idempotency_key,
            (
                f"{TENANT_ONE}:unapplied_cash:{leftover_id}:"
                f"{result.source_payload_hash}:v{result.proposal_contract_version}"
            ),
        )
        self.assertEqual(len(result.proposal_lines), 2)
        debit, credit = result.proposal_lines
        self.assertEqual(debit.account_role_code, "cash_receipt")
        self.assertEqual(debit.debit_amount, LEFTOVER)
        self.assertEqual(debit.credit_amount, Decimal("0"))
        self.assertEqual(credit.account_role_code, "unapplied_cash")
        self.assertEqual(credit.debit_amount, Decimal("0"))
        self.assertEqual(credit.credit_amount, LEFTOVER)
        self.assertEqual(debit.debit_amount + credit.debit_amount, credit.credit_amount)
        self.assertNotIsInstance(debit.debit_amount, float)
        payload = result.as_contract_dict()
        self.assertEqual(validate_journal_proposal(payload), ())
        self.assertNotIn("journal_proposal_outcome_code", payload)
        self.assertNotIn("statutory_account_id", payload)
        self.assertNotIn("chart_account_id", payload)
        self.assertNotIn("card_pan", payload)
        self.assertEqual(
            len(
                [
                    proposal
                    for proposal in ledger.journal_proposals.values()
                    if proposal.unapplied_cash_id == leftover_id
                ]
            ),
            1,
        )
        self.assertEqual(len(ledger.payment_receipts), prior_receipts)
        self.assertEqual(len(ledger.collection_write_offs), prior_write_offs)
        self.assertEqual(len(ledger.collection_case_settlements), prior_settlements)
        self.assertEqual(len(ledger.unapplied_cash_refunds), prior_refunds)
        self.assertEqual(len(ledger.unapplied_cash), prior_leftovers)
        self.assertEqual(len(ledger.accounting_export_records), 0)

    def test_park_accept_does_not_compose_a_journal(self) -> None:
        """#54 park stays without a leftover journal until the explicit compose command."""
        ledger, leftover_id = park_morning_leftover()
        prior_journals = len(
            [
                proposal
                for proposal in ledger.journal_proposals.values()
                if proposal.unapplied_cash_id is not None
            ]
        )
        self.assertEqual(prior_journals, 0)
        first = AccountingExportService(ledger).propose_unapplied_cash_journal(
            TENANT_ONE, leftover_id
        )
        self.assertEqual(first.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(
            len(
                [
                    proposal
                    for proposal in ledger.journal_proposals.values()
                    if proposal.unapplied_cash_id is not None
                ]
            ),
            1,
        )

    def test_second_propose_of_the_same_leftover_is_a_replay(self) -> None:
        """The same tenant and leftover reuse proposal_id and do not grow the store."""
        ledger, leftover_id = park_morning_leftover()
        service = AccountingExportService(ledger)
        first = service.propose_unapplied_cash_journal(TENANT_ONE, leftover_id)
        second = service.propose_unapplied_cash_journal(TENANT_ONE, leftover_id)
        self.assertEqual(first.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(second.journal_proposal_outcome_code, JournalProposalOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(second.proposal_id, first.proposal_id)
        self.assertEqual(second.source_payload_hash, first.source_payload_hash)
        self.assertEqual(second.proposal_lines[0].debit_amount, LEFTOVER)
        self.assertEqual(
            len(
                [
                    proposal
                    for proposal in ledger.journal_proposals.values()
                    if proposal.unapplied_cash_id is not None
                ]
            ),
            1,
        )
        self.assertEqual(validate_journal_proposal(second.as_contract_dict()), ())

    def test_invoice_and_cash_journals_stay_separate_identities(self) -> None:
        """A leftover journal sits beside the existing cash and draft proposals."""
        ledger, leftover_id = park_morning_leftover()
        leftover = AccountingExportService(ledger).propose_unapplied_cash_journal(
            TENANT_ONE, leftover_id
        )
        stored = ledger.get_unapplied_cash(leftover_id)
        self.assertIsNotNone(stored)
        collection_case = ledger.get_collection_case(stored.collection_case_id)
        self.assertIsNotNone(collection_case)
        draft = AccountingExportService(ledger).propose_journal(
            TENANT_ONE, collection_case.invoice_draft_id
        )
        self.assertEqual(leftover.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(draft.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertNotEqual(leftover.proposal_id, draft.proposal_id)
        self.assertEqual(leftover.proposal_lines[0].account_role_code, "cash_receipt")
        self.assertEqual(leftover.proposal_lines[1].account_role_code, "unapplied_cash")
        self.assertEqual(draft.proposal_lines[0].account_role_code, "accounts_receivable")
        cash_rows = [
            proposal
            for proposal in ledger.journal_proposals.values()
            if proposal.payment_receipt_id is not None
        ]
        leftover_rows = [
            proposal
            for proposal in ledger.journal_proposals.values()
            if proposal.unapplied_cash_id == leftover_id
        ]
        self.assertEqual(len(cash_rows), 1)
        self.assertEqual(len(leftover_rows), 1)
        self.assertNotEqual(cash_rows[0].journal_proposal_id, leftover_rows[0].journal_proposal_id)

    def test_other_tenant_cannot_see_or_propose_the_first_leftover(self) -> None:
        """A tenant cannot propose or list another tenant's leftover journal."""
        ledger, leftover_id = park_morning_leftover()
        service = AccountingExportService(ledger)
        owned = service.propose_unapplied_cash_journal(TENANT_ONE, leftover_id)
        crossed = service.propose_unapplied_cash_journal(TENANT_TWO, leftover_id)
        self.assertEqual(owned.proposal_lines[0].debit_amount, LEFTOVER)
        self.assertEqual(crossed.journal_proposal_outcome_code, JournalProposalOutcomeCode.REJECTED)
        self.assertEqual(
            crossed.rejection_reason_code,
            JournalProposalRejectionReasonCode.UNAPPLIED_CASH_NOT_FOUND,
        )
        self.assertNotIn("proposal_id", crossed.as_contract_dict())
        one_rows = ledger.list_journal_proposals(ledger.require_tenant(TENANT_ONE).tenant_account_id)
        two_rows = ledger.list_journal_proposals(ledger.require_tenant(TENANT_TWO).tenant_account_id)
        leftover_rows = [row for row in one_rows if row.unapplied_cash_id == leftover_id]
        self.assertEqual(len(leftover_rows), 1)
        self.assertEqual(leftover_rows[0].journal_proposal_id, owned.proposal_id)
        self.assertEqual(len(two_rows), 0)

    def test_missing_leftover_and_tenant_fail_closed(self) -> None:
        """A leftover journal cannot invent money without a stored tenant leftover."""
        ledger, _leftover_id = park_morning_leftover()
        service = AccountingExportService(ledger)
        missing_leftover = service.propose_unapplied_cash_journal(TENANT_ONE, generate_record_id())
        self.assertEqual(
            missing_leftover.journal_proposal_outcome_code,
            JournalProposalOutcomeCode.REJECTED,
        )
        self.assertEqual(
            missing_leftover.rejection_reason_code,
            JournalProposalRejectionReasonCode.UNAPPLIED_CASH_NOT_FOUND,
        )
        missing_tenant = service.propose_unapplied_cash_journal(
            "urn:cwl:missing_tenant", generate_record_id()
        )
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            JournalProposalRejectionReasonCode.TENANT_NOT_FOUND,
        )

    def test_currency_mismatch_invalid_amount_and_unparked_fail_closed(self) -> None:
        """Optional currency must match; zero, negative, IEEE, and unparked refuse."""
        ledger, leftover_id = park_morning_leftover()
        service = AccountingExportService(ledger)
        mismatch = service.propose_unapplied_cash_journal(
            TENANT_ONE, leftover_id, currency_code="EUR"
        )
        self.assertEqual(
            mismatch.rejection_reason_code,
            JournalProposalRejectionReasonCode.CURRENCY_MISMATCH,
        )
        matched = service.propose_unapplied_cash_journal(
            TENANT_ONE, leftover_id, currency_code="USD"
        )
        self.assertEqual(matched.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        stored = ledger.get_unapplied_cash(leftover_id)
        ledger.unapplied_cash[leftover_id] = replace(stored, unapplied_amount=Decimal("0"))
        zero = AccountingExportService(ledger).propose_unapplied_cash_journal(
            TENANT_ONE, leftover_id
        )
        self.assertEqual(zero.journal_proposal_outcome_code, JournalProposalOutcomeCode.DUPLICATE_REPLAY)
        leftover_ledger, leftover_leftover_id = park_morning_leftover()
        leftover_stored = leftover_ledger.get_unapplied_cash(leftover_leftover_id)
        leftover_ledger.unapplied_cash[leftover_leftover_id] = replace(
            leftover_stored, unapplied_amount=Decimal("0")
        )
        rejected_zero = AccountingExportService(leftover_ledger).propose_unapplied_cash_journal(
            TENANT_ONE, leftover_leftover_id
        )
        self.assertEqual(
            rejected_zero.rejection_reason_code,
            JournalProposalRejectionReasonCode.UNAPPLIED_AMOUNT_INVALID,
        )
        leftover_ledger.unapplied_cash[leftover_leftover_id] = replace(
            leftover_stored, unapplied_amount=Decimal("-1")
        )
        rejected_negative = AccountingExportService(leftover_ledger).propose_unapplied_cash_journal(
            TENANT_ONE, leftover_leftover_id
        )
        self.assertEqual(
            rejected_negative.rejection_reason_code,
            JournalProposalRejectionReasonCode.UNAPPLIED_AMOUNT_INVALID,
        )
        leftover_ledger.unapplied_cash[leftover_leftover_id] = replace(
            leftover_stored, unapplied_amount=0.003705  # type: ignore[arg-type]
        )
        floated = AccountingExportService(leftover_ledger).propose_unapplied_cash_journal(
            TENANT_ONE, leftover_leftover_id
        )
        self.assertEqual(
            floated.rejection_reason_code,
            JournalProposalRejectionReasonCode.UNAPPLIED_AMOUNT_INVALID,
        )
        leftover_ledger.unapplied_cash[leftover_leftover_id] = replace(
            leftover_stored, unapplied_cash_status="applied"
        )
        unparked = AccountingExportService(leftover_ledger).propose_unapplied_cash_journal(
            TENANT_ONE, leftover_leftover_id
        )
        self.assertEqual(
            unparked.rejection_reason_code,
            JournalProposalRejectionReasonCode.UNAPPLIED_CASH_NOT_PARKED,
        )
        with self.assertRaises(ExactDecimalError):
            parse_proposal_amount(0.003705)
        self.assertEqual(
            len(
                [
                    proposal
                    for proposal in leftover_ledger.journal_proposals.values()
                    if proposal.unapplied_cash_id is not None
                ]
            ),
            0,
        )

    def test_http_compose_lists_leftover_journal_without_a_second_write(self) -> None:
        """POST compose then GET /v1/journal-proposals leaves one validated leftover proposal."""
        ledger, leftover_id = park_morning_leftover()
        prior_leftover_journals = len(
            [
                proposal
                for proposal in ledger.journal_proposals.values()
                if proposal.unapplied_cash_id is not None
            ]
        )
        app = create_http_app(ledger)
        refused_status, refused_body = invoke_http(
            app,
            "POST",
            f"/v1/unapplied-cash/{leftover_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "card_pan": "4111111111111111"},
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(
            len(
                [
                    proposal
                    for proposal in ledger.journal_proposals.values()
                    if proposal.unapplied_cash_id is not None
                ]
            ),
            prior_leftover_journals,
        )
        mismatch_status, mismatch_body = invoke_http(
            app,
            "POST",
            f"/v1/unapplied-cash/{leftover_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "currency_code": "EUR"},
        )
        self.assertEqual(mismatch_status, 422)
        self.assertEqual(mismatch_body["rejection_reason_code"], "currency_mismatch")
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            f"/v1/unapplied-cash/{leftover_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "currency_code": "USD"},
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["proposal_status"], "validated")
        self.assertNotEqual(accepted_body["proposal_status"], "posted")
        self.assertNotIn("journal_proposal_outcome_code", accepted_body)
        self.assertEqual(accepted_body["lines"][0]["account_role_code"], "cash_receipt")
        self.assertEqual(accepted_body["lines"][0]["debit_amount"], format_exact_decimal(LEFTOVER))
        self.assertEqual(accepted_body["lines"][1]["account_role_code"], "unapplied_cash")
        self.assertEqual(accepted_body["lines"][1]["credit_amount"], format_exact_decimal(LEFTOVER))
        self.assertEqual(validate_journal_proposal(accepted_body), ())
        proposal_id = accepted_body["proposal_id"]
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            f"/v1/unapplied-cash/{leftover_id}/journal-proposals",
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
        leftover_items = [
            item
            for item in list_body["journal_proposals"]
            if item["lines"][1]["account_role_code"] == "unapplied_cash"
        ]
        self.assertEqual(len(leftover_items), 1)
        self.assertEqual(leftover_items[0]["proposal_id"], proposal_id)
        self.assertTrue(
            leftover_items[0]["idempotency_key"].startswith(
                f"{TENANT_ONE}:unapplied_cash:{leftover_id}:"
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
            f"/v1/unapplied-cash/{leftover_id}/journal-proposals",
            {"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(crossed_status, 422)
        self.assertEqual(crossed_body["rejection_reason_code"], "unapplied_cash_not_found")
        method_status, _method_body = invoke_http(
            app,
            "PUT",
            f"/v1/unapplied-cash/{leftover_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 405)
        self.assertEqual(
            len(
                [
                    proposal
                    for proposal in ledger.journal_proposals.values()
                    if proposal.unapplied_cash_id is not None
                ]
            ),
            1,
        )

    def test_resolver_and_ledger_guards_cover_identity(self) -> None:
        """Hollow tenant resolve raises; leftover journal rows stay append-only."""
        ledger, leftover_id = park_morning_leftover()
        service = AccountingExportService(ledger)
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.propose_unapplied_cash_journal(TENANT_ONE, leftover_id)
        first = service.propose_unapplied_cash_journal(TENANT_ONE, leftover_id)
        stored = ledger.journal_proposals[first.proposal_id]
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(stored, stored.proposal_lines)
        colliding = replace(stored, journal_proposal_id=generate_record_id())
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(colliding, colliding.proposal_lines)
        leftover_collision = replace(
            stored,
            journal_proposal_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            source_payload_hash="sha256:" + "a" * 64,
        )
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(leftover_collision, leftover_collision.proposal_lines)
        self.assertEqual(stored.unapplied_cash_id, leftover_id)
        self.assertIsNone(
            ledger.find_journal_proposal_for_unapplied_cash(
                stored.tenant_account_id,
                generate_record_id(),
            )
        )
        found = ledger.find_journal_proposal_for_unapplied_cash(
            stored.tenant_account_id,
            leftover_id,
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.journal_proposal_id, first.proposal_id)
        empty = AccountingExportService()
        rejected = empty.propose_unapplied_cash_journal(TENANT_ONE, generate_record_id())
        self.assertEqual(
            rejected.rejection_reason_code,
            JournalProposalRejectionReasonCode.TENANT_NOT_FOUND,
        )
        currency_type_status, currency_type_body = invoke_http(
            create_http_app(ledger),
            "POST",
            f"/v1/unapplied-cash/{leftover_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "currency_code": 1},
        )
        self.assertEqual(currency_type_status, 422)
        self.assertEqual(currency_type_body["rejection_reason_code"], "request_invalid")
        orphan_ledger, orphan_leftover_id = park_morning_leftover()
        orphan_stored = orphan_ledger.get_unapplied_cash(orphan_leftover_id)
        del orphan_ledger.collection_cases[orphan_stored.collection_case_id]
        missing_case = AccountingExportService(orphan_ledger).propose_unapplied_cash_journal(
            TENANT_ONE, orphan_leftover_id
        )
        self.assertEqual(
            missing_case.rejection_reason_code,
            JournalProposalRejectionReasonCode.UNAPPLIED_CASH_NOT_FOUND,
        )


if __name__ == "__main__":
    unittest.main()
