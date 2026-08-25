import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const ISSUED_INVOICE_VOID_CUSTOMER_COPY =
  "Void an unused issued invoice, then wait.";

/**
 * Return the operator-facing next-action copy for one unused invoice void.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "wait") {
    return "Wait";
  }
  return "Void unused invoice";
}

/**
 * Render one unused issued-invoice-void presentment.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderIssuedInvoiceVoid(statement) {
  const voided = assertExactDecimalString(statement.voided_amount, "voided_amount");
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-issued-invoice-void">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Issued invoice void</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.issued_invoice_void_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: voided,
      tax_amount: "0",
      status_label: statement.issued_invoice_void_status,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue({
      amount_due: voided,
      currency_code: statement.currency_code,
    })}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(ISSUED_INVOICE_VOID_CUSTOMER_COPY)}</p>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(String(statement.issued_invoice_id ?? ""))}</p>` +
    `</section>` +
    `</article>`
  );
}
