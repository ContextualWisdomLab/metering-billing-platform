import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const COLLECTION_CUSTOMER_COPY = "Open the collection case, then collect or credit.";

/**
 * Return the operator-facing next-action copy for one collection case.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "wait") {
    return "Wait";
  }
  if (action === "credit") {
    return "Credit";
  }
  return "Collect or credit";
}

/**
 * Render one collection-case presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderCollectionCase(statement) {
  const outstanding = assertExactDecimalString(
    statement.collection_outstanding,
    "collection_outstanding",
  );
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-collection-case">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Collection case</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.collection_case_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: outstanding,
      status_label: statement.collection_case_status,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue({
      amount_due: outstanding,
      currency_code: statement.currency_code,
    })}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(COLLECTION_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
