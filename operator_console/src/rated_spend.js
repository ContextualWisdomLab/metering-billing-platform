import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const RATED_SPEND_CUSTOMER_COPY =
  "Inspect rated product, project, credential, principal, or cost-center spend, then draft an invoice.";

/**
 * Render one already-rated spend presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderRatedSpend(statement) {
  const products = Array.isArray(statement.products) ? statement.products : [];
  const showProject = products.some((product) => product.project_reference);
  const firstProduct = products[0];
  const currencyCode = firstProduct ? String(firstProduct.currency_code ?? "") : "";
  const headlineAmount = firstProduct
    ? assertExactDecimalString(firstProduct.rated_amount, `${currencyCode} rated_amount`)
    : "0";
  const rows = products
    .map((product) => {
      const rowCurrency = String(product.currency_code ?? "");
      const amount = assertExactDecimalString(
        product.rated_amount,
        `${rowCurrency} rated_amount`,
      );
      const projectCell = showProject
        ? `<td>${escapeHtml(String(product.project_reference ?? ""))}</td>`
        : "";
      return (
        `<tr>` +
        `<th scope="row">${escapeHtml(rowCurrency)}</th>` +
        `<td>${escapeHtml(String(product.product_code ?? ""))}</td>` +
        projectCell +
        `<td>${renderAmountDue({
          amount_due: amount,
          currency_code: rowCurrency,
        })}</td>` +
        `</tr>`
      );
    })
    .join("");
  const projectHeader = showProject ? "<th>Project</th>" : "";
  const headline =
    firstProduct === undefined
      ? ""
      : renderAmountDue({
          amount_due: headlineAmount,
          currency_code: currencyCode,
        });
  return (
    `<article class="oc-rated-spend">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Rated spend</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(String(statement.billing_account_id ?? ""))}</p>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(String(statement.window_started_at ?? ""))}` +
    ` – ${escapeHtml(String(statement.window_ended_at ?? ""))}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: headlineAmount,
      status_label: firstProduct ? String(firstProduct.product_code ?? "rated") : "rated",
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${headline}` +
    `<table class="oc-line-table">` +
    `<thead><tr><th>Currency</th><th>Product</th>${projectHeader}<th>Amount</th></tr></thead>` +
    `<tbody>${rows}</tbody>` +
    `</table>` +
    `<section class="oc-invoice-statement__action">` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(RATED_SPEND_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
