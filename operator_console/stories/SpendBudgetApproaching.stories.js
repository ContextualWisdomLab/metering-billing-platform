import { renderSpendBudgetApproaching } from "../src/spend_budget_approaching.js";
import acceptedAtSignal from "../fixtures/accepted_at_signal.json";
import acceptedUnderApproachingSignal from "../fixtures/accepted_under_approaching_signal.json";
import duplicateReplayApproachingSignal from "../fixtures/duplicate_replay_approaching_signal.json";
import pendingSpendBudgetApproaching from "../fixtures/pending_spend_budget_approaching.json";

export default {
  title: "SpendBudgetApproaching",
};

export const FirstAtAccepted = {
  render: () => renderSpendBudgetApproaching(acceptedAtSignal, [pendingSpendBudgetApproaching]),
};

export const UnderAccepted = {
  render: () => renderSpendBudgetApproaching(acceptedUnderApproachingSignal, []),
};

export const DuplicateReplay = {
  render: () =>
    renderSpendBudgetApproaching(duplicateReplayApproachingSignal, [pendingSpendBudgetApproaching]),
};
