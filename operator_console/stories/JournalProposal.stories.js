import { renderJournalProposal } from "../src/journal_proposal.js";
import validatedMorningCashJournal from "../fixtures/validated_morning_cash_journal.json";
import validatedMorningInvoiceDraftJournal from "../fixtures/validated_morning_invoice_draft_journal.json";
import validatedTaxedInvoiceDraftJournal from "../fixtures/validated_taxed_invoice_draft_journal.json";

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
