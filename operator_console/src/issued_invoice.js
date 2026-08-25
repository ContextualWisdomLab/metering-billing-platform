import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderLineTable } from "./line_table.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const ISSUED_INVOICE_CUSTOMER_COPY = "Issue invoice, then collect or credit.";

/**
 * Return the operator-facing next-action copy for one issued invoice.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "collect") {
    return "Collect or credit";
  }
  return "Issue invoice";
}

/**
 * Map stored issued lines onto the tokenized invoice line table.
 *
 * @param {Record<string, unknown>} statement
 * @returns {{ invoice_lines: Array<Record<string, unknown>> }}
 */
function asInvoiceLines(statement) {
  const issuedLines = Array.isArray(statement.issued_invoice_lines)
    ? statement.issued_invoice_lines
    : [];
  return {
    invoice_lines: issuedLines.map((line) => ({
      line_number: line.line_number,
      metric_code: line.meter_code,
      quantity: line.rated_quantity,
      unit_amount: line.unit_price_amount,
      line_amount: line.line_total_amount,
    })),
  };
}

/**
 * Render one issued-invoice presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderIssuedInvoice(statement) {
  const inclusive = assertExactDecimalString(
    statement.tax_inclusive_amount,
    "tax_inclusive_amount",
  );
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-issued-invoice">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Issued invoice</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.issued_invoice_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: inclusive,
      tax_amount: statement.tax_amount,
      status_label: statement.issued_invoice_status,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue({
      amount_due: inclusive,
      currency_code: statement.currency_code,
    })}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(ISSUED_INVOICE_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `${renderLineTable(asInvoiceLines(statement))}` +
    `</article>`
  );
}
