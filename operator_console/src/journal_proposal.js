import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const JOURNAL_PROPOSAL_CUSTOMER_COPY =
  "Let AIS pull the validated journal.";

/**
 * Return the operator-facing next-action copy for one journal proposal.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "wait") {
    return "Wait";
  }
  return "Let AIS pull";
}

/**
 * Return the exact journal amount from stored debit-XOR-credit lines.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function journalProposalAmount(statement) {
  const lines = Array.isArray(statement.lines) ? statement.lines : [];
  const cashLine =
    lines.find((line) => line.account_role_code === "cash_receipt") ?? lines[0] ?? {};
  const debit = cashLine.debit_amount;
  if (debit !== undefined && debit !== "0") {
    return assertExactDecimalString(debit, "debit_amount");
  }
  return assertExactDecimalString(cashLine.credit_amount, "credit_amount");
}

/**
 * Render one stored journal-proposal GET for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderJournalProposal(statement) {
  const amount = journalProposalAmount(statement);
  const currencyCode = String(statement.transaction_currency ?? "");
  const action = String(statement.next_operator_action ?? "wait");
  return (
    `<article class="oc-journal-proposal">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Journal proposal</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(String(statement.proposal_id ?? ""))}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: amount,
      tax_amount: "0",
      status_label: statement.proposal_status,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue({
      amount_due: amount,
      currency_code: currencyCode,
    })}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(JOURNAL_PROPOSAL_CUSTOMER_COPY)}</p>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(String(statement.source_event_references?.[0] ?? ""))}</p>` +
    `</section>` +
    `</article>`
  );
}
