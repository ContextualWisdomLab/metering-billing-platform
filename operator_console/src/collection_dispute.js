import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const COLLECTION_DISPUTE_CUSTOMER_COPY = "Hold the disputed case, then wait.";
export const COLLECTION_DISPUTE_RELEASE_CUSTOMER_COPY =
  "Release the hold, then collect or dunn.";

/**
 * Return the operator-facing next-action copy for one collection dispute.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "wait") {
    return "Wait";
  }
  return "Collect or dunn";
}

/**
 * Render one collection-dispute hold or release presentment.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderCollectionDispute(statement) {
  const remaining = assertExactDecimalString(
    statement.remaining_outstanding_amount,
    "remaining_outstanding_amount",
  );
  const action = String(statement.next_operator_action ?? "");
  const released = statement.collection_dispute_status === "released";
  const title = released ? "Collection dispute release" : "Collection dispute";
  const customerCopy = released
    ? COLLECTION_DISPUTE_RELEASE_CUSTOMER_COPY
    : COLLECTION_DISPUTE_CUSTOMER_COPY;
  return (
    `<article class="oc-collection-dispute">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">${escapeHtml(title)}</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.collection_dispute_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: remaining,
      status_label: statement.collection_dispute_status,
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
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(customerCopy)}</p>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(String(statement.collection_case_status ?? ""))}</p>` +
    `</section>` +
    `</article>`
  );
}
