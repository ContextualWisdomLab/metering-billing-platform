import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const CREDIT_ADJUSTMENT_CUSTOMER_COPY =
  "Record the credit; AIS pulls the validated journal.";

/**
 * Return the operator-facing next-action copy for one credit adjustment.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "wait") {
    return "Wait for AIS";
  }
  return "Record the credit";
}

/**
 * Render one credit-adjustment presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderCreditAdjustment(statement) {
  const credited = assertExactDecimalString(statement.credit_amount, "credit_amount");
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-credit-adjustment">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Credit adjustment</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.credit_adjustment_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: credited,
      status_label: statement.credit_adjustment_status,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue({
      amount_due: credited,
      currency_code: statement.currency_code,
    })}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(CREDIT_ADJUSTMENT_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
