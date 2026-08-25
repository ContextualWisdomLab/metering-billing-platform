"""Realistic journal-proposal tests for exact money, replay, and tenant isolation."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing import (
    AccountingExportService,
    InvoiceDraftService,
    MemoryUsageLedger,
    TimeWindow,
    UsageIngestionService,
    UsageRatingService,
    format_exact_decimal,
)
from metering_billing.accounting_export import JournalProposalResult, parse_proposal_amount
from metering_billing.contracts import validate_journal_proposal
from metering_billing.errors import (
    ExactDecimalError,
    JournalProposalOutcomeCode,
    JournalProposalRejectionReasonCode,
)
from metering_billing.usage_ledger import generate_record_id
from metering_billing.webhook_outbox import EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
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


class JournalProposalTests(unittest.TestCase):
    """Verify invoice-draft exports stay balanced, exact, and proposal-only."""

    def test_known_invoice_draft_emits_balanced_exact_proposal(self) -> None:
        """Known usage, rating, and draft totals must become one balanced proposal."""
        ledger, invoice_draft_id = draft_known_morning()
        result = AccountingExportService(ledger).propose_journal(TENANT_ONE, invoice_draft_id)
        self.assertEqual(result.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertIsInstance(result.proposal_id, UUID)
        self.assertEqual(result.proposal_status, "validated")
        self.assertEqual(result.transaction_currency, "USD")
        self.assertEqual(result.intended_book_role_code, "primary_statutory")
        self.assertEqual(result.legal_entity_reference, f"{TENANT_ONE}:legal_entity:commercial")
        self.assertEqual(
            result.source_event_references,
            (f"{TENANT_ONE}:invoice_draft:{invoice_draft_id}",),
        )
        self.assertEqual(result.invoice_draft_id, invoice_draft_id)
        self.assertEqual(len(result.proposal_lines), 2)
        debit, credit = result.proposal_lines
        self.assertEqual(debit.account_role_code, "accounts_receivable")
        self.assertEqual(debit.debit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(debit.credit_amount, Decimal("0"))
        self.assertEqual(credit.account_role_code, "usage_revenue")
        self.assertEqual(credit.debit_amount, Decimal("0"))
        self.assertEqual(credit.credit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(debit.debit_amount + credit.debit_amount, credit.credit_amount)
        self.assertNotIsInstance(debit.debit_amount, float)
        self.assertEqual(validate_journal_proposal(result.as_contract_dict()), ())
        self.assertNotEqual(result.as_contract_dict()["proposal_status"], "posted")
        self.assertEqual(len(ledger.journal_proposals), 1)
        self.assertEqual(len(ledger.journal_proposal_lines), 2)
        self.assertEqual(len(ledger.accounting_export_records), 0)

    def test_second_propose_of_the_same_draft_is_a_replay(self) -> None:
        """The same tenant, draft, hash, and contract version reuse proposal_id."""
        ledger, invoice_draft_id = draft_known_morning()
        service = AccountingExportService(ledger)
        first = service.propose_journal(TENANT_ONE, invoice_draft_id)
        second = service.propose_journal(TENANT_ONE, invoice_draft_id)
        self.assertEqual(first.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(second.journal_proposal_outcome_code, JournalProposalOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(second.proposal_id, first.proposal_id)
        self.assertEqual(second.source_payload_hash, first.source_payload_hash)
        self.assertEqual(second.proposal_lines[0].debit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(len(ledger.journal_proposals), 1)
        self.assertEqual(validate_journal_proposal(second.as_contract_dict()), ())

    def test_replay_heals_missing_journal_proposal_validated_outbox(self) -> None:
        """A crash after insert and before outbox enqueue is healed by replay."""
        ledger, invoice_draft_id = draft_known_morning()
        first = AccountingExportService(ledger).propose_journal(TENANT_ONE, invoice_draft_id)
        tenant = ledger.require_tenant(TENANT_ONE)
        outbox_rows = [
            event
            for event in ledger.list_webhook_outbox_events_for_tenant(tenant.tenant_account_id)
            if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
            and event.source_id == first.proposal_id
        ]
        self.assertEqual(len(outbox_rows), 1)
        stored_outbox = outbox_rows[0]
        del ledger.webhook_outbox_events[stored_outbox.outbox_event_id]
        del ledger.webhook_outbox_identity_index[
            (
                stored_outbox.tenant_account_id,
                stored_outbox.event_type_code,
                stored_outbox.source_id,
                stored_outbox.payload_hash,
            )
        ]
        healed = AccountingExportService(ledger).propose_journal(TENANT_ONE, invoice_draft_id)
        self.assertEqual(
            healed.journal_proposal_outcome_code, JournalProposalOutcomeCode.DUPLICATE_REPLAY
        )
        self.assertEqual(healed.proposal_id, first.proposal_id)
        healed_rows = [
            event
            for event in ledger.list_webhook_outbox_events_for_tenant(tenant.tenant_account_id)
            if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
            and event.source_id == first.proposal_id
        ]
        self.assertEqual(len(healed_rows), 1)
        self.assertEqual(len(ledger.journal_proposals), 1)

    def test_other_tenant_cannot_see_or_propose_the_first_draft(self) -> None:
        """A tenant cannot propose or list another tenant's invoice draft."""
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
        service = AccountingExportService(ledger)
        one_proposal = service.propose_journal(TENANT_ONE, one_draft_id)
        two_proposal = service.propose_journal(TENANT_TWO, two_draft.invoice_draft_id)
        crossed = service.propose_journal(TENANT_TWO, one_draft_id)
        self.assertEqual(one_proposal.proposal_lines[0].debit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(
            two_proposal.proposal_lines[0].debit_amount,
            Decimal("10") * Decimal("0.000002"),
        )
        self.assertNotEqual(one_proposal.proposal_id, two_proposal.proposal_id)
        self.assertEqual(crossed.journal_proposal_outcome_code, JournalProposalOutcomeCode.REJECTED)
        self.assertEqual(
            crossed.rejection_reason_code,
            JournalProposalRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND,
        )
        self.assertNotIn("proposal_id", crossed.as_contract_dict())
        one_rows = ledger.list_journal_proposals(ledger.require_tenant(TENANT_ONE).tenant_account_id)
        two_rows = ledger.list_journal_proposals(ledger.require_tenant(TENANT_TWO).tenant_account_id)
        self.assertEqual(len(one_rows), 1)
        self.assertEqual(one_rows[0].journal_proposal_id, one_proposal.proposal_id)
        self.assertEqual(len(two_rows), 1)
        self.assertEqual(two_rows[0].journal_proposal_id, two_proposal.proposal_id)

    def test_missing_draft_and_tenant_fail_closed(self) -> None:
        """A proposal cannot invent money without a stored tenant invoice draft."""
        ledger, _invoice_draft_id = draft_known_morning()
        service = AccountingExportService(ledger)
        missing_draft = service.propose_journal(TENANT_ONE, generate_record_id())
        self.assertEqual(
            missing_draft.journal_proposal_outcome_code,
            JournalProposalOutcomeCode.REJECTED,
        )
        self.assertEqual(
            missing_draft.rejection_reason_code,
            JournalProposalRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND,
        )
        missing_tenant = service.propose_journal("urn:cwl:missing_tenant", generate_record_id())
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            JournalProposalRejectionReasonCode.TENANT_NOT_FOUND,
        )
        self.assertEqual(len(ledger.journal_proposals), 0)

    def test_zero_draft_total_fails_closed(self) -> None:
        """A zero invoice-intent total cannot become a journal line (debit XOR credit)."""
        ingest = ingest_known_batch()
        empty = TimeWindow.from_iso8601("2026-08-15T00:00:00Z", "2026-08-15T01:00:00Z")
        rating = UsageRatingService(ingest.ledger).rate_usage_window(TENANT_ONE, empty, 1)
        draft = InvoiceDraftService(ingest.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        self.assertEqual(draft.drafted_total_amount, Decimal("0"))
        rejected = AccountingExportService(ingest.ledger).propose_journal(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(
            rejected.rejection_reason_code,
            JournalProposalRejectionReasonCode.DRAFT_TOTAL_INVALID,
        )
        self.assertEqual(len(ingest.ledger.journal_proposals), 0)

    def test_binary_float_money_is_rejected_at_the_proposal_boundary(self) -> None:
        """Proposal amounts must be exact decimals, never IEEE binary floats."""
        with self.assertRaises(ExactDecimalError):
            parse_proposal_amount(0.003705)
        self.assertEqual(parse_proposal_amount("0.003705"), Decimal("0.003705"))
        self.assertEqual(parse_proposal_amount(Decimal("0.003705")), Decimal("0.003705"))
        self.assertEqual(format_exact_decimal(parse_proposal_amount("0.003705")), "0.003705")

    def test_default_service_and_rejected_contract_stay_sparse(self) -> None:
        """The zero-argument service constructs a ledger and rejected exports omit money."""
        empty = AccountingExportService()
        self.assertIsInstance(empty.ledger, MemoryUsageLedger)
        rejected = empty.propose_journal(TENANT_ONE, generate_record_id())
        payload = rejected.as_contract_dict()
        self.assertEqual(payload["journal_proposal_outcome_code"], "rejected")
        self.assertNotIn("proposal_id", payload)
        self.assertNotIn("lines", payload)
        self.assertNotIn("source_payload_hash", payload)


class JournalProposalCatalogAndContractTests(unittest.TestCase):
    """Cover proposal persistence edges and proposal-only contract semantics."""

    def test_journal_proposal_insert_is_immutable_and_balanced(self) -> None:
        """A second insert or unbalanced lines cannot replace or post history."""
        ledger, invoice_draft_id = draft_known_morning()
        first = AccountingExportService(ledger).propose_journal(TENANT_ONE, invoice_draft_id)
        stored = ledger.journal_proposals[first.proposal_id]
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(stored, stored.proposal_lines)
        colliding = replace(stored, journal_proposal_id=generate_record_id())
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(colliding, colliding.proposal_lines)
        unbalanced_lines = (
            replace(stored.proposal_lines[0], debit_amount=Decimal("0.003706")),
            stored.proposal_lines[1],
        )
        unbalanced = replace(
            stored,
            journal_proposal_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            source_payload_hash="sha256:" + "e" * 64,
            proposal_lines=unbalanced_lines,
        )
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(unbalanced, unbalanced_lines)
        duplicated_lines = (
            stored.proposal_lines[0],
            replace(stored.proposal_lines[1], line_number=1, journal_proposal_line_id=generate_record_id()),
        )
        duplicated = replace(
            stored,
            journal_proposal_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            source_payload_hash="sha256:" + "f" * 64,
            proposal_lines=duplicated_lines,
        )
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(duplicated, duplicated_lines)
        posted = replace(
            stored,
            journal_proposal_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            source_payload_hash="sha256:" + "a" * 64,
            proposal_status="posted",
        )
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(posted, posted.proposal_lines)
        both_sides = (
            replace(stored.proposal_lines[0], credit_amount=Decimal("0.001")),
            replace(stored.proposal_lines[1], debit_amount=Decimal("0.001")),
        )
        xor_violation = replace(
            stored,
            journal_proposal_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            source_payload_hash="sha256:" + "b" * 64,
            proposal_lines=both_sides,
        )
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(xor_violation, both_sides)
        self.assertIsNone(ledger.get_invoice_draft(generate_record_id()))
        stored_draft = ledger.get_invoice_draft(invoice_draft_id)
        self.assertIsNotNone(stored_draft)
        self.assertEqual(stored_draft.invoice_draft_id, invoice_draft_id)

    def test_unknown_outcome_and_missing_reason_stay_fail_closed(self) -> None:
        """Unsupported outcome text cannot be serialized as a journal proposal."""
        bogus = JournalProposalResult(
            journal_proposal_outcome_code="mystery",  # type: ignore[arg-type]
            proposal_contract_version=1,
            proposal_id=None,
            invoice_draft_id=None,
            tenant_reference=None,
            legal_entity_reference=None,
            intended_book_role_code=None,
            transaction_currency=None,
            transaction_date=None,
            accounting_date=None,
            source_payload_hash=None,
            proposed_at=None,
            proposal_status=None,
            source_event_references=(),
            idempotency_key=None,
            rejection_reason_code=None,
            proposal_lines=(),
        )
        with self.assertRaises(ValueError):
            bogus.as_contract_dict()
        rejected_without_reason = JournalProposalResult(
            journal_proposal_outcome_code=JournalProposalOutcomeCode.REJECTED,
            proposal_contract_version=1,
            proposal_id=None,
            invoice_draft_id=None,
            tenant_reference=None,
            legal_entity_reference=None,
            intended_book_role_code=None,
            transaction_currency=None,
            transaction_date=None,
            accounting_date=None,
            source_payload_hash=None,
            proposed_at=None,
            proposal_status=None,
            source_event_references=(),
            idempotency_key=None,
            rejection_reason_code=None,
            proposal_lines=(),
        )
        self.assertEqual(
            rejected_without_reason.as_contract_dict()["rejection_reason_code"],
            "invoice_draft_not_found",
        )
        accepted_without_time = JournalProposalResult(
            journal_proposal_outcome_code=JournalProposalOutcomeCode.ACCEPTED,
            proposal_contract_version=1,
            proposal_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            tenant_reference=TENANT_ONE,
            legal_entity_reference=f"{TENANT_ONE}:legal_entity:commercial",
            intended_book_role_code="primary_statutory",
            transaction_currency="USD",
            transaction_date="2026-08-17",
            accounting_date="2026-08-17",
            source_payload_hash="sha256:" + "c" * 64,
            proposed_at=None,
            proposal_status="validated",
            source_event_references=(f"{TENANT_ONE}:invoice_draft:{generate_record_id()}",),
            idempotency_key="missing-proposed-at",
            rejection_reason_code=None,
            proposal_lines=(),
        )
        with self.assertRaises(ValueError):
            accepted_without_time.as_contract_dict()

    def test_clock_stamps_proposed_at_and_dates_follow_the_draft(self) -> None:
        """A supplied clock stamps proposed_at; commercial dates follow the draft."""
        draft_time = datetime(2026, 8, 17, 18, 0, tzinfo=UTC)
        proposed_time = datetime(2026, 8, 17, 19, 30, tzinfo=UTC)
        ledger, invoice_draft_id = draft_known_morning(clock=draft_time)
        result = AccountingExportService(ledger, clock=lambda: proposed_time).propose_journal(
            TENANT_ONE, invoice_draft_id
        )
        self.assertEqual(ledger.journal_proposals[result.proposal_id].proposed_at, proposed_time)
        self.assertEqual(result.transaction_date, "2026-08-17")
        self.assertEqual(result.accounting_date, "2026-08-17")
        self.assertEqual(validate_journal_proposal(result.as_contract_dict()), ())


if __name__ == "__main__":
    unittest.main()
