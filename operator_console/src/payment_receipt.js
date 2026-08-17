import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const PAYMENT_RECEIPT_CUSTOMER_COPY =
  "Record the receipt, then drain or wait for AIS to pull the cash journal.";

/**
 * Return the operator-facing next-action copy for one payment receipt.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "record_receipt") {
    return "Record the receipt";
  }
  return "Drain or wait for AIS";
}

/**
 * Render one payment-receipt presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderPaymentReceipt(statement) {
  const received = assertExactDecimalString(statement.received_amount, "received_amount");
  const remaining = assertExactDecimalString(
    statement.remaining_outstanding_amount,
    "remaining_outstanding_amount",
  );
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-payment-receipt">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Payment receipt</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.payment_receipt_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: remaining,
      status_label: statement.payment_receipt_status,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue({
      amount_due: received,
      currency_code: statement.currency_code,
    })}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(PAYMENT_RECEIPT_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
