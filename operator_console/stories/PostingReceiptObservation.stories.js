import { renderPostingReceiptObservation } from "../src/posting_receipt_observation.js";
import observedPostedMorning from "../fixtures/observed_posted_morning.json";
import observedHeldReceipt from "../fixtures/observed_held_receipt.json";

export default {
  title: "PostingReceiptObservation",
};

export const ObservedPostedMorning = {
  render: () => renderPostingReceiptObservation(observedPostedMorning),
};

export const ObservedHeldReceipt = {
  render: () => renderPostingReceiptObservation(observedHeldReceipt),
};
