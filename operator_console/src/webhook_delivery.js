import { escapeHtml } from "./exact_decimal.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const WEBHOOK_DELIVERY_CUSTOMER_COPY =
  "Register an https callback, then run deliveries; AIS picks up new events automatically.";

/**
 * Return the operator-facing next-action copy for one delivery attempt.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "wait") {
    return "Wait";
  }
  if (action === "run_deliveries") {
    return "Run deliveries";
  }
  return "Run deliveries";
}

/**
 * Render one webhook-delivery presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderWebhookDelivery(statement) {
  const action = String(statement.next_operator_action ?? "");
  const statusLabel =
    statement.failure_reason_code ??
    (statement.http_status != null ? String(statement.http_status) : action);
  return (
    `<article class="oc-webhook-delivery">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Webhook delivery</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.delivery_attempt_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: "0",
      status_label: statusLabel,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(WEBHOOK_DELIVERY_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
