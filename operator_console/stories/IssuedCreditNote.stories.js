import { renderIssuedCreditNote } from "../src/issued_credit_note.js";
import issuedMorningCreditNote from "../fixtures/issued_morning_credit_note.json";
import issuedTaxedCreditNote from "../fixtures/issued_taxed_credit_note.json";

export default {
  title: "IssuedCreditNote",
};

export const IssuedMorningWait = {
  render: () => renderIssuedCreditNote(issuedMorningCreditNote),
};

export const IssuedTaxedWait = {
  render: () => renderIssuedCreditNote(issuedTaxedCreditNote),
};
