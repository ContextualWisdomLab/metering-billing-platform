import { escapeHtml } from "./exact_decimal.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const POSTING_RECEIPT_OBSERVATION_CUSTOMER_COPY =
  "Drain AIS outbox, then store the receipt observation; AIS may keep being polled only when the outbox is non-empty.";

/**
 * Return the operator-facing next-action copy for one observation.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "wait") {
    return "Wait";
  }
  return "Store the observation";
}

/**
 * Render one posting-receipt observation presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderPostingReceiptObservation(statement) {
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-posting-receipt-observation">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Posting receipt observation</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.posting_receipt_observation_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: "0",
      status_label: statement.posting_status_code,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(POSTING_RECEIPT_OBSERVATION_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
