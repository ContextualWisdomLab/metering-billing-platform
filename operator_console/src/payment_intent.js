import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const PAYMENT_INTENT_CUSTOMER_COPY =
  "Create a projected payment intent, then record the receipt.";

/**
 * Return the operator-facing next-action copy for one payment intent.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "wait") {
    return "Wait";
  }
  return "Record the receipt";
}

/**
 * Render one payment-intent presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderPaymentIntent(statement) {
  const amount = assertExactDecimalString(statement.payment_amount, "payment_amount");
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-payment-intent">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Payment intent</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.payment_intent_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: amount,
      status_label: statement.payment_intent_status,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue({
      amount_due: amount,
      currency_code: statement.currency_code,
    })}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(PAYMENT_INTENT_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
