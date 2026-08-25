import { renderRateCard } from "../src/rate_card.js";
import publishedStandardRate from "../fixtures/published_standard_rate.json";
import publishedPremiumRate from "../fixtures/published_premium_rate.json";

export default {
  title: "RateCard",
};

export const PublishedStandardRate = {
  render: () => renderRateCard(publishedStandardRate),
};

export const PublishedPremiumRate = {
  render: () => renderRateCard(publishedPremiumRate),
};
