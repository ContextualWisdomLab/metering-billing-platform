import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const USAGE_EVENT_CUSTOMER_COPY =
  "Ingest usage, then rate a window against a published card.";

/**
 * Return the operator-facing next-action copy for one usage event.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "rate_window") {
    return "Rate a window";
  }
  return "Ingest usage";
}

/**
 * Render one usage-event presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderUsageEvent(statement) {
  const measurements = Array.isArray(statement.measurements) ? statement.measurements : [];
  const firstLine = measurements[0] ?? {};
  const quantity = assertExactDecimalString(firstLine.quantity, "quantity");
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-usage-event">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Usage event</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.usage_event_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: quantity,
      status_label: firstLine.meter_code,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue({
      amount_due: quantity,
      currency_code: firstLine.unit_code,
    })}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(USAGE_EVENT_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
