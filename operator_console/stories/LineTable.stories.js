import { renderLineTable } from "../src/line_table.js";
import taxedPartialCredit from "../fixtures/taxed_partial_credit.json";
import untaxedMorning from "../fixtures/untaxed_morning.json";
import settledStatement from "../fixtures/settled_statement.json";

export default {
  title: "LineTable",
};

export const TaxedPartialCredit = {
  render: () => renderLineTable(taxedPartialCredit),
};

export const UntaxedMorning = {
  render: () => renderLineTable(untaxedMorning),
};

export const Settled = {
  render: () => renderLineTable(settledStatement),
};
