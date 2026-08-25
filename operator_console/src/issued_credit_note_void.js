import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const ISSUED_CREDIT_NOTE_VOID_CUSTOMER_COPY =
  "Void an unused issued credit note, then wait.";

/**
 * Return the operator-facing next-action copy for one unused credit-note void.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "wait") {
    return "Wait";
  }
  return "Void unused credit note";
}

/**
 * Render one unused issued-credit-note-void presentment.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderIssuedCreditNoteVoid(statement) {
  const voided = assertExactDecimalString(statement.voided_amount, "voided_amount");
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-issued-credit-note-void">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Issued credit note void</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.issued_credit_note_void_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: voided,
      tax_amount: "0",
      status_label: statement.issued_credit_note_void_status,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue({
      amount_due: voided,
      currency_code: statement.currency_code,
    })}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(ISSUED_CREDIT_NOTE_VOID_CUSTOMER_COPY)}</p>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(String(statement.issued_credit_note_id ?? ""))}</p>` +
    `</section>` +
    `</article>`
  );
}
