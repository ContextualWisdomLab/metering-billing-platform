import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const COLLECTION_CASE_SETTLEMENT_CUSTOMER_COPY =
  "Settle the zero-outstanding case, then wait.";

/**
 * Return the operator-facing next-action copy for one collection-case settlement.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "wait") {
    return "Wait";
  }
  return "Settle";
}

/**
 * Render one collection-case-settlement presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderCollectionCaseSettlement(statement) {
  const remaining = assertExactDecimalString(
    statement.remaining_outstanding_amount,
    "remaining_outstanding_amount",
  );
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-collection-case-settlement">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Collection case settlement</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.collection_case_settlement_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: remaining,
      tax_amount: "0",
      status_label: statement.collection_case_settlement_status,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue({
      amount_due: remaining,
      currency_code: statement.currency_code,
    })}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(COLLECTION_CASE_SETTLEMENT_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
