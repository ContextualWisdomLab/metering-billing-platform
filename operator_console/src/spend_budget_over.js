import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";
import {
  nextOperatorActionCopy,
  requireUtilizationStatus,
  utilizationChipStatement,
} from "./spend_budget.js";

export const SPEND_BUDGET_OVER_CUSTOMER_COPY =
  "Observe the spend budget over signal, then wait.";

export const SPEND_BUDGET_OVER_TENANT_REFERENCE = "urn:cwl:tenant_001";

const AMOUNT_LABELS = {
  budget_amount: "Budget",
  over_amount: "Over",
};

/**
 * Render one spend_budget.over observation for the operator console.
 *
 * The observation is the existing over-signal write. Over-signal rows are
 * existing #24 webhook-outbox presentment documents. Stories compose them;
 * this module does not invent a parallel envelope.
 *
 * @param {Record<string, unknown>} observation
 * @param {ReadonlyArray<Record<string, unknown>>} [overSignalRows]
 * @returns {string}
 */
export function renderSpendBudgetOver(observation, overSignalRows = []) {
  const budgetAmount = assertExactDecimalString(observation.budget_amount, "budget_amount");
  const overAmount = assertExactDecimalString(observation.over_amount, "over_amount");
  const utilizationStatus = requireUtilizationStatus(observation.utilization_status);
  const outcomeCode = requireOutcomeCode(observation.spend_budget_over_signal_outcome_code);
  const currencyCode = String(observation.currency_code ?? "");
  const action = String(observation.next_operator_action ?? "wait");
  const amounts = {
    budget_amount: budgetAmount,
    over_amount: overAmount,
  };
  const amountRows = Object.entries(AMOUNT_LABELS)
    .map(([fieldName, label]) => {
      return (
        `<tr>` +
        `<th scope="row">${escapeHtml(label)}</th>` +
        `<td>${renderAmountDue({
          amount_due: amounts[fieldName],
          currency_code: currencyCode,
        })}</td>` +
        `</tr>`
      );
    })
    .join("");
  const rows = Array.isArray(overSignalRows) ? overSignalRows : [];
  const outboxBody = rows.map(renderOverSignalRow).join("");
  return (
    `<article class="oc-spend-budget-over">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Spend budget over</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(String(observation.spend_budget_id ?? ""))}</p>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(outcomeCode)}</p>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(String(observation.window_started_at ?? ""))}` +
    ` – ${escapeHtml(String(observation.window_ended_at ?? ""))}</p>` +
    `</div>` +
    `${renderStatusChip(utilizationChipStatement(utilizationStatus, "0", overAmount))}` +
    `</header>` +
    `${renderTenantPin({ tenant_reference: SPEND_BUDGET_OVER_TENANT_REFERENCE })}` +
    `${renderAmountDue({
      amount_due: overAmount,
      currency_code: currencyCode,
    })}` +
    `<table class="oc-line-table">` +
    `<thead><tr><th>Bucket</th><th>Amount</th></tr></thead>` +
    `<tbody>${amountRows}</tbody>` +
    `</table>` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Over-signal rows</span>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(String(rows.length))}</p>` +
    `<table class="oc-line-table">` +
    `<thead><tr><th>Event</th><th>Source</th><th>Delivery</th></tr></thead>` +
    `<tbody>${outboxBody}</tbody>` +
    `</table>` +
    `</section>` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(SPEND_BUDGET_OVER_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}

/**
 * @param {Record<string, unknown>} row
 * @returns {string}
 */
function renderOverSignalRow(row) {
  if (row.event_type_code !== "spend_budget.over") {
    throw new TypeError("over-signal rows must use event_type_code spend_budget.over");
  }
  return (
    `<tr>` +
    `<th scope="row">${escapeHtml(String(row.event_type_code))}</th>` +
    `<td>${escapeHtml(String(row.source_id ?? ""))}</td>` +
    `<td>${renderStatusChip({
      amount_due: "0",
      status_label: String(row.delivery_status ?? ""),
    })}</td>` +
    `</tr>`
  );
}

/**
 * @param {unknown} outcomeCode
 * @returns {"accepted" | "duplicate_replay" | "rejected"}
 */
function requireOutcomeCode(outcomeCode) {
  switch (outcomeCode) {
    case "accepted":
    case "duplicate_replay":
    case "rejected":
      return outcomeCode;
    default: {
      const exhaustive = outcomeCode;
      throw new TypeError(
        `spend_budget_over_signal_outcome_code must be accepted, duplicate_replay, or rejected: ${exhaustive}`,
      );
    }
  }
}
