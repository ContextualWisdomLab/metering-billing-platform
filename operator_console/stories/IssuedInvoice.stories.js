import { renderIssuedInvoice } from "../src/issued_invoice.js";
import issuedUntaxedMorning from "../fixtures/issued_untaxed_morning.json";

export default {
  title: "IssuedInvoice",
};

export const IssuedUntaxedMorning = {
  render: () => renderIssuedInvoice(issuedUntaxedMorning),
};
