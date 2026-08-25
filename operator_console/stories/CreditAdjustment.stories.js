import { renderCreditAdjustment } from "../src/credit_adjustment.js";
import recordedMorningCredit from "../fixtures/recorded_morning_credit.json";
import recordedTaxedCredit from "../fixtures/recorded_taxed_credit.json";

export default {
  title: "CreditAdjustment",
};

export const RecordedMorningWait = {
  render: () => renderCreditAdjustment(recordedMorningCredit),
};

export const RecordedTaxedWait = {
  render: () => renderCreditAdjustment(recordedTaxedCredit),
};
