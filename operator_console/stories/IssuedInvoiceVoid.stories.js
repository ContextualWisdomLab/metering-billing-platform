import { renderIssuedInvoiceVoid } from "../src/issued_invoice_void.js";
import voidedUnusedIssuedInvoice from "../fixtures/voided_unused_issued_invoice.json";

export default {
  title: "IssuedInvoiceVoid",
};

export const VoidedUnusedWait = {
  render: () => renderIssuedInvoiceVoid(voidedUnusedIssuedInvoice),
};
