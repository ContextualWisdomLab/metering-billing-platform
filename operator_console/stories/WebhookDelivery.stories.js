import { renderWebhookDelivery } from "../src/webhook_delivery.js";
import deliveredMorning from "../fixtures/delivered_morning.json";
import failedCallback from "../fixtures/failed_callback.json";

export default {
  title: "WebhookDelivery",
};

export const DeliveredMorning = {
  render: () => renderWebhookDelivery(deliveredMorning),
};

export const FailedCallback = {
  render: () => renderWebhookDelivery(failedCallback),
};
