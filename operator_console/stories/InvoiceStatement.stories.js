import { renderInvoiceStatement } from "../src/invoice_statement.js";
import taxedPartialCredit from "../fixtures/taxed_partial_credit.json";
import untaxedMorning from "../fixtures/untaxed_morning.json";
import settledStatement from "../fixtures/settled_statement.json";

export default {
  title: "InvoiceStatement",
};

export const TaxedPartialCredit = {
  render: () => renderInvoiceStatement(taxedPartialCredit),
};

export const UntaxedMorning = {
  render: () => renderInvoiceStatement(untaxedMorning),
};

export const Settled = {
  render: () => renderInvoiceStatement(settledStatement),
};
