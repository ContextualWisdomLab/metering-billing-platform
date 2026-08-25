import { renderWebhookSubscription } from "../src/webhook_subscription.js";
import activeHttpsCallback from "../fixtures/active_https_callback.json";
import revokedHttpsCallback from "../fixtures/revoked_https_callback.json";

export default {
  title: "WebhookSubscription",
};

export const ActiveHttpsCallback = {
  render: () => renderWebhookSubscription(activeHttpsCallback),
};

export const RevokedHttpsCallback = {
  render: () => renderWebhookSubscription(revokedHttpsCallback),
};
