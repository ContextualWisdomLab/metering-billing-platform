import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const RATING_RUN_CUSTOMER_COPY =
  "Rate a window, then draft an invoice.";

/**
 * Return the operator-facing next-action copy for one rating run.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "draft_invoice") {
    return "Draft an invoice";
  }
  return "Rate a window";
}

/**
 * Render one rating-run presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderRatingRun(statement) {
  const total = assertExactDecimalString(statement.rated_total_amount, "rated_total_amount");
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-rating-run">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Rating run</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.rating_run_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: total,
      status_label: statement.rate_card_code,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue({
      amount_due: total,
      currency_code: statement.currency_code,
    })}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(RATING_RUN_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
