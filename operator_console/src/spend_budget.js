import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const SPEND_BUDGET_CUSTOMER_COPY =
  "Publish a commercial spend budget, then wait.";

const AMOUNT_LABELS = {
  budget_amount: "Budget",
  rated_amount: "Rated",
  remaining_amount: "Remaining",
  over_amount: "Over",
};

/**
 * Return the operator-facing next-action copy for one spend budget.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "wait") {
    return "Wait";
  }
  return "Wait";
}

/**
 * Render one spend-budget evaluation for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderSpendBudget(statement) {
  const budgetAmount = assertExactDecimalString(statement.budget_amount, "budget_amount");
  const ratedAmount = assertExactDecimalString(statement.rated_amount, "rated_amount");
  const remainingAmount = assertExactDecimalString(
    statement.remaining_amount,
    "remaining_amount",
  );
  const overAmount = assertExactDecimalString(statement.over_amount, "over_amount");
  const utilizationStatus = requireUtilizationStatus(statement.utilization_status);
  const currencyCode = String(statement.currency_code ?? "");
  const action = String(statement.next_operator_action ?? "");
  const amounts = {
    budget_amount: budgetAmount,
    rated_amount: ratedAmount,
    remaining_amount: remainingAmount,
    over_amount: overAmount,
  };
  const rows = Object.entries(AMOUNT_LABELS)
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
  return (
    `<article class="oc-spend-budget">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Spend budget</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(String(statement.spend_budget_id ?? ""))}</p>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(String(statement.window_started_at ?? ""))}` +
    ` – ${escapeHtml(String(statement.window_ended_at ?? ""))}</p>` +
    `</div>` +
    `${renderStatusChip(utilizationChipStatement(utilizationStatus, remainingAmount, overAmount))}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `${renderAmountDue({
      amount_due: remainingAmount,
      currency_code: currencyCode,
    })}` +
    `<table class="oc-line-table">` +
    `<thead><tr><th>Bucket</th><th>Amount</th></tr></thead>` +
    `<tbody>${rows}</tbody>` +
    `</table>` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(SPEND_BUDGET_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}

/**
 * @param {unknown} utilizationStatus
 * @returns {"under" | "at" | "over"}
 */
export function requireUtilizationStatus(utilizationStatus) {
  switch (utilizationStatus) {
    case "under":
    case "at":
    case "over":
      return utilizationStatus;
    default:
      throw new TypeError("utilization_status must be under, at, or over");
  }
}

/**
 * Map utilization onto the existing StatusChip tokens.
 *
 * Remaining is not used as amount_due for `under`: a healthy leftover would
 * otherwise paint as due. Under and at share settled. Only over uses due.
 *
 * @param {"under" | "at" | "over"} utilizationStatus
 * @param {string} remainingAmount
 * @param {string} overAmount
 * @returns {{ amount_due: string, status_label: string }}
 */
export function utilizationChipStatement(utilizationStatus, remainingAmount, overAmount) {
  switch (utilizationStatus) {
    case "under":
      return {
        amount_due: "0",
        status_label: "under",
      };
    case "at":
      return {
        amount_due: remainingAmount,
        status_label: "at",
      };
    case "over":
      return {
        amount_due: overAmount,
        status_label: "over",
      };
    default: {
      const exhaustive = utilizationStatus;
      throw new TypeError(`utilization_status must be under, at, or over: ${exhaustive}`);
    }
  }
}
