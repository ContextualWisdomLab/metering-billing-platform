import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const UNAPPLIED_CASH_CUSTOMER_COPY = "Park leftover remittance, then wait.";
export const UNAPPLIED_CASH_APPLICATION_CUSTOMER_COPY =
  "Apply parked leftover, then collect the residual.";
export const UNAPPLIED_CASH_REFUND_CUSTOMER_COPY =
  "Refund unused parked leftover, then wait.";

/**
 * Return the operator-facing next-action copy for leftover presentment.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "wait") {
    return "Wait";
  }
  if (action === "settle") {
    return "Settle";
  }
  return "Collect";
}

/**
 * Render one parked leftover, leftover-apply, or leftover-refund presentment.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderUnappliedCash(statement) {
  if (statement.unapplied_cash_refund_id !== undefined) {
    return renderLeftoverRefund(statement);
  }
  if (statement.unapplied_cash_application_id !== undefined) {
    return renderLeftoverApply(statement);
  }
  return renderParkedLeftover(statement);
}

/**
 * Render one parked leftover presentment.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
function renderParkedLeftover(statement) {
  const leftover = assertExactDecimalString(statement.unapplied_amount, "unapplied_amount");
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-unapplied-cash">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Unapplied cash</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.unapplied_cash_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: leftover,
      tax_amount: "0",
      status_label: statement.unapplied_cash_status,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue({
      amount_due: leftover,
      currency_code: statement.currency_code,
    })}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(UNAPPLIED_CASH_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}

/**
 * Render one leftover-apply presentment.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
function renderLeftoverApply(statement) {
  const applied = assertExactDecimalString(statement.applied_amount, "applied_amount");
  const remaining = assertExactDecimalString(
    statement.remaining_outstanding_amount,
    "remaining_outstanding_amount",
  );
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-unapplied-cash">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Unapplied cash application</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.unapplied_cash_application_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: remaining,
      status_label: statement.unapplied_cash_application_status,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue({
      amount_due: applied,
      currency_code: statement.currency_code,
    })}` +
    `${renderAmountDue({
      amount_due: remaining,
      currency_code: statement.currency_code,
    })}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(UNAPPLIED_CASH_APPLICATION_CUSTOMER_COPY)}</p>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(String(statement.collection_case_status ?? ""))}</p>` +
    `</section>` +
    `</article>`
  );
}

/**
 * Render one leftover-refund presentment.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
function renderLeftoverRefund(statement) {
  const refunded = assertExactDecimalString(statement.refund_amount, "refund_amount");
  const leftover = assertExactDecimalString(statement.unapplied_amount, "unapplied_amount");
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-unapplied-cash">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Unapplied cash refund</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.unapplied_cash_refund_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: refunded,
      tax_amount: "0",
      status_label: statement.unapplied_cash_refund_status,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue({
      amount_due: refunded,
      currency_code: statement.currency_code,
    })}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(UNAPPLIED_CASH_REFUND_CUSTOMER_COPY)}</p>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(String(statement.unapplied_cash_status ?? ""))}</p>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(leftover)}</p>` +
    `</section>` +
    `</article>`
  );
}
