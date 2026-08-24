import { renderRatedSpend } from "../src/rated_spend.js";
import ratedSpendMorningProduct from "../fixtures/rated_spend_morning_product.json";
import ratedSpendMorningProject from "../fixtures/rated_spend_morning_project.json";

export default {
  title: "RatedSpend",
};

export const ProductGroupedMorning = {
  render: () => renderRatedSpend(ratedSpendMorningProduct),
};

export const ProjectGroupedMorning = {
  render: () => renderRatedSpend(ratedSpendMorningProject),
};
