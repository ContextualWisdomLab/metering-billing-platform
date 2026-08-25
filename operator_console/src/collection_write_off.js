import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const COLLECTION_WRITE_OFF_CUSTOMER_COPY =
  "Write off leftover remaining, then settle.";

/**
 * Return the operator-facing next-action copy for one collection write-off.
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
 * Render one recorded leftover-remaining collection-write-off presentment.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderCollectionWriteOff(statement) {
  const writtenOff = assertExactDecimalString(
    statement.write_off_amount,
    "write_off_amount",
  );
  const remaining = assertExactDecimalString(
    statement.remaining_outstanding_amount,
    "remaining_outstanding_amount",
  );
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-collection-write-off">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Collection write-off</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.collection_write_off_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: writtenOff,
      tax_amount: "0",
      status_label: statement.collection_write_off_status,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue({
      amount_due: writtenOff,
      currency_code: statement.currency_code,
    })}` +
    `${renderAmountDue({
      amount_due: remaining,
      currency_code: statement.currency_code,
    })}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(COLLECTION_WRITE_OFF_CUSTOMER_COPY)}</p>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(String(statement.collection_case_status ?? ""))}</p>` +
    `</section>` +
    `</article>`
  );
}
