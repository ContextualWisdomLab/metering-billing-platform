import { renderCollectionDispute } from "../src/collection_dispute.js";
import heldMorningCollectionDispute from "../fixtures/held_morning_collection_dispute.json";
import releasedMorningCollectionDispute from "../fixtures/released_morning_collection_dispute.json";

export default {
  title: "CollectionDispute",
};

export const HeldMorningWait = {
  render: () => renderCollectionDispute(heldMorningCollectionDispute),
};

export const ReleasedMorningFailClose = {
  render: () => renderCollectionDispute(releasedMorningCollectionDispute),
};
