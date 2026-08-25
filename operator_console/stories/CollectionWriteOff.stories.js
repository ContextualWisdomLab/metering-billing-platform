import { renderCollectionWriteOff } from "../src/collection_write_off.js";
import recordedLeftoverCollectionWriteOff from "../fixtures/recorded_leftover_collection_write_off.json";

export default {
  title: "CollectionWriteOff",
};

export const RecordedLeftoverRemainingSettle = {
  render: () => renderCollectionWriteOff(recordedLeftoverCollectionWriteOff),
};
