import { renderCollectionCaseSettlement } from "../src/collection_case_settlement.js";
import settledMorningZero from "../fixtures/settled_morning_zero.json";

export default {
  title: "CollectionCaseSettlement",
};

export const SettledMorningZero = {
  render: () => renderCollectionCaseSettlement(settledMorningZero),
};
