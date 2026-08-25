import { renderIssuedCreditNoteVoid } from "../src/issued_credit_note_void.js";
import voidedUnusedIssuedCreditNote from "../fixtures/voided_unused_issued_credit_note.json";

export default {
  title: "IssuedCreditNoteVoid",
};

export const VoidedUnusedWait = {
  render: () => renderIssuedCreditNoteVoid(voidedUnusedIssuedCreditNote),
};
