import { escapeHtml } from "./exact_decimal.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const WEBHOOK_OUTBOX_EVENT_CUSTOMER_COPY =
  "Register an https callback, then run deliveries; AIS may keep polling.";

/**
 * Return the operator-facing next-action copy for one outbox event.
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
 * Render one webhook-outbox-event presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderWebhookOutboxEvent(statement) {
  const action = String(statement.next_operator_action ?? "");
  const statusLabel = String(statement.delivery_status ?? action);
  return (
    `<article class="oc-webhook-outbox-event">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Webhook outbox event</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.outbox_event_id)}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(statement.event_type_code)}</p>` +
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
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(WEBHOOK_OUTBOX_EVENT_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
