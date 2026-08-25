import { renderJournalProposal } from "../src/journal_proposal.js";
import validatedMorningCashJournal from "../fixtures/validated_morning_cash_journal.json";
import validatedMorningInvoiceDraftJournal from "../fixtures/validated_morning_invoice_draft_journal.json";
import validatedTaxedInvoiceDraftJournal from "../fixtures/validated_taxed_invoice_draft_journal.json";
import validatedMorningLeftoverJournal from "../fixtures/validated_morning_leftover_journal.json";
import validatedMorningLeftoverApplyJournal from "../fixtures/validated_morning_leftover_apply_journal.json";
import validatedMorningLeftoverRefundJournal from "../fixtures/validated_morning_leftover_refund_journal.json";

export default {
  title: "JournalProposal",
};

export const ValidatedMorningCashWait = {
  render: () => renderJournalProposal(validatedMorningCashJournal),
};

export const ValidatedMorningInvoiceDraftWait = {
  render: () => renderJournalProposal(validatedMorningInvoiceDraftJournal),
};

export const ValidatedTaxedInvoiceDraftWait = {
  render: () => renderJournalProposal(validatedTaxedInvoiceDraftJournal),
};

export const ValidatedMorningLeftoverWait = {
  render: () => renderJournalProposal(validatedMorningLeftoverJournal),
};

export const ValidatedMorningLeftoverApplyWait = {
  render: () => renderJournalProposal(validatedMorningLeftoverApplyJournal),
};

export const ValidatedMorningLeftoverRefundWait = {
  render: () => renderJournalProposal(validatedMorningLeftoverRefundJournal),
};
