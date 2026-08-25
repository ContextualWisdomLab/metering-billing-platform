import { assertExactDecimalString, escapeHtml } from "./exact_decimal.js";
import { renderAmountDue } from "./amount_due.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";
import {
  nextOperatorActionCopy,
  requireUtilizationStatus,
  utilizationChipStatement,
} from "./spend_budget.js";

export const BUDGET_STATUS_CUSTOMER_COPY = "Open the account budget status, then wait.";

export const BUDGET_STATUS_TENANT_REFERENCE = "urn:cwl:tenant_001";

const ROW_AMOUNT_FIELDS = [
  "budget_amount",
  "rated_amount",
  "remaining_amount",
  "over_amount",
];

/**
 * Render published spend-budget evaluations for one billing account.
 *
 * The GET envelope is `{budget_statuses, next_cursor}` only. Tenant pin is
 * the commercial `tenant_account` used on `X-CWL-Tenant-Reference`.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderBudgetStatus(statement) {
  const rows = Array.isArray(statement.budget_statuses) ? statement.budget_statuses : [];
  const firstRow = rows[0];
  const currencyCode = firstRow ? String(firstRow.currency_code ?? "") : "";
  const remainingAmount = firstRow
    ? assertExactDecimalString(firstRow.remaining_amount, "remaining_amount")
    : "0";
  const overAmount = firstRow
    ? assertExactDecimalString(firstRow.over_amount, "over_amount")
    : "0";
  const utilizationStatus = firstRow
    ? requireUtilizationStatus(firstRow.utilization_status)
    : "under";
  const action = firstRow ? String(firstRow.next_operator_action ?? "wait") : "wait";
  const tableRows = rows.map(renderBudgetStatusRow).join("");
  const headline =
    firstRow === undefined
      ? ""
      : renderAmountDue({
          amount_due: remainingAmount,
          currency_code: currencyCode,
        });
  const nextCursor = statement.next_cursor;
  const cursorBlock =
    typeof nextCursor === "string" && nextCursor.length > 0
      ? `<span class="oc-invoice-statement__action-label">Next cursor</span>` +
        `<p class="oc-invoice-statement__id">${escapeHtml(nextCursor)}</p>`
      : "";
  return (
    `<article class="oc-budget-status">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Budget status</h1>` +
    `</div>` +
    `${renderStatusChip(utilizationChipStatement(utilizationStatus, remainingAmount, overAmount))}` +
    `</header>` +
    `${renderTenantPin({ tenant_reference: BUDGET_STATUS_TENANT_REFERENCE })}` +
    `${headline}` +
    `<table class="oc-line-table">` +
    `<thead><tr><th>Id</th><th>Window</th><th>Utilization</th>` +
    `<th>Budget</th><th>Rated</th><th>Remaining</th><th>Over</th></tr></thead>` +
    `<tbody>${tableRows}</tbody>` +
    `</table>` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(BUDGET_STATUS_CUSTOMER_COPY)}</p>` +
    `${cursorBlock}` +
    `</section>` +
    `</article>`
  );
}

/**
 * @param {Record<string, unknown>} row
 * @returns {string}
 */
function renderBudgetStatusRow(row) {
  const amounts = Object.fromEntries(
    ROW_AMOUNT_FIELDS.map((fieldName) => [
      fieldName,
      assertExactDecimalString(row[fieldName], fieldName),
    ]),
  );
  const utilizationStatus = requireUtilizationStatus(row.utilization_status);
  const currencyCode = String(row.currency_code ?? "");
  return (
    `<tr>` +
    `<th scope="row">${escapeHtml(String(row.spend_budget_id ?? ""))}</th>` +
    `<td>${escapeHtml(String(row.window_started_at ?? ""))}` +
    ` – ${escapeHtml(String(row.window_ended_at ?? ""))}</td>` +
    `<td>${renderStatusChip(
      utilizationChipStatement(
        utilizationStatus,
        amounts.remaining_amount,
        amounts.over_amount,
      ),
    )}</td>` +
    `<td>${renderAmountDue({
      amount_due: amounts.budget_amount,
      currency_code: currencyCode,
    })}</td>` +
    `<td>${renderAmountDue({
      amount_due: amounts.rated_amount,
      currency_code: currencyCode,
    })}</td>` +
    `<td>${renderAmountDue({
      amount_due: amounts.remaining_amount,
      currency_code: currencyCode,
    })}</td>` +
    `<td>${renderAmountDue({
      amount_due: amounts.over_amount,
      currency_code: currencyCode,
    })}</td>` +
    `</tr>`
  );
}
