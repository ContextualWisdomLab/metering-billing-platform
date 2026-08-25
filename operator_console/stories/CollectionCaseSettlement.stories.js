import { renderCollectionCaseSettlement } from "../src/collection_case_settlement.js";
import settledMorningZero from "../fixtures/settled_morning_zero.json";
import settledLeftoverWriteOffZero from "../fixtures/settled_leftover_write_off_zero.json";

export default {
  title: "CollectionCaseSettlement",
};

export const SettledMorningZero = {
  render: () => renderCollectionCaseSettlement(settledMorningZero),
};

export const SettledLeftoverWriteOffZero = {
  render: () => renderCollectionCaseSettlement(settledLeftoverWriteOffZero),
};
