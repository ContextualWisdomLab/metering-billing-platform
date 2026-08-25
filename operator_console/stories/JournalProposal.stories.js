import { renderJournalProposal } from "../src/journal_proposal.js";
import validatedMorningCashJournal from "../fixtures/validated_morning_cash_journal.json";

export default {
  title: "JournalProposal",
};

export const ValidatedMorningCashWait = {
  render: () => renderJournalProposal(validatedMorningCashJournal),
};
