import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderLineTable } from "./line_table.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const NEXT_OPERATOR_ACTION = "Collect or credit";

/**
 * Render one #21 presentment statement for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderInvoiceStatement(statement) {
  for (const fieldName of [
    "tax_exclusive_amount",
    "tax_amount",
    "tax_inclusive_amount",
    "credited_amount",
    "amount_due",
  ]) {
    assertExactDecimalString(statement[fieldName], fieldName);
  }
  return (
    `<article class="oc-invoice-statement">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Invoice draft statement</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.invoice_draft_id)}</p>` +
    `</div>` +
    `${renderStatusChip(statement)}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue(statement)}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${NEXT_OPERATOR_ACTION}</p>` +
    `</section>` +
    `${renderLineTable(statement)}` +
    `</article>`
  );
}
