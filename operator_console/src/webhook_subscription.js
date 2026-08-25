import { escapeHtml } from "./exact_decimal.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const WEBHOOK_SUBSCRIPTION_CUSTOMER_COPY =
  "Register an https callback, then run deliveries; AIS may keep polling.";

/**
 * Return the operator-facing next-action copy for one subscription.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "run_deliveries") {
    return "Run deliveries";
  }
  if (action === "register") {
    return "Register a callback";
  }
  return "Register a callback";
}

/**
 * Render one webhook-subscription presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderWebhookSubscription(statement) {
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-webhook-subscription">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Webhook subscription</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.webhook_subscription_id)}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(statement.callback_url)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: "0",
      status_label: statement.subscription_status,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(WEBHOOK_SUBSCRIPTION_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
