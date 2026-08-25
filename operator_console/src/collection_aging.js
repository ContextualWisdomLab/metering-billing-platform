import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderTenantPin } from "./tenant_pin.js";

export const COLLECTION_AGING_CUSTOMER_COPY =
  "Open the aging statement, then collect or credit.";

const BUCKET_LABELS = {
  current: "Current",
  days_1_30: "1-30",
  days_31_60: "31-60",
  days_61_90: "61-90",
  days_90_plus: "90+",
};

/**
 * Render one collection-aging presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderCollectionAging(statement) {
  const currencies = Array.isArray(statement.currencies) ? statement.currencies : [];
  const rows = currencies
    .map((currency) => {
      const currencyCode = String(currency.currency_code ?? "");
      return Object.entries(BUCKET_LABELS)
        .map(([bucketName, label]) => {
          const bucket = currency[bucketName] ?? {};
          const outstanding = assertExactDecimalString(
            bucket.outstanding_amount,
            `${currencyCode} ${bucketName} outstanding_amount`,
          );
          return (
            `<tr>` +
            `<th scope="row">${escapeHtml(currencyCode)}</th>` +
            `<td>${escapeHtml(label)}</td>` +
            `<td>${escapeHtml(String(bucket.case_count ?? 0))}</td>` +
            `<td>${renderAmountDue({
              amount_due: outstanding,
              currency_code: currencyCode,
            })}</td>` +
            `</tr>`
          );
        })
        .join("");
    })
    .join("");
  return (
    `<article class="oc-collection-aging">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Collection aging</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(String(statement.as_of ?? ""))}</p>` +
    `</div>` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `<table class="oc-line-table">` +
    `<thead><tr><th>Currency</th><th>Bucket</th><th>Cases</th><th>Outstanding</th></tr></thead>` +
    `<tbody>${rows}</tbody>` +
    `</table>` +
    `<section class="oc-invoice-statement__action">` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(COLLECTION_AGING_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
