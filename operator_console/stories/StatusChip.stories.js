import { renderStatusChip } from "../src/status_chip.js";
import taxedPartialCredit from "../fixtures/taxed_partial_credit.json";
import untaxedMorning from "../fixtures/untaxed_morning.json";
import settledStatement from "../fixtures/settled_statement.json";

export default {
  title: "StatusChip",
};

export const TaxedPartialCredit = {
  render: () => renderStatusChip(taxedPartialCredit),
};

export const UntaxedMorning = {
  render: () => renderStatusChip(untaxedMorning),
};

export const Settled = {
  render: () => renderStatusChip(settledStatement),
};
