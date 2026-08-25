import { renderAmountDue } from "../src/amount_due.js";
import taxedPartialCredit from "../fixtures/taxed_partial_credit.json";
import untaxedMorning from "../fixtures/untaxed_morning.json";
import settledStatement from "../fixtures/settled_statement.json";

export default {
  title: "AmountDue",
};

export const TaxedPartialCredit = {
  render: () => renderAmountDue(taxedPartialCredit),
};

export const UntaxedMorning = {
  render: () => renderAmountDue(untaxedMorning),
};

export const Settled = {
  render: () => renderAmountDue(settledStatement),
};
