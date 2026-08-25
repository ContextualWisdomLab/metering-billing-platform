import { renderTaxAssessment } from "../src/tax_assessment.js";
import assessedMorningVat from "../fixtures/assessed_morning_vat.json";
import assessedPartialVat from "../fixtures/assessed_partial_vat.json";

export default {
  title: "TaxAssessment",
};

export const AssessedMorningVat = {
  render: () => renderTaxAssessment(assessedMorningVat),
};

export const AssessedPartialVat = {
  render: () => renderTaxAssessment(assessedPartialVat),
};
