import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";

/**
 * Render the tokenized invoice line table.
 *
 * @param {{ invoice_lines: Array<Record<string, unknown>> }} statement
 * @returns {string}
 */
export function renderLineTable(statement) {
  const lines = Array.isArray(statement.invoice_lines) ? statement.invoice_lines : [];
  const rows = lines
    .map((line) => {
      const quantity = assertExactDecimalString(line.quantity, "quantity");
      const unitAmount = assertExactDecimalString(line.unit_amount, "unit_amount");
      const lineAmount = assertExactDecimalString(line.line_amount, "line_amount");
      return (
        `<tr>` +
        `<td>${escapeHtml(line.line_number)}</td>` +
        `<td>${escapeHtml(line.metric_code)}</td>` +
        `<td>${escapeHtml(quantity)}</td>` +
        `<td>${escapeHtml(unitAmount)}</td>` +
        `<td>${escapeHtml(lineAmount)}</td>` +
        `</tr>`
      );
    })
    .join("");
  return (
    `<table class="oc-line-table">` +
    `<thead><tr>` +
    `<th>Line</th><th>Metric</th><th>Quantity</th><th>Unit amount</th><th>Line amount</th>` +
    `</tr></thead>` +
    `<tbody>${rows}</tbody>` +
    `</table>`
  );
}
