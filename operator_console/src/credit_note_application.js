import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const CREDIT_NOTE_APPLICATION_CUSTOMER_COPY =
  "Apply the issued credit note, then collect the residual.";

/**
 * Return the operator-facing next-action copy for one credit-note application.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "wait") {
    return "Wait";
  }
  return "Collect";
}

/**
 * Render one credit-note-application presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderCreditNoteApplication(statement) {
  const applied = assertExactDecimalString(statement.applied_amount, "applied_amount");
  const remaining = assertExactDecimalString(
    statement.remaining_outstanding_amount,
    "remaining_outstanding_amount",
  );
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-credit-note-application">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Credit note application</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.credit_note_application_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: remaining,
      tax_amount: "0",
      status_label: statement.credit_note_application_status,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue({
      amount_due: applied,
      currency_code: statement.currency_code,
    })}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(CREDIT_NOTE_APPLICATION_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
