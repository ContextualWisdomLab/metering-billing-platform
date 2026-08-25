import { renderUnappliedCash } from "../src/unapplied_cash.js";
import parkedMorningUnappliedCash from "../fixtures/parked_morning_unapplied_cash.json";
import appliedMorningUnappliedCash from "../fixtures/applied_morning_unapplied_cash.json";
import refundedMorningUnappliedCash from "../fixtures/refunded_morning_unapplied_cash.json";

export default {
  title: "UnappliedCash",
};

export const ParkedMorningWait = {
  render: () => renderUnappliedCash(parkedMorningUnappliedCash),
};

export const AppliedMorningCollect = {
  render: () => renderUnappliedCash(appliedMorningUnappliedCash),
};

export const RefundedMorningWait = {
  render: () => renderUnappliedCash(refundedMorningUnappliedCash),
};
