import { renderUsageEvent } from "../src/usage_event.js";
import storedMorningUsage from "../fixtures/stored_morning_usage.json";
import storedPartialTokenUsage from "../fixtures/stored_partial_token_usage.json";

export default {
  title: "UsageEvent",
};

export const StoredMorningUsage = {
  render: () => renderUsageEvent(storedMorningUsage),
};

export const StoredPartialTokenUsage = {
  render: () => renderUsageEvent(storedPartialTokenUsage),
};
