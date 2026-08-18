import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const ACCOUNT_STATEMENT_CUSTOMER_COPY =
  "Open the account statement, then collect, credit, park, apply, or refund.";

const BUCKET_LABELS = {
  issued_invoice_total: "Issued",
  voided_invoice_total: "Voided invoice",
  open_collection_remaining: "Remaining",
  applied_credit_total: "Applied credit",
  voided_credit_total: "Voided credit",
  write_off_total: "Write-off",
  parked_unapplied_cash: "Parked leftover",
  refunded_unapplied_cash: "Refunded leftover",
};

/**
 * Render one billing-account statement for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderAccountStatement(statement) {
  const currencies = Array.isArray(statement.currencies) ? statement.currencies : [];
  const firstCurrency = currencies[0];
  const currencyCode = firstCurrency ? String(firstCurrency.currency_code ?? "") : "";
  const remaining = firstCurrency
    ? assertExactDecimalString(
        firstCurrency.open_collection_remaining,
        `${currencyCode} open_collection_remaining`,
      )
    : "0";
  const rows = currencies
    .map((currency) => {
      const rowCurrency = String(currency.currency_code ?? "");
      return Object.entries(BUCKET_LABELS)
        .map(([fieldName, label]) => {
          const amount = assertExactDecimalString(
            currency[fieldName],
            `${rowCurrency} ${fieldName}`,
          );
          return (
            `<tr>` +
            `<th scope="row">${escapeHtml(rowCurrency)}</th>` +
            `<td>${escapeHtml(label)}</td>` +
            `<td>${renderAmountDue({
              amount_due: amount,
              currency_code: rowCurrency,
            })}</td>` +
            `</tr>`
          );
        })
        .join("");
    })
    .join("");
  const headline =
    firstCurrency === undefined
      ? ""
      : renderAmountDue({
          amount_due: remaining,
          currency_code: currencyCode,
        });
  return (
    `<article class="oc-account-statement">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Account statement</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(String(statement.billing_account_id ?? ""))}</p>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(String(statement.as_of ?? ""))}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: remaining,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${headline}` +
    `<table class="oc-line-table">` +
    `<thead><tr><th>Currency</th><th>Bucket</th><th>Amount</th></tr></thead>` +
    `<tbody>${rows}</tbody>` +
    `</table>` +
    `<section class="oc-invoice-statement__action">` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(ACCOUNT_STATEMENT_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
