import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const TAX_ASSESSMENT_CUSTOMER_COPY =
  "Publish a tax rate, assess the draft, then propose the journal and let AIS pull.";

/**
 * Return the operator-facing next-action copy for one tax assessment.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "propose_journal") {
    return "Propose the journal";
  }
  return "Assess the draft";
}

/**
 * Render one tax-assessment presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderTaxAssessment(statement) {
  const inclusive = assertExactDecimalString(
    statement.tax_inclusive_amount,
    "tax_inclusive_amount",
  );
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-tax-assessment">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Tax assessment</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.tax_assessment_id)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: inclusive,
      status_label: statement.tax_code,
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
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(TAX_ASSESSMENT_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
