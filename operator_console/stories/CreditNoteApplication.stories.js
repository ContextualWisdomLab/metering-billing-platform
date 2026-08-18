import { renderCreditNoteApplication } from "../src/credit_note_application.js";
import appliedMorningCreditNote from "../fixtures/applied_morning_credit_note.json";

export default {
  title: "CreditNoteApplication",
};

export const AppliedMorningWait = {
  render: () => renderCreditNoteApplication(appliedMorningCreditNote),
};
