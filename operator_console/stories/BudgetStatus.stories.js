import { renderBudgetStatus } from "../src/budget_status.js";
import accountBudgetStatusUnderOver from "../fixtures/account_budget_status_under_over.json";
import accountBudgetStatusNextCursor from "../fixtures/account_budget_status_next_cursor.json";

export default {
  title: "BudgetStatus",
};

export const PublishedUnderOver = {
  render: () => renderBudgetStatus(accountBudgetStatusUnderOver),
};

export const KeysetNextCursor = {
  render: () => renderBudgetStatus(accountBudgetStatusNextCursor),
};
