import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const RATE_CARD_CUSTOMER_COPY =
  "Publish a rate card, then rate a window against that version.";

/**
 * Return the operator-facing next-action copy for one rate card.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "rate_window") {
    return "Rate a window";
  }
  return "Publish a rate card";
}

/**
 * Render one rate-card presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderRateCard(statement) {
  const lines = Array.isArray(statement.lines) ? statement.lines : [];
  const firstLine = lines[0] ?? {};
  const unitAmount = assertExactDecimalString(firstLine.unit_amount, "unit_amount");
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-rate-card">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Rate card</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.rate_card_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: unitAmount,
      status_label: statement.rate_card_name,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue({
      amount_due: unitAmount,
      currency_code: statement.currency_code,
    })}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(RATE_CARD_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
