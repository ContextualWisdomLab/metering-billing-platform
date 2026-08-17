import { renderPaymentReceipt } from "../src/payment_receipt.js";
import appliedFullPaymentReceipt from "../fixtures/applied_full_payment_receipt.json";
import appliedPartialPaymentReceipt from "../fixtures/applied_partial_payment_receipt.json";

export default {
  title: "PaymentReceipt",
};

export const AppliedFullDrainOrWait = {
  render: () => renderPaymentReceipt(appliedFullPaymentReceipt),
};

export const AppliedPartialRecordReceipt = {
  render: () => renderPaymentReceipt(appliedPartialPaymentReceipt),
};
