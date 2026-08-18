import { renderCollectionAging } from "../src/collection_aging.js";
import morningCollectionAging from "../fixtures/morning_collection_aging.json";

export default {
  title: "CollectionAging",
};

export const MorningUsdBuckets = {
  render: () => renderCollectionAging(morningCollectionAging),
};
