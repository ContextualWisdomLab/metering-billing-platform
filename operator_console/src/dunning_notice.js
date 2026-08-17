import { escapeHtml } from "./exact_decimal.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const DUNNING_NOTICE_CUSTOMER_COPY =
  "Record the commercial reminder, then collect or credit.";

/**
 * Return the operator-facing next-action copy for one dunning notice.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "wait") {
    return "Wait";
  }
  if (action === "collect") {
    return "Collect or credit";
  }
  return "Collect or credit";
}

/**
 * Render one dunning-event presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderDunningNotice(statement) {
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-dunning-notice">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Dunning notice</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.dunning_event_id)}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(statement.dunning_notice_code)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: "0",
      status_label: statement.dunning_notice_code,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(DUNNING_NOTICE_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
