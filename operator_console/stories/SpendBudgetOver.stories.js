import { renderSpendBudgetOver } from "../src/spend_budget_over.js";
import acceptedOverSignal from "../fixtures/accepted_over_signal.json";
import acceptedUnderSignal from "../fixtures/accepted_under_signal.json";
import duplicateReplayOverSignal from "../fixtures/duplicate_replay_over_signal.json";
import pendingSpendBudgetOver from "../fixtures/pending_spend_budget_over.json";

export default {
  title: "SpendBudgetOver",
};

export const FirstOverAccepted = {
  render: () => renderSpendBudgetOver(acceptedOverSignal, [pendingSpendBudgetOver]),
};

export const UnderAccepted = {
  render: () => renderSpendBudgetOver(acceptedUnderSignal, []),
};

export const DuplicateReplay = {
  render: () => renderSpendBudgetOver(duplicateReplayOverSignal, [pendingSpendBudgetOver]),
};
