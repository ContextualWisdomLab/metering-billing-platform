import { renderPaymentIntent } from "../src/payment_intent.js";
import projectedPaymentIntent from "../fixtures/projected_payment_intent.json";
import cancelledPaymentIntent from "../fixtures/cancelled_payment_intent.json";

export default {
  title: "PaymentIntent",
};

export const ProjectedRecordReceipt = {
  render: () => renderPaymentIntent(projectedPaymentIntent),
};

export const CancelledWait = {
  render: () => renderPaymentIntent(cancelledPaymentIntent),
};
