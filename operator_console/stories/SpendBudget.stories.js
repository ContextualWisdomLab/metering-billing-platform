import { renderSpendBudget } from "../src/spend_budget.js";
import publishedUnderBudget from "../fixtures/published_under_budget.json";
import publishedAtBudget from "../fixtures/published_at_budget.json";
import publishedOverBudget from "../fixtures/published_over_budget.json";

export default {
  title: "SpendBudget",
};

export const PublishedUnder = {
  render: () => renderSpendBudget(publishedUnderBudget),
};

export const PublishedAt = {
  render: () => renderSpendBudget(publishedAtBudget),
};

export const PublishedOver = {
  render: () => renderSpendBudget(publishedOverBudget),
};
