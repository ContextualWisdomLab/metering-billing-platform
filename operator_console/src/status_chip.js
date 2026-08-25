import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";

/**
 * Resolve a commercial status chip from presentment amounts.
 *
 * @param {{ amount_due: string, tax_amount?: string }} statement
 * @returns {"settled" | "due" | "draft"}
 */
export function resolveStatementStatus(statement) {
  const amountDue = assertExactDecimalString(statement.amount_due, "amount_due");
  if (/^0(\.0+)?$/.test(amountDue)) {
    return "settled";
  }
  if (statement.tax_amount !== undefined) {
    const taxAmount = assertExactDecimalString(statement.tax_amount, "tax_amount");
    if (/^0(\.0+)?$/.test(taxAmount)) {
      return "draft";
    }
  }
  return "due";
}

/**
 * Render the tokenized status chip.
 *
 * @param {{ amount_due: string, tax_amount?: string, status_label?: string }} statement
 * @returns {string}
 */
export function renderStatusChip(statement) {
  const statusCode = resolveStatementStatus(statement);
  const label = escapeHtml(statement.status_label ?? statusCode);
  return `<span class="oc-status-chip oc-status-chip--${statusCode}">${label}</span>`;
}
