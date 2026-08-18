import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const ISSUED_CREDIT_NOTE_CUSTOMER_COPY =
  "Issue the credit note; the validated journal remains available for AIS.";

/**
 * Return the operator-facing next-action copy for one issued credit note.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "wait") {
    return "Wait for AIS";
  }
  return "Issue the credit note";
}

/**
 * Render one issued-credit-note presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderIssuedCreditNote(statement) {
  const inclusive = assertExactDecimalString(
    statement.tax_inclusive_amount,
    "tax_inclusive_amount",
  );
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-issued-credit-note">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Issued credit note</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.issued_credit_note_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: inclusive,
      tax_amount: statement.tax_amount,
      status_label: statement.issued_credit_note_status,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue({
      amount_due: inclusive,
      currency_code: statement.currency_code,
    })}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(ISSUED_CREDIT_NOTE_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
