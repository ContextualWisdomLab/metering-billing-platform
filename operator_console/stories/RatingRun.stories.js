import { renderRatingRun } from "../src/rating_run.js";
import ratedMorningWindow from "../fixtures/rated_morning_window.json";
import ratedPartialWindow from "../fixtures/rated_partial_window.json";

export default {
  title: "RatingRun",
};

export const RatedMorningWindow = {
  render: () => renderRatingRun(ratedMorningWindow),
};

export const RatedPartialWindow = {
  render: () => renderRatingRun(ratedPartialWindow),
};
