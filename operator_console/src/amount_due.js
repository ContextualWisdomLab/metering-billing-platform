import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";

/**
 * Render the tokenized amount-due module.
 *
 * @param {{ amount_due: string, currency_code: string }} statement
 * @returns {string}
 */
export function renderAmountDue(statement) {
  const amountDue = assertExactDecimalString(statement.amount_due, "amount_due");
  const currencyCode = escapeHtml(statement.currency_code);
  return (
    `<section class="oc-amount-due">` +
    `<span class="oc-amount-due__label">Amount due</span>` +
    `<p class="oc-amount-due__value">${escapeHtml(amountDue)} ${currencyCode}</p>` +
    `</section>`
  );
}
